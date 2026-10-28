#!/usr/bin/env python3
"""Отчёт аудита (report.json) из ответов инструментов Seller AI — для render.py.

Скрипт ничего не запрашивает: агент сохраняет ответы инструментов в файлы и передаёт их сюда.

    python3 build_report.py --profit profit-2025.json profit-2026.json \
        --accruals accruals-2025.json accruals-2026.json \
        [--assets assets.json] [--batches batches.json] [--non-pickups np-*.json] [--seller seller.json] \
        [--removals removals.json] [--decommissioned dg-*.json] [--config audit-config.json] --out report.json

--profit    ответы economics_profit_get с group=month (весь период одним окном или окна подряд, без пересечений);
--accruals  ответы unit_economics_accruals_list за те же окна;
--assets    ответ assets_summary_get (остатки и активы сейчас);
--batches   ответ cost_batches_list без sku (сверка остатка FBO с журналом партий);
--non-pickups  страницы finance_accruals_list с type_ids=6,40,59 за период: невыкупы и возвраты — уникальные отправления
            (unit_number) и сумма этих услуг; без них — non_pickup_count конфига (строк, а не отправлений);
--seller    ответ stores_seller_info_get: юрлицо, ИНН и система налогообложения Ozon на обложку;
--removals  ответы removals_list за период (date_from, date_to; хватает limit=1 — нужны totals и coverage): вывоз со складов;
--decommissioned  страницы decommissioned_goods_list за период (все, limit 500): причины списаний и списанное по товарам;
--config    {"store_name": "…", "extra": [{"label", "amount", "note"}], "non_pickup_count": N}.
"""
import argparse
import collections
import json
import sys

CATEGORY_RU = {
    "sales": "Продажи",
    "returns": "Возвраты",
    "commission": "Вознаграждение Ozon",
    "logistics": "Логистика",
    "returns_logistics": "Возвраты и невыкупы",
    "acquiring": "Эквайринг",
    "advertising": "Реклама и продвижение",
    "supply_storage": "Поставки и склад",
    "disposal": "Утилизация",
    "payouts": "Выплаты",
    "subscriptions": "Подписки и сервисы",
    "fines": "Штрафы",
    "compensations": "Компенсации и корректировки",
    "other": "Прочее",
}
SALES_KEYS = ("sales", "returns")
SUMMARY_SUM = ("sales", "returns", "accrued", "cogs_sold", "cogs_disposed", "vat", "tax", "profit", "units_sold",
               "units_returned")
MIN_UNITS_FOR_UNIT_ECONOMICS = 20
OZON_WAREHOUSE_SECTIONS = ("ozon_warehouses", "problem")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def zero(value):
    v = num(value)
    return 0.0 if v is None else v


def merge_profit(answers):
    """Несколько окон economics_profit_get (group=month) в одно: месяцы подряд, суммы по категориям и строкам."""
    answers = sorted(answers, key=lambda a: a["period"]["from"])
    summary = {k: 0.0 for k in SUMMARY_SUM}
    estimate = None
    categories = collections.OrderedDict()
    types = collections.defaultdict(lambda: {"name": "", "amount": 0.0, "category": None})
    months = []
    warnings, quality = [], {"missing_cost_skus": 0, "ozon_cost_skus": 0, "unallocated": 0.0, "tax_rate_missing": False}
    for a in answers:
        if a.get("group") != "month":
            raise ValueError("economics_profit_get нужен с group=month")
        s = a.get("summary") or {}
        for k in SUMMARY_SUM:
            summary[k] += zero(s.get(k))
        if s.get("estimate"):
            estimate = (estimate or 0.0) + zero(s["estimate"].get("amount"))
        for c in a.get("categories") or []:
            categories.setdefault(c["key"], 0.0)
            categories[c["key"]] += zero(c.get("amount"))
            for t in c.get("types") or []:
                entry = types[t["key"]]
                entry["name"] = t.get("name") or entry["name"]
                entry["amount"] += zero(t.get("amount"))
                entry["category"] = c["key"]
        for row in a.get("series") or []:
            amounts = row.get("amounts") or {}
            sales = zero(row.get("sales"))
            returns = zero(amounts.get("returns"))
            commission = zero(amounts.get("commission"))
            accrued = zero(row.get("accrued"))
            months.append({
                "ym": row["from"][:7], "from": row["from"], "to": row["to"],
                "sales": sales + returns, "commission": commission,
                "services": accrued - sales - returns - commission, "accrued": accrued,
                "cogs": zero(row.get("cogs_sold")), "disposal_cogs": zero(row.get("cogs_disposed")),
                "tax": zero(row.get("tax")), "estimate": num(row.get("estimate")), "net": zero(row.get("profit")),
            })
        q = a.get("quality") or {}
        quality["missing_cost_skus"] = max(quality["missing_cost_skus"], int(q.get("missing_cost_skus") or 0))
        quality["ozon_cost_skus"] = max(quality["ozon_cost_skus"], int(q.get("ozon_cost_skus") or 0))
        quality["unallocated"] += zero(q.get("unallocated"))
        quality["tax_rate_missing"] = quality["tax_rate_missing"] or bool(q.get("tax_rate_missing"))
        for w in a.get("warnings") or []:
            if w not in warnings:
                warnings.append(w)
    if not answers:
        raise ValueError("нужен хотя бы один ответ economics_profit_get")
    period = {"from": answers[0]["period"]["from"], "to": answers[-1]["period"]["to"]}
    return {"period": period, "summary": summary, "estimate": estimate, "categories": categories, "types": types,
            "months": months, "quality": quality, "warnings": warnings, "tax": answers[-1].get("tax"),
            "cost_method": answers[-1].get("cost_method"), "scheme": answers[-1].get("scheme")}


