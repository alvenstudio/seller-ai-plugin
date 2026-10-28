#!/usr/bin/env python3
"""Плановая юнит-экономика товара, которого ещё нет в магазине (нет карточки в Ozon).

Для карточек магазина план считает Seller AI (unit_economics_plan_list / _preview). Этот скрипт нужен только для нового
товара: он берёт тарифы логистики FBO из справочника Ozon (references/logistika-fbo-fbs.xlsx), фиксированные доли рынка
кластеров и считает строку той же формулой v4, что Seller AI (references/plan-formula.schema.json), — ничего не запрашивая.

    python3 new_product_plan.py --inputs product.json [--plan plan.json] [--settings settings.json] \
        [--formula formula.json] [--clusters "Москва, МО и Дальние регионы" "Казань" | --ideal] [--json]

product.json — то, что известно о товаре: price, cost, volume_liters, drr (доля), commission_micro / _low / _high (доли из
references/commissions-*.tsv навыка category-commissions или stores_commission_rules_list), при желании любые другие входы
формулы (buyout, last_mile, supply_cost …).
--plan      ответ unit_economics_plan_list магазина: входы, которых нет в product.json, берутся медианой по строкам магазина
            (как значение магазина в Seller AI);
--settings  ответ stores_economics_settings_get: налог, база, НДС, выкуп по умолчанию, прочие расходы, звёздные, досрочная;
--formula   formula_schema из ответа unit_economics_plan_list (по умолчанию — копия в references);
--clusters  кластеры, где будет лежать товар (по умолчанию — идеальная раскладка: товар в каждом кластере).
"""
import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REFERENCES = Path(__file__).resolve().parent.parent / "references"
TARIFFS_XLSX = REFERENCES / "logistika-fbo-fbs.xlsx"
MARKET_SHARES = REFERENCES / "cluster_market_share_defaults.json"
FORMULA = REFERENCES / "plan-formula.schema.json"

# Ozon переименовывает листы от версии к версии файла: до 28.08.2026 «Тарифы на логистику» / «Универсальные тарифы»,
# с 28.08.2026 «Логистика РФ» / «Тарифы по умолчанию». Порядок столбцов: объём, откуда, куда, тариф до 300 ₽, от 300 ₽.
TARIFF_SHEETS = ("Логистика РФ", "Тарифы на логистику")
DEFAULT_SHEETS = ("Тарифы по умолчанию", "Универсальные тарифы")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

# Значения по умолчанию плана Seller AI, когда у магазина нет своей истории (economics-v2 §6.3).
DEFAULTS = {
    "drr": 0.0, "other_unit_cost": 0.0, "commission_rate_manual": None, "last_mile": 25.0, "return_processing": 15.0,
    "buyout": 0.93, "cancel_before_ship": 0.0, "disposal_share": 0.0, "disposal_fee": 75.0, "supply_cost": 0.0,
    "placement": 0.0, "acquiring_rate": 0.01, "stars_rate": 0.0, "insurance_rate": 0.0, "other_ozon_fees": 0.0,
    "early_payout_rate": 0.0, "points_share": 0.0, "tax_rate": None, "vat_rate": 0.0, "tax_on_sales": 0,
    "tax_on_profit": 0,
}
# Входы, которые разумно брать значением магазина (медиана строк его плана).
STORE_PRIOR_INPUTS = ("buyout", "cancel_before_ship", "last_mile", "return_processing", "acquiring_rate", "points_share",
                      "disposal_share", "disposal_fee", "stars_rate", "early_payout_rate", "insurance_rate",
                      "other_ozon_fees", "supply_cost")


# ── справочник тарифов ────────────────────────────────────────────────

