#!/usr/bin/env python3
"""План массового переноса таймслотов «на самое позднее» — чистый расчёт над ответами инструментов Seller AI; ничего
не вызывает и ничего не отправляет.

    python3 reschedule_plan.py in.json
    вход: {"orders": [ответы или строки supplies_orders_list, все страницы], "total": total ответа (необязательно),
           "timeslots": {order_id: ответ supplies_orders_timeslots_list}, "accuracy": ответ supplies_accuracy_get,
           "now": "2026-10-28T09:00:00Z", "horizon_days": 10, "threshold_percent": 10, "max_candidates": 30}

Отбор — заявки «Подготовки к отгрузке» (DATA_FILLING, READY_TO_SUPPLY; строки других статусов — в пропуски) с началом
слота в прошлом и до now + horizon_days. Точность ниже порога или неизвестна — магазин пропускается целиком.
Кандидаты заявки — слоты строго позже её текущего начала и позже now, по убыванию времени (сравниваются мгновения с
поясом точки отгрузки, а не строки), не больше max_candidates. По каждой заявке — тело
`supplies_orders_timeslot_set` для первого кандидата (`timeslot` — строки Ozon как есть, `expected` — текущий слот).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone


MOVABLE = {"DATA_FILLING", "READY_TO_SUPPLY"}


class PlanError(ValueError):
    pass


def _instant(value, offset_s=None):
    """Мгновение из строки Ozon: со смещением или «Z» — как есть, без него — время точки отгрузки (offset_s)."""
    if not value:
        return None
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is None:
        if offset_s is None:
            raise PlanError(f"Время «{value}» без пояса, а смещения точки отгрузки нет")
        t = t.replace(tzinfo=timezone(timedelta(seconds=int(offset_s))))
    return t.astimezone(timezone.utc)


def _rows(orders):
    """Строки заявок: из ответов supplies_orders_list (поле items) или уже строками; повторы страниц — один раз."""
    rows, seen = [], set()
    for page in orders or []:
        for r in page.get("items", [page]) if isinstance(page, dict) else []:
            if isinstance(r, dict) and r.get("order_id") and str(r["order_id"]) not in seen:
                seen.add(str(r["order_id"]))
                rows.append(r)
    return rows


def _total(data):
    if data.get("total") is not None:
        return int(data["total"])
    for page in data.get("orders") or []:
        if isinstance(page, dict) and "items" in page and page.get("total") is not None:
            return int(page["total"])
    return None


def store_gate(accuracy, threshold):
    """Можно ли переносить в магазине: точность известна и не ниже порога."""
    a = accuracy or {}
    rate = a.get("rate_percent")
    gate = {"rate_percent": rate, "threshold_percent": threshold,
            "estimated": bool((a.get("estimate") or {}).get("estimated")), "ok": True, "reason": None}
    if rate is None:
        gate.update(ok=False, reason="точность отгрузок неизвестна")
    elif rate < threshold:
        gate.update(ok=False, reason=f"точность {rate} % ниже порога {threshold:g} %")
    elif gate["estimated"] and rate < threshold + 2:
        gate["reason"] = "точность у порога и оценочная — точное значение сверить в кабинете Ozon"
    return gate


def plan(data):
    now = _instant(data.get("now"))
    if now is None:
        raise PlanError("Нужно now — текущее время UTC")
    horizon = float(data.get("horizon_days", 10))
    threshold = float(data.get("threshold_percent", 10))
    limit = int(data.get("max_candidates", 30))
    rows, total = _rows(data.get("orders")), _total(data)
    gate = store_gate(data.get("accuracy"), threshold)
    timeslots = {str(k): v for k, v in (data.get("timeslots") or {}).items()}
    result = {"complete": total is None or len(rows) >= total, "rows": len(rows), "total": total, "store": gate,
              "moves": [], "skipped": [], "out_of_range": 0}
    if not result["complete"]:
        result["warning"] = f"собрано {len(rows)} заявок из {total}: дочитать страницы, до этого не переносить"
    until = now + timedelta(days=horizon)
    for r in rows:
        oid, number = str(r["order_id"]), r.get("order_number") or str(r["order_id"])
        slot = r.get("timeslot")

        def skip(reason):
            result["skipped"].append({"order_id": oid, "order_number": number, "reason": reason})

        if r.get("state") and r["state"] not in MOVABLE:
            skip(f"заявка {r['state']} — переносят только «Подготовку к отгрузке» (DATA_FILLING, READY_TO_SUPPLY)")
            continue
        if (r.get("tags") or {}).get("virtual"):
            skip("виртуальная заявка — только в кабинете Ozon")
            continue
        if not slot or not slot.get("from"):
            skip("без таймслота — требует проверки")
            continue
        start = _instant(slot["from"], slot.get("utc_offset_s"))
        if start > until:
            result["out_of_range"] += 1
            continue
        if not gate["ok"]:
            skip(gate["reason"])
            continue
        answer = timeslots.get(oid)
        if not answer:
            skip("нет ответа supplies_orders_timeslots_list")
            continue
        if answer.get("forbidden"):
            skip("Ozon запрещает перенос: " + ", ".join(answer["forbidden"]))
            continue
        left = (answer.get("changes") or {}).get("left")
        if left == 0:
            skip("переносов не осталось")
            continue
        offset = (answer.get("timezone") or {}).get("offset_s")
        if offset is None:
            offset = slot.get("utc_offset_s")
        cands = []
        for s in answer.get("slots") or []:
            t = _instant(s.get("from"), offset)
            if t > start and t > now:
                cands.append((t, s))
        cands.sort(key=lambda c: c[0], reverse=True)
        if not cands:
            skip("нет слота позже текущего")
            continue
        current = answer.get("current") or {"from": slot.get("from"), "to": slot.get("to")}
        best = cands[0][1]
        result["moves"].append({
            "order_id": oid, "order_number": number,
            "current": {"from": slot.get("from"), "local_from": slot.get("local_from")},
            "changes_left": left,
            "candidates": [{"from": s["from"], "to": s.get("to"), "label": s.get("label"), "date": s.get("date"),
                            "start_utc": t.isoformat().replace("+00:00", "Z")} for t, s in cands[:limit]],
            "body": {"timeslot": {"from": best["from"], "to": best.get("to")},
                     "expected": {"from": current.get("from"), "to": current.get("to")},
                     "confirm_limit": left is not None}})
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="JSON-файл или - для stdin")
    a = ap.parse_args()
    data = json.loads(sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read())
    try:
        r = plan(data)
    except (PlanError, KeyError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
