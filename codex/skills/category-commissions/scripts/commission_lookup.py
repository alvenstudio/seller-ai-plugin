#!/usr/bin/env python3
"""Ставка вознаграждения Ozon по справочнику категорий (references/commissions-*.tsv) для цены и схемы — поиск
подстрокой без регистра в «Категории» и «Типе товара»; ничего не вызывает.

    python3 commission_lookup.py "сухофрукт" --price 450 --scheme fbo [--field type|category] [--tsv …] [--json]

Схемы: fbo, fbo_fresh, fbs (три диапазона цены: до 100, 100–300, свыше 300 ₽), rfbs (одна ставка). Диапазон — по цене
продавца с его акциями: ≤ 100 ₽, выше 100 и до 300 включительно, > 300 ₽. Пустая ячейка — у Ozon ставки нет (не 0).
--json — строки со ставками долями (commission_micro / _low / _high) для new_product_plan.py навыка unit-economics.
"""
import argparse
import csv
import json
import sys
from decimal import Decimal
from pathlib import Path

TSV = sorted((Path(__file__).resolve().parent.parent / "references").glob("commissions-*.tsv"))[-1]
COLUMNS = {"fbo": ("FBO до 100", "FBO 100-300", "FBO свыше 300"),
           "fbo_fresh": ("FBO Fresh до 100", "FBO Fresh 100-300", "FBO Fresh свыше 300"),
           "fbs": ("FBS до 100", "FBS 100-300", "FBS свыше 300"),
           "rfbs": ("RFBS", "RFBS", "RFBS")}
BANDS = ("до 100 ₽", "100–300 ₽", "свыше 300 ₽")


def band(price):
    return 0 if price <= 100 else (1 if price <= 300 else 2)


def _pct(cell):
    cell = (cell or "").strip().replace(",", ".")
    return Decimal(cell) if cell else None


def lookup(rows, query, scheme="fbo", price=None, field=None):
    q = query.lower().strip()
    cols = COLUMNS[scheme]
    out = []
    for r in rows:
        where = {"type": [r["Тип товара"]], "category": [r["Категория"]]}.get(field, [r["Категория"], r["Тип товара"]])
        if not any(q in (w or "").lower() for w in where):
            continue
        rates = [_pct(r[c]) for c in cols]
        row = {"main_category": r["Основная категория"], "category": r["Категория"], "type": r["Тип товара"],
               "scheme": scheme, "rates_pct": {b: (str(x) if x is not None else None) for b, x in zip(BANDS, rates)},
               "commission_micro": float(rates[0] / 100) if rates[0] is not None else None,
               "commission_low": float(rates[1] / 100) if rates[1] is not None else None,
               "commission_high": float(rates[2] / 100) if rates[2] is not None else None}
        if price is not None:
            rate = rates[band(Decimal(str(price)))]
            row.update(price=float(price), band=BANDS[band(Decimal(str(price)))],
                       rate_pct=str(rate) if rate is not None else None,
                       commission_rub=float((Decimal(str(price)) * rate / 100).quantize(Decimal("0.01")))
                       if rate is not None else None)
        out.append(row)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--price", type=float)
    ap.add_argument("--scheme", choices=sorted(COLUMNS), default="fbo")
    ap.add_argument("--field", choices=["type", "category"])
    ap.add_argument("--tsv", default=str(TSV))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    with open(a.tsv, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    found = lookup(rows, a.query, a.scheme, a.price, a.field)
    if a.json:
        print(json.dumps(found, ensure_ascii=False, indent=1))
        return 0 if found else 1
    if not found:
        print("Не нашлось: родственные слова или категория выше")
        return 1
    print(f"Справочник {Path(a.tsv).name}, схема {a.scheme}" + (f", цена {a.price:g} ₽" if a.price is not None else ""))
    for r in found:
        line = f"- {r['main_category']} / {r['category']} / {r['type']}: " + " · ".join(
            f"{b} {v or '—'} %" for b, v in r["rates_pct"].items())
        if a.price is not None:
            line += (f" → {r['band']}: {r['rate_pct']} % = {r['commission_rub']:.2f} ₽" if r["rate_pct"] is not None
                     else f" → {r['band']}: ставки нет")
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
