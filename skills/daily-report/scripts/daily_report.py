#!/usr/bin/env python3
"""Ежедневный отчёт по рекламе и продажам из ответов инструментов Seller AI.

Скрипт ничего не запрашивает: агент сохраняет ответы инструментов в файлы и передаёт их сюда.

    python3 daily_report.py --products products.json --plan plan.json \
        [--cpc cpc.json] [--campaign-products c1.json c2.json …] [--cpo cpo.json] \
        [--previous products_earlier.json] [--alerts alerts.json] [--strategy strategy.json] [--store-id ID] \
        [--store-name NAME] [--json]

--products  ответ promotion_analytics_get со slice=product (все типы продвижения) за день или период;
--cpc       тот же запрос с types=cpc_search_reco,cpc_search,special — чтобы разделить расход на CPC и CPO;
--plan      ответ unit_economics_plan_list (целевой ДРР товара — inputs.drr, доля);
--campaign-products  ответы promotion_campaign_products_list активных кампаний за клики (по файлу на кампанию):
            текущая ставка товара в каждой («CPC тек.», у автостратегии — «авто») и цель биддера (`bidder`);
--cpo       ответ promotion_cpo_products_list: ставка «Оплаты за заказ» товара в % и ₽;
--previous  более ранний ответ --products за тот же период (промежуточный срез);
--alerts    ответ promotion_bidder_alerts_list (actionable=true): текущие алерты, которые решает продавец, — аномалии
            «БИДДЕР ПРОСИТ РЕШЕНИЯ», замолчавший биддер (bidder_silent) — первой строкой;
--strategy  references/store-promotion-strategies.json (пороги и коридор цели).

Цель товара: в кампании биддера («Целевой общий расход») — его цель в силе (`bidder.target_total_drr`: своя, из плана
или разгона), иначе ДРР плана юнит-экономики.

Печатает Markdown (или JSON с --json): аномалии, итог, таблица по товарам, ДРР к цели, наблюдения, срез.
"""
import argparse
import json
import sys

# ── Пороги аномалий (владелец: «отчёт читает сам себя», текст — КАПСОМ) ──
MIN_SPEND = 300.0           # ₽: ниже расход — шум, ДРР скачет случайно
MIN_OVERSPEND = 300.0       # ₽: перерасход сверх цели, начиная с которого кричим
SPEND_NO_SALES_MIN = 500.0  # ₽: расход при нулевой выручке
NO_AD_ORDERS_MIN = 1000.0   # ₽: расход при нулевых рекламных продажах (Ozon отдаёт их с задержкой)
HIGH_DRR_NO_TARGET = 40.0   # %: ДРР товара без цели
DRR_OVER_TARGET_PCT = 1.0   # п. п. сверх цели — перерасход
SPEND_SHARE_ALERT = 50.0    # %: доля одного товара в расходе магазина
STORE_MIN_SPEND = 1000.0    # ₽: минимальный расход магазина для итоговых аномалий
DELTA_MIN_SPEND = 300.0     # ₽: прирост расхода между срезами
MAX_LINES = 12
HIGH_DRR_MARK = 40.0        # %: пометка ⚠️ в таблице
DELTA_DRR_MARK = 25.0       # %: пометка ⚠️ в срезе

DEFAULT_STRATEGY = {"target_band": {"below": -2.0, "above": 1.0}, "min_spend_for_action_rub": 300}
# Стратегии со ставками товаров (у остальных — автостратегии Ozon: ставки он ставит сам, `bid` = 0).
BID_STRATEGIES = {"TARGET_BIDS", "TARGET_TOTAL_DRR"}


