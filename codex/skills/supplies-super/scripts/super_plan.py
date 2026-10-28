#!/usr/bin/env python3
"""Серия Super-заявок без таймслота — чистые проверки над JSON из инструментов Seller AI; ничего не вызывает.

    python3 super_plan.py allocate  in.json  # {"total", "box_size", "shares": {cluster_id: доля}} → штуки по кластерам
    python3 super_plan.py calendar  in.json  # {"answers": [ответы supplies_drafts_timeslots_list], "date_from", "date_to"}
    python3 super_plan.py check     in.json  # {"calculation": draft.calculation, "plan": {cluster_id: {sku: штук}},
                                             #  "require_super": true} → принято ли ровно запланированное
    python3 super_plan.py order     in.json  # {"order": ответ supplies_orders_get, "plan": …, "dropoff_id"} → сверка заявки
"""
import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from fractions import Fraction


class SuperError(ValueError):
    pass


def positive_int(value, name):
    if isinstance(value, bool):
        raise SuperError(f"{name}: нужно целое больше нуля")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise SuperError(f"{name}: нужно целое больше нуля") from None
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise SuperError(f"{name}: нужно целое больше нуля")
    return parsed


def allocate_boxes(total_units, units_per_box, market_shares):
    """Штуки по кластерам: точная сумма, целые коробки, минимум одна коробка, остальное — по наибольшему дефициту квоты."""
    total = positive_int(total_units, "total")
    box = positive_int(units_per_box, "box_size")
    if total % box or not market_shares:
        raise SuperError("Нужны целые коробки и хотя бы один кластер")
    shares = {}
    for cid, weight in market_shares.items():
        key = str(positive_int(cid, "cluster_id"))
        if key in shares or isinstance(weight, bool):
            raise SuperError("Повтор кластера или неверная доля")
        try:
            value = Fraction(str(weight))
        except (ValueError, ZeroDivisionError):
            raise SuperError("Доли — конечные положительные числа") from None
        if value <= 0:
            raise SuperError("Доли — положительные числа")
        shares[key] = value
    boxes = total // box
    if boxes < len(shares):
        raise SuperError(f"Коробок {boxes} меньше, чем кластеров {len(shares)}: покажите достижимый охват")
    weight_sum = sum(shares.values())
    quotas = {cid: boxes * w / weight_sum for cid, w in shares.items()}
    assigned = {cid: 1 for cid in shares}
    for _ in range(boxes - len(shares)):
        cid = max(shares, key=lambda k: (quotas[k] - assigned[k], shares[k], -int(k)))
        assigned[cid] += 1
    return {cid: assigned[cid] * box for cid in sorted(shares, key=int)}


def calendar_metrics(answers, date_from, date_to):
    """Календарь одного точного плана по одному или нескольким окнам: дни со слотами, доля, недели, наибольший разрыв."""
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if end < start:
        raise SuperError("Окно задом наперёд")
    if isinstance(answers, dict):
        answers = [answers]
    if not answers:
        raise SuperError("Нет ответов таймслотов")
    slots_by_day, zones, covered = {}, set(), set()
    for a in answers:
        if not isinstance(a, dict) or not isinstance(a.get("days"), list):
            raise SuperError("Это не ответ supplies_drafts_timeslots_list — ошибка не равна пустому календарю")
        if a.get("date_from") and a.get("date_to"):
            d = date.fromisoformat(a["date_from"])
            while d <= date.fromisoformat(a["date_to"]):
                covered.add(d)
                d += timedelta(days=1)
        zones.add((a.get("timezone") or {}).get("iana_name"))
        for day in a["days"]:
            if not isinstance(day, dict) or not isinstance(day.get("slots"), list) or not day.get("date"):
                raise SuperError("Неверный день календаря")
            d = date.fromisoformat(day["date"])
            for s in day["slots"]:
                if not s.get("from") or not s.get("to"):
                    raise SuperError("Неверный интервал")
                if start <= d <= end:
                    slots_by_day.setdefault(d, set()).add((s["from"], s["to"]))
    span = (end - start).days + 1
    if covered:
        missing = [start + timedelta(days=i) for i in range(span) if start + timedelta(days=i) not in covered]
        if missing:
            raise SuperError(f"Окна ответов не покрывают период: нет {missing[0].isoformat()}")
    available = sorted(slots_by_day)
    longest = current = 0
    for i in range(span):
        current = 0 if start + timedelta(days=i) in slots_by_day else current + 1
        longest = max(longest, current)
    weeks = Counter(f"{d.isocalendar().year}-W{d.isocalendar().week:02}" for d in available)
    return {"date_from": start.isoformat(), "date_to": end.isoformat(), "timezones": sorted(z for z in zones if z),
            "window_days": span, "available_days": len(available), "coverage": round(len(available) / span, 3),
            "available_dates": [d.isoformat() for d in available],
            "unique_slots": sum(len(s) for s in slots_by_day.values()),
            "longest_gap_days": longest, "days_per_week": dict(sorted(weeks.items()))}


