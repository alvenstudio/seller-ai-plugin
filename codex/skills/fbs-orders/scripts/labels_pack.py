#!/usr/bin/env python3
"""Файлы FBS-этикеток для печати из многих PDF `postings_fbs_labels_download` (по 20 номеров за вызов): склейка в
заданном порядке, отдельный файл на товар и на каждый состав упаковки, деление по лимиту страниц, понятные имена, по
просьбе — двойная этикетка (товарная + FBS на одном листе, как `combine_labels.py`).

Сеть не нужна: страницы опознаются по известным номерам отправлений из ответов `postings_fbs_list` / `postings_fbs_get`.

    labels_pack.py --postings postings.json --pdf a.pdf --pdf b.pdf --out-dir out [--per-file 100]
        [--first reprint.json] [--name "{Group} {kind} {n}"]
        [--product-label "<артикул>=товарная.pdf" --size 75x120 --margin 1.5]

Группы: «<артикул>» — отправления из одного товара по 1 шт; собранные без деления — по составу одной упаковки:
«пастила 1000 ×5 в одной упаковке», «курага 400 ×2 + пастила 500 ×1 в одной упаковке» (двойными не делаются). Порядок в
группе — как страницы в PDF, номера из --first (перепечатка) — в начало. Имя файла — шаблон --name, по умолчанию
«{group} {kind} {n} шт»: {group} / {offer} — группа, {Group} / {Offer} — она же с заглавной, {kind} — FBS или двойные,
{size} — 58x40 или --size двойных, {n} — страниц в файле; при делении добавляется «(часть K из M)». Страница без ровно
одного известного номера — ошибка: файлы не пишутся (сначала verify_labels.py).
"""
import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fbs_bonus import load_items  # noqa: E402
from verify_labels import composition, numbers_on  # noqa: E402

NAME = "{group} {kind} {n} шт"
FBS_SIZE = "58x40"


class PackError(ValueError):
    pass


def group_of(posting):
    """«<артикул>» — 1 шт одного товара; больше штук — состав одной упаковки (собрано без деления, упаковать вместе)."""
    have = collections.Counter()
    for x in posting.get("products") or []:
        have[x.get("offer_id") or x.get("sku")] += int(x.get("quantity") or 0)
    if not have:
        return "без товаров"
    if sum(have.values()) == 1:
        return next(iter(have))
    return f"{composition(have)} в одной упаковке"


def file_name(template, group, kind, size, n, part=None):
    up = group[:1].upper() + group[1:]
    name = template.format(group=group, offer=group, Group=up, Offer=up, kind=kind, size=size, n=n)
    if part:
        name += f" (часть {part[0]} из {part[1]})"
    return " ".join(re.sub(r'[\\/:*?"<>|]+', " ", name).split()) + ".pdf"


def plan(pages, postings, per_file=None, first=(), name=NAME, double=(), size="75x120"):
    """pages — [(ключ страницы, текст)] по порядку; double — группы (артикулы) с двойной этикеткой.
    Возвращает [{"name", "group", "kind", "pages": [ключи], "postings": [...]}]."""
    by_number = {p["posting_number"]: p for p in postings}
    groups, order, problems = {}, [], []
    for key, text in pages:
        hits = numbers_on(text, set(by_number))
        if len(hits) != 1:
            problems.append(f"{key}: известных номеров {len(hits)}")
            continue
        g = group_of(by_number[hits[0]])
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append((hits[0], key))
    if problems:
        raise PackError("; ".join(problems))
    first = [str(n) for n in first]
    files = []
    for g in order:
        items = sorted(groups[g], key=lambda it: (it[0] not in first, first.index(it[0]) if it[0] in first else 0))
        step = per_file or len(items)
        parts = [items[i:i + step] for i in range(0, len(items), step)]
        kind, sz = ("двойные", size) if g in double else ("FBS", FBS_SIZE)
        for k, part in enumerate(parts, 1):
            files.append({"name": file_name(name, g, kind, sz, len(part), (k, len(parts)) if len(parts) > 1 else None),
                          "group": g, "kind": kind, "postings": [n for n, _ in part],
                          "pages": [key for _, key in part]})
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--postings", required=True)
    ap.add_argument("--pdf", action="append", required=True, help="PDF этикеток в нужном порядке (можно много)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--per-file", type=int, help="не больше страниц в файле")
    ap.add_argument("--first", help="JSON-список номеров, которые ставятся в начало (перепечатка)")
    ap.add_argument("--name", default=NAME, help="шаблон имени: {group} {Group} {offer} {Offer} {kind} {size} {n}")
    ap.add_argument("--product-label", action="append", default=[], help="«<артикул>=товарная.pdf»: двойная этикетка")
    ap.add_argument("--size", default="75x120")
    ap.add_argument("--margin", type=float, default=1.5)
    a = ap.parse_args(argv)
    from pypdf import PdfReader, PdfWriter
    pages, src = [], {}
    for f in a.pdf:
        for i, page in enumerate(PdfReader(f).pages, 1):
            key = f"{Path(f).name} стр. {i}"
            src[key] = page
            pages.append((key, "".join((page.extract_text() or "").split())))
    first = json.loads(Path(a.first).read_text(encoding="utf-8")) if a.first else ()
    product = {}
    for spec in a.product_label:
        offer, _, path = spec.partition("=")
        product[offer] = PdfReader(path).pages[0]
    try:
        files = plan(pages, load_items(a.postings), a.per_file, first, a.name, set(product), a.size)
    except (PackError, KeyError, IndexError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    if product:
        from combine_labels import combine_page
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        w = PdfWriter()
        pp = product.get(f["group"])
        for key in f["pages"]:
            w.add_page(combine_page(pp, src[key], a.size, a.margin) if pp is not None else src[key])
        w.write(str(out / f["name"]))
        f["path"] = str(out / f["name"])
        f["double"] = pp is not None
        del f["pages"]
    print(json.dumps(files, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