def _sheet_rows(path, names):
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iterfind(".//m:t", NS)) for si in sst.findall("m:si", NS)]
        sheet = None
        for s in wb.find("m:sheets", NS):
            if s.attrib["name"] in names:
                target = targets[s.attrib[REL]]
                target = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
                sheet = ET.fromstring(z.read(target))
                break
        if sheet is None:
            raise RuntimeError(f"лист {names} не найден в {Path(path).name}")

        def value(c):
            v = c.find("m:v", NS)
            if v is None:
                return ""
            return shared[int(v.text)] if c.attrib.get("t") == "s" else (v.text or "")

        return [[value(c) for c in row.findall("m:c", NS)] for row in sheet.find("m:sheetData", NS).findall("m:row", NS)]


def _float(text):
    return float(str(text).replace(",", ".").replace(" ", "").replace(" ", ""))


def load_tariffs(path=TARIFFS_XLSX):
    """(матрица {(объём, откуда, куда, cheap|expensive): ₽}, порядок диапазонов, тарифы по умолчанию {(объём, бакет): ₽})."""
    matrix, order = {}, []
    for vals in _sheet_rows(path, TARIFF_SHEETS):
        if len(vals) < 5:
            continue
        vol, src, dst, cheap, expensive = (v.strip() for v in vals[:5])
        if not vol or vol == "Объём товара" or src.startswith("Кластер"):
            continue
        if vol not in order:
            order.append(vol)
        if cheap:
            matrix[(vol, src, dst, "cheap")] = _float(cheap)
        if expensive:
            matrix[(vol, src, dst, "expensive")] = _float(expensive)
    defaults = {}
    for vals in _sheet_rows(path, DEFAULT_SHEETS):
        if len(vals) < 3:
            continue
        vol, cheap, expensive = (v.strip() for v in vals[:3])
        if not vol or vol == "Объём товара":
            continue
        if cheap:
            defaults[(vol, "cheap")] = _float(cheap)
        if expensive:
            defaults[(vol, "expensive")] = _float(expensive)
    return matrix, order, defaults


def _range(label):
    text = label.replace(" л", "").replace(" ", "")
    if text.startswith("От"):
        return float(text.replace("От", "").replace(",", ".")), float("inf")
    left, right = text.split("-")
    return float(left.replace(",", ".")), float(right.replace(",", "."))


def volume_bucket(volume_liters, order):
    """Первый диапазон, верхняя граница которого не меньше объёма: «X,001 – Y» Ozon по смыслу непрерывны,
    а объём из габаритов часто попадает в микро-зазор между ними (1,00009 л)."""
    for (_low, high), label in sorted(((_range(lbl), lbl) for lbl in order), key=lambda x: x[0][0]):
        if volume_liters <= high:
            return label
    raise ValueError(f"нет диапазона объёма для {volume_liters} л")


def cluster_logistics(volume_liters, source_clusters, tariffs, shares, bucket):
    """Средний тариф FBO по долям рынка кластеров доставки: локальная продажа — тариф кластера в себя,
    иначе самый дешёвый маршрут из кластеров с товаром, маршрута нет в матрице — тариф по умолчанию объёма."""
    matrix, order, defaults = tariffs
    vol = volume_bucket(volume_liters, order)
    sources = [c.strip() for c in source_clusters]
    details, total = [], 0.0
    for dst, share in shares.items():
        local = matrix.get((vol, dst, dst, bucket)) if dst in sources else None
        if local is not None:
            src, tariff, how = dst, local, "local"
        else:
            options = [(matrix[(vol, s, dst, bucket)], s) for s in sources if (vol, s, dst, bucket) in matrix]
            if options:
                tariff, src = min(options)
                how = "nonlocal"
            else:
                tariff, src, how = defaults.get((vol, bucket)), None, "default"
        if tariff is None:
            raise ValueError(f"нет тарифа для {dst} ({vol}, {bucket})")
        total += tariff * share
        details.append({"destination_cluster": dst, "share": share, "source_cluster": src, "route": how,
                        "tariff": round(tariff, 2)})
    return {"volume_bucket": vol, "average": round(total, 2), "details": details}


# ── формула плана v4 (AST из plan-formula.schema.json) ────────────────

