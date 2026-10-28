#!/usr/bin/env python3
"""Двойная этикетка: сверху товарная (состав, КБЖУ, ШК), снизу FBS-этикетка Ozon — на одной
странице. Размер страницы задаётся (75x120 — обе половины ~×1.25; 58x80 — обе 1:1).

Работает только с локальными файлами: товарная PDF (навык product-label или файл пользователя) и
PDF FBS-этикеток — ответ `postings_fbs_labels_download`, сохранённый агентом в файл (или файл пользователя).
Сеть не нужна. Много PDF и деление по товарам — `labels_pack.py --product-label`.

    combine_labels.py <товарная.pdf> <fbs.pdf> <out.pdf> [--size 75x120] [--margin 1.5] [--limit N]

Одна товарная страница кладётся на все FBS-страницы. Сначала — тест на одну страницу
(--limit 1), рендер и пробная печать с проверкой QR, массово — после «да».
"""
import argparse

from pypdf import PdfReader, PdfWriter, PageObject, Transformation

MM = 72 / 25.4


def place(dst, src, y0, w, half, margin):
    """Вписать страницу src в половину листа (нижний край половины — y0), по центру."""
    sw, sh = float(src.mediabox.width), float(src.mediabox.height)
    k = min((w - 2 * margin) / sw, (half - 2 * margin) / sh)
    x = (w - sw * k) / 2
    y = y0 + (half - sh * k) / 2
    dst.merge_transformed_page(src, Transformation().scale(k).translate(x, y))


def combine_page(product_page, fbs_page, size="75x120", margin_mm=1.5):
    w_mm, h_mm = (float(v) for v in size.lower().split("x"))
    w, h, m = w_mm * MM, h_mm * MM, margin_mm * MM
    page = PageObject.create_blank_page(width=w, height=h)
    place(page, product_page, h / 2, w, h / 2, m)   # верх — товарная
    place(page, fbs_page, 0, w, h / 2, m)           # низ — FBS
    return page


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("product_pdf"); ap.add_argument("fbs_pdf"); ap.add_argument("out_pdf")
    ap.add_argument("--size", default="75x120", help="ШxВ листа, мм")
    ap.add_argument("--margin", type=float, default=1.5, help="поле в мм (58x80 — 0)")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    prod = PdfReader(a.product_pdf).pages[0]
    fbs = PdfReader(a.fbs_pdf).pages
    if a.limit:
        fbs = fbs[:a.limit]
    w = PdfWriter()
    for fp in fbs:
        w.add_page(combine_page(prod, fp, a.size, a.margin))
    w.write(a.out_pdf)
    print(a.out_pdf, len(fbs))


if __name__ == "__main__":
    main()
