#!/usr/bin/env python3
"""Точный объём из целых поставок готовых Super-заявок — чистый подбор над ответами `supplies_orders_get`.

    python3 kits_select.py rows    in.json  # {"sku", "orders": [ответы supplies_orders_get], "now": "…Z",
                                            #  "dropoff_ids": [точки отгрузки, откуда везут] | null} → кандидаты
    python3 kits_select.py select  in.json  # {"rows": [...], "target": 6000 | "targets": [6000, 3000],
                                            #  "allow_repeats": true, "exclude_supply_ids": [...], "node_limit": 300000}
    python3 kits_select.py by_need in.json  # {"rows": [...], "need": [{"cluster_id", "name", "need", "ads"}],
                                            #  "exclude_supply_ids": [...], "total": 1128 | null, "node_limit": 1000000}
                                            #  → по поставке на нуждающийся кластер: больше покрыто (ровно total —
                                            #  лучше всего) → больше штук начатых → меньше заявок; остаток новой
                                            #  заявки — только в кластеры с продажами

Атом — целая поставка (кластер) заявки: весь её товар и все её грузоместа. Приоритеты подбора `select`: точная сумма →
больше начатых заявок, взятых до конца → больше штук из начатых заявок → больше разных кластеров → меньше новых
заявок → меньше поставок → при равенстве — поставки с более ранним слотом (без слота — последними). «Начатая»
заявка — та, часть поставок которой уже отгружена или уже взята в комплекты
(`exclude_supply_ids`), а часть ещё ждёт. Несколько объёмов (`targets`) — непересекающиеся комплекты: поставки
первого не входят во второй, а их заявки для второго — начатые. Приоритеты `by_need` — в его описании; перебор у
обоих один (`_search`).
"""
import argparse
import itertools
import json
import math
import sys
from collections import namedtuple
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
    """Кандидаты и причины отказа по каждой поставке: Super-заявка, поставка READY_TO_SUPPLY или DATA_FILLING (владелец
    06.11.2026: чего у заявки не хватает — слота, водителя и машины — заполняют и берут: `needs_timeslot`,
    `needs_vehicle`; null — водитель ещё не прочитан), один SKU, места заполнены, точка отгрузки — из `dropoff_ids`
    (откуда пользователь везёт), если они заданы."""
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
        vehicle = o.get("vehicle") or {}
        needs_vehicle = (None if vehicle.get("required") is None
                         else bool(vehicle["required"]) and not (vehicle.get("number") and vehicle.get("driver_name")))
        for s in supplies:
            sid = str(s["supply_id"])
            base = {"order_id": oid, "order_number": o.get("order_number"), "supply_id": sid,
                    "cluster_id": str((s.get("cluster") or {}).get("id")), "cluster": (s.get("cluster") or {}).get("name")}
            why = None
            if (o.get("tags") or {}).get("super") is not True:
                why = "заявка без метки Super"
            elif points is not None and str(dropoff.get("id")) not in points:
                why = f"другая точка отгрузки: {dropoff.get('name') or dropoff.get('id')}"
            elif s.get("state") not in ("READY_TO_SUPPLY", "DATA_FILLING"):
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
                             units_per_place=sizes, started=started, needs_timeslot=not slot.get("from"),
                             needs_vehicle=needs_vehicle,
                             timeslot_from=slot.get("from"),
                             timeslot_local=slot.get("local_from"), dropoff=dropoff.get("name"),
                             dropoff_id=None if dropoff.get("id") is None else str(dropoff["id"])))
    return {"rows": rows, "rejected": rejected}


def _free_rows(rows, exclude):
    """Свободные поставки; заявка, часть поставок которой уже взята в комплекты (exclude), — начатая."""
    excl = {str(x) for x in exclude}
    taken_orders = {str(r["order_id"]) for r in rows if str(r["supply_id"]) in excl}
    return [dict(r, started=bool(r.get("started")) or str(r["order_id"]) in taken_orders)
            for r in rows if str(r["supply_id"]) not in excl and int(r["quantity"]) > 0]


