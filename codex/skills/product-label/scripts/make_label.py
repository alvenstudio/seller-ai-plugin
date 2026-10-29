#!/usr/bin/env python3
"""
Товарная этикетка одной командой: ответ `products_get` (сохранённый агентом в файл) + labels-config.json → PDF и PNG.

Сеть не нужна: данные карточки агент берёт инструментами Seller AI `products_get` и `products_content_get`, реквизиты —
из локального labels-config.json, а юрлицо и ИНН владельца без него — из ответа `stores_seller_info_get`
(`--seller-info`), значения из чата — флагами. Характеристики карточки (состав, КБЖУ, срок, хранение,
страна, масса) — ответ `products_content_get`, сохранённый в файл, или любой список в формате Ozon (`attributes[]`: `id`
или `attribute_id`, `values[].value`): `--attributes` берёт их сами — состав 8050, белки 7584, жиры 7585, углеводы 7586,
ккал 7609, срок 7578 (дни → «12 месяцев»), хранение 8787, страна 4389, масса 4383 (только сверка с названием).

    make_label.py --product product.json --store <store_id> --title "КУРАГА БЕЗ САХАРА" --packing-date 01.10.2026
    make_label.py --product product.json --attributes content.json --store <store_id> --packer "Иванов" --no-title
    make_label.py --product product.json --store <store_id> --packer-name "ИП Иванов И. И." --inn … --address …

Приоритет: флаги > характеристики карточки (`--attributes`) > настройки товара в labels-config.json > карточка
(`products_get`).
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
from label_config import DEFAULT_CONFIG, build_label_data, entity_for, load, product_config, seller_entity  # noqa: E402

WEIGHT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|г|гр|грамм|мл|л)\b", re.I)
# Характеристики Ozon, которые идут на этикетку (id атрибута).
ATTR = {"composition": 8050, "protein": 7584, "fat": 7585, "carbs": 7586, "calories": 7609, "shelf_days": 7578,
        "storage": 8787, "country": 4389, "weight": 4383}


def shelf_text(days) -> str:
    """Срок годности из дней характеристики: 365 → «1 год», 360 → «12 месяцев», 45 → «45 дней»."""
    try:
        d = int(float(str(days).replace(",", ".")))
    except (TypeError, ValueError):
        return str(days or "")
    if d and d % 365 == 0:
        y = d // 365
        return f"{y} год" if y == 1 else (f"{y} года" if y in (2, 3, 4) else f"{y} лет")
    if d and d % 30 == 0:
        m = d // 30
        return f"{m} месяц" if m == 1 else (f"{m} месяца" if m in (2, 3, 4) else f"{m} месяцев")
    return f"{d} дней"


def attributes_data(answer) -> dict:
    """Характеристики карточки (список attributes в формате Ozon) → поля этикетки; чего нет — поля нет."""
    attrs = answer
    if isinstance(answer, dict):
        attrs = answer.get("attributes")
        for key in ("item", "result", "items"):
            inner = answer.get(key)
            if attrs is None and isinstance(inner, list) and inner:
                attrs = inner[0].get("attributes")
            elif attrs is None and isinstance(inner, dict):
                attrs = inner.get("attributes")
    vals = {}
    for a in attrs or []:
        aid = a.get("id", a.get("attribute_id"))
        got = [" ".join(str(v.get("value")).split()) for v in a.get("values") or [] if v.get("value") not in (None, "")]
        if aid is not None and got:
            vals[int(aid)] = got
    one = lambda k: (vals.get(ATTR[k]) or [""])[0]  # noqa: E731
    out = {}
    comp = one("composition")
    for pre in ("Состав:", "состав:", "СОСТАВ:"):
        if comp.startswith(pre):
            comp = comp[len(pre):].strip()
    if comp:
        out["composition"] = comp.rstrip(".") + "."
    kbzhu = {k: one(k) for k in ("protein", "fat", "carbs", "calories")}
    if any(kbzhu.values()):
        out["kbzhu"] = {k: v or "—" for k, v in kbzhu.items()}
    if one("shelf_days"):
        out["shelf_life"] = shelf_text(one("shelf_days"))
    if one("storage"):
        out["storage_conditions"] = one("storage")
    if vals.get(ATTR["country"]):
        out["country"] = ", ".join(vals[ATTR["country"]])
    if one("weight"):
        out["weight_attr"] = one("weight")
    return out


def grams(text: str):
    """«400 г» → 400.0, «1,5 кг» → 1500.0; объём и пустое — None."""
    m = WEIGHT.search(text or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    return v * 1000 if unit == "кг" else (v if unit in ("г", "гр", "грамм") else None)


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


CARD_FIELDS = {"composition": ("состав", "--composition"), "kbzhu": ("КБЖУ", "--kbzhu"),
               "shelf_life": ("срок годности", "--shelf-life"), "storage_conditions": ("условия хранения", "--storage"),
               "country": ("страна", "--country")}
KBZHU = {"protein": "белки", "fat": "жиры", "carbs": "углеводы", "calories": "ккал"}


def shown(value) -> str:
    """Значение поля для предупреждения: КБЖУ — «белки 5.2, жиры 0.3, …»."""
    if isinstance(value, dict):
        return ", ".join(f"{KBZHU.get(k, k)} {v}" for k, v in value.items())
    return str(value)


def apply_card(d: dict, card: dict, name_weight: str) -> list[str]:
    """Характеристики карточки поверх настроек товара; расхождение — предупреждение с обоими значениями. Масса — из
    названия."""
    warns = []
    for k, (what, flag) in CARD_FIELDS.items():
        if card.get(k):
            if d.get(k) and d[k] != card[k]:
                warns.append(f"{what}: в карточке «{shown(card[k])}» — взято; в labels-config.json «{shown(d[k])}» — "
                             f"спросить, какое оставить (сохранённое — флагом {flag})")
            d[k] = card[k]
    attr_g, name_g = grams(f"{card.get('weight_attr', '')} г"), grams(name_weight)
    if attr_g is not None and name_g is not None and abs(attr_g - name_g) > 0.001:
        warns.append(f"масса в характеристиках {card['weight_attr']} г, в названии {name_weight} — на этикетке из "
                     f"названия; расхождение назвать (бывает «техническая» масса против дублей)")
    return warns


def main(argv=None):
    ap = argparse.ArgumentParser(description="Товарная этикетка по ответу products_get")
    ap.add_argument("--product", required=True, help="JSON — ответ products_get")
    ap.add_argument("--attributes", help="JSON — ответ products_content_get или характеристики в формате Ozon (attributes[])")
    ap.add_argument("--seller-info", help="JSON — ответ stores_seller_info_get: юрлицо и ИНН владельца по умолчанию")
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
    ap.add_argument("--kbzhu", help='JSON {"protein": "…", "fat": "…", "carbs": "…", "calories": "…"}')
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

    seller = {}
    if a.seller_info:
        seller = seller_entity(json.loads(Path(a.seller_info).read_text(encoding="utf-8")))
        if not seller:
            warns.append("в ответе stores_seller_info_get нет юрлица: Seller AI ещё не прочитал реквизиты продавца")
    if a.packer_name:
        entity = {"legal_entity": a.packer_name, "inn": "", "kpp": "", "address": "", "phone": ""}
    else:
        entity, w = entity_for(cfg_all, a.store, a.packer or cfg.get("packer"), seller)
        if w:
            warns.append(w)

    d = build_label_data(cfg, prod, entity)
    if a.attributes:
        warns += apply_card(d, attributes_data(json.loads(Path(a.attributes).read_text(encoding="utf-8"))),
                            prod.get("weight", ""))
    for k, v in (("inn", a.inn), ("kpp", a.kpp), ("address", a.address), ("phone", a.phone),
                 ("packing_date", a.packing_date), ("shelf_life", a.shelf_life), ("storage_conditions", a.storage),
                 ("composition", a.composition), ("country", a.country), ("weight", a.weight),
                 ("barcode", a.barcode), ("label_type", a.label_type)):
        if v:
            d[k] = v
    if a.kbzhu:
        d["kbzhu"] = json.loads(a.kbzhu)
    if a.no_title:
        d["title"] = ""; d["name"] = ""          # пустой title падает обратно на name
    elif a.title:
        d["title"] = a.title
    if a.hide_country:
        d["show_country"] = False
    if a.no_barcode:
        d["barcode"] = ""

    if not a.no_title and not d.get("title"):
        lost = ("сверху будет полное название карточки мелким шрифтом" if d["label_type"] == "barcode_only"
                else "названия товара на этикетке не будет")
        warns.append(f"заголовок не задан — {lost}: по умолчанию — короткое название КАПСОМ из названия карточки, "
                     f"--title «…»")
    if d["label_type"] != "barcode_only":
        for k, what in (("composition", "состав"), ("kbzhu", "КБЖУ"), ("shelf_life", "срок годности"),
                        ("storage_conditions", "условия хранения")):
            if not d.get(k):
                warns.append(f"нет: {what} — " + ("ни в характеристиках карточки, ни в labels-config.json: взять у "
                                                  "пользователя" if a.attributes else
                                                  "передайте --attributes (ответ products_content_get) или возьмите у "
                                                  "пользователя"))
        if not d.get("weight"):
            warns.append("нет: масса нетто — в названии карточки её нет: --weight со слов пользователя")
        for k in ("legal_entity", "inn", "address"):
            if not d.get(k):
                warns.append(f"пустое поле {k}")
        if not d.get("country") and d.get("show_country", True):
            warns.append("страна не задана — строка страны не выведена")
    if not d.get("barcode") and not a.no_barcode:
        warns.append("нет EAN-13: новый код — ean13.py new, привязать к карточке products_barcodes_add "
                     "(или --barcode)")

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
