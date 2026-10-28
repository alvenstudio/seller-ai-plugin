#!/usr/bin/env python3
"""Проверка PDF с FBS-этикетками перед тем, как отдать их пользователю: каждая страница — ровно одно ожидаемое
отправление, без дублей и пропусков, в статусе «ожидает отгрузки», нужного склада и товара.

Сеть не нужна: PDF — то, что вернул `postings_fbs_labels_download` (сохранённое в файл), данные отправлений — ответы
`postings_fbs_list` (`tab=awaiting_deliver`, все страницы) и/или `postings_fbs_get`, прочитанные прямо перед проверкой.

    verify_labels.py --postings postings.json --pdf labels.pdf [--pdf more.pdf …] [--expected expected.json]
        [--offer "<артикул>"] [--warehouse <warehouse.id>] [--multi-ok multi.json] [--now "2026-10-28T09:00:00Z"] [--json]

expected.json — номера отправлений, которые должны быть в файлах (все вместе); без него — все отправления из
--postings (с --offer — только этого товара). multi.json — номера, собранные раньше без деления: для них несколько
штук под одной этикеткой — норма. Номера бывают 8- и 10-значными и слипаются с соседними цифрами, поэтому на странице
ищутся ИЗВЕСТНЫЕ номера подстрокой (без пробелов), а номер внутри более длинного найденного (…-1 в …-11) не считается.
Код выхода 1 — есть проблема: такие файлы не отдавать.
"""
import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fbs_bonus import load_items  # noqa: E402

FRESH_MINUTES = 15


def page_texts(pdf_path):
    """Текст каждой страницы без пробелов и переносов."""
    from pypdf import PdfReader
    return ["".join((p.extract_text() or "").split()) for p in PdfReader(pdf_path).pages]


def numbers_on(text, known):
    """Известные номера отправлений на странице; номер, который входит в более длинный найденный, отбрасывается."""
    hits = [n for n in known if n and n in text]
    return sorted(h for h in hits if not any(h != o and h in o for o in hits))


def verify(pages, postings, expected=None, offer=None, warehouse=None, multi_ok=(), now=None):
    """pages — [(файл, номер страницы, текст)]; postings — строки отправлений. Возвращает отчёт с problems."""
    by_number = {p["posting_number"]: p for p in postings}
    if expected is None:
        expected = [n for n, p in by_number.items()
                    if not offer or any(x.get("offer_id") == offer for x in p.get("products") or [])]
    expected = {str(n) for n in expected}
    known = expected | set(by_number)
    problems, seen, files = [], collections.Counter(), collections.Counter()
    for name, i, text in pages:
        files[name] += 1
        hits = numbers_on(text, known)
        if len(hits) != 1:
            problems.append(f"{name} стр. {i}: номеров отправлений {len(hits)}" + (f" ({', '.join(hits)})" if hits else ""))
            continue
        seen[hits[0]] += 1
    problems += [f"дубль: {n} — {c} стр." for n, c in sorted(seen.items()) if c > 1]
    missing = sorted(expected - set(seen))
    extra = sorted(set(seen) - expected)
    problems += [f"нет в PDF: {n}" for n in missing]
    problems += [f"лишнее в PDF: {n}" for n in extra]
    cancelled, unknown, multi = [], [], []
    for n in sorted(seen):
        p = by_number.get(n)
        if p is None:
            unknown.append(n)
            problems.append(f"{n}: отправления нет в прочитанных данных — перечитать postings_fbs_get")
            continue
        status = p.get("status")
        if status == "cancelled":
            cancelled.append(n)
            problems.append(f"{n}: ОТМЕНЕНО — этикетку не клеить")
        elif status != "awaiting_deliver":
            problems.append(f"{n}: статус {status}, а не «ожидает отгрузки»")
        wh = (p.get("warehouse") or {}).get("id")
        if warehouse and str(wh) != str(warehouse):
            problems.append(f"{n}: склад {wh}, а не {warehouse}")
        prods = p.get("products") or []
        units = sum(int(x.get("quantity") or 0) for x in prods)
        if n in multi_ok:
            multi.append(f"{n}: " + ", ".join(f"{x.get('offer_id')} × {x.get('quantity')}" for x in prods))
        elif offer and (len(prods) != 1 or units != 1):
            problems.append(f"{n}: в отправлении {units} шт ({len(prods)} товаров), а не 1 шт")
        if offer and not any(x.get("offer_id") == offer for x in prods):
            problems.append(f"{n}: в отправлении не «{offer}»")
    ages = [p.get("observed_at") for p in by_number.values() if p.get("observed_at")]
    oldest = min(ages) if ages else None
    if now and oldest:
        age = (dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
               - dt.datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds() / 60
        if age > FRESH_MINUTES:
            problems.append(f"данные отправлений прочитаны {age:.0f} мин назад — перечитать список перед отправкой")
    checked = len(set(seen) & expected)
    return {"pages": sum(files.values()), "files": dict(files), "unique": len(seen), "expected": len(expected),
            "checked": checked, "summary": f"проверено {checked} из {len(expected)}", "missing": missing,
            "extra": extra, "cancelled": cancelled, "unknown": unknown, "multi_in_one_package": multi,
            "oldest_observed_at": oldest, "problems": problems, "ok": not problems}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--postings", required=True, help="ответы postings_fbs_list / postings_fbs_get")
    ap.add_argument("--pdf", action="append", required=True, help="PDF этикеток (можно несколько)")
    ap.add_argument("--expected", help="JSON-список номеров, которые должны быть в PDF")
    ap.add_argument("--offer", help="артикул товара: в каждом отправлении 1 шт этого товара")
    ap.add_argument("--warehouse", help="warehouse.id склада")
    ap.add_argument("--multi-ok", help="JSON-список номеров, собранных раньше без деления")
    ap.add_argument("--now", help="текущее время UTC — проверить свежесть данных")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    pages = [(Path(f).name, i, t) for f in a.pdf for i, t in enumerate(page_texts(f), 1)]
    load = lambda f: json.loads(Path(f).read_text(encoding="utf-8")) if f else None  # noqa: E731
    r = verify(pages, load_items(a.postings), load(a.expected), a.offer, a.warehouse, set(load(a.multi_ok) or ()),
               a.now)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(f"страниц {r['pages']}, номеров {r['unique']}, ожидалось {r['expected']} — {r['summary']}")
        if r["multi_in_one_package"]:
            print("несколько штук под одной этикеткой (собраны раньше без деления):", "; ".join(r["multi_in_one_package"]))
        for p in r["problems"]:
            print("⚠️", p)
        print("OK — можно отдавать" if r["ok"] else "НЕ ОТДАВАТЬ: исправить и проверить снова")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
