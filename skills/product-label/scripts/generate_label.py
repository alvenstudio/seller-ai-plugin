#!/usr/bin/env python3
"""
Генерация этикетки товара в формате PDF (локально, без сети).

Размеры: 58x40 мм (по умолчанию) или 58x60 мм.
Формат: сплошной текст + штрихкод горизонтально внизу.

Штрихкод — EAN-13: 13 цифр с верной контрольной цифрой или 12 цифр (EAN-13 без контрольной — она дописывается).
Всё остальное на этикетку не выводится, и об этом печатается предупреждение (stderr и `last_warnings`).
"""

import argparse
import json
import os
import sys

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.barcode.eanbc import Ean13BarcodeWidget
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ean13 import ean13_check_digit, make_ean13, normalize_ean13  # noqa: E402,F401


def register_fonts():
    """Register Inter font (primary) or DejaVu Sans (fallback) for Cyrillic support."""
    # Try Inter first (bundled with skill)
    skill_fonts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")
    inter_regular = os.path.join(skill_fonts_dir, "Inter-Regular.ttf")
    inter_bold = os.path.join(skill_fonts_dir, "Inter-Bold.ttf")
    if os.path.exists(inter_regular) and os.path.exists(inter_bold):
        try:
            pdfmetrics.registerFont(TTFont("DejaVu", inter_regular))
            pdfmetrics.registerFont(TTFont("DejaVuBold", inter_bold))
            return True
        except Exception:
            pass

    # Fallback to DejaVu Sans
    font_dirs = ["/usr/share/fonts", "/usr/local/share/fonts"]
    regular = bold = None
    for d in font_dirs:
        for root, _, files in os.walk(d):
            for f in files:
                fp = os.path.join(root, f)
                fl = f.lower().replace(" ", "")
                if "dejavusans-bold" in fl and f.endswith(".ttf") and not bold:
                    try:
                        pdfmetrics.registerFont(TTFont("DejaVuBold", fp))
                        bold = fp
                    except Exception:
                        pass
                elif "dejavusans" in fl and "bold" not in fl and "oblique" not in fl and f.endswith(".ttf") and not regular:
                    try:
                        pdfmetrics.registerFont(TTFont("DejaVu", fp))
                        regular = fp
                    except Exception:
                        pass
    return regular is not None


last_warnings: list[str] = []


def _warn(msg: str):
    last_warnings.append(msg)


