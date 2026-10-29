#!/usr/bin/env python3
"""Сводка прибыли по магазинам и аномалии последнего закрытого дня — из ответов инструментов Seller AI.

Скрипт ничего не запрашивает: агент сохраняет ответы инструментов в файлы и передаёт их сюда.

    python3 profit_anomalies.py --store "Магазин=profit.json" [--store …] [--expenses "Магазин=expenses.json" …] \
        [--error "Магазин=причина" …] [--title "2026-10"] [--totals-only] [--today YYYY-MM-DD] [--json]

--store       ответ economics_profit_get магазина (group=day, с 1-го числа месяца по вчера); имя — как показать в отчёте;
--expenses    ответ promotion_expenses_get (или список ответов-страниц) за те же дни — рост рекламы по кампаниям;
--error       магазин, который не посчитан (ошибка инструмента, not_ready): строка «⚠️», а не 0;
--totals-only только блок прибыли (прошлый месяц в первые 5 дней);
--today       сегодняшний день по Москве (по умолчанию — по часам машины): он не закрыт и в расчёт не идёт.

Аномалии — последний закрытый день против среднего дня остальных дней периода (нужен хотя бы один базовый день): расход категории
(`series[].amounts`, по модулю), себестоимость проданного или утилизированного выросли не меньше чем на max(3 000 ₽, 1,5 % дневных
продаж базы) и в 1,5 раза, или с базы до 300 ₽/д; продажи упали на 15 % и не меньше 15 000 ₽/д; прибыль упала не меньше чем на
15 000 ₽/д. Реклама дня — списания плюс оценка (`estimate`); пока у дня есть оценка (Ozon ещё не списал), рост рекламы не
аномалия, а пометка «оценка». Рост рекламы раскрывается по кампаниям: до двух, с приростом от 3 000 ₽/д.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

MIN_ABS = 3000.0          # ₽/д: меньший рост расхода — шум
REL_SALES = 0.015         # доля дневных продаж базы — порог роста у крупного магазина
REL_GROWTH = 1.5          # рост в разах
SMALL_BASE = 300.0        # ₽/д: база не больше — «рост с нуля», без проверки в разах
SALES_DROP_REL = 0.15
SALES_DROP_ABS = 15000.0
PROFIT_DROP_ABS = 15000.0
CAMPAIGN_MIN = 3000.0     # ₽/д: прирост кампании, который показываем
MSK = timezone(timedelta(hours=3))

EXPENSES = (
    ("commission", "Вознаграждение Ozon"),
    ("logistics", "Логистика"),
    ("returns_logistics", "Возвраты и невыкупы"),
    ("acquiring", "Эквайринг"),
    ("advertising", "Реклама и продвижение"),
    ("supply_storage", "Поставки и склад"),
    ("disposal", "Утилизация (сбор)"),
    ("payouts", "Выплаты"),
    ("subscriptions", "Подписки и сервисы"),
    ("fines", "Штрафы"),
    ("other", "Прочее"),
    ("returns", "Возвраты (выручка)"),
    ("cogs_sold", "Себестоимость проданного"),
    ("cogs_disposed", "Себестоимость утилизированного"),
)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def num(value):
    """Число из ответа (число или десятичная строка); пусто — 0: категории, которой нет в ответе, — 0."""
    if value is None or value == "":
        return 0.0
    return float(value)


def rub(value):
    text = f"{abs(value):,.0f}".replace(",", " ")
    return f"−{text} ₽" if value <= -0.5 else f"{text} ₽"


def named(pairs, what):
    out = []
    for item in pairs or []:
        name, sep, rest = item.partition("=")
        if not sep or not name.strip() or not rest.strip():
            raise ValueError(f"{what}: нужно «Имя=значение», а не «{item}»")
        out.append((name.strip(), rest.strip()))
    return out


# ── расчёт ────────────────────────────────────────────────────────────

def day_metrics(bucket):
    """Плоский срез дня: продажи и прибыль, расходы — положительными числами (сколько потрачено)."""
    amounts = bucket.get("amounts") or {}
    metrics = {"sales": num(bucket.get("sales")), "profit": num(bucket.get("profit"))}
    for key, _ in EXPENSES:
        metrics[key] = abs(num(bucket.get(key))) if key.startswith("cogs_") else -num(amounts.get(key))
    metrics["advertising"] -= num(bucket.get("estimate"))      # оценка Seller AI — со знаком минус, как списание
    return metrics


def closed_days(answer, today):
    """Дни ответа без сегодняшнего (он не закрыт) и был ли он в ответе."""
    series = [b for b in answer.get("series") or [] if b.get("from")]
    kept = [b for b in series if b["from"] < today]
    return kept, len(kept) != len(series)


def find_anomalies(day, base, estimated):
    """(аномалии, пометки): строки по убыванию суммы."""
    found, notes = [], []
    scale = max(MIN_ABS, REL_SALES * base["sales"])
    for key, label in EXPENSES:
        d, b = day[key], base[key]
        delta = d - b
        if delta < scale or (b > SMALL_BASE and d < b * REL_GROWTH):
            continue
        if b > SMALL_BASE:
            how = f"×{d / b:.1f} (было {rub(b)}/д, стало {rub(d)}/д)"
        elif b < 0.5:
            how = f"с нуля до {rub(d)}/д"
        else:
            how = f"с {rub(b)}/д до {rub(d)}/д"
        if key == "advertising" and estimated:
            notes.append(f"{label} — оценка, Ozon ещё не списал: {how}")
            continue
        found.append((delta, key, f"{label}: {how}, +{rub(delta)}/д"))
    drop = base["sales"] - day["sales"]
    if base["sales"] > 0 and drop >= SALES_DROP_ABS and drop / base["sales"] >= SALES_DROP_REL:
        found.append((drop, "sales", f"Продажи: −{drop / base['sales'] * 100:.0f} % (было {rub(base['sales'])}/д, "
                                     f"стало {rub(day['sales'])}/д)"))
    profit_drop = base["profit"] - day["profit"]
    if profit_drop >= PROFIT_DROP_ABS:
        found.append((profit_drop, "profit", f"Прибыль: −{rub(profit_drop)}/д (было {rub(base['profit'])}/д, "
                                             f"стало {rub(day['profit'])}/д)"))
    found.sort(key=lambda x: -x[0])
    return [{"key": key, "text": text} for _, key, text in found], notes


def campaign_growth(answers, last_day, base_days):
    """До двух кампаний с приростом расхода за последний день против среднего базового дня от 3 000 ₽/д."""
    names, day_spend, base_spend = {}, {}, {}
    for answer in answers:
        for row in answer.get("items") or []:
            cid = str(row.get("campaign_id"))
            names[cid] = row.get("title") or cid
            spent = num(row.get("money_spent"))
            if row.get("date") == last_day:
                day_spend[cid] = day_spend.get(cid, 0.0) + spent
            elif row.get("date") in base_days:
                base_spend[cid] = base_spend.get(cid, 0.0) + spent
    grown = []
    for cid in set(day_spend) | set(base_spend):
        d, b = day_spend.get(cid, 0.0), base_spend.get(cid, 0.0) / len(base_days)
        if d - b >= CAMPAIGN_MIN:
            grown.append((d - b, cid, b, d))
    grown.sort(key=lambda x: (-x[0], x[1]))
    return [f"↳ «{names[cid]}» (id {cid}): {rub(b)} → {rub(d)}/д, +{rub(delta)}/д" for delta, cid, b, d in grown[:2]]


def store_report(name, answer, today, expenses=()):
    days, dropped = closed_days(answer, today)
    summary = answer.get("summary") or {}
    profit = sum(num(b.get("profit")) for b in days) if dropped or summary.get("profit") is None else num(summary["profit"])
    out = {"name": name, "profit": round(profit, 2), "today_dropped": dropped, "anomalies": [], "notes": [], "campaigns": [],
           "last_day": days[-1]["from"] if days else None, "base": None, "error": None}
    if len(days) < 2:
        return out
    last, base_days = days[-1], days[:-1]
    metrics = [day_metrics(b) for b in base_days]
    base = {k: sum(m[k] for m in metrics) / len(metrics) for k in metrics[0]}
    out["base"] = {"from": base_days[0]["from"], "to": base_days[-1]["from"], "days": len(base_days)}
    estimated = last.get("estimate") not in (None, 0, "0")
    out["anomalies"], out["notes"] = find_anomalies(day_metrics(last), base, estimated)
    if any(a["key"] == "advertising" for a in out["anomalies"]) and expenses:
        out["campaigns"] = campaign_growth(expenses, last["from"], {b["from"] for b in base_days})
    return out


# ── вывод ─────────────────────────────────────────────────────────────

def render(stores, errors, title, period, totals_only=False):
    lines = [f"📊 **Чистая прибыль — {title}**", f"_{period[0]} → {period[1]} (МСК)_", ""]
    for s in stores:
        lines.append(f"{'🟢' if s['profit'] >= 0 else '🔴'} {s['name']}: {rub(s['profit'])}")
    for name, reason in errors:
        lines.append(f"⚠️ {name}: не посчитан ({reason})")
    lines += ["", f"**ИТОГО: {rub(sum(s['profit'] for s in stores))}**"]
    if any(s["today_dropped"] for s in stores):
        lines.append("_Сегодняшний день не закрыт — в сводку не вошёл._")
    if totals_only:
        return "\n".join(lines) + "\n"
    with_base = [s for s in stores if s["base"]]
    lines.append("")
    if not with_base:
        lines.append("🔎 Аномалии: базовых дней нет (закрыт один день) — не искал.")
        return "\n".join(lines) + "\n"
    first = with_base[0]
    lines += [f"🔎 **Аномалии за {first['last_day']}** _(против среднего дня {first['base']['from']} → "
              f"{first['base']['to']})_", ""]
    found = False
    for s in stores:
        if not s["anomalies"] and not s["notes"]:
            continue
        found = True
        lines.append(f"{'🔴' if s['anomalies'] else '⚪'} **{s['name']}**")
        for anomaly in s["anomalies"]:
            lines.append(f"  • {anomaly['text']}")
            if anomaly["key"] == "advertising":
                lines += [f"    {c}" for c in s["campaigns"]]
        lines += [f"  • {text}" for text in s["notes"]]
        lines.append("")
    for name, reason in errors:
        lines.append(f"⚠️ {name}: не проверен ({reason})")
        found = True
    if not found:
        lines.append("✅ Ничего необычного не нашёл.")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", action="append", default=[])
    ap.add_argument("--expenses", action="append", default=[])
    ap.add_argument("--error", action="append", default=[])
    ap.add_argument("--title")
    ap.add_argument("--totals-only", action="store_true")
    ap.add_argument("--today", default=datetime.now(MSK).date().isoformat())
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        stores = named(args.store, "--store")
        errors = named(args.error, "--error")
        expenses = {}
        for name, path in named(args.expenses, "--expenses"):
            data = load(path)
            expenses.setdefault(name, []).extend(data if isinstance(data, list) else [data])
        if not stores and not errors:
            raise ValueError("нужен хотя бы один --store")
        reports, period = [], None
        for name, path in stores:
            answer = load(path)
            report = store_report(name, answer, args.today, expenses.get(name, ()))
            reports.append(report)
            if period is None:
                start = (answer.get("period") or {}).get("from") or "?"
                period = (start, report["last_day"] or (answer.get("period") or {}).get("to") or "?")
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    period = period or ("?", "?")
    title = args.title or f"{period[0][:7]} (по {period[1]})"
    if args.json:
        json.dump({"title": title, "period": {"from": period[0], "to": period[1]}, "stores": reports,
                   "errors": [{"name": n, "reason": r} for n, r in errors],
                   "total": round(sum(r["profit"] for r in reports), 2)}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(reports, errors, title, period, args.totals_only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
