#!/usr/bin/env python3
"""Точный объём из целых поставок готовых Super-заявок — чистый подбор над ответами `supplies_orders_get`.

    python3 kits_select.py rows   in.json   # {"sku", "orders": [ответы supplies_orders_get], "now": "…Z"} → кандидаты
    python3 kits_select.py select in.json   # {"rows": [...], "target": 6000, "allow_repeats": true,
                                            #  "exclude_supply_ids": [...], "node_limit": 300000} → выбор

Атом — целая поставка (кластер) заявки: весь её товар и все её грузоместа. Приоритеты подбора: точная сумма →
больше начатых заявок, взятых до конца → больше штук из начатых заявок → больше разных кластеров → меньше новых
заявок → меньше поставок. «Начатая» заявка — та, часть поставок которой уже отгружена, а часть ещё ждёт.
"""
import argparse
import json
import sys
from datetime import datetime

SHIPPED = {"ACCEPTED_AT_SUPPLY_WAREHOUSE", "IN_TRANSIT", "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
           "REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED", "COMPLETED"}


class KitError(ValueError):
    pass


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def rows_from_orders(order_pages, sku, now=None):
    """Кандидаты и причины отказа по каждой поставке: Super-заявка, поставка READY_TO_SUPPLY, один SKU, места заполнены."""
    sku, now_ts = str(sku), _ts(now) if now else None
    rows, rejected = [], []
    for page in order_pages:
        o = page.get("order") or {}
        oid = str(o.get("order_id"))
        comp = {}
        for item in page.get("items") or []:
            for s in item.get("supplies") or []:
                comp.setdefault(str(s["supply_id"]), {})[str(item["sku"])] = int(s.get("quantity") or 0)
        cargo = {}
        for c in page.get("cargoes") or []:
            cargo.setdefault(str(c["supply_id"]), []).append(c)
        supplies = page.get("supplies") or []
        started = any(s.get("state") in SHIPPED for s in supplies)
        slot = o.get("timeslot") or {}
        for s in supplies:
            sid = str(s["supply_id"])
            base = {"order_id": oid, "order_number": o.get("order_number"), "supply_id": sid,
                    "cluster_id": str((s.get("cluster") or {}).get("id")), "cluster": (s.get("cluster") or {}).get("name")}
            why = None
            if (o.get("tags") or {}).get("super") is not True:
                why = "заявка без метки Super"
            elif s.get("state") != "READY_TO_SUPPLY":
                why = f"поставка {s.get('state')}"
            elif set(comp.get(sid, {})) != {sku}:
                why = "в поставке другой или смешанный товар" if comp.get(sid) else "состав не прочитан"
            elif not cargo.get(sid):
                why = "нет грузомест"
            elif any(not c.get("items") for c in cargo[sid]):
                why = "состав грузомест не прочитан"
            elif sum(int(i.get("quantity") or 0) for c in cargo[sid] for i in c["items"]) != comp[sid][sku]:
                why = "грузоместа не сходятся с количеством"
            elif slot.get("to") and now_ts and _ts(slot["to"]) < now_ts:
                why = "таймслот заявки прошёл"
            if why:
                rejected.append(dict(base, reason=why))
                continue
            sizes = sorted({sum(int(i["quantity"]) for i in c["items"]) for c in cargo[sid]})
            rows.append(dict(base, quantity=comp[sid][sku], cargo_ids=sorted(str(c["cargo_id"]) for c in cargo[sid]),
                             units_per_place=sizes, started=started, timeslot_from=slot.get("from"),
                             timeslot_local=slot.get("local_from"), dropoff=(o.get("dropoff") or {}).get("name")))
    return {"rows": rows, "rejected": rejected}