def check_calculation(calc, plan, require_super=False):
    """Расчёт принял ровно план: все кластеры full, принятое по SKU = план, исключённого нет, Super (по требованию)."""
    if not isinstance(calc, dict) or calc.get("status") != "success":
        raise SuperError("Расчёт не успешен — заявку не создавать")
    problems = []
    by_id = {str(c.get("macrolocal_cluster_id")): c for c in calc.get("clusters") or []}
    for cid, items in plan.items():
        c = by_id.get(str(cid))
        if c is None:
            problems.append(f"{cid}: кластера нет в расчёте")
            continue
        name = c.get("name") or cid
        if c.get("availability") != "full":
            problems.append(f"{name}: {c.get('availability')} — {c.get('reason_text') or c.get('reason')}")
        accepted = {}
        for i in (c.get("accepted") or {}).get("items") or []:
            accepted[str(i["sku"])] = accepted.get(str(i["sku"]), 0) + int(i["quantity"])
        want = {str(k): int(v) for k, v in items.items()}
        if accepted != want:
            problems.append(f"{name}: принято {accepted}, план {want}")
        restricted = (c.get("restricted") or {}).get("units") or 0
        if restricted:
            problems.append(f"{name}: исключено {restricted} шт")
        if require_super and c.get("super") is not True:
            problems.append(f"{name}: Super не подтверждён")
    extra = set(by_id) - {str(k) for k in plan}
    if extra:
        problems.append(f"в расчёте лишние кластеры: {', '.join(sorted(extra))}")
    return {"ok": not problems, "problems": problems, "timeslots_allowed": calc.get("timeslots_allowed"),
            "usable_until": calc.get("usable_until")}


def check_order(order_page, plan, dropoff_id=None):
    """Созданная заявка: Super, без таймслота, точка, кластеры и штуки по поставкам = план."""
    o = order_page.get("order") or {}
    problems = []
    if (o.get("tags") or {}).get("super") is not True:
        problems.append("у заявки нет метки Super")
    if o.get("timeslot") is not None:
        problems.append("у заявки выбран таймслот")
    if dropoff_id is not None and str((o.get("dropoff") or {}).get("id")) != str(dropoff_id):
        problems.append("точка отгрузки не та")
    if not o.get("composition_complete"):
        problems.append("состав ещё не прочитан синком — проверить позже, не считать нулём")
    got = {}
    for item in order_page.get("items") or []:
        for s in item.get("supplies") or []:
            cid = str((s.get("cluster") or {}).get("id"))
            got.setdefault(cid, {})[str(item["sku"])] = got.get(cid, {}).get(str(item["sku"]), 0) + int(s.get("quantity") or 0)
    want = {str(c): {str(k): int(v) for k, v in items.items()} for c, items in plan.items()}
    for cid in sorted(set(want) | set(got)):
        if got.get(cid) != want.get(cid):
            problems.append(f"кластер {cid}: в заявке {got.get(cid)}, план {want.get(cid)}")
    return {"ok": not problems, "problems": problems, "state": o.get("state"), "order_number": o.get("order_number")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["allocate", "calendar", "check", "order"])
    ap.add_argument("input")
    a = ap.parse_args()
    d = json.loads(sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read())
    try:
        if a.command == "allocate":
            r = allocate_boxes(d["total"], d["box_size"], d["shares"])
        elif a.command == "calendar":
            r = calendar_metrics(d["answers"], d["date_from"], d["date_to"])
        elif a.command == "check":
            r = check_calculation(d["calculation"], d["plan"], d.get("require_super", False))
        else:
            r = check_order(d["order"], d["plan"], d.get("dropoff_id"))
    except (SuperError, KeyError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
