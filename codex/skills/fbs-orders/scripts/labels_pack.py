#!/usr/bin/env python3
"""Файлы FBS-этикеток для печати из многих PDF `postings_fbs_labels_download` (по 20 номеров за вызов): склейка в
заданном порядке, отдельный файл на товар (и на состав «несколько штук в одной упаковке»), деление по лимиту страниц,
понятные имена, по просьбе — двойная этикетка (товарная + FBS на одном листе, как `combine_labels.py`).

Сеть не нужна: страницы опознаются по известным номерам отправлений из ответов `postings_fbs_list` / `postings_fbs_get`.

    labels_pack.py --postings postings.json --pdf a.pdf --pdf b.pdf --out-dir out \
        [--per-file 100] [--first reprint.json] [--product-label "<артикул>=товарная.pdf" --size 75x120 --margin 1.5]

Группы: «<артикул>» — отправления из одного товара по 1 шт; «<артикул> по N шт в упаковке» — собранные без деления;
«смешанные» — заказы из разных товаров. Порядок в группе — как страницы в PDF, номера из --first (перепечатка) — в
начало. Имя файла — «<группа> <N> шт.pdf», при делении — «… (часть K из M).pdf». Страница без ровно одного известного
номера — ошибка: файлы не пишутся (сначала verify_labels.py).
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fbs_bonus import load_items  # noqa: E402
from verify_labels import numbers_on  # noqa: E402


class PackError(ValueError):
    pass


def group_of(posting):
    prods = posting.get("products") or []
    offers = {x.get("offer_id") or x.get("sku") for x in prods}
    if len(offers) != 1:
        return "смешанные"
    units = sum(int(x.get("quantity") or 0) for x in prods)
    offer = offers.pop()
    return offer if units == 1 else f"{offer} по {units} шт в упаковке"


def plan(pages, postings, per_file=None, first=()):
    """pages — [(ключ страницы, текст)] по порядку. Возвращает [{"name", "group", "pages": [ключи], "postings": [...]}]."""
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
        size = per_file or len(items)
        parts = [items[i:i + size] for i in range(0, len(items), size)]
        safe = re.sub(r'[\\/:*?"<>|]+', " ", g).strip()
        for k, part in enumerate(parts, 1):
            tail = f" (часть {k} из {len(parts)})" if len(parts) > 1 else ""
            files.append({"name": f"{safe} {len(part)} шт{tail}.pdf", "group": g,
                          "postings": [n for n, _ in part], "pages": [key for _, key in part]})
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--postings", required=True)
    ap.add_argument("--pdf", action="append", required=True, help="PDF этикеток в нужном порядке (можно много)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--per-file", type=int, help="не больше страниц в файле")
    ap.add_argument("--first", help="JSON-список номеров, которые ставятся в начало (перепечатка)")
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
    try:
        files = plan(pages, load_items(a.postings), a.per_file, first)
    except PackError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    product = {}
    for spec in a.product_label:
        offer, _, path = spec.partition("=")
        product[offer] = PdfReader(path).pages[0]
    if product:
        from combine_labels import combine_page
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        w = PdfWriter()
        for key in f["pages"]:
            pp = product.get(f["group"])
            w.add_page(combine_page(pp, src[key], a.size, a.margin) if pp is not None else src[key])
        w.write(str(out / f["name"]))
        f["path"] = str(out / f["name"])
        f["double"] = f["group"] in product
        del f["pages"]
    print(json.dumps(files, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
