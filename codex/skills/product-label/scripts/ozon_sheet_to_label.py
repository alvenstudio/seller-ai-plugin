#!/usr/bin/env python3
"""Этикетка со штрихкодом Ozon из присланного листа («Ozon Seller.pdf», коды OZN…): блок штрихкода вырезается из
растра листа без изменения ширины и ставится наверх этикетки 58×40, текст (состав, КБЖУ, срок, дата упаковки…) —
ниже, шаблоном generate_label. Так код Ozon остаётся тем, что знает склад. Своя этикетка с EAN-13 — инструментом Seller AI
`products_labels_create` (навык product-label).

Сеть не нужна: только присланный файл и данные из чата или карточки.

    pdftoppm -png -r 600 -f 1 -l 1 "Ozon Seller.pdf" sheet            # растр нужной страницы, 600 dpi
    ozon_sheet_to_label.py --png sheet-1.png --dpi 600 --crop 120,340,1300,520 --data data.json --out label.pdf

--crop — x,y,ширина,высота блока штрихкода (с цифрами под ним) в пикселях растра от левого верхнего угла: найти по
картинке. --data — поля generate_label (состав, kbzhu, срок, хранение, дата упаковки, реквизиты); штрихкод не нужен.
Блок шире этикетки — ошибка: сжатие меняет ширину штрихов; --fit сжимает, и тогда скан проверить обязательно.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MARGIN_MM, GAP_MM, MIN_TEXT_MM = 1.0, 0.5, 8.0


class LabelError(ValueError):
    pass


def compose(png, dpi, crop, data, out, width_mm=58.0, height_mm=40.0, fit=False):
    from PIL import Image
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import generate_label

    x, y, w, h = (int(v) for v in crop)
    sheet = Image.open(png)
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > sheet.width or y + h > sheet.height:
        raise LabelError(f"Область {crop} вне растра {sheet.width}×{sheet.height}")
    block = sheet.crop((x, y, x + w, y + h))
    bw, bh = w / dpi * 25.4, h / dpi * 25.4
    room = width_mm - 2 * MARGIN_MM
    scale = 1.0
    if bw > room:
        if not fit:
            raise LabelError(f"Блок штрихкода {bw:.1f} мм шире этикетки ({room:.1f} мм): сжатие меняет ширину штрихов. "
                             f"Другой размер этикетки или --fit (и обязательно проверить сканером)")
        scale = room / bw
    bw, bh = bw * scale, bh * scale
    text_h = height_mm - MARGIN_MM - bh - GAP_MM
    if text_h < MIN_TEXT_MM:
        raise LabelError(f"Под текст остаётся {text_h:.1f} мм: этикетку выше (58x60) или блок поменьше")
    c = canvas.Canvas(str(out), pagesize=(width_mm * mm, height_mm * mm))
    c.drawImage(ImageReader(block), (width_mm - bw) / 2 * mm, (height_mm - MARGIN_MM - bh) * mm,
                width=bw * mm, height=bh * mm)
    generate_label.last_warnings.clear()
    text = dict(data, barcode="")
    if text.get("label_type") == "barcode_only":
        text["label_type"] = "packer"
    generate_label.draw_label(c, text, width_mm, text_h)      # текст — снизу, в оставшейся высоте
    c.showPage()
    c.save()
    return {"out": str(out), "block_mm": [round(bw, 1), round(bh, 1)], "scaled": scale != 1.0,
            "text_height_mm": round(text_h, 1), "warnings": list(generate_label.last_warnings)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--png", required=True, help="растр страницы листа Ozon")
    ap.add_argument("--dpi", type=float, default=600)
    ap.add_argument("--crop", required=True, help="x,y,ширина,высота блока штрихкода, px")
    ap.add_argument("--data", required=True, help="JSON-файл с полями этикетки")
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", default="58x40", help="ШxВ мм")
    ap.add_argument("--fit", action="store_true", help="сжать широкий блок (проверить сканером)")
    a = ap.parse_args(argv)
    w_mm, h_mm = (float(v) for v in a.size.lower().split("x"))
    try:
        r = compose(a.png, a.dpi, a.crop.split(","), json.loads(Path(a.data).read_text(encoding="utf-8")), a.out,
                    w_mm, h_mm, a.fit)
    except LabelError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    for x in r["warnings"]:
        print("⚠️", x)
    if r["scaled"]:
        print("⚠️ блок штрихкода сжат — распечатать пробную и проверить сканером")
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