def merge_accruals(answers):
    """Несколько окон unit_economics_accruals_list: товары по SKU и итоговая сверка."""
    items = collections.OrderedDict()
    reconciliation = []
    for a in answers:
        for it in a.get("items") or []:
            row = items.setdefault(it["sku"], {
                "sku": it["sku"], "offer_id": it.get("offer_id"), "name": it.get("name"),
                "sold": 0, "returned": 0, "net": 0, "disposed": 0, "sales": 0.0, "amounts": collections.Counter(),
                "accrued": 0.0, "cogs_sold": 0.0, "cogs_disposed": 0.0, "tax": 0.0, "profit": 0.0, "unit_cost": None,
                "missing_units": 0})
            u = it.get("units") or {}
            for k in ("sold", "returned", "net", "disposed"):
                row[k] += int(u.get(k) or 0)
            row["sales"] += zero((it.get("sales") or {}).get("total"))
            for k, v in (it.get("amounts") or {}).items():
                row["amounts"][k] += zero(v)
            row["accrued"] += zero(it.get("accrued"))
            cogs = it.get("cogs") or {}
            row["cogs_sold"] += zero(cogs.get("sold"))
            row["cogs_disposed"] += zero(cogs.get("disposed"))
            row["missing_units"] += int(cogs.get("missing_units") or 0)
            if cogs.get("unit_cost") is not None:
                row["unit_cost"] = num(cogs["unit_cost"])
            row["tax"] += zero((it.get("tax") or {}).get("amount"))
            row["profit"] += zero(it.get("profit"))
        rec = (a.get("totals") or {}).get("reconciliation")
        if rec:
            reconciliation.append({"period": a.get("period"), **rec})
    return list(items.values()), reconciliation


def unit_economics(items):
    out = []
    for r in items:
        net = r["net"]
        if net < MIN_UNITS_FOR_UNIT_ECONOMICS:
            continue
        check = r["sales"] / net
        commission = -r["amounts"].get("commission", 0.0) / net
        logistics = -(r["amounts"].get("logistics", 0.0) + r["amounts"].get("returns_logistics", 0.0)) / net
        profit = r["profit"] / net
        out.append({
            "offer_id": r["offer_id"] or r["sku"], "name": r["name"], "units": net, "accrual": check,
            "commission": commission, "commission_pct": 100 * commission / check if check else 0.0,
            "logistics": logistics, "cost": r["unit_cost"], "margin": profit,
            "margin_pct": 100 * profit / check if check else 0.0,
        })
    out.sort(key=lambda x: -x["units"])
    return out


