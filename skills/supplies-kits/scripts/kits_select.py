#!/usr/bin/env python3
"""Точный объём из целых поставок готовых Super-заявок — чистый подбор над ответами `supplies_orders_get`.

    python3 kits_select.py rows    in.json  # {"sku", "orders": [ответы supplies_orders_get], "now": "…Z",
                                            #  "dropoff_ids": [точки отгрузки, откуда везут] | null} → кандидаты
    python3 kits_select.py select  in.json  # {"rows": [...], "target": 6000 | "targets": [6000, 3000],
                                            #  "allow_repeats": true, "exclude_supply_ids": [...], "node_limit": 300000}
    python3 kits_select.py by_need in.json  # {"rows": [...], "need": [{"cluster_id", "name", "need", "ads"}],
                                            #  "exclude_supply_ids": [...], "total": 1128 | null}
                                            #  → по поставке на нуждающийся кластер, не больше total

Атом — целая поставка (кластер) заявки: весь её товар и все её грузоместа. Приоритеты подбора: точная сумма →
больше начатых заявок, взятых до конца → больше штук из начатых заявок → больше разных кластеров → меньше новых
заявок → меньше поставок → при равенстве — поставки с более ранним слотом (без слота — последними). «Начатая»
заявка — та, часть поставок которой уже отгружена или уже взята в комплекты
(`exclude_supply_ids`), а часть ещё ждёт. Несколько объёмов (`targets`) — непересекающиеся комплекты: поставки
первого не входят во второй, а их заявки для второго — начатые.
"""
import argparse
import itertools
import json
import sys
from datetime import datetime, timezone

SHIPPED = {"ACCEPTED_AT_SUPPLY_WAREHOUSE", "IN_TRANSIT", "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
           "REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED", "COMPLETED"}


class KitError(ValueError):
    pass


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


_LAST = datetime.max.replace(tzinfo=timezone.utc)  # строка без слота — после всех слотов


def _slot_key(v):
    """Ключ сортировки по началу слота: без пояса — UTC (так его отдаёт страница заявки), без слота — в конец."""
    t = _ts(v)
    if t is None:
        return _LAST
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def rows_from_orders(order_pages, sku, now=None, dropoff_ids=None):
    """Кандидаты и причины отказа по каждой поставке: Super-заявка, поставка READY_TO_SUPPLY, один SKU, места заполнены,
    точка отгрузки — из `dropoff_ids` (откуда пользователь везёт), если они заданы."""
    sku, now_ts = str(sku), _ts(now) if now else None
    points = {str(x) for x in dropoff_ids} if dropoff_ids else None
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
        dropoff = o.get("dropoff") or {}
        for s in supplies:
            sid = str(s["supply_id"])
            base = {"order_id": oid, "order_number": o.get("order_number"), "supply_id": sid,
                    "cluster_id": str((s.get("cluster") or {}).get("id")), "cluster": (s.get("cluster") or {}).get("name")}
            why = None
            if (o.get("tags") or {}).get("super") is not True:
                why = "заявка без метки Super"
            elif points is not None and str(dropoff.get("id")) not in points:
                why = f"другая точка отгрузки: {dropoff.get('name') or dropoff.get('id')}"
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
                             timeslot_local=slot.get("local_from"), dropoff=dropoff.get("name"),
                             dropoff_id=None if dropoff.get("id") is None else str(dropoff["id"])))
    return {"rows": rows, "rejected": rejected}


def _free_rows(rows, exclude):
    """Свободные поставки; заявка, часть поставок которой уже взята в комплекты (exclude), — начатая."""
    excl = {str(x) for x in exclude}
    taken_orders = {str(r["order_id"]) for r in rows if str(r["supply_id"]) in excl}
    return [dict(r, started=bool(r.get("started")) or str(r["order_id"]) in taken_orders)
            for r in rows if str(r["supply_id"]) not in excl and int(r["quantity"]) > 0]


def select(rows, target, allow_repeats=True, exclude=(), node_limit=300000):
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise KitError("Нужно целое количество больше нуля")
    rows = _free_rows(rows, exclude)
    if not rows:
        raise KitError("Нет свободных подходящих поставок")
    # Ранний слот — первым (без слота — в конце): последний разрыв ничьей берёт поставки с меньшими номерами строк.
    rows.sort(key=lambda r: (not r.get("started"), _slot_key(r.get("timeslot_from")), str(r["order_id"]),
                             -int(r["quantity"]), str(r["supply_id"])))
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
        return (completed, units_started, clusters, -new_orders, -len(chosen), -sum(chosen))

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


