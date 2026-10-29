#!/usr/bin/env python3
"""Проверка PDF с FBS-этикетками перед тем, как отдать их пользователю: каждая страница — ровно одно ожидаемое
отправление, без дублей и пропусков, в статусе «ожидает отгрузки», нужного склада и товара, 1 шт.

Сеть не нужна: PDF — то, что вернул `postings_fbs_labels_download` (сохранённое в файл), данные отправлений — ответы
`postings_fbs_list` (`tab=awaiting_deliver`, все страницы) и/или `postings_fbs_get`, прочитанные прямо перед проверкой.

    verify_labels.py --postings postings.json --pdf labels.pdf [--pdf "Курага 400 FBS 90 шт.pdf=курага 400" …]
        [--expected expected.json] [--offer "<артикул>"] [--warehouse <warehouse.id>] [--multi-ok multi.json]
        [--now "2026-10-28T09:00:00Z"] [--json]
    verify_labels.py --postings postings.json --pdf a.pdf --pdf b.pdf --pages 108-429 --numbers-out part.json

--pdf ФАЙЛ=АРТИКУЛ — в этом файле каждая страница — 1 шт этого товара (файлы `labels_pack.py` по товарам); --offer — то
же для всех файлов. expected.json — номера, которые должны быть в файлах (все вместе); без него — все отправления из
--postings (с --offer — только этого товара). Ожидаемый номер, которого нет в PDF, а в --postings он отменён или уже не
ждёт отгрузки, — «выбыл», а не пропуск: этикеток = ожидаемых − выбывших. multi.json — номера, где в одной упаковке
больше 1 шт (собраны без деления): списком или {номер: {артикул: шт}} — тогда сверяется и состав; остальные — 1 шт.

--pages — своя выборка для отгрузки: только эти страницы, нумерация сквозная по всем --pdf («108-429», «1-107,200»).
Ожидаемые — сами номера с этих страниц, «1 шт» проверяется только с артикулом. --numbers-out получает их по порядку —
только ждущие отгрузки, одного метода доставки (остальные — в excluded); не пишется, если страница без номера или дубль.

Номера бывают 8- и 10-значными и слипаются с соседними цифрами, поэтому на странице ищутся ИЗВЕСТНЫЕ номера подстрокой
(без пробелов), а номер внутри более длинного найденного (…-1 в …-11) не считается. Код выхода 1 — есть проблема: такие
файлы не отдавать. Страница без номера — сперва проверить разбор текста PDF, ошибкой этикетки это не называть.
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


def split_pdf(spec):
    """«файл.pdf=артикул» → (файл, артикул); просто файл → (файл, None)."""
    k = spec.lower().find(".pdf=")
    return (spec[:k + 4], spec[k + 5:] or None) if k >= 0 else (spec, None)


def parse_pages(spec, total):
    """«1-107,200» → номера страниц по порядку; страница вне 1…total — ValueError."""
    out = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        out += range(int(lo), int(hi or lo) + 1)
    bad = [p for p in out if not 1 <= p <= total]
    if bad:
        raise ValueError(f"страниц всего {total}, а названы {bad[:5]}")
    return out


def composition(units_by_offer):
    """{артикул: шт} → «курага 400 ×2 + пастила 500 ×1»."""
    return " + ".join(f"{o} ×{q}" for o, q in sorted(units_by_offer.items()))


def verify(pages, postings, expected=None, offer=None, warehouse=None, multi_ok=(), now=None, selection=False):
    """pages — [(файл, номер страницы, текст[, артикул файла])]; postings — строки отправлений.

    multi_ok — номера (или {номер: {артикул: шт}}) с несколькими штуками в одной упаковке. selection — своя выборка
    страниц для отгрузки: ожидаемые — найденные на них номера, «1 шт» — только с артикулом. Возвращает отчёт с problems.
    """
    by_number = {p["posting_number"]: p for p in postings}
    if expected is None and not selection:
        expected = [n for n, p in by_number.items()
                    if not offer or any(x.get("offer_id") == offer for x in p.get("products") or [])]
    known = set(by_number) | {str(n) for n in expected or ()}
    problems, seen, files, order, want = [], collections.Counter(), collections.Counter(), [], {}
    for page in pages:
        name, i, text = page[:3]
        article = (page[3] if len(page) > 3 else None) or offer
        files[name] += 1
        hits = numbers_on(text, known)
        if len(hits) != 1:
            problems.append(f"{name} стр. {i}: номеров отправлений {len(hits)}" + (f" ({', '.join(hits)})" if hits else ""))
            continue
        seen[hits[0]] += 1
        order.append(hits[0])
        if article:
            want.setdefault(hits[0], article)
    pages_ok = not problems
    expected = set(seen) if expected is None else {str(n) for n in expected}
    dupes = [f"дубль: {n} — {c} стр." for n, c in sorted(seen.items()) if c > 1]
    pages_ok = pages_ok and not dupes
    problems += dupes
    dropped, missing = {}, []
    for n in sorted(expected - set(seen)):
        status = (by_number.get(n) or {}).get("status")
        if status and status != "awaiting_deliver":
            dropped[n] = status                      # отменено или передано после сборки — этикетки и не должно быть
        else:
            missing.append(n)
    extra = sorted(set(seen) - expected)
    problems += [f"нет в PDF: {n}" + ("" if n in by_number else " (отменено? — строку postings_fbs_get в --postings)")
                 for n in missing]
    problems += [f"лишнее в PDF: {n}" for n in extra]
    cancelled, unknown, multi, excluded, methods = [], [], [], {}, collections.Counter()
    for n in sorted(seen):
        p = by_number.get(n)
        if p is None:
            unknown.append(n)
            excluded[n] = "нет в данных"
            problems.append(f"{n}: отправления нет в прочитанных данных — перечитать postings_fbs_get")
            continue
        status = p.get("status")
        if status == "cancelled":
            cancelled.append(n)
            excluded[n] = "отменено"
            problems.append(f"{n}: ОТМЕНЕНО — этикетку не клеить, коробку разобрать")
        elif status != "awaiting_deliver":
            excluded[n] = f"статус {status}"
            problems.append(f"{n}: статус {status}, а не «ожидает отгрузки»")
        wh = (p.get("warehouse") or {}).get("id")
        if warehouse and str(wh) != str(warehouse):
            excluded.setdefault(n, f"склад {wh}")
            problems.append(f"{n}: склад {wh}, а не {warehouse}")
        prods = p.get("products") or []
        have = collections.Counter()
        for x in prods:
            have[x.get("offer_id")] += int(x.get("quantity") or 0)
        article = want.get(n)
        if n in multi_ok:
            spec = multi_ok.get(n) if isinstance(multi_ok, dict) else None
            if spec and dict(have) != {k: int(v) for k, v in spec.items()}:
                excluded.setdefault(n, "состав не тот")
                problems.append(f"{n}: в упаковке {composition(have)}, а ждали {composition(spec)}")
            multi.append(f"{n}: {composition(have)}")
        elif (article or not selection) and (len(prods) != 1 or sum(have.values()) != 1):
            excluded.setdefault(n, f"{sum(have.values())} шт")
            problems.append(f"{n}: в отправлении {sum(have.values())} шт ({len(prods)} товаров), а не 1 шт")
        if article and article not in have:
            excluded.setdefault(n, f"не «{article}»")
            problems.append(f"{n}: в отправлении {composition(have)}, а не «{article}»")
        if n not in excluded:
            methods[(p.get("delivery_method") or {}).get("id")] += 1
    if selection and len(methods) > 1:
        pages_ok = False
        problems.append(f"отправления разных методов доставки {dict(methods)} — разделить выборку по методам")
    ages = [p.get("observed_at") for p in by_number.values() if p.get("observed_at")]
    oldest = min(ages) if ages else None
    if now and oldest:
        age = (dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
               - dt.datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds() / 60
        if age > FRESH_MINUTES:
            problems.append(f"данные отправлений прочитаны {age:.0f} мин назад — перечитать список перед отправкой")
    checked = len(set(seen) & expected)
    due = len(expected) - len(dropped)
    summary = f"проверено {checked} из {due}"
    if dropped:
        summary += f", выбыли {len(dropped)}: " + ", ".join(f"{n} ({s})" for n, s in sorted(dropped.items()))
    return {"pages": sum(files.values()), "files": dict(files), "unique": len(seen), "expected": len(expected),
            "due": due, "checked": checked, "summary": summary, "missing": missing, "dropped": dropped,
            "extra": extra, "cancelled": cancelled, "unknown": unknown, "multi_in_one_package": multi,
            "numbers": [n for n in order if n not in excluded], "excluded": excluded, "pages_ok": pages_ok,
            "oldest_observed_at": oldest, "problems": problems, "ok": not problems}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--postings", required=True, help="ответы postings_fbs_list / postings_fbs_get")
    ap.add_argument("--pdf", action="append", required=True,
                    help="PDF этикеток (можно несколько); «файл.pdf=артикул» — в файле только этот товар по 1 шт")
    ap.add_argument("--expected", help="JSON-список номеров, которые должны быть в PDF")
    ap.add_argument("--offer", help="артикул товара: в каждом отправлении 1 шт этого товара")
    ap.add_argument("--warehouse", help="warehouse.id склада")
    ap.add_argument("--multi-ok", help="JSON: номера (или {номер: {артикул: шт}}) с несколькими шт в одной упаковке")
    ap.add_argument("--pages", help="своя выборка: страницы сквозь все --pdf, «108-429» или «1-107,200»")
    ap.add_argument("--numbers-out", help="JSON: номера со страниц --pages по порядку (ждущие отгрузки)")
    ap.add_argument("--now", help="текущее время UTC — проверить свежесть данных")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    pages = []
    for spec in a.pdf:
        path, article = split_pdf(spec)
        pages += [(Path(path).name, i, t, article) for i, t in enumerate(page_texts(path), 1)]
    if a.pages:
        try:
            pages = [pages[k - 1] for k in parse_pages(a.pages, len(pages))]
        except ValueError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 2
    load = lambda f: json.loads(Path(f).read_text(encoding="utf-8")) if f else None  # noqa: E731
    multi = load(a.multi_ok) or ()
    r = verify(pages, load_items(a.postings), load(a.expected), a.offer, a.warehouse,
               multi if isinstance(multi, dict) else set(multi), a.now, selection=bool(a.pages))
    if a.numbers_out and r["pages_ok"]:
        Path(a.numbers_out).write_text(json.dumps(r["numbers"], ensure_ascii=False), encoding="utf-8")
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(f"страниц {r['pages']}, номеров {r['unique']}, ожидалось {r['expected']} — {r['summary']}")
        if r["multi_in_one_package"]:
            print("несколько штук под одной этикеткой (собраны без деления):", "; ".join(r["multi_in_one_package"]))
        if a.numbers_out:
            if r["pages_ok"]:
                first = f", первый {r['numbers'][0]}" if r["numbers"] else ""
                print(f"в {a.numbers_out}: {len(r['numbers'])} номеров{first}")
            else:
                print(f"{a.numbers_out} не записан: страницы не опознаны однозначно")
            if r["excluded"]:
                print("не в выборку:", "; ".join(f"{n} — {why}" for n, why in r["excluded"].items()))
        for p in r["problems"]:
            print("⚠️", p)
        print("OK — можно отдавать" if r["ok"] else "НЕ ОТДАВАТЬ: исправить и проверить снова")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