def load(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def num(value):
    """Число из ответа Seller AI (число или десятичная строка); None — неизвестно."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value):
    if value is None:
        return "—"
    return f"{value:,.0f} ₽".replace(",", " ").replace("-", "−")


def pct(value, digits=1):
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def product_rows(answer):
    """Строки товаров ответа promotion_analytics_get (slice=product) по SKU."""
    rows = {}
    for row in (answer or {}).get("rows") or []:
        if row.get("kind") != "product" or row.get("sku") is None:
            continue
        rows[str(row["sku"])] = row
    return rows


def targets_from_plan(plan):
    """SKU → целевой ДРР в процентах. ДРР плана задаёт только продавец: не задан — 0 в inputs.drr, такой товар без цели."""
    out = {}
    for item in (plan or {}).get("items") or []:
        sku = item.get("sku")
        drr = num((item.get("inputs") or {}).get("drr"))
        if sku is None or drr is None or drr <= 0:
            continue
        out[str(sku)] = drr * 100
    return out


def bids_from_campaigns(answers):
    """SKU → ставки по кампаниям и цель биддера из ответов promotion_campaign_products_list."""
    out = {}
    for a in answers or []:
        strategy = (a.get("strategy") or "").upper() or None
        for item in a.get("items") or []:
            sku = str(item.get("sku"))
            e = out.setdefault(sku, {"campaigns": [], "bidder": None, "bidder_spend": None})
            bidder = item.get("bidder")
            auto = strategy is not None and strategy not in BID_STRATEGIES and not bidder
            e["campaigns"].append({"campaign_id": a.get("campaign_id"), "strategy": strategy, "auto": auto,
                                   "bid": None if auto else num(item.get("bid")), "bidder": bool(bidder)})
            spend = num((item.get("stats") or {}).get("expense")) or 0.0
            if bidder and bidder.get("target_total_drr") is not None and (e["bidder_spend"] is None
                                                                           or spend > e["bidder_spend"]):
                e["bidder"] = {"target": num(bidder["target_total_drr"]), "source": bidder.get("target_source")}
                e["bidder_spend"] = spend
    return out


def cpo_bids(answer):
    """SKU → ставка «Оплаты за заказ»: включена ли, % и ₽ (как в кабинете: display_bid_pct / display_bid_rub)."""
    out = {}
    for item in (answer or {}).get("items") or []:
        out[str(item.get("sku"))] = {"enabled": item.get("enabled"), "pct": num(item.get("display_bid_pct")),
                                     "rub": num(item.get("display_bid_rub")), "reduced": bool(item.get("reduced"))}
    return out


def bid_text(bids):
    """«CPC тек.» по кампаниям: «12 ₽ / авто»; биддер — «12 ₽ (б)»."""
    if not bids:
        return "—"
    parts = []
    for c in bids["campaigns"]:
        if c["auto"]:
            parts.append("авто")
        elif c["bid"] is None:
            parts.append("—")
        else:
            parts.append(f"{c['bid']:,.0f} ₽".replace(",", " ") + (" (б)" if c["bidder"] else ""))
    return " / ".join(parts)


def build_rows(products, cpc, targets, bids=None, cpo=None):
    cpc_rows = product_rows(cpc) if cpc else {}
    bids, cpo = bids or {}, cpo or {}
    out = []
    for sku, row in product_rows(products).items():
        m = row.get("metrics") or {}
        spend = num(m.get("expense")) or 0.0
        c = (cpc_rows.get(sku) or {}).get("metrics") or {}
        cpc_spend = num(c.get("expense")) if cpc else None
        clicks = num(m.get("clicks"))
        orders = num(m.get("orders"))
        b = bids.get(sku)
        bidder = (b or {}).get("bidder")
        target, source = (bidder["target"], f"bidder:{bidder['source']}") if bidder else (targets.get(sku), "plan")
        c_bid = cpo.get(sku)
        out.append({
            "sku": sku,
            "name": row.get("name") or "",
            "spend": spend,
            "cpc_spend": cpc_spend,
            "cpo_spend": None if cpc_spend is None else round(spend - cpc_spend, 2),
            "ad_revenue": num(m.get("sales")) or 0.0,
            "revenue": num(m.get("gmv")),           # вся выручка товара по заказам; None — не загружена
            "orders": orders,
            "ctr": num(m.get("ctr")),
            "conversion": (orders / clicks * 100) if clicks and orders is not None else None,
            "drr": num(m.get("drr_promo")),
            "drr_total": num(m.get("drr")),
            "target": target,
            "target_source": source if target is not None else None,
            "avg_cpc": num(c.get("click_cost")) if cpc else None,
            "bids": b["campaigns"] if b else None,
            "cpc_now": bid_text(b) if b else None,
            "cpo": c_bid,
        })
    out.sort(key=lambda r: -r["spend"])
    return out


def totals_of(products):
    m = ((products or {}).get("total") or {}).get("metrics") or {}
    gmv = products.get("gmv") or {}
    return {
        "spend": num(m.get("expense")) or 0.0,
        "ad_revenue": num(m.get("sales")) or 0.0,
        "orders": num(m.get("orders")),
        "ctr": num(m.get("ctr")),
        "drr": num(m.get("drr_promo")),
        "drr_total": num(m.get("drr")),
        "revenue": num(gmv.get("total")) if gmv.get("covered", True) else None,
        "covered": gmv.get("covered", True),
    }


def _label(r):
    return (r.get("name") or r["sku"])[:40].upper()


def bidder_alerts(answer):
    """Текущие алерты биддера, которые решает продавец (promotion_bidder_alerts_list): (вид, текст)."""
    out = []
    for a in (answer or {}).get("items") or []:
        if not a.get("actionable") or a.get("state") != "current":
            continue
        where = " — ".join(x for x in (a.get("campaign_title") or f"кампания {a.get('campaign_id')}",
                                       a.get("name") or a.get("sku")) if x)
        out.append((a.get("kind"), f"БИДДЕР ПРОСИТ РЕШЕНИЯ: {where}: {a.get('message') or a.get('kind')}"))
    return out


def detect_anomalies(rows, totals, single_day=True, previous=None, data_errors=(), alerts=()):
    """Аномалии: список {kind, text, impact} по убыванию денег (impact, ₽); замолчавший биддер — первым: кампания
    крутится на последних ставках без контроля ДРР."""
    found = []

    def add(kind, text, impact):
        found.append({"kind": kind, "text": text.upper(), "impact": round(impact, 2)})

    for err in data_errors:
        add("data", f"ДАННЫЕ НЕПОЛНЫЕ: {err}", 0)
    for kind, text in alerts:
        add("bidder_silent" if kind == "bidder_silent" else "bidder", text, 0)
    store_spend = totals["spend"]
    for r in rows:
        spend, revenue, label = r["spend"], r["revenue"], _label(r)
        if spend < MIN_SPEND or revenue is None:
            continue
        target, drr_total = r["target"], r["drr_total"] or 0.0
        if revenue <= 0:
            if spend >= SPEND_NO_SALES_MIN:
                add("spend_no_sales", f"РАСХОД БЕЗ ПРОДАЖ: {label} — ПОТРАЧЕНО {money(spend)}, ВЫРУЧКА 0", spend)
            continue
        if target is not None:
            overspend = spend - revenue * target / 100
            if drr_total > target + DRR_OVER_TARGET_PCT and overspend >= MIN_OVERSPEND:
                add("overspend", f"ПЕРЕРАСХОД: {label} — ДРР {pct(drr_total)} ПРИ ЦЕЛИ {pct(target)}, ЛИШНИЕ "
                    f"{money(overspend)} (РАСХОД {money(spend)} / ВЫРУЧКА {money(revenue)})", overspend)
        elif drr_total > HIGH_DRR_NO_TARGET:
            add("high_drr", f"ВЫСОКИЙ ДРР БЕЗ ЦЕЛИ: {label} — ДРР {pct(drr_total)} (РАСХОД {money(spend)} / ВЫРУЧКА "
                f"{money(revenue)})", spend * (drr_total - HIGH_DRR_NO_TARGET) / max(drr_total, 1))
        if r["ad_revenue"] <= 0 and spend >= NO_AD_ORDERS_MIN:
            add("no_ad_orders", f"РЕКЛАМА БЕЗ ЗАКАЗОВ: {label} — РАСХОД {money(spend)}, РЕКЛАМНЫХ ПРОДАЖ 0 "
                f"(ВЫРУЧКА ВСЕГО {money(revenue)})", spend)
        if store_spend > 0 and spend >= STORE_MIN_SPEND:
            share = spend / store_spend * 100
            over = target is not None and drr_total > target + DRR_OVER_TARGET_PCT
            if share >= SPEND_SHARE_ALERT and (over or drr_total > HIGH_DRR_NO_TARGET):
                add("concentration", f"КОНЦЕНТРАЦИЯ БЮДЖЕТА: {label} — {share:.0f}% РАСХОДА МАГАЗИНА ({money(spend)}) "
                    f"ПРИ ДРР {pct(drr_total)}", 0)
    for d in deltas(rows, previous) if previous else []:
        if d["spend"] < DELTA_MIN_SPEND:
            continue
        if d["revenue"] is None:
            continue
        if d["revenue"] <= 0:
            add("delta_no_revenue", f"С ПРОШЛОГО СРЕЗА: {d['label']} — РАСХОД +{money(d['spend'])}, ВЫРУЧКА НЕ ВЫРОСЛА",
                d["spend"])
            continue
        d_drr = d["spend"] / d["revenue"] * 100
        limit = d["target"] * 2 if d["target"] is not None else HIGH_DRR_NO_TARGET
        if d_drr > limit:
            add("delta_high_drr", f"С ПРОШЛОГО СРЕЗА: {d['label']} — ДРР ПРИРОСТА {pct(d_drr)} (+{money(d['spend'])} "
                f"РАСХОДА НА +{money(d['revenue'])} ВЫРУЧКИ)", d["spend"] - d["revenue"] * limit / 100)
    store_revenue = totals["revenue"]
    if single_day and store_spend >= STORE_MIN_SPEND and store_revenue:
        num_, den = 0.0, 0.0
        for r in rows:
            if r["target"] is not None and r["revenue"]:
                num_ += r["target"] * r["revenue"]
                den += r["revenue"]
        if den > 0:
            store_target = num_ / den
            store_drr = store_spend / store_revenue * 100
            overspend = store_spend - store_revenue * store_target / 100
            if store_drr > store_target + DRR_OVER_TARGET_PCT and overspend >= MIN_OVERSPEND:
                add("store_overspend", f"ПО МАГАЗИНУ: ДРР {pct(store_drr)} ПРИ ЦЕЛИ {pct(store_target)}, ПЕРЕРАСХОД "
                    f"{money(overspend)} ЗА ДЕНЬ", overspend)
    found.sort(key=lambda a: (a["kind"] != "bidder_silent", -a["impact"]))
    return found


def deltas(rows, previous_rows):
    """Прирост между более ранним срезом и текущим по каждому товару."""
    prev = {r["sku"]: r for r in previous_rows}
    out = []
    for r in rows:
        p = prev.get(r["sku"])
        if not p:
            continue
        d_rev = None if r["revenue"] is None or p["revenue"] is None else r["revenue"] - p["revenue"]
        out.append({"sku": r["sku"], "label": _label(r), "name": r["name"], "spend": r["spend"] - p["spend"],
                    "revenue": d_rev, "target": r["target"]})
    return out


def signal(row, band):
    """🔴 выше цели, 🟢 в коридоре, 🔵 ниже, None — без цели или без выручки."""
    if row["target"] is None or row["drr_total"] is None:
        return None
    diff = row["drr_total"] - row["target"]
    if diff > band["above"]:
        return "above"
    if diff < band["below"]:
        return "below"
    return "ok"


def observations(rows, totals):
    out = []
    active = [r for r in rows if r["spend"] > 0]
    if active and totals["spend"] > 0:
        top = active[0]
        out.append(f"Больше всего расхода — {top['name'][:50]}: {money(top['spend'])} "
                   f"({top['spend'] / totals['spend'] * 100:.0f}% бюджета), ДРР общ. {pct(top['drr_total'])}.")
    high = [r for r in active if (r["drr_total"] or 0) > HIGH_DRR_MARK]
    if high:
        out.append("ДРР общ. выше 40% — пересмотреть ставки: " + ", ".join(r["name"][:30] for r in high[:3]) + ".")
    with_rev = [r for r in active if r["revenue"] and r["drr_total"]]
    if with_rev:
        best = min(with_rev, key=lambda r: r["drr_total"])
        out.append(f"Лучший ДРР общ. — {best['name'][:50]}: {pct(best['drr_total'])}.")
    with_conv = [r for r in active if r["conversion"]]
    if with_conv:
        best = max(with_conv, key=lambda r: r["conversion"])
        out.append(f"Лучшая конверсия клика в заказ — {best['name'][:50]}: {pct(best['conversion'])}.")
    return out


def build(products, plan, cpc=None, previous=None, strategy=None, store_id=None, campaign_products=None, cpo=None,
          alerts=None):
    strategy_all = strategy or {}
    merged = dict(DEFAULT_STRATEGY)
    merged.update(strategy_all.get("default_strategy") or {})
    merged.update((strategy_all.get("stores") or {}).get(str(store_id), {}) if store_id else {})
    band = merged.get("target_band") or DEFAULT_STRATEGY["target_band"]
    targets = targets_from_plan(plan)
    bids = bids_from_campaigns(campaign_products)
    rows = build_rows(products, cpc, targets, bids, cpo_bids(cpo))
    totals = totals_of(products)
    period = products.get("period") or {}
    single_day = period.get("from") == period.get("to")
    errors = []
    if not totals["covered"]:
        errors.append("ОТПРАВЛЕНИЯ ПЕРИОДА ЕЩЁ НЕ ЗАГРУЖЕНЫ — ОБЩИЙ ДРР НЕИЗВЕСТЕН")
    if (products.get("models") or {}).get("unknown_days"):
        errors.append("ЗАКАЗЫ МОДЕЛЕЙ НЕИЗВЕСТНЫ ЗА " + ", ".join(products["models"]["unknown_days"]))
    prev_rows = build_rows(previous, None, targets, bids) if previous else None
    anomalies = detect_anomalies(rows, totals, single_day, prev_rows, errors, bidder_alerts(alerts))
    for r in rows:
        r["signal"] = signal(r, band) if single_day else None
    return {
        "period": period, "store_id": store_id, "totals": totals, "rows": rows, "anomalies": anomalies,
        "observations": observations(rows, totals), "band": band, "bids_known": campaign_products is not None,
        "cpo_known": cpo is not None,
        "deltas": deltas(rows, prev_rows) if prev_rows else None,
    }


def render(report, store_name=""):
    p = report["period"]
    when = p.get("from") if p.get("from") == p.get("to") else f"{p.get('from')} — {p.get('to')}"
    t = report["totals"]
    lines = [f"# Отчёт по рекламе — {store_name} — {when}".replace(" —  —", " —"), "", "## 🚨 АНОМАЛИИ", ""]
    anomalies = report["anomalies"]
    if not anomalies:
        lines.append("АНОМАЛИЙ НЕ НАЙДЕНО")
    for a in anomalies[:MAX_LINES]:
        lines.append(f"- **{a['text']}**")
    if len(anomalies) > MAX_LINES:
        lines.append(f"- …И ЕЩЁ {len(anomalies) - MAX_LINES} АНОМАЛИЙ")
    lines += ["", "## Итого", "", "| Показатель | Значение |", "|---|---|",
              f"| Расход на рекламу | {money(t['spend'])} |",
              f"| Продажи с рекламы | {money(t['ad_revenue'])} |",
              f"| Вся выручка (заказы) | {money(t['revenue'])} |",
              f"| Продано с рекламы, шт. | {'—' if t['orders'] is None else int(t['orders'])} |",
              f"| ДРР в продвижении | {pct(t['drr'])} |",
              f"| ДРР общий | {pct(t['drr_total'])} |",
              f"| CTR | {pct(t['ctr'], 2)} |", "",
              "## По товарам", ""]
    extra = (["CPC тек."] if report.get("bids_known") else []) + (["CPO %", "CPO ₽"] if report.get("cpo_known") else [])
    head = ["Товар", "Расход CPC", "Расход CPO", "Расход ∑", "Прод. рекл.", "Прод. итого", "CTR", "Конв.", "ДРР",
            "ДРР общ.", "Цел. ДРР", "CPC ср."] + extra
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in report["rows"]:
        mark = " ⚠️" if (r["drr_total"] or 0) > HIGH_DRR_MARK else ""
        star = "*" if (r.get("target_source") or "").startswith("bidder") else ""
        cells = [f"{r['name'][:50]} ({r['sku']})", money(r['cpc_spend']), money(r['cpo_spend']), money(r['spend']),
                 money(r['ad_revenue']), money(r['revenue']), pct(r['ctr'], 2) if r['ctr'] else '—',
                 pct(r['conversion']) if r['conversion'] else '—', pct(r['drr']) if r['drr'] else '—',
                 f"{pct(r['drr_total'])}{mark}", f"{pct(r['target'])}{star}",
                 money(r['avg_cpc']) if r['avg_cpc'] else '—']
        if report.get("bids_known"):
            cells.append(r.get("cpc_now") or "—")
        if report.get("cpo_known"):
            c = r.get("cpo")
            if not c:
                cells += ["—", "—"]
            elif c["enabled"] is False:
                cells += ["выкл.", "—"]
            else:
                cells += [pct(c["pct"]), money(c["rub"])]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("| " + " | ".join(["**ИТОГО**", "", "", f"**{money(t['spend'])}**", f"**{money(t['ad_revenue'])}**",
                                    f"**{money(t['revenue'])}**", f"**{pct(t['ctr'], 2)}**", "", f"**{pct(t['drr'])}**",
                                    f"**{pct(t['drr_total'])}**", "", ""] + [""] * len(extra)) + " |")
    if any((r.get("target_source") or "").startswith("bidder") for r in report["rows"]):
        lines += ["", "\\* цель биддера («Целевой общий расход»: своя, из плана или разгон) — сигналы и перерасход "
                      "считаются к ней. «(б)» — ставку ставит биддер."]
    groups = {"above": [], "ok": [], "below": []}
    for r in report["rows"]:
        if r.get("signal"):
            groups[r["signal"]].append(r)
    if any(groups.values()):
        band = report["band"]
        lines += ["", "## ДРР к цели", ""]
        for key, title in (("above", f"🔴 Выше цели (больше +{band['above']:g} п. п.)"),
                           ("ok", f"🟢 В коридоре ({band['below']:g}…+{band['above']:g} п. п.)"),
                           ("below", f"🔵 Ниже цели (меньше {band['below']:g} п. п.)")):
            if groups[key]:
                lines.append(f"- {title}: " + "; ".join(
                    f"{r['name'][:40]} {pct(r['drr_total'])} при цели {pct(r['target'])}" for r in groups[key]))
    if report["observations"]:
        lines += ["", "## Наблюдения", ""] + [f"- {o}" for o in report["observations"]]
    if report.get("deltas"):
        changed = [d for d in report["deltas"] if abs(d["spend"]) >= 1 or (d["revenue"] or 0) >= 1]
        if changed:
            lines += ["", "## Промежуточный срез (с прошлого отчёта)", "",
                      "| Товар | Выручка | Расход | ДРР прироста |", "|---|---|---|---|"]
            ts, tr = 0.0, 0.0
            for d in changed:
                ts += d["spend"]
                tr += d["revenue"] or 0.0
                drr = d["spend"] / d["revenue"] * 100 if d["revenue"] and d["revenue"] > 0 else None
                mark = " ⚠️" if drr is not None and drr > DELTA_DRR_MARK else ""
                lines.append(f"| {d['name'][:50]} | {money(d['revenue'])} | {money(d['spend'])} | {pct(drr)}{mark} |")
            lines.append(f"| **ИТОГО** | **{money(tr)}** | **{money(ts)}** | **{pct(ts / tr * 100 if tr > 0 else None)}** |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--products", required=True)
    ap.add_argument("--plan")
    ap.add_argument("--cpc")
    ap.add_argument("--campaign-products", nargs="*", help="ответы promotion_campaign_products_list")
    ap.add_argument("--cpo", help="ответ promotion_cpo_products_list")
    ap.add_argument("--previous")
    ap.add_argument("--alerts", help="ответ promotion_bidder_alerts_list (actionable=true)")
    ap.add_argument("--strategy")
    ap.add_argument("--store-id")
    ap.add_argument("--store-name", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    campaigns = [load(f) for f in args.campaign_products] if args.campaign_products is not None else None
    report = build(load(args.products), load(args.plan), load(args.cpc), load(args.previous), load(args.strategy),
                   args.store_id, campaigns, load(args.cpo), load(args.alerts))
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(report, args.store_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
