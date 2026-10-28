#!/usr/bin/env python3
"""Заказы по товарам и дням из страниц `orders_list` — Markdown или JSON; ничего не вызывает.

    python3 orders_summary.py in.json [--json]
    вход: {"pages": [ответы orders_list, все страницы], "group": ["sku"] | ["day"] | ["sku", "day"],
           "exclude_statuses": ["cancelled"], "exclude_postings": [номера родителей разделённых FBS]}

Заказы — все статусы, если их не исключить; день — по Москве: FBO по `created_at`, FBS и rFBS по `in_process_at` (как
в «Заказах» кабинета). Суммы — по цене продавца: Σ price × quantity, выплата — Σ payout × quantity там, где Ozon её отдал
(штуки без выплаты — отдельно: неизвестно, не 0). Отправление на перекрывающихся страницах считается один раз.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

MSK = timezone(timedelta(hours=3))


def _day(row):
    ts = row.get("created_at") if row.get("scheme") == "fbo" else (row.get("in_process_at") or row.get("created_at"))
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(MSK).date().isoformat()


def summarize(pages, group=("sku",), exclude_statuses=(), exclude_postings=()):
    seen, agg = set(), {}
    skip_s, skip_p = {s.lower() for s in exclude_statuses}, {str(p) for p in exclude_postings}
    for page in pages:
        for row in page.get("items", []) if isinstance(page, dict) else []:
            num = row.get("posting_number")
            if num in seen or num in skip_p or (row.get("status") or "").lower() in skip_s:
                continue
            seen.add(num)
            day = _day(row)
            for prod in row.get("products") or []:
                key = tuple(prod.get("sku") if g == "sku" else day for g in group)
                a = agg.setdefault(key, {"sku": prod.get("sku"), "offer_id": prod.get("offer_id"),
                                         "name": prod.get("name"), "day": day, "units": 0, "amount": Decimal(0),
                                         "payout": Decimal(0), "payout_units": 0, "unknown_price_units": 0,
                                         "postings": set()})
                q = int(prod.get("quantity") or 0)
                a["units"] += q
                a["postings"].add(num)
                if prod.get("price") is None:
                    a["unknown_price_units"] += q
                else:
                    a["amount"] += Decimal(prod["price"]) * q
                if prod.get("payout") is not None:
                    a["payout"] += Decimal(prod["payout"]) * q
                    a["payout_units"] += q
    rows = []
    for key, a in sorted(agg.items(), key=lambda kv: (-kv[1]["units"], str(kv[0]))):
        rows.append({**{g: v for g, v in zip(group, key)}, "offer_id": a["offer_id"] if "sku" in group else None,
                     "name": a["name"] if "sku" in group else None, "units": a["units"],
                     "amount": str(a["amount"].quantize(Decimal("0.01"))),
                     "avg_price": str((a["amount"] / (a["units"] - a["unknown_price_units"])).quantize(Decimal("0.01")))
                     if a["units"] > a["unknown_price_units"] else None,
                     "payout": str(a["payout"].quantize(Decimal("0.01"))) if a["payout_units"] else None,
                     "payout_unknown_units": a["units"] - a["payout_units"], "postings": len(a["postings"])})
    return {"group": list(group), "rows": rows, "postings": len(seen),
            "units": sum(r["units"] for r in rows), "amount": str(sum(Decimal(r["amount"]) for r in rows))}


def _rub(v):
    return "—" if v is None else f"{Decimal(v):,.2f} ₽".replace(",", " ")


def render(res):
    head = [{"sku": "Товар", "day": "День"}[g] for g in res["group"]] + ["Штук", "Сумма", "Ср. цена", "Выплата", "Отправлений"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in res["rows"]:
        cells = []
        for g in res["group"]:
            cells.append(f"[{r['offer_id'] or r['sku']}](https://www.ozon.ru/product/{r['sku']})" if g == "sku" else r["day"] or "—")
        payout = _rub(r["payout"]) + (f" (без выплаты {r['payout_unknown_units']} шт)" if r["payout"] and r["payout_unknown_units"] else "")
        cells += [str(r["units"]), _rub(r["amount"]), _rub(r["avg_price"]), payout, str(r["postings"])]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append(f"| **ИТОГО** |" + " |" * (len(res["group"]) - 1) + f" **{res['units']}** | **{_rub(res['amount'])}** | | | "
                 f"**{res['postings']}** |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = json.loads(sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read())
    res = summarize(d.get("pages") or [], tuple(d.get("group") or ("sku",)), d.get("exclude_statuses") or (),
                    d.get("exclude_postings") or ())
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        sys.stdout.write(render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