def select_many(rows, targets, allow_repeats=True, exclude=(), node_limit=300000):
    """Несколько комплектов из непересекающихся поставок: по порядку пользователя, а если так не выходит —
    в другом порядке (первый удачный); поставки взятых комплектов исключаются из следующих."""
    if not targets or len(targets) > 6:
        raise KitError("Нужно от 1 до 6 объёмов")
    order, last = list(range(len(targets))), None
    for perm in itertools.permutations(order):
        taken, kits = list(exclude), {}
        try:
            for i in perm:
                kits[i] = select(rows, targets[i], allow_repeats, taken, node_limit)
                taken += [r["supply_id"] for r in kits[i]["supplies"]]
        except KitError as e:
            last = e
            continue
        return {"targets": list(targets), "order_used": [targets[i] for i in perm],
                "kits": [kits[i] for i in order], "proven": all(k["proven"] for k in kits.values())}
    raise KitError(f"Непересекающихся комплектов на {targets} нет ни в каком порядке: {last}")


def by_need(rows, need, exclude=(), total=None):
    """На каждый нуждающийся кластер — одна целая поставка: наименьшая не меньше потребности, иначе наибольшая
    (наименьший недобор); перебор — в днях запаса по ADS. Кластеры без поставок — остаток новой заявкой.

    `total` — общий объём, который назвал пользователь («ровно 1128»): поставка, которая вывела бы взятое за него, не
    берётся (кластер — в `over_total`, его потребность — новой заявкой), а `left_for_new_order` — ровно total − взятое.
    Без `total` каждый кластер берёт свою поставку целиком, и взятое вместе с остатком бывает больше потребности."""
    free = _free_rows(rows, exclude)
    used, out, missing, over_total = set(), [], [], []
    budget = None if total is None else int(total)
    taken = 0
    for n in need:
        cid = str(n.get("cluster_id") or n.get("id") or "")
        raw = n.get("need", n.get("quantity", n.get("recommended_supply")))
        want = int(raw or 0)
        if not cid or want <= 0:
            continue
        cand = [r for r in free if str(r["cluster_id"]) == cid and str(r["supply_id"]) not in used]
        if not cand:
            missing.append({"cluster_id": cid, "name": n.get("name"), "need": want})
            continue
        if budget is not None:
            cand = [r for r in cand if taken + int(r["quantity"]) <= budget]
            if not cand:
                over_total.append({"cluster_id": cid, "name": n.get("name"), "need": want})
                continue
        enough = [r for r in cand if int(r["quantity"]) >= want]
        pick = (min(enough, key=lambda r: (int(r["quantity"]), not r.get("started"), str(r["supply_id"]))) if enough
                else max(cand, key=lambda r: (int(r["quantity"]), r.get("started"), str(r["supply_id"]))))
        used.add(str(pick["supply_id"]))
        qty, ads = int(pick["quantity"]), float(n.get("ads") or 0)
        taken += qty
        out.append({"cluster_id": cid, "name": n.get("name") or pick.get("cluster"), "need": want, "supply": pick,
                    "quantity": qty, "over": max(0, qty - want), "short": max(0, want - qty),
                    "over_days": round((qty - want) / ads, 1) if ads > 0 and qty > want else None})
    result = {"clusters": out, "missing": missing, "taken_units": taken,
              "need_units": sum(c["need"] for c in out) + sum(m["need"] for m in missing + over_total),
              "left_for_new_order": sum(c["short"] for c in out) + sum(m["need"] for m in missing),
              "orders": sorted({c["supply"]["order_id"] for c in out})}
    if budget is not None:
        result.update({"total": budget, "over_total": over_total, "left_for_new_order": max(0, budget - taken)})
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["rows", "select", "by_need"])
    ap.add_argument("input")
    a = ap.parse_args()
    d = json.loads(sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read())
    try:
        if a.command == "rows":
            r = rows_from_orders(d["orders"], d["sku"], d.get("now"), d.get("dropoff_ids"))
        elif a.command == "by_need":
            r = by_need(d["rows"], d["need"], d.get("exclude_supply_ids") or (), d.get("total"))
        elif d.get("targets"):
            r = select_many(d["rows"], d["targets"], d.get("allow_repeats", True), d.get("exclude_supply_ids") or (),
                            int(d.get("node_limit", 300000)))
        else:
            r = select(d["rows"], d["target"], d.get("allow_repeats", True), d.get("exclude_supply_ids") or (),
                       int(d.get("node_limit", 300000)))
    except (KitError, KeyError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
