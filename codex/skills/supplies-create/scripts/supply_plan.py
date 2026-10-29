#!/usr/bin/env python3
"""Распределение объёма поставки FBO по кластерам — чистый расчёт над JSON из инструментов Seller AI.

Ничего не вызывает: вход — то, что агент прочитал `supplies_planning_availability`, `supplies_items_list` и
`supplies_orders_list`, выход — план по кластерам и его разбиение на заявки по лимиту коробок.

    python3 supply_plan.py plan      plan_input.json      # распределение
    python3 supply_plan.py pending   pending_input.json   # что уже отгружено / подано по кластерам
    python3 supply_plan.py intersect slots.json           # общие слоты нескольких черновиков или заявок (одна дата)
    python3 supply_plan.py plan -                          # вход из stdin

Вход `plan`:
    {"total": 4692, "box_size": 12, "method": "auto|idc|market_share|mixed", "horizon_days": 28,
     "min_boxes": 1, "box_limit": 30,
     "clusters": [{"id": "4039", "name": "Казань", "ads": 3.2, "available": 40, "pending": 12, "travel_days": 2}],
     "shares": {"Казань": 4.51},            # необязательно; по умолчанию MARKET_SHARE_PCT по имени
     "fixed": {"4039": 390},                # необязательно: закреплено за кластером (палета целиком), вне раскладки
     "origin": {"latitude": 55.7, "longitude": 37.6}}   # необязательно: координаты точки отгрузки → дни в пути
    Дни в пути (travel_days кластера или оценка по origin: расстояние до склада кластера × 1,3 / 700 км в сутки, не
    меньше суток) уменьшают остаток к приезду поставки: остаток − ADS × дни в пути.
Вход `intersect`: {"answers": [ответы supplies_drafts_timeslots_list черновиков (`days[].slots[]`) или
    supplies_orders_timeslots_list заявок (`slots[]`) одной точки]}; выход — `common[]` со строками каждого ответа
    (`per_answer`, прежнее имя `per_draft`)
Вход `pending`:
    {"mode": "shipped|all", "items": [строки supplies_items_list]}
    кластер — из строки (`cluster_id`, `cluster_name`); `orders` (строки supplies_orders_list) — необязательно, для строк
    без кластера
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone

# Доли рынка Ozon по кластерам доставки, % заказанных штук FBO. Пересчитано 21.08.2026 по фактическим заказам FBO
# за 28 дней (24.07–21.08.2026) нескольких магазинов (больше 100 тыс. шт), отменённые исключены. Армении и Узбекистана нет.
MARKET_SHARE_PCT = {
    "Москва, МО и Дальние регионы": 22.77,
    "Санкт-Петербург и СЗО": 12.0,
    "Ростов": 6.25,
    "Красноярск": 5.49,
    "Новосибирск": 5.1,
    "Казань": 4.51,
    "Дальний Восток": 4.23,
    "Екатеринбург": 4.2,
    "Ярославль": 3.87,
    "Уфа": 3.46,
    "Воронеж": 3.46,
    "Краснодар": 3.07,
    "Самара": 2.99,
    "Саратов": 2.97,
    "Тюмень": 2.95,
    "Невинномысск": 2.38,
    "Омск": 2.07,
    "Пермь": 1.77,
    "Оренбург": 1.56,
    "Махачкала": 1.48,
    "Тверь": 1.21,
    "Калининград": 0.96,
    "Астана": 0.64,
    "Беларусь": 0.33,
    "Алматы": 0.28,
}

# Основной склад кластера (широта, долгота) — для оценки дней в пути от точки отгрузки. Москва — между Хоругвино и
# Пушкино; Армения и Узбекистан — по столицам.
CLUSTER_COORDS = {
    "Москва, МО и Дальние регионы": (56.05, 37.47), "Санкт-Петербург и СЗО": (59.80, 30.40), "Ростов": (47.24, 39.71),
    "Красноярск": (56.01, 92.87), "Новосибирск": (55.03, 82.92), "Казань": (55.80, 49.11),
    "Дальний Восток": (48.48, 135.07), "Екатеринбург": (56.84, 60.60), "Ярославль": (57.63, 39.87), "Уфа": (54.74, 55.96),
    "Воронеж": (51.67, 39.19), "Краснодар": (44.88, 39.19), "Самара": (53.20, 50.10), "Саратов": (51.53, 46.03),
    "Тюмень": (57.15, 65.53), "Невинномысск": (44.63, 41.94), "Омск": (54.99, 73.37), "Пермь": (58.01, 56.23),
    "Оренбург": (51.77, 55.10), "Махачкала": (42.98, 47.50), "Тверь": (56.86, 35.91), "Калининград": (54.71, 20.51),
    "Астана": (51.17, 71.45), "Беларусь": (53.90, 27.57), "Алматы": (43.24, 76.95), "Армения": (40.18, 44.51),
    "Узбекистан": (41.31, 69.28),
}
ROAD_FACTOR, KM_PER_DAY = 1.3, 700

SHIPPED = {"ACCEPTED_AT_SUPPLY_WAREHOUSE": "at_dropoff", "IN_TRANSIT": "in_transit",
           "ACCEPTANCE_AT_STORAGE_WAREHOUSE": "at_warehouse"}
SUBMITTED = {"DATA_FILLING", "READY_TO_SUPPLY"}


class PlanError(ValueError):
    pass


def round_variants(total, box):
    """Две кратные коробке суммы вокруг total: вниз и вверх."""
    down = total // box * box
    return {"down": {"boxes": down // box, "units": down, "diff": down - total},
            "up": {"boxes": -(-total // box), "units": -(-total // box) * box, "diff": -(-total // box) * box - total}}


def _num(value, name):
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise PlanError(f"{name}: нужно неотрицательное число")
    return float(value)


def _clusters(raw):
    if not raw:
        raise PlanError("Нужен хотя бы один кластер")
    out, seen = [], set()
    for c in raw:
        cid = str(c.get("id") or c.get("name") or "")
        if not cid or cid in seen:
            raise PlanError(f"Кластер без id или повтор: {c}")
        seen.add(cid)
        out.append({"id": cid, "name": c.get("name") or cid, "ads": _num(c.get("ads"), "ads"),
                    "stock": _num(c.get("available"), "available") + _num(c.get("pending"), "pending"),
                    "ads_known": c.get("ads") is not None,
                    "travel": None if c.get("travel_days") is None else _num(c.get("travel_days"), "travel_days")})
    return out


def travel_days(origin, name):
    """Дни в пути от точки отгрузки до склада кластера: по прямой × 1,3 на дороги, 700 км в сутки, не меньше суток."""
    to = CLUSTER_COORDS.get(name)
    if not origin or not to:
        return None
    lat = origin.get("latitude", origin.get("lat"))
    lon = origin.get("longitude", origin.get("lon"))
    if lat is None or lon is None:
        return None
    p1, p2 = math.radians(float(lat)), math.radians(to[0])
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(to[1] - float(lon)) / 2) ** 2)
    km = 6371 * 2 * math.asin(math.sqrt(a))
    return max(1, math.ceil(km * ROAD_FACTOR / KM_PER_DAY))


def _apply_travel(clusters, origin, warnings):
    """Остаток к приезду поставки: товар продаётся, пока она едет."""
    for c in clusters:
        days = c["travel"] if c["travel"] is not None else travel_days(origin, c["name"])
        c["travel"] = days
        c["stock_now"] = c["stock"]
        if days:
            c["stock"] = max(0.0, c["stock"] - c["ads"] * days)
        elif origin:
            warnings.append(f"{c['name']}: координат склада нет — дни в пути не учтены")


def _share(c, shares):
    for key in (c["id"], c["name"]):
        if key in shares:
            return float(shares[key])
    return 0.0


def _days(stock, ads):
    return round(stock / ads, 1) if ads > 0 else None


def _largest_remainder(weights, boxes, min_boxes):
    """boxes коробок по весам, минимум min_boxes каждому; сумма точная."""
    n = len(weights)
    if boxes < n * min_boxes:
        raise PlanError(f"Коробок {boxes} меньше, чем нужно на минимум по {min_boxes} в {n} кластеров")
    total_w = sum(weights)
    quota = [boxes * w / total_w if total_w > 0 else boxes / n for w in weights]
    alloc = [min_boxes] * n
    for _ in range(boxes - n * min_boxes):
        i = max(range(n), key=lambda k: (quota[k] - alloc[k], weights[k], -k))
        alloc[i] += 1
    return alloc


def market_share_boxes(clusters, boxes, min_boxes, shares, warnings):
    weights = []
    for c in clusters:
        s = _share(c, shares)
        if s <= 0:
            warnings.append(f"{c['name']}: доли рынка нет в таблице — получит минимум; объём задайте сами")
        weights.append(s)
    if not any(weights):
        raise PlanError("Ни у одного кластера нет доли рынка")
    return _largest_remainder(weights, boxes, min_boxes)


def idc_boxes(clusters, boxes, min_boxes, box):
    """Равный запас в днях: T такое, что Σ max(min, ⌊(T·ads − stock)/box⌋) = boxes, остаток — самым нуждающимся."""
    def need(c, t):
        if c["ads"] <= 0:
            return min_boxes
        return max(min_boxes, math.floor((t * c["ads"] - c["stock"]) / box))

    if boxes < len(clusters) * min_boxes:
        raise PlanError(f"Коробок {boxes} меньше, чем нужно на минимум по {min_boxes} в {len(clusters)} кластеров")
    lo, hi = 0.0, 1.0
    while sum(need(c, hi) for c in clusters) < boxes and hi < 1e6:
        hi *= 2
    for _ in range(100):
        mid = (lo + hi) / 2
        if sum(need(c, mid) for c in clusters) < boxes:
            lo = mid
        else:
            hi = mid
    alloc = [need(c, lo) for c in clusters]

    def after(i):
        c = clusters[i]
        return (c["stock"] + alloc[i] * box) / c["ads"] if c["ads"] > 0 else math.inf

    while sum(alloc) < boxes:  # по коробке туда, где запас после поставки меньше всего
        i = min(range(len(clusters)), key=lambda k: (after(k), -clusters[k]["ads"], k))
        alloc[i] += 1
    while sum(alloc) > boxes:
        cand = [k for k in range(len(clusters)) if alloc[k] > min_boxes]
        i = max(cand, key=lambda k: (after(k) if after(k) != math.inf else 1e18, k))
        alloc[i] -= 1
    return alloc


def split_orders(plan, box_limit):
    """k-я порция каждого кластера (по box_limit коробок) — в k-ю заявку; закреплённое — целиком в первую."""
    orders = []
    for c in plan:
        if c.get("fixed"):
            if not orders:
                orders.append({"index": 1, "clusters": []})
            orders[0]["clusters"].append({"id": c["id"], "name": c["name"], "boxes": c["boxes"],
                                          "quantity": c["quantity"], "fixed": True})
            continue
        left, k = c["boxes"], 0
        while left > 0:
            take = min(box_limit, left)
            while len(orders) <= k:
                orders.append({"index": len(orders) + 1, "clusters": []})
            orders[k]["clusters"].append({"id": c["id"], "name": c["name"], "boxes": take,
                                          "quantity": take * c["box_size"]})
            left -= take
            k += 1
    for o in orders:
        o["units"] = sum(c["quantity"] for c in o["clusters"])
        if len(o["clusters"]) > 20:
            o["warning"] = "больше 20 кластеров: делите на заявки до 20 кластеров"
    return orders


def plan(data):
    try:
        total, box = int(data["total"]), int(data["box_size"])
    except (KeyError, TypeError, ValueError):
        raise PlanError("Нужны целые total и box_size") from None
    if total <= 0 or box <= 0:
        raise PlanError("total и box_size должны быть больше нуля")
    min_boxes = int(data.get("min_boxes", 1))
    box_limit = int(data.get("box_limit", 30))
    horizon = float(data.get("horizon_days", 28))
    shares = data.get("shares") or MARKET_SHARE_PCT
    method = data.get("method", "auto")
    every = _clusters(data.get("clusters"))
    fixed = {str(k): int(v) for k, v in (data.get("fixed") or {}).items()}
    unknown = set(fixed) - {c["id"] for c in every}
    if unknown or any(v <= 0 for v in fixed.values()):
        raise PlanError(f"fixed: кластеры из clusters и штуки больше нуля ({', '.join(sorted(unknown))})")
    rest = total - sum(fixed.values())
    clusters = [c for c in every if c["id"] not in fixed] if rest else []
    if rest < 0 or (rest > 0 and not clusters):
        raise PlanError("Закреплено больше объёма или раскладывать остаток некуда")
    if rest % box:
        raise PlanError(json.dumps({"error": "остаток не кратен коробке" if fixed else "total не кратен коробке",
                                    "variants": round_variants(rest, box)}, ensure_ascii=False))
    boxes = rest // box
    warnings = []
    origin = data.get("origin")
    _apply_travel(every, origin, warnings)
    total_ads = sum(c["ads"] for c in clusters)
    reason = None

    if method == "auto":
        if total_ads == 0:
            method, reason = "market_share", "продаж нет (ADS = 0) — по долям рынка"
        elif total_ads * 56 < rest:
            method = "market_share"
            reason = f"поставка больше продаж за 56 дней ({total_ads:g} × 56 < {rest}) — по долям рынка"
        else:
            method, reason = "idc", "по продажам и остаткам: равный запас в днях"

    if not clusters:
        alloc = []
    elif method == "idc":
        for c in clusters:  # продажи есть, а в кластере ADS = 0 (товара не было) — виртуальный ADS по доле рынка
            if c["ads"] == 0 and total_ads > 0:
                s = _share(c, shares)
                if s > 0:
                    c["ads"] = round(total_ads * s / 100, 2)
                    if c["travel"]:
                        c["stock"] = max(0.0, c["stock_now"] - c["ads"] * c["travel"])
                    warnings.append(f"{c['name']}: ADS = 0, взят виртуальный ADS {c['ads']} по доле рынка {s} %")
        alloc = idc_boxes(clusters, boxes, min_boxes, box)
    elif method == "market_share":
        alloc = market_share_boxes(clusters, boxes, min_boxes, shares, warnings)
    elif method == "mixed":  # потребность на горизонт по ADS, остаток — по долям рынка
        need = [max(min_boxes, math.ceil(max(0.0, horizon * c["ads"] - c["stock"]) / box)) for c in clusters]
        if sum(need) > boxes:
            alloc = idc_boxes(clusters, boxes, min_boxes, box)
            warnings.append("потребность на горизонт больше объёма — распределено равным запасом в днях")
        else:
            extra = market_share_boxes(clusters, boxes - sum(need) + len(clusters), 1, shares, warnings)
            alloc = [n + e - 1 for n, e in zip(need, extra)]
        reason = reason or f"смешанно: потребность на {horizon:g} дн. по ADS, остаток — по долям рынка"
    else:
        raise PlanError(f"Неизвестный метод {method}")

    rows, target_days = [], []
    for c, b in zip(clusters, alloc):
        q = b * box
        row = {"id": c["id"], "name": c["name"], "box_size": box, "boxes": b, "quantity": q,
               "ads": c["ads"], "stock": c["stock"], "days_before": _days(c["stock"], c["ads"]),
               "days_after": _days(c["stock"] + q, c["ads"]),
               "share_pct": _share(c, shares) or None, "over_box_limit": b > box_limit}
        if c["travel"]:
            row.update(travel_days=c["travel"], stock_now=c["stock_now"])
        if row["days_after"] is not None and b > min_boxes:
            target_days.append(row["days_after"])
        rows.append(row)
    for c in every:
        if c["id"] in fixed:
            q = fixed[c["id"]]
            rows.append({"id": c["id"], "name": c["name"], "box_size": box, "boxes": q // box if q % box == 0 else None,
                         "quantity": q, "fixed": True, "ads": c["ads"], "stock": c["stock"],
                         "days_before": _days(c["stock"], c["ads"]), "days_after": _days(c["stock"] + q, c["ads"]),
                         "share_pct": _share(c, shares) or None, "over_box_limit": False})
    if fixed:
        warnings.append("закреплённое (fixed) — целиком в первую заявку, вне раскладки; его места задайте сами")
    rows.sort(key=lambda r: -r["quantity"])
    over = [r["name"] for r in rows if r["over_box_limit"]]
    if over:
        warnings.append(f"больше {box_limit} коробок в поставку: {', '.join(over)} — остаток уходит отдельной заявкой")
    return {"method": method, "reason": reason, "total": sum(r["quantity"] for r in rows), "boxes": boxes,
            "fixed": fixed or None,
            "target_days": round(min(target_days), 1) if method == "idc" and target_days else None,
            "clusters": rows, "orders": split_orders(rows, box_limit), "warnings": warnings}


def pending(data):
    """Штуки по кластерам из заявок: на точке отгрузки, в пути, на приёмке и (mode=all) поданные.

    Кластер поставки — в строке supplies_items_list (`cluster_id`, `cluster_name`); у строки без кластера — из
    `supplies[].cluster` ответов supplies_orders_list, если они переданы."""
    mode = data.get("mode", "shipped")
    if mode not in ("shipped", "all"):
        raise PlanError("mode: shipped или all")
    cluster_of = {}
    for order in data.get("orders") or []:
        for s in order.get("supplies") or []:
            cl = s.get("cluster") or {}
            cluster_of[str(s.get("supply_id"))] = (cl.get("id"), cl.get("name"))
    out, unknown = {}, 0
    for row in data.get("items") or []:
        state = row.get("supply_state") or row.get("order_state")
        bucket = SHIPPED.get(state) or ("submitted" if state in SUBMITTED else None)
        if bucket is None:
            continue
        if row.get("cluster_id") is not None:
            cid, name = row["cluster_id"], row.get("cluster_name")
        else:
            cid, name = cluster_of.get(str(row.get("supply_id")), (None, None))
        if cid is None:
            unknown += int(row.get("quantity") or 0)
            continue
        c = out.setdefault(str(cid), {"name": name, "at_dropoff": 0, "in_transit": 0, "at_warehouse": 0,
                                      "submitted": 0, "counted": 0})
        q = int(row.get("quantity") or 0)
        c[bucket] += q
        if bucket != "submitted" or mode == "all":
            c["counted"] += q
    return {"mode": mode, "clusters": out, "unknown_cluster_units": unknown}


def _instant(value, offset_s):
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is None:
        if offset_s is None:
            raise PlanError(f"Время слота «{value}» без пояса, а смещения точки нет")
        t = t.replace(tzinfo=timezone(timedelta(seconds=int(offset_s))))
    return t.astimezone(timezone.utc)


def _answer_slots(a):
    """Слоты одного ответа: черновика (`days[].slots[]`) или заявки (`slots[]`); ключ — мгновения начала и конца."""
    if not isinstance(a, dict):
        raise PlanError("Это не ответ таймслотов — ошибка не равна пустому списку")
    offset = (a.get("timezone") or {}).get("offset_s")
    if isinstance(a.get("days"), list):
        rows = [(x, day.get("date")) for day in a["days"] for x in day.get("slots") or []]
    elif isinstance(a.get("slots"), list):
        if a.get("forbidden"):
            raise PlanError("Ozon не даёт менять таймслот заявки: " + ", ".join(map(str, a["forbidden"])))
        rows = [(x, x.get("date")) for x in a["slots"]]
    else:
        raise PlanError("Это не ответ supplies_drafts_timeslots_list или supplies_orders_timeslots_list — "
                        "ошибка не равна пустому списку")
    return {(_instant(x["from"], offset), _instant(x["to"], offset)):
            {"from": x["from"], "to": x["to"], "label": x.get("label"), "date": date} for x, date in rows}


def intersect(data):
    """Слоты, общие для всех черновиков или заявок одной поставки (одна дата и время); сравниваются мгновения, а в
    команду каждого черновика или заявки идут его собственные строки слота."""
    answers = data.get("answers") or []
    if len(answers) < 2:
        raise PlanError("Нужно хотя бы два ответа таймслотов (черновиков или заявок)")
    per = [_answer_slots(a) for a in answers]
    common = sorted(set(per[0]).intersection(*per[1:]))
    out = []
    for k in common:
        rows = [{"from": p[k]["from"], "to": p[k]["to"]} for p in per]
        out.append({"start_utc": k[0].isoformat().replace("+00:00", "Z"), "label": per[0][k]["label"],
                    "date": per[0][k]["date"], "per_answer": rows, "per_draft": rows})
    return {"answers": len(answers), "drafts": len(answers), "common": out,
            "dates": sorted({c["date"] for c in out if c["date"]}), "no_common_slot": not out}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "pending", "intersect"])
    ap.add_argument("input", help="JSON-файл или - для stdin")
    a = ap.parse_args()
    raw = sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read()
    try:
        result = {"plan": plan, "pending": pending, "intersect": intersect}[a.command](json.loads(raw))
    except PlanError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