def draw_label(c: canvas.Canvas, data: dict, width_mm: float, height_mm: float):
    """Draw one label on the canvas."""
    w = width_mm * mm
    h = height_mm * mm

    has_fonts = register_fonts()
    bold_font = "DejaVuBold" if has_fonts else "Helvetica-Bold"
    regular_font = "DejaVu" if has_fonts else "Helvetica"

    margin = 1.5 * mm

    # Barcode-only mode: just a big barcode + number
    if data.get("label_type") == "barcode_only":
        barcode_value, warning = normalize_ean13(data.get("barcode", ""))
        if warning:
            _warn(warning)
        elif not barcode_value:
            _warn("этикетка «только штрихкод» без штрихкода — пустая")
        if barcode_value:
            import barcode as bc_lib
            from barcode.writer import ImageWriter
            import io
            from reportlab.lib.utils import ImageReader

            ean = bc_lib.get('ean13', barcode_value, writer=ImageWriter())
            buf = io.BytesIO()
            ean.write(buf, options={
                'write_text': False,
                'module_width': 0.5,
                'module_height': 15,
                'quiet_zone': 1,
            })
            buf.seek(0)
            img = ImageReader(buf)

            has_fonts = register_fonts()
            bold_f = "DejaVuBold" if has_fonts else "Helvetica-Bold"
            reg_f = "DejaVu" if has_fonts else "Helvetica"

            # Title at top — always single line, auto-shrink font to fit
            title = data.get("title") or data.get("name", "")
            title_size = 6 if height_mm <= 40 else 7.5
            title_y = h - margin
            if title:
                tw_max = w - 2 * margin
                # Shrink font until title fits in one line
                ts = title_size
                while ts > 2.0 and stringWidth(title, bold_f, ts) > tw_max:
                    ts -= 0.2
                c.setFont(bold_f, ts)
                title_y -= ts * 1.2
                c.drawString(margin, title_y, title)
                title_y -= 0.5 * mm

            # Barcode number at bottom
            num_font_size = 6
            num_y = margin
            bc_y = num_y + num_font_size + 1 * mm
            bc_h = title_y - bc_y - 1 * mm
            bc_w = w - 1 * mm

            c.drawImage(img, 0.5 * mm, bc_y, width=bc_w, height=max(bc_h, 5 * mm),
                         preserveAspectRatio=False, mask='auto')

            c.setFont(bold_f, num_font_size)
            num_w = stringWidth(barcode_value, bold_f, num_font_size)
            c.drawString((w - num_w) / 2, num_y, barcode_value)
        return

    # Reserve space for barcode at bottom
    barcode_value, warning = normalize_ean13(data.get("barcode", ""))
    if warning:
        _warn(warning)
    has_barcode = bool(barcode_value)
    bc_zone_h = 7.5 * mm if has_barcode else 0

    text_w = w - 2 * margin
    text_top = h - 0.5 * mm
    text_bottom = margin + bc_zone_h

    # Build the full text first, then auto-size font to fill space
    title = data.get("title", "")

    parts = []

    # Изготовитель / Упаковано + ИНН + адрес
    label_type = data.get("label_type", "packer")
    type_map = {"manufacturer": "Производитель:", "packer": "Упаковано:", "maker": "Изготовитель:"}
    label_prefix = type_map.get(label_type, "Упаковано:")
    legal = data.get("legal_entity", "")
    if legal:
        parts.append(f"{label_prefix} {legal}")

    inn = data.get("inn", "")
    kpp = data.get("kpp", "")
    if inn:
        inn_str = f"ИНН/КПП {inn}/{kpp}" if kpp else f"ИНН: {inn}"
        parts.append(inn_str)

    address = data.get("address", "")
    if address:
        parts.append(address)

    phone = data.get("phone", "")
    if phone:
        parts.append(f"Тел: {phone}")

    # Состав (поддержка обоих ключей: composition и composition_override)
    composition = data.get("composition", "") or data.get("composition_override", "")
    if composition:
        comp = composition.rstrip(". ")
        parts.append(f"Состав: {comp}.")

    # КБЖУ
    kbzhu = data.get("kbzhu")
    if kbzhu:
        p = kbzhu.get("protein", "—")
        f_ = kbzhu.get("fat", "—")
        carbs = kbzhu.get("carbs", "—")
        cal = kbzhu.get("calories", "—")
        kj = data.get("energy_kj", "")
        kj_str = f", {kj} кДж/100г" if kj else ""
        parts.append(f"Пищевая ценность – в 100 г. продукта содержится: белки – {p} г., жиры – {f_} г., углеводы – {carbs} г. Энергетическая ценность – {cal} ккал/100г{kj_str}.")

    # Условия хранения
    storage = data.get("storage_conditions", "")
    if storage:
        parts.append(storage)

    # Срок годности
    shelf_life = data.get("shelf_life", "")
    if shelf_life:
        parts.append(f"Срок годности: {shelf_life}.")

    # Масса нетто
    weight = data.get("weight", "")
    if weight:
        parts.append(f"Масса нетто: {weight}.")

    # Страна
    show_country = data.get("show_country", True)
    country = data.get("country", "")
    if country and show_country is not False:
        parts.append(f"Страна происхождения: {country}.")

    # Join all text with spaces for continuous flow (без даты — она отдельно)
    full_text = " ".join(parts)

    # Дата упаковки — отдельная строка
    show_packing_date = data.get("show_packing_date", True)
    packing_date = data.get("packing_date", "")
    label_type = data.get("label_type", "packer")
    default_date_label = "Дата упаковки" if label_type == "packer" else "Дата изготовления"
    date_label_text = data.get("date_label", default_date_label)
    date_line = f"{date_label_text}: {packing_date}" if (packing_date and show_packing_date is not False) else ""

    def wrap_for_size(text, font_name, fs, max_w):
        words = text.split()
        lines = []
        cur = ""
        for word in words:
            test = f"{cur} {word}".strip()
            if stringWidth(test, font_name, fs) <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def calc_title_height(fs):
        if not title:
            return 0, [], 0
        ts = fs + 1.5
        # Allow title to wrap to multiple lines at large font
        title_lines = wrap_for_size(title, bold_font, ts, text_w)
        # If too many lines, shrink
        while ts > 2.0 and len(title_lines) > 3:
            ts -= 0.2
            title_lines = wrap_for_size(title, bold_font, ts, text_w)
        total_h = len(title_lines) * ts * 1.2 + 0.5 * mm
        return total_h, title_lines, ts

    # Auto-size: find largest font where everything fits
    available_h = text_top - text_bottom
    best_fs = 3.0
    best_lines = []
    best_title_lines = []
    best_title_size = 0

    for fs_10 in range(30, 120):  # 3.0 to 12.0 in 0.1 steps
        fs = fs_10 / 10.0
        lh = fs * 1.2
        th, tl, ts_actual = calc_title_height(fs)
        lines = wrap_for_size(full_text, bold_font, fs, text_w)
        date_extra = lh if date_line else 0
        total_h = th + len(lines) * lh + date_extra
        if total_h <= available_h:
            best_fs = fs
            best_lines = lines
            best_title_lines = tl
            best_title_size = ts_actual
        else:
            break

    font_size = best_fs
    n_title = len(best_title_lines)
    n_body = len(best_lines) + (1 if date_line else 0)

    # Calculate title height
    title_block_h = 0
    if n_title:
        title_block_h = n_title * best_title_size * 1.2 + 0.5 * mm

    # Distribute remaining space as line height so text fills the area
    body_available = available_h - title_block_h
    if n_body > 0:
        line_h = body_available / n_body
        line_h = min(line_h, font_size * 1.15)
    else:
        line_h = font_size * 1.2

    # Font selection: font_bold=True (all bold), False (all regular), "title_only" (bold title, regular body)
    font_bold_val = data.get("font_bold", "title_only")
    use_bold = font_bold_val is True
    use_title_bold = font_bold_val is True or font_bold_val == "title_only"
    body_font = bold_font if use_bold else regular_font
    title_font = bold_font if use_title_bold else regular_font

    # Draw title
    y = text_top
    if best_title_lines:
        c.setFont(title_font, best_title_size)
        if use_title_bold:
            # Use fill+stroke for extra bold effect
            c.setLineWidth(best_title_size * 0.04)
            c.setStrokeColorRGB(0, 0, 0)
        for tl in best_title_lines:
            y -= best_title_size * 1.2
            if use_title_bold:
                # Draw text with stroke for extra thickness
                tp = c.beginText(margin, y)
                tp.setTextRenderMode(2)  # fill + stroke
                tp.textLine(tl)
                c.drawText(tp)
            else:
                c.drawString(margin, y, tl)
        if use_title_bold:
            # Reset render mode
            tp2 = c.beginText(0, 0)
            tp2.setTextRenderMode(0)
            c.drawText(tp2)
        y -= 0.5 * mm

    # Draw text lines with distributed spacing
    c.setFont(body_font, font_size)
    for line in best_lines:
        y -= line_h
        if y < text_bottom - 1:
            break
        c.drawString(margin, y, line)

    # Draw date on a separate line
    if date_line:
        y -= line_h
        if y >= text_bottom - 1:
            c.drawString(margin, y, date_line)

    # Draw barcode at the bottom, centered
    if has_barcode:
        import barcode as bc_lib
        from barcode.writer import ImageWriter
        import io
        from reportlab.lib.utils import ImageReader

        # Generate barcode as image (no text — we draw it ourselves)
        ean = bc_lib.get('ean13', barcode_value, writer=ImageWriter())
        buf = io.BytesIO()
        ean.write(buf, options={
            'write_text': False,
            'module_width': 0.5,
            'module_height': 8,
            'quiet_zone': 1,
        })
        buf.seek(0)
        img = ImageReader(buf)

        # Draw barcode image stretched to full width
        bc_img_h = 7 * mm
        bc_img_w = w - 2 * 0.5 * mm

        num_font_size = 5
        num_y = margin * 0.5
        bc_y = num_y + num_font_size

        c.drawImage(img, 0.5 * mm, bc_y, width=bc_img_w, height=bc_img_h,
                     preserveAspectRatio=False, mask='auto')

        # Draw barcode number centered below
        c.setFont(bold_font, num_font_size)
        num_w = stringWidth(barcode_value, bold_font, num_font_size)
        c.drawString((w - num_w) / 2, num_y, barcode_value)


def generate_label_pdf(data: dict, output_path: str, width_mm: float = 58, height_mm: float = 40):
    """Generate a single-page PDF label. Предупреждения последнего вызова — в `last_warnings`."""
    last_warnings.clear()
    w = width_mm * mm
    h = height_mm * mm
    c = canvas.Canvas(output_path, pagesize=(w, h))
    draw_label(c, data, width_mm, height_mm)
    c.save()
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Генерация этикетки товара в PDF")
    parser.add_argument("--data", required=True, help="JSON-файл или JSON-строка с данными этикетки")
    parser.add_argument("--output", required=True, help="Путь для сохранения PDF")
    parser.add_argument("--width", type=float, default=58, help="Ширина этикетки в мм (по умолчанию 58)")
    parser.add_argument("--height", type=float, default=40, help="Высота этикетки в мм (по умолчанию 40)")

    args = parser.parse_args()

    if os.path.isfile(args.data):
        with open(args.data) as f:
            data = json.load(f)
    else:
        data = json.loads(args.data)

    path = generate_label_pdf(data, args.output, args.width, args.height)
    for w in last_warnings:
        print(f"⚠️ {w}", file=sys.stderr)
    print(f"Этикетка сохранена: {path}")


if __name__ == "__main__":
    main()
