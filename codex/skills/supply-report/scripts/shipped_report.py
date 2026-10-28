#!/usr/bin/env python3
"""Отчёт по отгрузкам FBO — Markdown над строками `supplies_items_list` (и, для фасовки, ответами
`supplies_orders_get`); ничего не вызывает.

    python3 shipped_report.py in.json > report.md
    вход: {"items": [строки supplies_items_list], "orders": [ответы supplies_orders_get] (необязательно),
           "period": {"from": "2026-01-01", "to": "2026-03-18"}, "group": ["product", "month", "state", "order"]}

Отгруженным считается товар поставок в статусах от «на точке отгрузки» и дальше; DATA_FILLING, READY_TO_SUPPLY,
CANCELLED, OVERDUE, REJECTED_AT_SUPPLY_WAREHOUSE не считаются. Месяц и день — по началу таймслота, время Москвы.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

SHIPPED = ["ACCEPTED_AT_SUPPLY_WAREHOUSE", "IN_TRANSIT", "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
           "REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED", "COMPLETED"]
STATE_RU = {"ACCEPTED_AT_SUPPLY_WAREHOUSE": "на точке отгрузки", "IN_TRANSIT": "в пути",
            "ACCEPTANCE_AT_STORAGE_WAREHOUSE": "приёмка", "REPORTS_CONFIRMATION_AWAITING": "согласование актов",
            "REPORT_REJECTED": "акт отклонён", "COMPLETED": "завершена"}
MSK = timezone(timedelta(hours=3))


def _day(row):
    ts = row.get("timeslot_from")
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(MSK).date()


def shipped_rows(items, period=None):
    out = []
    for r in items:
        state = r.get("supply_state") or r.get("order_state")
        if state not in SHIPPED:
            continue
        d = _day(r)
        if period and d is not None:
            if period.get("from") and d.isoformat() < period["from"]:
                continue
            if period.get("to") and d.isoformat() > period["to"]:
                continue
        out.append(dict(r, _state=state, _day=d))
    return out


def _fmt(n):
    return f"{n:,}".replace(",", " ")


def _table(head, rows):
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def packaging(rows, orders):
    """По отгруженной поставке: штук в месте; фасовка N — если все места по N."""
    cargo = {}
    for page in orders or []:
        for c in page.get("cargoes") or []:
            units = sum(int(i.get("quantity") or 0) for i in c.get("items") or [])
            cargo.setdefault(str(c["supply_id"]), []).append((c.get("type"), units if c.get("items") else None))
    agg = {}
    for r in rows:
        places = cargo.get(str(r["supply_id"]))
        if not places:
            key = "места не прочитаны"
        elif any(u is None for _, u in places):
            key = "состав мест не прочитан"
        else:
            sizes = {u for _, u in places}
            key = f"по {sizes.pop()} шт" if len(sizes) == 1 else "смешанная"
        a = agg.setdefault((r.get("offer_id") or r["sku"], key), {"places": 0, "units": 0, "days": set()})
        a["places"] += len(places or [])
        a["units"] += int(r["quantity"])
        if r["_day"]:
            a["days"].add(r["_day"].isoformat())
    return agg


def report(data):
    rows = shipped_rows(data.get("items") or [], data.get("period"))
    groups = data.get("group") or ["product", "month", "state"]
    total = sum(int(r["quantity"]) for r in rows)
    out = [f"**Отгружено: {_fmt(total)} шт** — {len({r['supply_id'] for r in rows})} поставок, "
           f"{len({r['order_id'] for r in rows})} заявок."]
    no_slot = [r for r in rows if r["_day"] is None]
    if no_slot:
        out.append(f"Без даты таймслота: {_fmt(sum(int(r['quantity']) for r in no_slot))} шт — в период не отфильтрованы.")
    if "product" in groups:
        agg = {}
        for r in rows:
            a = agg.setdefault(r["sku"], {"name": r.get("name") or "", "offer": r.get("offer_id") or "", "q": 0,
                                         "orders": set()})
            a["q"] += int(r["quantity"])
            a["orders"].add(r["order_id"])
        out += ["", "### По товарам", _table(["Товар", "Артикул", "Штук", "Заявок"],
                [[f"[{a['name']}](https://www.ozon.ru/product/{sku})", a["offer"], _fmt(a["q"]), len(a["orders"])]
                 for sku, a in sorted(agg.items(), key=lambda kv: -kv[1]["q"])])]
    if "month" in groups:
        agg = {}
        for r in rows:
            k = r["_day"].strftime("%Y-%m") if r["_day"] else "без даты"
            agg[k] = agg.get(k, 0) + int(r["quantity"])
        out += ["", "### По месяцам (дата отгрузки)", _table(["Месяц", "Штук"], [[k, _fmt(v)] for k, v in sorted(agg.items())])]
    if "state" in groups:
        agg = {}
        for r in rows:
            agg[r["_state"]] = agg.get(r["_state"], 0) + int(r["quantity"])
        out += ["", "### По статусам", _table(["Статус", "Штук"], [[STATE_RU[s], _fmt(agg[s])] for s in SHIPPED if s in agg])]
    if "order" in groups:
        agg = {}
        for r in rows:
            a = agg.setdefault(r["order_id"], {"num": r.get("order_number") or r["order_id"], "day": r["_day"],
                                              "states": set(), "q": 0})
            a["q"] += int(r["quantity"])
            a["states"].add(STATE_RU[r["_state"]])
        out += ["", "### По заявкам", _table(["Заявка", "Отгрузка", "Статус", "Штук"],
                [[f"[{a['num']}](https://seller.ozon.ru/app/supply/orders/{oid})", a["day"] or "—",
                  ", ".join(sorted(a["states"])), _fmt(a["q"])]
                 for oid, a in sorted(agg.items(), key=lambda kv: str(kv[1]["day"]))])]
    if data.get("orders"):
        agg = packaging(rows, data["orders"])
        out += ["", "### По фасовке", _table(["Товар", "Фасовка", "Мест", "Штук", "Дни отгрузки"],
                [[k[0], k[1], a["places"], _fmt(a["units"]), ", ".join(sorted(a["days"]))[:200]]
                 for k, a in sorted(agg.items())])]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    a = ap.parse_args()
    data = json.loads(sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read())
    sys.stdout.write(report(data))


if __name__ == "__main__":
    main()