def evaluate(ast, env):
    """Правила AST контракта: null в арифметике — null, сравнение с null — false, if ленивый, деление на 0 — null."""
    if ast is None or isinstance(ast, bool):
        return ast
    if isinstance(ast, (int, float)):
        return float(ast)
    if isinstance(ast, str):
        value = env.get(ast)
        return None if value is None else float(value)
    op, args = ast["op"], ast["args"]
    if op == "if":
        return evaluate(args[1], env) if evaluate(args[0], env) else evaluate(args[2], env)
    if op == "coalesce":
        for a in args:
            v = evaluate(a, env)
            if v is not None:
                return v
        return None
    values = [evaluate(a, env) for a in args]
    if op in ("lt", "lte", "eq"):
        a, b = values
        if a is None or b is None:
            return False
        return a < b if op == "lt" else a <= b if op == "lte" else a == b
    if any(v is None for v in values):
        return None
    if op == "add":
        return sum(values)
    if op == "sub":
        return values[0] - values[1]
    if op == "mul":
        out = 1.0
        for v in values:
            out *= v
        return out
    if op == "div":
        return None if values[1] == 0 else values[0] / values[1]
    raise ValueError(f"неизвестная операция формулы: {op}")


def compute(formula, inputs):
    env = dict(inputs)
    computed = {}
    for step in formula["steps"]:
        value = evaluate(step["ast"], env)
        env[step["key"]] = value
        computed[step["key"]] = value
    return computed


# ── сборка входов ─────────────────────────────────────────────────────

def store_priors(plan):
    """Медиана входов по строкам плана магазина — значение магазина для нового товара."""
    out = {}
    rows = (plan or {}).get("items") or []
    for key in STORE_PRIOR_INPUTS:
        vals = [float(r["inputs"][key]) for r in rows if (r.get("inputs") or {}).get(key) is not None]
        if vals:
            out[key] = statistics.median(vals)
    return out


def settings_inputs(settings):
    if not settings:
        return {}
    out = {}
    if settings.get("default_buyout") is not None:
        out["buyout"] = float(settings["default_buyout"])
    for src, dst in (("tax_rate", "tax_rate"), ("vat_rate", "vat_rate"), ("other_unit_cost", "other_unit_cost"),
                     ("stars_rate", "stars_rate"), ("early_payout_rate", "early_payout_rate")):
        if settings.get(src) is not None:
            out[dst] = float(settings[src])
    base = settings.get("tax_base") or "revenue"
    out["tax_on_sales"] = 1 if base == "sales" else 0
    out["tax_on_profit"] = 1 if base == "profit" else 0
    return out


def build_inputs(product, plan=None, settings=None, clusters=None, tariffs=None, shares=None):
    """Входы формулы и откуда каждый: product, иначе медиана плана магазина, иначе его настройки, иначе умолчания Seller AI."""
    inputs, sources = dict(DEFAULTS), {k: "default" for k in DEFAULTS}
    for layer, name in ((settings_inputs(settings), "store_setting"), (store_priors(plan), "store_prior"),
                        ({k: v for k, v in product.items() if k != "volume_liters"}, "product")):
        for k, v in layer.items():
            inputs[k] = v
            sources[k] = name
    logistics = None
    need = [k for k in ("logistics_lt_300", "logistics_gte_300") if k not in product]
    if need:
        if product.get("volume_liters") is None:
            raise ValueError("нужен volume_liters (или logistics_lt_300 и logistics_gte_300)")
        source = clusters if clusters else list(shares)
        logistics = {"clusters": "ideal" if not clusters else source}
        for key, bucket in (("logistics_lt_300", "cheap"), ("logistics_gte_300", "expensive")):
            res = cluster_logistics(float(product["volume_liters"]), source, tariffs, shares, bucket)
            logistics[key] = res
            if key in need:
                inputs[key] = res["average"]
                sources[key] = "tariff_model"
        if clusters:
            ideal = cluster_logistics(float(product["volume_liters"]), list(shares), tariffs, shares,
                                      "cheap" if float(product.get("price") or 0) <= 300 else "expensive")
            bucket_key = "logistics_lt_300" if float(product.get("price") or 0) <= 300 else "logistics_gte_300"
            logistics["overpay"] = round(logistics[bucket_key]["average"] - ideal["average"], 2)
    if "reverse_logistics" not in product:
        price = product.get("price")
        key = "logistics_lt_300" if price is not None and float(price) <= 300 else "logistics_gte_300"
        inputs["reverse_logistics"] = inputs.get(key)
        sources["reverse_logistics"] = "tariff_model"
    missing = [k for k in ("price", "cost", "commission_micro", "commission_low", "commission_high", "tax_rate")
               if inputs.get(k) is None and not (k.startswith("commission_") and inputs.get("commission_rate_manual"))]
    return inputs, sources, logistics, missing


