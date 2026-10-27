#!/usr/bin/env python3
"""
Товарная этикетка одной командой: ответ `products_get` (сохранённый агентом в файл) + labels-config.json → PDF и PNG.

Сеть не нужна: данные карточки агент берёт инструментом Seller AI `products_get`, реквизиты и состав — из локального
labels-config.json, значения из чата — флагами.

    make_label.py --product product.json --store <store_id> --title "КУРАГА БЕЗ САХАРА" --packing-date 01.10.2026
    make_label.py --product product.json --store <store_id> --packer "Иванов" --no-title
    make_label.py --product product.json --store <store_id> --packer-name "ИП Иванов И. И." --inn … --address …

Приоритет: флаги > настройки товара в labels-config.json > карточка (`products_get`).
Предупреждения (печатаются строками «⚠️ …») пользователю называть: какой штрихкод взят, чего не хватает, что не выведено.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ean13 import normalize_ean13                                         # noqa: E402
from label_config import DEFAULT_CONFIG, build_label_data, entity_for, load, product_config  # noqa: E402

WEIGHT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|г|гр|грамм|мл|л)\b", re.I)


def weight_from_name(name: str) -> str:
    """Масса из названия карточки («Курага 400 г» → «400 г»); вес из габаритов — брутто, на этикетку не идёт."""
    m = WEIGHT.search(name or "")
    if not m:
        return ""
    unit = {"гр": "г", "грамм": "г"}.get(m.group(2).lower(), m.group(2).lower())
    return f"{m.group(1).replace('.', ',')} {unit}"


def product_data(answer: dict) -> tuple[str, dict, list[str], list[str]]:
    """Ответ products_get → (sku, данные для этикетки, все штрихкоды, предупреждения)."""
    item = answer.get("item") or answer
    barcodes = [str(b) for b in item.get("barcodes") or []]
    warns = []
    usable = []
    for b in barcodes:
        code, w = normalize_ean13(b)
        if code:
            usable.append(code)
        elif w:
            warns.append(f"в карточке {w}")
    if len(usable) > 1:
        warns.append(f"в карточке несколько штрихкодов EAN-13 {usable}, взят {usable[0]}")
    data = {"name": item.get("name") or "", "barcode": usable[0] if usable else "",
            "weight": weight_from_name(item.get("name") or "")}
    return str(item.get("sku") or ""), data, barcodes, warns


def main(argv=None):
    ap = argparse.ArgumentParser(description="Товарная этикетка по ответу products_get")
    ap.add_argument("--product", required=True, help="JSON — ответ products_get")
    ap.add_argument("--store", required=True, help="store_id Seller AI")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--title", help="заголовок (короткое название КАПСОМ)")
    ap.add_argument("--no-title", action="store_true")
    ap.add_argument("--label-type", choices=["packer", "manufacturer", "maker", "barcode_only"])
    ap.add_argument("--packer", help="юрлицо из labels-config.json по подстроке названия (фамилия)")
    ap.add_argument("--packer-name", help="юрлицо вручную (его нет в labels-config.json)")
    ap.add_argument("--inn"); ap.add_argument("--kpp"); ap.add_argument("--address"); ap.add_argument("--phone")
    ap.add_argument("--packing-date", help="ДД.ММ.ГГГГ")
    ap.add_argument("--shelf-life"); ap.add_argument("--storage"); ap.add_argument("--composition")
    ap.add_argument("--country"); ap.add_argument("--weight", help="«400 г», «0,45 кг ± 0,02 кг»")
    ap.add_argument("--barcode", help="EAN-13 (или 12 цифр без контрольной) вместо кода карточки")
    ap.add_argument("--no-barcode", action="store_true", help="этикетка без штрихкода")
    ap.add_argument("--hide-country", action="store_true")
    ap.add_argument("--size", default="58x40", help="ШxВ мм: 58x40 или 58x60")
    ap.add_argument("--out", help="PDF (по умолчанию «Этикетка <артикул>.pdf»)")
    ap.add_argument("--no-png", action="store_true", help="не рендерить PNG для проверки")
    ap.add_argument("--json", action="store_true", help="напечатать итоговые данные этикетки")
    a = ap.parse_args(argv)

    answer = json.loads(Path(a.product).read_text(encoding="utf-8"))
    sku, prod, _, warns = product_data(answer)
    cfg_all = load(a.config)
    cfg = product_config(cfg_all, a.store, sku)

    if a.packer_name:
        entity = {"legal_entity": a.packer_name, "inn": "", "kpp": "", "address": "", "phone": ""}
    else:
        entity, w = entity_for(cfg_all, a.store, a.packer or cfg.get("packer"))
        if w:
            warns.append(w)

    d = build_label_data(cfg, prod, entity)
    for k, v in (("inn", a.inn), ("kpp", a.kpp), ("address", a.address), ("phone", a.phone),
                 ("packing_date", a.packing_date), ("shelf_life", a.shelf_life), ("storage_conditions", a.storage),
                 ("composition", a.composition), ("country", a.country), ("weight", a.weight),
                 ("barcode", a.barcode), ("label_type", a.label_type)):
        if v:
            d[k] = v
    if a.no_title:
        d["title"] = ""; d["name"] = ""          # пустой title падает обратно на name
    elif a.title:
        d["title"] = a.title
    if a.hide_country:
        d["show_country"] = False
    if a.no_barcode:
        d["barcode"] = ""

    if d["label_type"] != "barcode_only":
        for k, what in (("composition", "состав"), ("kbzhu", "КБЖУ"), ("shelf_life", "срок годности"),
                        ("storage_conditions", "условия хранения"), ("weight", "масса нетто")):
            if not d.get(k):
                warns.append(f"нет: {what} — в Seller AI характеристик карточки нет, взять у пользователя "
                             f"(флагом или в labels-config.json)")
        for k in ("legal_entity", "inn", "address"):
            if not d.get(k):
                warns.append(f"пустое поле {k}")
        if not d.get("country") and d.get("show_country", True):
            warns.append("страна не задана — строка страны не выведена")
    if not d.get("barcode") and not a.no_barcode:
        warns.append("нет EAN-13: передайте --barcode (новый код к карточке в Seller AI пока не привязать)")

    w_mm, h_mm = (float(x) for x in a.size.lower().split("x"))
    offer = (answer.get("item") or answer).get("offer_id") or sku or "товар"
    out = a.out or f"Этикетка {offer}.pdf"
    import generate_label                    # reportlab нужен только для рендера
    generate_label.generate_label_pdf(d, out, width_mm=w_mm, height_mm=h_mm)
    warns += generate_label.last_warnings
    if not a.no_png:
        subprocess.run(["pdftoppm", "-png", "-r", "250", "-singlefile", out, out[:-4]], check=False)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    for x in warns:
        print("⚠️", x)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
