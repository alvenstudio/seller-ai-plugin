#!/usr/bin/env python3
"""
Локальные настройки этикеток: реквизиты продавца и упаковщиков, данные товаров, которых нет в Seller AI.

Файл — `labels-config.json` в рабочей папке пользователя (в git не попадает: он в .gitignore). Его заполняет пользователь
(или агент с его слов и после его «да»). Образец — `references/labels-config.example.json`.

    {
      "legal_entities": [{"legal_entity": "ИП …", "inn": "…", "kpp": null, "address": "…", "phone": "…"}],
      "stores": {"<store_id>": {"owner": "ИП …"}},
      "products": {"<store_id>": {"<sku>": {"label_type": "packer", "title": "…", "composition": "…", …}}}
    }

    label_config.py list --store <store_id>
    label_config.py get --store <store_id> --sku <sku>
    label_config.py set --store <store_id> --sku <sku> --label-type manufacturer --title "ПАСТИЛА ФРУКТОВАЯ"
    label_config.py set-entity --legal-entity "ИП Иванов И. И." --inn 123456789012 --address "…" [--owner-of <store_id>]
    label_config.py remove --store <store_id> --sku <sku>
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_CONFIG = Path.cwd() / "labels-config.json"

LABEL_TYPES = ("packer", "manufacturer", "maker", "barcode_only")
PRODUCT_DEFAULTS = {
    "label_type": "packer",          # packer «Упаковано» | manufacturer «Производитель» | maker «Изготовитель» | barcode_only
    "packer": None,                  # юрлицо из legal_entities (подстрока названия); по умолчанию — владелец магазина
    "title": None,
    "barcode": None,
    "weight_text": None,
    "composition": None,
    "kbzhu": None,                   # {"protein": "…", "fat": "…", "carbs": "…", "calories": "…"}
    "shelf_life": None,
    "storage_conditions": None,
    "country": None,
    "show_country": True,
    "show_packing_date": True,
}
ENTITY_FIELDS = ("legal_entity", "inn", "kpp", "address", "phone")


def load(path: Path = DEFAULT_CONFIG) -> dict:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault("legal_entities", [])
    data.setdefault("stores", {})
    data.setdefault("products", {})
    return data


def save(data: dict, path: Path = DEFAULT_CONFIG):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def product_config(data: dict, store_id: str, sku: str) -> dict:
    return {**PRODUCT_DEFAULTS, **data["products"].get(str(store_id), {}).get(str(sku), {})}


def find_entities(data: dict, query: str) -> list[dict]:
    q = (query or "").lower()
    return [e for e in data["legal_entities"] if q and q in (e.get("legal_entity") or "").lower()]


def seller_entity(answer: dict | None) -> dict:
    """Ответ `stores_seller_info_get` → {"legal_entity", "inn"} владельца магазина; {} — Seller AI их не прочитал."""
    answer = answer or {}
    company = answer.get("company") or (answer.get("item") or {}).get("company") or {}
    if not company.get("legal_name"):
        return {}
    return {"legal_entity": company["legal_name"], "inn": company.get("inn") or ""}


def entity_for(data: dict, store_id: str, packer: str | None = None,
               seller: dict | None = None) -> tuple[dict, str | None]:
    """Юрлицо для этикетки → (реквизиты, предупреждение). packer — подстрока названия; иначе владелец магазина:
    из labels-config.json (`stores.<store_id>.owner`), а без него — юрлицо и ИНН из Seller AI (`seller_entity()`)."""
    query = packer or (data["stores"].get(str(store_id)) or {}).get("owner")
    if not query:
        if seller:
            return ({**{k: "" for k in ENTITY_FIELDS}, **seller},
                    f"владелец — из Seller AI: {seller['legal_entity']}, ИНН {seller['inn'] or '—'}; адреса и телефона "
                    f"там нет — спросить и сохранить: label_config.py set-entity … --owner-of {store_id}")
        return {}, f"в labels-config.json нет владельца магазина {store_id} (stores.{store_id}.owner) — реквизиты пустые"
    hits = find_entities(data, query)
    if len(hits) != 1:
        names = [e.get("legal_entity") for e in hits]
        return {}, f"юрлицо «{query}»: найдено {len(hits)} {names} — уточните (реквизиты не выдумывать)"
    entity = {k: hits[0].get(k) or "" for k in ENTITY_FIELDS}
    if not packer and seller and seller.get("inn") and entity["inn"] and entity["inn"] != seller["inn"]:
        return entity, (f"ИНН владельца в labels-config.json {entity['inn']}, а у продавца в Ozon {seller['inn']} "
                        f"({seller['legal_entity']}) — уточнить, чьи реквизиты ставить")
    return entity, None


def set_product(data: dict, store_id: str, sku: str, **fields) -> dict:
    cur = data["products"].setdefault(str(store_id), {}).setdefault(str(sku), {})
    for k, v in fields.items():
        if v is None:
            continue
        if v == "__DELETE__":
            cur.pop(k, None)
        else:
            cur[k] = v
    return cur


def set_entity(data: dict, entity: dict, owner_of: str | None = None) -> dict:
    name = entity["legal_entity"]
    for e in data["legal_entities"]:
        if e.get("legal_entity") == name:
            e.update({k: v for k, v in entity.items() if v is not None})
            break
    else:
        data["legal_entities"].append({k: entity.get(k) for k in ENTITY_FIELDS})
    if owner_of:
        data["stores"].setdefault(str(owner_of), {})["owner"] = name
    return entity


def build_label_data(cfg: dict, product: dict, entity: dict) -> dict:
    """Данные для generate_label_pdf. Приоритет: настройки товара > данные Seller AI (`product`); реквизиты — `entity`.

    product: {"name", "barcode", "weight"} из products_get (см. make_label.py).
    """
    data = {"label_type": cfg.get("label_type") or "packer"}
    if cfg.get("title"):
        data["title"] = cfg["title"]
    if product.get("name"):
        data["name"] = product["name"]
    for k in ENTITY_FIELDS:
        data[k] = entity.get(k, "")
    data["composition"] = cfg.get("composition") or ""
    if cfg.get("kbzhu"):
        data["kbzhu"] = cfg["kbzhu"]
    for k in ("shelf_life", "storage_conditions", "country"):
        if cfg.get(k):
            data[k] = cfg[k]
    data["weight"] = cfg.get("weight_text") or product.get("weight", "")
    data["barcode"] = cfg.get("barcode") or product.get("barcode", "")
    data["show_country"] = cfg.get("show_country", True)
    data["show_packing_date"] = cfg.get("show_packing_date", True)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="Настройки этикеток (labels-config.json)")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list"); p.add_argument("--store", required=True)
    p = sub.add_parser("get"); p.add_argument("--store", required=True); p.add_argument("--sku", required=True)
    p = sub.add_parser("remove"); p.add_argument("--store", required=True); p.add_argument("--sku", required=True)
    p = sub.add_parser("set")
    p.add_argument("--store", required=True); p.add_argument("--sku", required=True)
    p.add_argument("--label-type", choices=LABEL_TYPES)
    for name in ("packer", "title", "barcode", "weight-text", "composition", "shelf-life", "storage-conditions", "country"):
        p.add_argument(f"--{name}")
    p.add_argument("--kbzhu", help='JSON: {"protein": "…", "fat": "…", "carbs": "…", "calories": "…"}')
    p.add_argument("--show-country", choices=["true", "false"])
    p.add_argument("--show-packing-date", choices=["true", "false"])
    p = sub.add_parser("set-entity")
    for name in ENTITY_FIELDS:
        p.add_argument(f"--{name.replace('_', '-')}", required=name == "legal_entity")
    p.add_argument("--owner-of", help="store_id: это юрлицо — владелец магазина")
    a = ap.parse_args(argv)

    data = load(a.config)
    if a.command == "list":
        items = data["products"].get(a.store, {})
        owner = (data["stores"].get(a.store) or {}).get("owner")
        print(f"владелец магазина: {owner or '— не задан'}")
        for sku, c in items.items():
            print(f"  {sku}: {c.get('label_type', 'packer')} | {c.get('title') or ''} | {c.get('packer') or 'владелец'}")
    elif a.command == "get":
        print(json.dumps(product_config(data, a.store, a.sku), ensure_ascii=False, indent=2))
    elif a.command == "remove":
        gone = data["products"].get(a.store, {}).pop(a.sku, None)
        save(data, a.config)
        print("удалено" if gone is not None else "не найдено")
    elif a.command == "set":
        fields = {"label_type": a.label_type, "packer": a.packer, "title": a.title, "barcode": a.barcode,
                  "weight_text": a.weight_text, "composition": a.composition, "shelf_life": a.shelf_life,
                  "storage_conditions": a.storage_conditions, "country": a.country,
                  "kbzhu": json.loads(a.kbzhu) if a.kbzhu else None,
                  "show_country": None if a.show_country is None else a.show_country == "true",
                  "show_packing_date": None if a.show_packing_date is None else a.show_packing_date == "true"}
        print(json.dumps(set_product(data, a.store, a.sku, **fields), ensure_ascii=False, indent=2))
        save(data, a.config)
    elif a.command == "set-entity":
        entity = {k: getattr(a, k) for k in ENTITY_FIELDS}
        set_entity(data, entity, a.owner_of)
        save(data, a.config)
        print(f"сохранено: {entity['legal_entity']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