def render(result):
    c, i = result["computed"], result["inputs"]

    def rub(v):
        return "—" if v is None else f"{v:,.2f} ₽".replace(",", " ")

    def frac(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    lines = [f"Цена {rub(i.get('price'))}, себестоимость {rub(i.get('cost'))}, ДРР {frac(i.get('drr'))}, "
             f"выкуп {frac(i.get('buyout'))}", "", "| Шаг | На выкупленную единицу |", "|---|---|"]
    for key, label in (("commission", "Вознаграждение Ozon"), ("acquiring", "Эквайринг"),
                       ("logistics_total", "Логистика с невыкупами"), ("disposal_fees", "Сбор за утилизацию"),
                       ("total_ozon", "ИТОГО Ozon"), ("advertising", "Реклама"), ("payout", "Выплата Ozon"),
                       ("early_payout", "Досрочная выплата"), ("vat", "НДС"), ("tax", "Налог"),
                       ("profit", "**Прибыль**")):
        lines.append(f"| {label} | {rub(c.get(key))} |")
    lines.append(f"| Маржа | {frac(c.get('margin'))} |")
    lines.append(f"| Рентабельность | {frac(c.get('profitability'))} |")
    if result["missing"]:
        lines += ["", "Не хватает входов: " + ", ".join(result["missing"]) + " — прибыль неизвестна."]
    log = result.get("logistics")
    if log:
        lines += ["", f"Логистика FBO по кластерам ({'идеальная раскладка' if log['clusters'] == 'ideal' else 'товар в: ' + ', '.join(log['clusters'])}): "
                  f"до 300 ₽ — {rub(log['logistics_lt_300']['average'])}, от 300 ₽ — {rub(log['logistics_gte_300']['average'])}, "
                  f"объём {log['logistics_gte_300']['volume_bucket']}"]
        if "overpay" in log:
            lines.append(f"Переплата за раскладку против идеальной — {rub(log['overpay'])} на единицу.")
    return "\n".join(lines) + "\n"


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--plan")
    ap.add_argument("--settings")
    ap.add_argument("--formula")
    ap.add_argument("--clusters", nargs="*")
    ap.add_argument("--ideal", action="store_true")
    ap.add_argument("--tariffs", default=str(TARIFFS_XLSX))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    formula = load(args.formula) if args.formula else load(FORMULA)
    formula = formula.get("formula_schema", formula)
    product = load(args.inputs)
    need_tariffs = "logistics_lt_300" not in product or "logistics_gte_300" not in product
    tariffs = load_tariffs(args.tariffs) if need_tariffs else None
    shares = load(MARKET_SHARES)
    inputs, sources, logistics, missing = build_inputs(
        product, load(args.plan) if args.plan else None, load(args.settings) if args.settings else None,
        None if args.ideal else args.clusters, tariffs, shares)
    result = {"formula_version": formula.get("version"), "inputs": inputs, "sources": sources,
              "computed": compute(formula, inputs), "logistics": logistics, "missing": missing}
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