def _search(groups, score, target=None, prune=None, arrange=None, node_limit=300000):
    """Перебор с пределом шагов — общий для `select` и `by_need`.

    `groups` — по группе список вариантов (штук, элемент, ключ): из группы берётся не больше одного, два варианта с
    одним ключом (не None) вместе не берутся. `target` — точная сумма штук (недостижимые суммы отсекают битовые маски),
    None — набор любой суммы, лист — после последней группы. `score(chosen)` — кортеж, больше — лучше, при равенстве
    остаётся найденный первым. `prune(g, chosen, remaining, best_score)` → True отсекает ветку. `arrange(g, варианты,
    chosen, remaining, best_score)` — варианты группы по порядку, None среди них — «из группы ничего»; без `arrange` —
    все варианты, затем «ничего». → (лучший набор элементов или None, его оценка, шагов: 0 — `target` недостижим; больше
    `node_limit` — перебор оборван, оптимум не доказан)."""
    n = len(groups)
    reach = None
    if target is not None:
        mask = (1 << (target + 1)) - 1
        reach = [0] * (n + 1)
        reach[n] = 1
        for g in range(n - 1, -1, -1):  # суммы, достижимые группами g… (биты), чтобы не ходить в тупики
            r = reach[g + 1]
            for q, _, _ in groups[g]:
                r |= reach[g + 1] << q
            reach[g] = r & mask
        if not (reach[0] >> target) & 1:
            return None, None, 0
    best, best_score, nodes, chosen, used = None, None, 0, [], {}

    def dfs(g, remaining):
        nonlocal best, best_score, nodes
        nodes += 1
        if nodes > node_limit:
            return
        if (remaining == 0) if target is not None else (g == n):
            s = score(chosen)
            if best_score is None or s > best_score:
                best, best_score = list(chosen), s
            return
        if g == n or (reach is not None and not (reach[g] >> remaining) & 1):
            return
        if prune is not None and prune(g, chosen, remaining, best_score):
            return
        for opt in (groups[g] + [None]) if arrange is None else arrange(g, groups[g], chosen, remaining, best_score):
            if opt is None:
                dfs(g + 1, remaining)
                continue
            q, item, key = opt
            if (target is not None and q > remaining) or (key is not None and used.get(key)):
                continue
            chosen.append(item)
            if key is not None:
                used[key] = used.get(key, 0) + 1
            dfs(g + 1, remaining - q if target is not None else remaining)
            if key is not None:
                used[key] -= 1
            chosen.pop()

    sys.setrecursionlimit(max(1000, 4 * n + 100))
    dfs(0, 0 if target is None else target)
    return best, best_score, nodes


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
    n = len(rows)
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

    started_left = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        started_left[i] = started_left[i + 1] + (q[i] if rows[i].get("started") else 0)

    def prune(i, chosen, remaining, best_score):  # все начатые взяты до конца — штук начатых больше не набрать
        if best_score is None or best_score[0] != len(started_orders):
            return False
        done = sum(q[k] for k in chosen if rows[k].get("started"))
        return done + min(remaining, started_left[i]) < best_score[1]

    # Группа — одна поставка; без повторов ключ — кластер.
    groups = [[(q[i], i, None if allow_repeats else rows[i]["cluster_id"])] for i in range(n)]
    best, _, nodes = _search(groups, score, target, prune, node_limit=node_limit)
    if not nodes:
        raise KitError(f"Точной комбинации целых поставок на {target} шт нет")
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


# Вариант поставки в кластере для `by_need`: q — штук; st — штук начатой заявки (q или 0); order — заявка; units —
# перебор сверх потребности в кластере без продаж (штук); days — перебор в днях запаса по ADS, ×10⁶ (целые — суммы
# точные); cov — покрытая потребность кластера; gi — номер кластера в переборе; row — строка-кандидат.
_Opt = namedtuple("_Opt", "q st order units days cov gi row")