def select(rows, target, allow_repeats=True, exclude=(), node_limit=300000):
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise KitError("Нужно целое количество больше нуля")
    excl = {str(x) for x in exclude}
    rows = [r for r in rows if str(r["supply_id"]) not in excl and int(r["quantity"]) > 0]
    if not rows:
        raise KitError("Нет свободных подходящих поставок")
    rows.sort(key=lambda r: (not r.get("started"), str(r["order_id"]), -int(r["quantity"]), str(r["supply_id"])))
    q = [int(r["quantity"]) for r in rows]
    n, mask = len(rows), (1 << (target + 1)) - 1
    reach = [0] * (n + 1)
    reach[n] = 1
    for i in range(n - 1, -1, -1):  # суммы, достижимые поставками i… (биты), чтобы не ходить в тупики
        reach[i] = (reach[i + 1] | (reach[i + 1] << q[i])) & mask
    if not (reach[0] >> target) & 1:
        raise KitError(f"Точной комбинации целых поставок на {target} шт нет")
    started_orders = {}
    for i, r in enumerate(rows):
        if r.get("started"):
            started_orders.setdefault(str(r["order_id"]), []).append(i)

    def score(chosen):
        ids = set(chosen)
        completed = sum(1 for ix in started_orders.values() if all(i in ids for i in ix))
        units_started = sum(q[i] for i in chosen if rows[i].get("started"))
        clusters = len({rows[i]["cluster_id"] for i in chosen})
        new_orders = len({rows[i]["order_id"] for i in chosen if not rows[i].get("started")})
        return (completed, units_started, clusters, -new_orders, -len(chosen))

    best, best_score, nodes, chosen, used_clusters = None, None, 0, [], {}
    started_left = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        started_left[i] = started_left[i + 1] + (q[i] if rows[i].get("started") else 0)

    def dfs(i, remaining):
        nonlocal best, best_score, nodes
        nodes += 1
        if nodes > node_limit:
            return
        if remaining == 0:
            s = score(chosen)
            if best_score is None or s > best_score:
                best, best_score = list(chosen), s
            return
        if i == n or not (reach[i] >> remaining) & 1:
            return
        if best_score is not None and best_score[0] == len(started_orders):
            done = sum(q[k] for k in chosen if rows[k].get("started"))
            if done + min(remaining, started_left[i]) < best_score[1]:
                return
        c = rows[i]["cluster_id"]
        if q[i] <= remaining and (allow_repeats or not used_clusters.get(c)):
            chosen.append(i)
            used_clusters[c] = used_clusters.get(c, 0) + 1
            dfs(i + 1, remaining - q[i])
            used_clusters[c] -= 1
            chosen.pop()
        dfs(i + 1, remaining)

    sys.setrecursionlimit(max(1000, 4 * n + 100))
    dfs(0, target)
    if best is None:
        raise KitError(f"Точной комбинации на {target} шт без повторов кластеров нет" if not allow_repeats
                       else f"Точной комбинации на {target} шт не найдено за {node_limit} шагов")
    picked = [rows[i] for i in best]
    assert sum(int(r["quantity"]) for r in picked) == target
    from_started = sum(int(r["quantity"]) for r in picked if r.get("started"))
    leftovers = [r for r in rows if r.get("started") and r not in picked]
    return {"target": target, "proven": nodes <= node_limit, "supplies": picked,
            "from_started_orders": from_started, "from_other_orders": target - from_started,
            "clusters": len({r["cluster_id"] for r in picked}),
            "repeated_clusters": len(picked) - len({r["cluster_id"] for r in picked}),
            "orders": sorted({r["order_id"] for r in picked}),
            "started_leftovers_not_taken": leftovers}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["rows", "select"])
    ap.add_argument("input")
    a = ap.parse_args()
    d = json.loads(sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read())
    try:
        if a.command == "rows":
            r = rows_from_orders(d["orders"], d["sku"], d.get("now"))
        else:
            r = select(d["rows"], d["target"], d.get("allow_repeats", True), d.get("exclude_supply_ids") or (),
                       int(d.get("node_limit", 300000)))
    except (KitError, KeyError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
