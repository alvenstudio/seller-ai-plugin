#!/usr/bin/env python3
"""
Тариф отгрузки FBS: какие отправления сдать в ближайшее окно, чтобы не потерять скидку и не попасть на штраф.

Считает только по ответу `postings_fbs_list` (поле `tariff.steps`: ступени Ozon «Скидка / Без скидки / Штраф» со сроком
`deadline_at` и суммой `charge` в рублях). Ничего не вызывает: агент сохраняет страницы ответа в файл (JSON — один ответ,
список ответов или список строк `items`) и передаёт его сюда.

Тариф решает момент передачи отправления (сдачи), а не сборка. Ступень в момент t — первая, срок которой ещё не прошёл;
когда прошли все — последняя.

Режимы:
  план (без --need): по товару и состоянию (собрано / не собрано) — штук, ₽ в ближайшее окно, ₽ в следующее, потеря при переносе;
  отбор (--offer X --need N): N штук X с наибольшей потерей при переносе, при равенстве — от старых к новым, только
      «чистые» заказы X (без других товаров, если не --allow-mixed) → JSON номеров отправлений (--out).

    fbs_bonus.py postings.json --ship-at "2026-10-19 19:00" --next-ship-at "2026-10-20 11:30" [--warehouse <id>]
    fbs_bonus.py postings.json --ship-at … --next-ship-at … --offer "пастила 1000" --need 300 --out pick.json

Времена окон — московские («YYYY-MM-DD HH:MM»), их называет пользователь.
"""
import argparse
import collections
import datetime as dt
import json
import sys
from decimal import Decimal

