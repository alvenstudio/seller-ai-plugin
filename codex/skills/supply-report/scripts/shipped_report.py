#!/usr/bin/env python3
"""Отчёт по отгрузкам FBO — Markdown над строками `supplies_items_list` (и, для фасовки, ответами
`supplies_orders_get`); ничего не вызывает.

    python3 shipped_report.py in.json > report.md
    вход: {"items": [строки supplies_items_list], "orders": [ответы supplies_orders_get] (необязательно),
           "period": {"from": "2026-01-01", "to": "2026-03-18"}, "date_basis": "timeslot" | "created",
           "group": ["product", "month", "state", "order", "cluster", "warehouse", "date_state"]}

Отгруженным считается товар поставок в статусах от «на точке отгрузки» и дальше; DATA_FILLING, READY_TO_SUPPLY,
CANCELLED, OVERDUE, REJECTED_AT_SUPPLY_WAREHOUSE не считаются. Месяц и день — по началу таймслота (`date_basis:
timeslot`, по умолчанию) или по дате создания заявки (`created`), время Москвы. `cluster` — кластер поставки,
`warehouse` — склад хранения, `date_state` — сводная «дата × статус».
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


def _day(row, basis="timeslot"):
    ts = row.get("created_at" if basis == "created" else "timeslot_from")
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(MSK).date()


def shipped_rows(items, period=None, basis="timeslot"):
    out = []
    for r in items:
        state = r.get("supply_state") or r.get("order_state")
        if state not in SHIPPED:
            continue
        d = _day(r, basis)
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
    """По отгруженной поставке и товару: штук этого товара в каждом месте, где он есть; фасовка N — все такие места
    по N. Место, где лежат и другие товары, — «в местах с другими товарами»."""
    per, unread, shared = {}, set(), set()
    for page in orders or []:
        for c in page.get("cargoes") or []:
            sid, items = str(c["supply_id"]), c.get("items")
            if not items:
                unread.add(sid)
                continue
            units = {}
            for i in items:
                units[str(i.get("sku"))] = units.get(str(i.get("sku")), 0) + int(i.get("quantity") or 0)
            for sku, n in units.items():
                per.setdefault((sid, sku), []).append(n)
                if len(units) > 1:
                    shared.add((sid, sku))
    agg = {}
    for r in rows:
        k = (str(r["supply_id"]), str(r["sku"]))
        places = per.get(k) or []
        if k[0] in unread:
            key = "состав мест не прочитан"
        elif not places:
            key = "места не прочитаны"
        elif k in shared:
            key = "в местах с другими товарами"
        else:
            key = f"по {places[0]} шт" if len(set(places)) == 1 else "смешанная"
        a = agg.setdefault((r.get("offer_id") or r["sku"], key), {"places": 0, "units": 0, "days": set()})
        a["places"] += len(places)
        a["units"] += int(r["quantity"])
        if r["_day"]:
            a["days"].add(r["_day"].isoformat())
    return agg


def _by(rows, key):
    agg = {}
    for r in rows:
        k = r.get(key) or "не известно"
        a = agg.setdefault(k, {"q": 0, "supplies": set()})
        a["q"] += int(r["quantity"])
        a["supplies"].add(r["supply_id"])
    return sorted(agg.items(), key=lambda kv: -kv[1]["q"])


def report(data):
    basis = data.get("date_basis") or "timeslot"
    if basis not in ("timeslot", "created"):
        raise ValueError("date_basis: timeslot или created")
    rows = shipped_rows(data.get("items") or [], data.get("period"), basis)
    groups = data.get("group") or ["product", "month", "state"]
    total = sum(int(r["quantity"]) for r in rows)
    what = "дата отгрузки" if basis == "timeslot" else "дата создания заявки"
    out = [f"**Отгружено: {_fmt(total)} шт** — {len({r['supply_id'] for r in rows})} поставок, "
           f"{len({r['order_id'] for r in rows})} заявок. Даты — {what}, время Москвы."]
    no_slot = [r for r in rows if r["_day"] is None]
    if no_slot:
        out.append(f"Без даты ({what}): {_fmt(sum(int(r['quantity']) for r in no_slot))} шт — в период не отфильтрованы.")
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
        out += ["", f"### По месяцам ({what})", _table(["Месяц", "Штук"], [[k, _fmt(v)] for k, v in sorted(agg.items())])]
    if "state" in groups:
        agg = {}
        for r in rows:
            agg[r["_state"]] = agg.get(r["_state"], 0) + int(r["quantity"])
        out += ["", "### По статусам", _table(["Статус", "Штук"], [[STATE_RU[s], _fmt(agg[s])] for s in SHIPPED if s in agg])]
    if "cluster" in groups:
        out += ["", "### По кластерам", _table(["Кластер", "Штук", "Поставок"],
                [[k, _fmt(a["q"]), len(a["supplies"])] for k, a in _by(rows, "cluster_name")])]
    if "warehouse" in groups:
        out += ["", "### По складам хранения", _table(["Склад", "Штук", "Поставок"],
                [[k, _fmt(a["q"]), len(a["supplies"])] for k, a in _by(rows, "storage_warehouse_name")])]
    if "date_state" in groups:
        states = [st for st in SHIPPED if any(r["_state"] == st for r in rows)]
        agg = {}
        for r in rows:
            k = r["_day"].isoformat() if r["_day"] else "без даты"
            agg.setdefault(k, {})[r["_state"]] = agg.get(k, {}).get(r["_state"], 0) + int(r["quantity"])
        out += ["", f"### {what.capitalize()} × статус", _table(["Дата"] + [STATE_RU[st] for st in states] + ["Всего"],
                [[k] + [_fmt(v.get(st, 0)) if v.get(st) else "—" for st in states] + [_fmt(sum(v.values()))]
                 for k, v in sorted(agg.items())])]
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