def assets_part(assets):
    if not assets:
        return None
    by_section = collections.Counter()
    for g in assets.get("groups") or []:
        if g.get("reference"):
            continue
        by_section[g["section"]] += int(g.get("units") or 0)
    groups = {g["key"]: g for g in assets.get("groups") or []}
    on_warehouses = sum(int(g.get("units") or 0) for g in groups.values()
                        if g["section"] in OZON_WAREHOUSE_SECTIONS and not g.get("reference"))
    by_cluster = collections.Counter()
    by_offer = collections.Counter()
    section_of = {g["key"]: g["section"] for g in groups.values()}
    for it in assets.get("items") or []:
        units = it.get("units") or {}
        own = sum(v for k, v in units.items() if section_of.get(k) in OZON_WAREHOUSE_SECTIONS)
        if own:
            by_offer[it.get("offer_id") or it["sku"]] += own
        for w in it.get("warehouses") or []:
            if (w.get("scheme") or "").upper() != "FBO":
                continue
            qty = sum(v for k, v in (w.get("units") or {}).items() if section_of.get(k) in OZON_WAREHOUSE_SECTIONS)
            if qty:
                by_cluster[w.get("cluster") or "?"] += qty
    summary = assets.get("summary") or {}
    return {
        "as_of": assets.get("as_of"),
        "total": on_warehouses,
        "by_section": dict(by_section),
        "by_cluster": dict(by_cluster.most_common()),
        "by_offer": dict(by_offer.most_common()),
        "inbound": int((groups.get("supply_transit") or {}).get("units") or 0),
        "removal": sum(int((groups.get(k) or {}).get("units") or 0) for k in ("removal_ready", "removal_in_transit")),
        "goods_cost": num(summary.get("goods_cost")),
        "goods_retail": num(summary.get("goods_retail")),
        "assets_total": num(summary.get("total")),
        "balance": num((summary.get("money") or {}).get("balance")),
        "problem_cost": num(summary.get("problem_cost")),
        "requested": int((assets.get("excluded") or {}).get("requested") or 0),
    }


def material_balance(batches):
    """Сверка остатка FBO с журналом партий (cost_batches_list): журнал − (у Ozon + в доставке)."""
    if not batches:
        return None
    rows = []
    for it in batches.get("items") or []:
        diff = int(it.get("difference") or 0)
        cost = num(it.get("remaining_avg_cost"))
        rows.append({"offer_id": it.get("offer_id") or it["sku"], "sku": it["sku"], "name": it.get("name"),
                     "ledger": int(it.get("ledger_remaining") or 0), "ozon_stock": int(it.get("ozon_stock") or 0),
                     "in_delivery": int(it.get("in_delivery") or 0), "gap": diff,
                     "gap_cost": None if cost is None else diff * cost})
    rows.sort(key=lambda r: -abs(r["gap"]))
    shortage = [r for r in rows if r["gap"] > 0]
    surplus = [r for r in rows if r["gap"] < 0]
    return {
        "rows": rows,
        "ledger_units": sum(r["ledger"] for r in rows),
        "ozon_units": sum(r["ozon_stock"] for r in rows),
        "in_delivery_units": sum(r["in_delivery"] for r in rows),
        "shortage_units": sum(r["gap"] for r in shortage),
        "shortage_cost": sum(r["gap_cost"] or 0.0 for r in shortage),
        "surplus_units": -sum(r["gap"] for r in surplus),
        "unknown_cost_units": sum(abs(r["gap"]) for r in rows if r["gap"] and r["gap_cost"] is None),
    }