MSK = dt.timezone(dt.timedelta(hours=3))


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_items(path):
    """Строки отправлений из файла: ответ postings_fbs_list, список ответов или список строк."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pages = data if isinstance(data, list) else [data]
    items = []
    for p in pages:
        if isinstance(p, dict) and "items" in p:
            items += p["items"]
        elif isinstance(p, dict) and "posting_number" in p:
            items.append(p)
    seen, out = set(), []
    for it in items:                                    # страницы могут перекрываться
        if it["posting_number"] not in seen:
            seen.add(it["posting_number"])
            out.append(it)
    return out


def step_at(steps, t):
    """Ступень тарифа в момент передачи t: первая со сроком впереди, иначе последняя. None — шкалы нет."""
    if not steps:
        return None
    for s in steps:
        d = s.get("deadline_at")
        if d and ts(d) >= t:
            return s
    return steps[-1]


def value(step):
    """Знаковая сумма ступени, ₽: скидка +, штраф −, без скидки 0. None — неизвестно (шкалы или суммы нет)."""
    if step is None:
        return None
    if step.get("effect") == "none":
        return Decimal(0)
    if step.get("charge") is None:
        return None
    v = Decimal(step["charge"])
    return v if step.get("effect") == "discount" else -v


def rows(items, warehouse, exclude, t1, t2):
    out = []
    for p in items:
        if p.get("status") not in ("awaiting_packaging", "awaiting_deliver"):
            continue
        if warehouse and str((p.get("warehouse") or {}).get("id")) != str(warehouse):
            continue
        if p["posting_number"] in exclude:
            continue
        steps = (p.get("tariff") or {}).get("steps") or []
        v1, v2 = value(step_at(steps, t1)), value(step_at(steps, t2))
        offers = {x.get("offer_id") for x in p.get("products", [])}
        out.append(dict(pn=p["posting_number"], st=p["status"], t0=p.get("in_process_at") or "",
                        q=sum(x.get("quantity", 0) for x in p.get("products", [])),
                        offers=offers, pure=len(offers) == 1, now=v1, later=v2))
    return out


def plan(rs):
    """{(offer, состояние): {шт, сейчас, потом, неизвестно}} — смешанные отправления относятся к «смешанное»."""
    agg = collections.defaultdict(lambda: {"postings": 0, "units": 0, "now": Decimal(0), "later": Decimal(0),
                                           "unknown": 0, "penalty_now": 0})
    for r in rs:
        offer = next(iter(r["offers"])) if r["pure"] else "смешанное"
        g = agg[(offer, "собрано" if r["st"] == "awaiting_deliver" else "не собрано")]
        g["postings"] += 1
        g["units"] += r["q"]
        if r["now"] is None or r["later"] is None:
            g["unknown"] += 1
            continue
        g["now"] += r["now"]
        g["later"] += r["later"]
        if r["now"] < 0:
            g["penalty_now"] += 1
    return agg


def pick(rs, offer, need, allow_mixed=False):
    cand = [r for r in rs if r["st"] == "awaiting_packaging" and offer in r["offers"]
            and (r["pure"] or allow_mixed) and r["now"] is not None and r["later"] is not None and r["q"] > 0]
    cand.sort(key=lambda r: (-(r["now"] - r["later"]) / r["q"], r["t0"]))
    chosen, n = [], 0
    for r in cand:
        if n + r["q"] > need:
            continue
        chosen.append(r)
        n += r["q"]
        if n == need:
            break
    return chosen, n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Тариф отгрузки FBS: план по окнам сдачи и отбор отправлений")
    ap.add_argument("postings", help="JSON с ответом(ами) postings_fbs_list")
    ap.add_argument("--ship-at", required=True, help="ближайшее окно сдачи, МСК «YYYY-MM-DD HH:MM»")
    ap.add_argument("--next-ship-at", required=True, help="следующее окно сдачи, МСК")
    ap.add_argument("--warehouse", help="warehouse.id склада продавца")
    ap.add_argument("--offer")
    ap.add_argument("--need", type=int, help="сколько штук отобрать")
    ap.add_argument("--allow-mixed", action="store_true", help="брать и смешанные заказы")
    ap.add_argument("--exclude", help="JSON-список номеров, которые уже распределены")
    ap.add_argument("--out", default="pick.json")
    a = ap.parse_args(argv)

    t1 = dt.datetime.fromisoformat(a.ship_at).replace(tzinfo=MSK)
    t2 = dt.datetime.fromisoformat(a.next_ship_at).replace(tzinfo=MSK)
    exclude = set(json.load(open(a.exclude, encoding="utf-8"))) if a.exclude else set()
    rs = rows(load_items(a.postings), a.warehouse, exclude, t1, t2)

    if not a.need:
        print(f"окна: {t1:%d.%m %H:%M} и {t2:%d.%m %H:%M} МСК; отправлений {len(rs)}")
        agg = plan(rs)
        for (off, st), g in sorted(agg.items(), key=lambda kv: -(kv[1]["now"] - kv[1]["later"])):
            loss = g["now"] - g["later"]
            line = (f"{off} | {st}: {g['postings']} отпр., {g['units']} шт; в ближайшее окно {g['now']:+.2f} ₽, "
                    f"в следующее {g['later']:+.2f} ₽, потеря при переносе {loss:.2f} ₽")
            if g["penalty_now"]:
                line += f"; уже со штрафом к ближайшему окну: {g['penalty_now']}"
            if g["unknown"]:
                line += f"; без шкалы тарифа (неизвестно): {g['unknown']}"
            print(line)
        return 0

    if not a.offer:
        sys.exit("--need требует --offer")
    chosen, n = pick(rs, a.offer, a.need, a.allow_mixed)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump([r["pn"] for r in chosen], f, ensure_ascii=False)
    gain = sum((r["now"] - r["later"] for r in chosen), Decimal(0))
    print(f"взято {len(chosen)} отправлений, {n} шт из {a.need}; выгода против следующего окна {gain:.2f} ₽; файл {a.out}")
    if chosen:
        print(f"приняты в обработку {min(r['t0'] for r in chosen)} … {max(r['t0'] for r in chosen)} (UTC)")
    if n < a.need:
        print(f"не хватило {a.need - n} шт: чистых несобранных отправлений с известной шкалой меньше")
    return 0


if __name__ == "__main__":
    sys.exit(main())