def by_need(rows, need, exclude=(), total=None, node_limit=1000000):
    """По одной целой поставке на нуждающийся кластер; что не покрыто готовыми поставками — остаток новой заявкой.

    Владелец, 08.11.2026: «Давай приоритетно брать такие поставки, чтобы потребность максимально покрыть. Лучше будет
    если суперами покроем потребность, чем новые заявки подавать. Новые заявки хуже того если мы не будем брать
    сгорающие поставки.» Поэтому подборы сравниваются по порядку:
    1. больше покрыто готовыми поставками — меньше `left_for_new_order`: с `total` взятое не больше него и ровно
       `total` — лучше всего (`exact`); без `total` — в каждом кластере больше покрытой потребности;
    2. затем больше штук начатых заявок: слот частично отгруженной заявки не перенести, её невзятые поставки сгорят;
    3. затем меньше заявок (слотов) — лучше одна; затем меньше перебора сверх потребности (сначала штуки в кластерах
       без продаж, затем дни запаса по ADS); затем более ранние слоты.
    Перебор — общий с `select` (`_search`): точная сумма по битовым маскам, границы по каждому критерию, предел
    `node_limit` шагов. Покрытие и штуки начатых считаются точно до перебора, и любой найденный подбор их держит;
    оборван перебор — `proven: false`: не доказано лишь, что меньше заявок, перебора и раньше слоты не найти.

    `total` — общий объём, который назвал пользователь («ровно 1128»): взятое + `left_for_new_order` = total, кластер
    с поставками, не вошедшими в подбор, — в `over_total`. Без `total` каждый кластер берёт поставку целиком, и взятое
    бывает больше потребности. Остаток новой заявки (`new_order`) — только в кластеры потребности с продажами
    (`ads` > 0): по непокрытой потребности, больший — первым; излишек сверх неё при `total` — в кластер с наибольшим
    ADS. Непокрытое там, где продаж нет, — `no_sales`, новой заявке не отдаётся. `ads` не передан ни одному кластеру —
    продаж не проверить: `sales_checked: false`, остаток — по непокрытой потребности всех кластеров.
    `started_leftovers_not_taken` — невзятые поставки начатых заявок (назвать пользователю)."""
    free = _free_rows(rows, exclude)
    budget = None if total is None else int(total)
    if budget is not None and budget < 0:
        raise KitError("total — целое число не меньше нуля")
    wants, seen = [], {}
    for idx, n in enumerate(need):
        cid = str(n.get("cluster_id") or n.get("id") or "")
        raw = n.get("need", n.get("quantity", n.get("recommended_supply")))
        want = int(raw or 0)
        if not cid or want <= 0:
            continue
        if cid in seen:  # кластер назван дважды — одна потребность
            seen[cid]["need"] += want
            continue
        ads = n.get("ads")
        seen[cid] = {"idx": idx, "cluster_id": cid, "name": n.get("name"), "need": want,
                     "ads": None if ads is None else float(ads)}
        wants.append(seen[cid])
    cand = {}
    for r in free:
        cand.setdefault(str(r["cluster_id"]), []).append(r)
    groups = [w for w in wants if cand.get(w["cluster_id"])]
    slot_rank = {k: i for i, k in enumerate(sorted({_slot_key(r.get("timeslot_from")) for r in free}))}
    order_rank, spread = {}, {}  # место слота заявки (раньше — меньше); в скольких кластерах у заявки есть поставка
    for w in groups:
        for r in cand[w["cluster_id"]]:
            o = str(r["order_id"])
            order_rank[o] = min(order_rank.get(o, len(slot_rank)), slot_rank[_slot_key(r.get("timeslot_from"))])
        for o in {str(r["order_id"]) for r in cand[w["cluster_id"]]}:
            spread[o] = spread.get(o, 0) + 1
    opts = []
    for gi, w in enumerate(groups):
        sells = (w["ads"] or 0) > 0
        o = []
        for r in cand[w["cluster_id"]]:
            q = int(r["quantity"])
            over = max(0, q - w["need"])
            o.append(_Opt(q, q if r.get("started") else 0, str(r["order_id"]), 0 if sells else over,
                          round(over * 1e6 / w["ads"]) if sells else 0, min(q, w["need"]), gi, r))
        if budget is None:  # без total кластеры не связаны: в каждом — больше покрытия, затем больше штук начатых
            top = max(x.cov for x in o)
            o = [x for x in o if x.cov == top]
            top = max(x.st for x in o)
            o = [x for x in o if x.st == top]
        o.sort(key=lambda x: (-x.st, -spread[x.order], x.units, x.days, order_rank[x.order], x.order,
                              str(x.row["supply_id"])))
        opts.append(o)
    # Перебор — с кластеров, где выбрать заявку не из чего (меньше разных заявок): раньше видно, сколько их нужно.
    seq = sorted(range(len(groups)), key=lambda i: (len({x.order for x in opts[i]}), groups[i]["idx"]))
    groups, opts = [groups[i] for i in seq], [[x._replace(gi=g) for x in opts[i]] for g, i in enumerate(seq)]
    n = len(groups)

    def ranks(orders):
        return sum(order_rank[o] for o in orders)

    def score(chosen):
        orders = {x.order for x in chosen}
        return (sum(x.cov if budget is None else x.q for x in chosen), sum(x.st for x in chosen), -len(orders),
                -sum(x.units for x in chosen), -sum(x.days for x in chosen), -ranks(orders))

    def split(options, chosen, best_score):  # сначала поставки уже взятых заявок; новые — пока заявок меньше лучшего
        orders = {x.order for x in chosen}
        cap = None if best_score is None else -best_score[2]
        same = [t for t in options if t[1].order in orders]
        other = [] if cap is not None and len(orders) >= cap else [t for t in options if t[1].order not in orders]
        return same, other

    if budget is not None:
        reach, mask = 1, (1 << (budget + 1)) - 1
        for o in opts:
            r = reach
            for q in {x.q for x in o}:
                r |= reach << q
            reach = r & mask
        target, step = reach.bit_length() - 1, 0  # больше всего штук, не больше total
        for o in opts:
            for x in o:
                step = math.gcd(step, x.q)
        step, mask = step or 1, (1 << (target + 1)) - 1
        top = target // step
        # dp[g][s] — по кластерам g… ровно s·step штук: (больше штук начатых, меньше перебора без продаж, меньше дней
        # перебора), каждое — отдельная граница; None — такой суммы не набрать.
        dp = [None] * (n + 1)
        dp[n] = [(0, 0, 0)] + [None] * top
        for g in range(n - 1, -1, -1):
            nxt, cur = dp[g + 1], list(dp[g + 1])
            for x in opts[g]:
                k = x.q // step
                for s in range(k, top + 1):
                    b = nxt[s - k]
                    if b is not None:
                        c, v = cur[s], (b[0] + x.st, b[1] + x.units, b[2] + x.days)
                        cur[s] = v if c is None else (max(c[0], v[0]), min(c[1], v[1]), min(c[2], v[2]))
            dp[g] = cur
        best_started = dp[0][top][0]
        sizes = [{} for _ in range(n)]  # кластер → заявка → штук в её поставках
        for g, o in enumerate(opts):
            for x in o:
                sizes[g].setdefault(x.order, []).append(x.q)
        memo = {}  # alone() в пределах этого подбора

        def alone(g, orders):  # суммы кластеров g…, которые набирают поставки только этих заявок (по одной на кластер)
            r = memo.get((g, orders))
            if r is None:
                r = 1
                if g < n:
                    nxt = r = alone(g + 1, orders)
                    for o in orders:
                        for q in sizes[g].get(o, ()):
                            r |= nxt << q
                    r &= mask
                if len(memo) > 50000:
                    memo.clear()
                memo[(g, orders)] = r
            return r

        def completes(g, orders, remaining):  # взятые заявки сами наберут остаток
            return (alone(g, frozenset(orders)) >> remaining) & 1

        def fits(g, st, remaining):  # ещё можно набрать наибольшую сумму и наибольшее число штук начатых
            b = dp[g][remaining // step]
            return b is not None and st + b[0] >= best_started

        def arrange(g, options, chosen, remaining, best_score):
            st = sum(x.st for x in chosen)
            same, other = split([t for t in options if t[0] <= remaining
                                 and fits(g + 1, st + t[1].st, remaining - t[0])], chosen, best_score)
            if other:  # новая заявка, с которой взятые наберут остаток, — первой
                orders = {x.order for x in chosen}
                other.sort(key=lambda t: not completes(g + 1, orders | {t[1].order}, remaining - t[0]))
            skip = [None] if fits(g + 1, st, remaining) else []
            return same + skip + other if chosen else other + skip

        def prune(g, chosen, remaining, best_score):
            b = dp[g][remaining // step]
            if b is None:
                return True
            if best_score is None:
                return False
            orders = {x.order for x in chosen}
            k = len(orders)  # заявок будет не меньше; ещё одна, если взятые заявки остаток не наберут
            if k + 1 >= -best_score[2] and remaining and not completes(g, orders, remaining):
                k += 1
            return (target, best_started, -k, -sum(x.units for x in chosen) - b[1],
                    -sum(x.days for x in chosen) - b[2], -ranks(orders)) <= best_score
    else:
        target = None
        low = [(0, 0)] * (n + 1)  # наименьший перебор кластеров g…: (штук без продаж, дней)
        for g in range(n - 1, -1, -1):
            low[g] = (low[g + 1][0] + min(x.units for x in opts[g]), low[g + 1][1] + min(x.days for x in opts[g]))
        group_orders = [{x.order for x in o} for o in opts]
        order_groups = {}
        for g, orders in enumerate(group_orders):
            for o in orders:
                order_groups.setdefault(o, set()).add(g)
        best_cov, best_started = sum(o[0].cov for o in opts), sum(o[0].st for o in opts)

        def uncovered(g, orders):  # кластеры g…, где у взятых заявок нет поставки
            return {h for h in range(g, n) if not group_orders[h] & orders}

        def arrange(g, options, chosen, remaining, best_score):  # каждый кластер с поставками берёт одну
            same, other = split(options, chosen, best_score)
            if other:  # новая заявка, что закроет больше оставшихся кластеров, — первой
                rest = uncovered(g + 1, {x.order for x in chosen})
                other.sort(key=lambda t: -len(order_groups[t[1].order] & rest))
            return same + other

        def prune(g, chosen, remaining, best_score):
            if best_score is None:
                return False
            orders = {x.order for x in chosen}
            k = len(orders)  # где взятых заявок нет, нужны ещё: не меньше «кластеров ÷ наибольшее, что закроет одна»
            rest = uncovered(g, orders)
            if rest:  # кластеры без общих заявок — каждому своя
                apart, seen_orders = 0, set()
                for h in sorted(rest, key=lambda h: (len(group_orders[h]), h)):
                    if not group_orders[h] & seen_orders:
                        apart, seen_orders = apart + 1, seen_orders | group_orders[h]
                most = max(len(order_groups[o] & rest) for h in rest for o in group_orders[h])
                k += max(apart, -(-len(rest) // most))
            return (best_cov, best_started, -k, -sum(x.units for x in chosen) - low[g][0],
                    -sum(x.days for x in chosen) - low[g][1], -ranks(orders)) <= best_score

    best, _, nodes = _search([[(x.q, x, None) for x in o] for o in opts], score, target, prune, arrange, node_limit)
    if best is None:
        raise KitError(f"Подбор по потребности не уложился в {node_limit} шагов")
    pick = {x.gi: x for x in best}
    got = {groups[x.gi]["cluster_id"]: x.q for x in best}
    out, over_total = [], []
    for gi in sorted(range(n), key=lambda g: groups[g]["idx"]):
        w, x = groups[gi], pick.get(gi)
        if x is None:
            over_total.append({"cluster_id": w["cluster_id"], "name": w["name"], "need": w["need"]})
            continue
        ads = w["ads"] or 0
        out.append({"cluster_id": w["cluster_id"], "name": w["name"] or x.row.get("cluster"), "need": w["need"],
                    "supply": x.row, "quantity": x.q, "over": max(0, x.q - w["need"]),
                    "short": max(0, w["need"] - x.q),
                    "over_days": round((x.q - w["need"]) / ads, 1) if ads > 0 and x.q > w["need"] else None})
    missing = [{"cluster_id": w["cluster_id"], "name": w["name"], "need": w["need"]}
               for w in wants if not cand.get(w["cluster_id"])]
    taken = sum(x.q for x in best)
    # Остаток новой заявки — только туда, где товар продаётся (без данных о продажах — везде).
    need_left = {w["cluster_id"]: max(0, w["need"] - got.get(w["cluster_id"], 0)) for w in wants}
    sales_checked = any(w["ads"] is not None for w in wants)
    sells = [w for w in wants if not sales_checked or (w["ads"] or 0) > 0]
    selling = {w["cluster_id"] for w in sells}
    tail = {}
    if budget is None:
        tail = {w["cluster_id"]: need_left[w["cluster_id"]] for w in sells if need_left[w["cluster_id"]]}
        left = sum(tail.values())
    else:
        left = rest = budget - taken
        for w in sorted((w for w in sells if need_left[w["cluster_id"]]),
                        key=lambda w: (-need_left[w["cluster_id"]], -(w["ads"] or 0), w["idx"])):
            if not rest:
                break
            tail[w["cluster_id"]] = min(rest, need_left[w["cluster_id"]])
            rest -= tail[w["cluster_id"]]
        if rest and sells:  # продающая потребность покрыта, а штуки остались — туда, где продаж больше всего
            top_w = max(sells, key=lambda w: ((w["ads"] or 0), -w["idx"]))
            tail[top_w["cluster_id"]] = tail.get(top_w["cluster_id"], 0) + rest
    new_order = [{"cluster_id": w["cluster_id"],
                  "name": w["name"] or (cand.get(w["cluster_id"]) or [{}])[0].get("cluster"),
                  "quantity": tail[w["cluster_id"]], "ads": w["ads"]} for w in wants if w["cluster_id"] in tail]
    no_sales = [{"cluster_id": w["cluster_id"], "name": w["name"], "need_left": need_left[w["cluster_id"]],
                 "ads": w["ads"]} for w in wants if w["cluster_id"] not in selling and need_left[w["cluster_id"]]]
    used = {str(x.row["supply_id"]) for x in best}
    leftovers = sorted((r for r in free if r.get("started") and str(r["supply_id"]) not in used),
                       key=lambda r: (_slot_key(r.get("timeslot_from")), str(r["order_id"]), str(r["supply_id"])))
    result = {"clusters": out, "missing": missing, "taken_units": taken,
              "need_units": sum(w["need"] for w in wants), "left_for_new_order": left,
              "new_order": new_order, "no_sales": no_sales, "sales_checked": sales_checked,
              "from_started_orders": sum(x.st for x in best), "started_leftovers_not_taken": leftovers,
              "orders": sorted({x.row["order_id"] for x in best}),
              "slots": sorted({x.row.get("timeslot_from") for x in best}, key=lambda v: (_slot_key(v), str(v))),
              "proven": nodes <= node_limit}
    if budget is not None:
        result.update({"total": budget, "over_total": over_total, "exact": taken == budget})
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
            r = by_need(d["rows"], d["need"], d.get("exclude_supply_ids") or (), d.get("total"),
                        int(d.get("node_limit", 1000000)))
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