def non_pickups(pages):
    """Невыкупы и возвраты по строкам начислений 6, 40, 59: уникальные отправления и сумма услуг; повторы строк страниц — раз."""
    seen, units, amount = set(), set(), 0.0
    for page in pages:
        for r in page.get("items") or []:
            key = r.get("accrual_id") or (r.get("unit_number"), r.get("type_id"), r.get("date"), r.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            amount += float(r.get("amount") or 0)
            if r.get("unit_number"):
                units.add(str(r["unit_number"]))
    return {"count": len(units), "rows": len(seen), "accruals_amount": round(amount, 2), "basis": "postings"}


def seller_part(seller):
    """Реквизиты продавца (stores_seller_info_get): юрлицо, ИНН, система налогообложения Ozon; None — не прочитаны."""
    company = (seller or {}).get("company")
    if not company:
        return None
    return {"legal_name": company.get("legal_name") or company.get("name"), "inn": company.get("inn"),
            "tax_system": company.get("tax_system")}


def _merge_counts(into, rows, key, fields):
    """Строки «по состоянию» нескольких ответов — в одну: состояния сравниваются без учёта регистра и «ё»."""
    for r in rows or []:
        state = r.get(key)
        norm = (state or "").strip().lower().replace("ё", "е")
        entry = into.setdefault(norm, {key: state, **{f: 0 for f in fields}})
        for f in fields:
            entry[f] += int(r.get(f) or 0)


def removals_part(answers):
    """Вывоз со складов за период — итоги removals_list (одного ответа или окон без пересечений)."""
    if not answers:
        return None
    out = {k: 0 for k in ("units", "requests", "boxes", "given_out_units", "utilized_units")}
    by_state, by_box, by_type, complete = {}, {}, {}, True
    for a in answers:
        t = a.get("totals") or {}
        for k in out:
            out[k] += int(t.get(k) or 0)
        _merge_counts(by_state, t.get("by_state"), "state", ("requests", "units"))
        _merge_counts(by_box, t.get("by_box_state"), "state", ("boxes", "units"))
        _merge_counts(by_type, t.get("by_stock_type"), "stock_type", ("units",))
        complete = complete and (a.get("coverage") or {}).get("complete") is True
    def by_units(rows):
        return sorted(rows.values(), key=lambda r: -r["units"])

    return dict(out, by_state=by_units(by_state), by_box_state=by_units(by_box), by_stock_type=by_units(by_type),
                complete=complete)


def decommissioned_part(pages):
    """Списанные товары за период (decommissioned_goods_list): итоги по причинам — из totals ответа, по товарам — из
    строк всех страниц; страницы одного запроса (те же фильтры), totals у них одинаковые."""
    if not pages:
        return None
    t = pages[0].get("totals") or {}
    by_product = collections.OrderedDict()
    rows = 0
    for page in pages:
        for r in page.get("items") or []:
            rows += 1
            key = r.get("sku") or r.get("offer_id")
            entry = by_product.setdefault(key, {"offer_id": r.get("offer_id") or r.get("sku"), "name": r.get("name"),
                                                "qty": 0, "fee": 0.0, "compensation": 0.0})
            entry["qty"] += int(r.get("quantity") or 0)
            entry["fee"] += zero(r.get("disposal_fee"))
            entry["compensation"] += zero(r.get("compensation_price"))
    return {
        "quantity": int(t.get("quantity") or 0),
        "fee": zero(t.get("disposal_fee")),
        "compensation": zero(t.get("compensation_price")),
        "by_reason": [{"reason": r.get("reason"), "label": r.get("reason_label") or r.get("reason") or "не указана",
                       "qty": int(r.get("quantity") or 0), "fee": zero(r.get("disposal_fee")),
                       "compensation": zero(r.get("compensation_price"))} for r in t.get("by_reason") or []],
        "by_product": sorted(by_product.values(), key=lambda x: -x["qty"]),
        "rows": rows,
        "total": int(pages[0].get("total") or rows),
        "complete": (pages[0].get("coverage") or {}).get("complete") is True,
    }


def build(profit_answers, accrual_answers, assets=None, batches=None, config=None, non_pickup_pages=None,
          seller=None, removal_answers=None, decommissioned_pages=None):
    config = config or {}
    p = merge_profit(profit_answers)
    items, reconciliation = merge_accruals(accrual_answers)
    s = p["summary"]
    commission = p["categories"].get("commission", 0.0)
    sales_net = s["sales"] + s["returns"]
    services = s["accrued"] - sales_net - commission
    disposed_units = sum(r["disposed"] for r in items)
    totals = {
        "sales": sales_net, "commission": commission, "services": services, "accrued": s["accrued"],
        "estimate": p["estimate"], "cogs": s["cogs_sold"], "disposal_cogs": s["cogs_disposed"], "vat": s["vat"],
        "tax": s["tax"], "net": s["profit"], "sold_units": int(s["units_sold"] - s["units_returned"]),
        "disposed_units": disposed_units,
    }
    by_category = {CATEGORY_RU.get(k, k): v for k, v in p["categories"].items() if k not in SALES_KEYS + ("commission",)}
    by_category = dict(sorted(by_category.items(), key=lambda kv: -abs(kv[1])))
    detail = [{"category": CATEGORY_RU.get(t["category"], t["category"]), "key": key, "name": t["name"],
               "amount": t["amount"]}
              for key, t in p["types"].items() if t["category"] not in SALES_KEYS + ("commission",)]
    detail.sort(key=lambda d: d["amount"])
    disposal_rows = sorted(({"offer_id": r["offer_id"] or r["sku"], "name": r["name"], "qty": r["disposed"],
                             "cost": r["cogs_disposed"], "fee": -r["amounts"].get("disposal", 0.0)}
                            for r in items if r["disposed"]), key=lambda d: -d["qty"])
    warnings = []
    q = p["quality"]
    if q["tax_rate_missing"]:
        warnings.append("ставка налога не задана хотя бы на часть периода: налог там не вычтен")
    if q["missing_cost_skus"]:
        warnings.append(f"товаров без себестоимости: {q['missing_cost_skus']} — их себестоимость не вычтена")
    if q["ozon_cost_skus"]:
        warnings.append(f"товаров с себестоимостью «из Ozon» (net_price карточки): {q['ozon_cost_skus']}")
    if p["estimate"]:
        warnings.append(f"в прибыли — реклама, которую Ozon ещё не списал, по статистике: {p['estimate']:.2f} ₽")
    for rec in reconciliation:
        diff = num(rec.get("difference"))
        if diff:
            warnings.append(f"сверка с «Начислениями» за {rec['period']['from']}–{rec['period']['to']}: "
                            f"расхождение {diff:.2f} ₽ — дефект расчёта Seller AI")
    mb = material_balance(batches)
    if mb and mb["unknown_cost_units"]:
        warnings.append(f"расхождение партий без себестоимости: {mb['unknown_cost_units']} шт — не оценено в рублях")
    removals = removals_part(removal_answers)
    if removals and not removals["complete"]:
        warnings.append("вывоз: Seller AI прочитал из Ozon не все кварталы периода (coverage.complete) — цифры вывоза неполные")
    decommissioned = decommissioned_part(decommissioned_pages)
    if decommissioned and not decommissioned["complete"]:
        warnings.append("списанные товары: Seller AI прочитал из Ozon не все дни периода (coverage.complete) — список неполный")
    if decommissioned and decommissioned["rows"] < decommissioned["total"]:
        warnings.append(f"списанное по товарам — по {decommissioned['rows']} из {decommissioned['total']} строк: "
                        "передайте все страницы decommissioned_goods_list")
    for w in p["warnings"]:
        warnings.append(str(w))
    returns_logistics = p["categories"].get("returns_logistics", 0.0)
    return {
        "meta": {"store_id": config.get("store_id"), "store_name": config.get("store_name"),
                 "date_from": p["period"]["from"], "date_to": p["period"]["to"], "scheme": p["scheme"],
                 "cost_method": p["cost_method"], "tax": p["tax"], "seller": seller_part(seller)},
        "months": p["months"],
        "totals": totals,
        "services": {"by_category": by_category, "detail": detail},
        "disposal": {"by_product": disposal_rows, "fee": -p["categories"].get("disposal", 0.0)},
        "unit_economics": unit_economics(items),
        "non_pickup": dict({"amount": returns_logistics, "count": config.get("non_pickup_count"),
                            "basis": "rows" if config.get("non_pickup_count") is not None else None},
                           **(non_pickups(non_pickup_pages) if non_pickup_pages else {})),
        "stock": assets_part(assets),
        "material_balance": mb,
        "removals": removals,
        "decommissioned": decommissioned,
        "unallocated": q["unallocated"],
        "extra": config.get("extra", []),
        "warnings": warnings,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profit", nargs="+", required=True)
    ap.add_argument("--accruals", nargs="+", required=True)
    ap.add_argument("--assets")
    ap.add_argument("--batches")
    ap.add_argument("--non-pickups", nargs="*")
    ap.add_argument("--seller")
    ap.add_argument("--removals", nargs="*")
    ap.add_argument("--decommissioned", nargs="*")
    ap.add_argument("--config")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    report = build([load(f) for f in args.profit], [load(f) for f in args.accruals],
                   load(args.assets) if args.assets else None, load(args.batches) if args.batches else None,
                   load(args.config) if args.config else None,
                   [load(f) for f in args.non_pickups] if args.non_pickups else None,
                   load(args.seller) if args.seller else None,
                   [load(f) for f in args.removals] if args.removals else None,
                   [load(f) for f in args.decommissioned] if args.decommissioned else None)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    t = report["totals"]
    print(f"{report['meta']['date_from']} — {report['meta']['date_to']}: начислено {t['accrued']:.2f}, "
          f"чистая прибыль {t['net']:.2f}, месяцев {len(report['months'])}", file=sys.stderr)
    for w in report["warnings"]:
        print("⚠", w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
