#!/usr/bin/env python3
"""Обратный расчёт плановой юнит-экономики: цена под целевую прибыль, маржу или рентабельность и предельная себестоимость.

Скрипт ничего не запрашивает. Строка товара — ответ unit_economics_plan_list (или одна его строка, или ответ
unit_economics_plan_preview), либо вывод new_product_plan.py --json; формула — formula_schema ответа plan list, иначе копия
в references (та же версия 5).

    python3 plan_solve.py --row plan.json [--product ID] --solve price|cost \
        (--target-profit 0,20,50 | --target-margin 0.1,0.2 | --target-profitability 0.3) \
        [--price P] [--cost C] [--formula formula.json] [--max-price N] [--json]

--row       ответ plan list целиком (строку выбирает --product: product_id, offer_id или SKU; одна строка — она), одна строка
            плана (объект с inputs) или вывод new_product_plan.py --json;
--solve     price — наименьшая цена P (целые рубли), при которой цель выполнена; cost — наибольшая себестоимость (до копейки);
--target-*  цели через запятую (десятичная точка): прибыль, ₽ на выкупленную единицу; маржа — доля от цены; рентабельность —
            доля от себестоимости;
--price / --cost  считать при другой цене P или себестоимости (иначе — из строки);
--max-price верхняя граница перебора цены (по умолчанию 3 × наибольшее из цены, себестоимости и 300 ₽).

Цена перебирается по рублю, а не делением пополам: ставка вознаграждения меняется на 100 и 300 ₽, бакет логистики — на 300 ₽,
и прибыль на этих границах прыгает. Если цель выполнена ниже границы, выше теряется и возвращается дальше, скрипт это говорит.
Найденная цена — P, цена с акциями продавца, как в плане; цифры для ответа — из unit_economics_plan_preview с этой ценой.
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from new_product_plan import FORMULA, compute, load  # noqa: E402

LABELS = {"profit": "прибыль", "margin": "маржа", "profitability": "рентабельность"}
EPS = 1e-9
COST_LIMIT = 1e9  # ₽: дальше себестоимость не ищем


class Unknown(ValueError):
    """Прибыль неизвестна: формуле не хватает входов."""


# ── входы ─────────────────────────────────────────────────────────────

def pick_row(data, product=None):
    """Строка плана: из ответа plan list (по --product), одна строка или вывод new_product_plan.py --json."""
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
        if product is not None:
            key = str(product)
            for row in items:
                if key in {str(row.get("product_id")), str(row.get("offer_id")), str(row.get("sku"))}:
                    return row
            raise ValueError(f"в ответе плана нет товара {key}")
        if len(items) == 1:
            return items[0]
        raise ValueError(f"в ответе плана {len(items)} строк — укажите --product (product_id, offer_id или SKU)")
    if isinstance(data, dict) and isinstance(data.get("inputs"), dict):
        return data
    raise ValueError("нужна строка плана (с inputs), ответ unit_economics_plan_list или вывод new_product_plan.py --json")


def pick_formula(data, path=None):
    """--formula, иначе formula_schema ответа plan list, иначе копия в references."""
    if path:
        formula = load(path)
    elif isinstance(data, dict) and isinstance(data.get("formula_schema"), dict):
        formula = data["formula_schema"]
    else:
        formula = load(FORMULA)
    return formula.get("formula_schema", formula)


def env_at(row, price=None, cost=None):
    """Входы строки с другой ценой P и (или) себестоимостью.

    У строк Seller AI обратная логистика своя (по начислениям или по цене карточки) и от черновой цены не зависит, как в preview;
    у вывода new_product_plan.py (`tariff_model`) это тариф бакета цены — он идёт за ценой."""
    inputs = dict(row["inputs"])
    if price is not None:
        inputs["price"] = float(price)
        if (row.get("sources") or {}).get("reverse_logistics") == "tariff_model":
            inputs["reverse_logistics"] = inputs.get("logistics_lt_300" if float(price) <= 300 else "logistics_gte_300")
    if cost is not None:
        inputs["cost"] = float(cost)
    return inputs


def missing_inputs(formula, env, skip=()):
    """Пустые входы формулы — почему прибыль неизвестна."""
    manual = env.get("commission_rate_manual") is not None
    return [key for key in formula["inputs"]
            if key not in skip and key != "commission_rate_manual" and env.get(key) is None
            and not (manual and key.startswith("commission_"))]


def parse_targets(args):
    targets = []
    for kind, text in (("profit", args.target_profit), ("margin", args.target_margin),
                       ("profitability", args.target_profitability)):
        for part in (text or "").split(","):
            if part.strip():
                targets.append((kind, float(part.strip())))
    if not targets:
        raise ValueError("нужна цель: --target-profit, --target-margin или --target-profitability")
    return targets


# ── решение ───────────────────────────────────────────────────────────

def meets(computed, kind, target):
    value = computed.get(kind)
    return value is not None and value >= target - EPS


def default_max_price(env):
    return int(math.ceil(3 * max(float(env.get("price") or 0), float(env.get("cost") or 0), 300.0)))


def solve_price(formula, row, targets, cost=None, max_price=None):
    """Для каждой цели — наименьшая цена P (целые рубли) с выполненной целью и где цель теряется выше и возвращается.

    {(вид, цель): {price, computed, until, again}}: price — None, если цель не достигается до max_price; until — последняя цена
    первого отрезка, если выше неё цель теряется; again — где она выполняется снова."""
    base = env_at(row, cost=cost)
    if any(kind == "profitability" for kind, _ in targets) and not float(base.get("cost") or 0) > 0:
        raise ValueError("себестоимость не задана или 0 — рентабельность не считается")
    top = max(1, int(max_price or default_max_price(base)))
    runs = {t: [] for t in targets}
    inside = {t: False for t in targets}
    known = False
    for price in range(1, top + 1):
        computed = compute(formula, env_at(row, price=price, cost=cost))
        known = known or computed.get("profit") is not None
        for t in targets:
            ok = meets(computed, *t)
            if ok and not inside[t]:
                runs[t].append([price, price, computed])
            elif ok:
                runs[t][-1][1] = price
            inside[t] = ok
    if not known:
        raise Unknown(missing_inputs(formula, base, skip=("price",)))
    out = {}
    for t in targets:
        found = runs[t]
        if not found:
            out[t] = {"price": None, "computed": None, "until": None, "again": None, "max_price": top}
            continue
        start, end, computed = found[0]
        out[t] = {"price": start, "computed": computed, "until": end if end < top else None,
                  "again": found[1][0] if len(found) > 1 else None, "max_price": top}
    return out


def solve_cost(formula, row, kind, target, price=None):
    """Наибольшая себестоимость (до копейки), при которой цель выполнена; None — не выполняется и при нулевой.

    Прибыль падает с ростом себестоимости при любой базе налога, поэтому делением пополам."""
    p = env_at(row, price=price).get("price")
    if p is None:
        raise ValueError("в строке нет цены — укажите --price")
    p = float(p)
    if kind == "profitability" and target < 0:
        raise ValueError("цель рентабельности для себестоимости — не меньше 0")

    def margin_left(cost):
        computed = compute(formula, env_at(row, price=price, cost=cost))
        profit = computed.get("profit")
        if profit is None:
            raise Unknown(missing_inputs(formula, env_at(row, price=price, cost=cost), skip=("cost",)))
        need = target if kind == "profit" else target * p if kind == "margin" else target * cost
        return profit - need

    low = 0.01 if kind == "profitability" else 0.0
    if margin_left(low) < -EPS:
        return None, None
    high = max(p, 1.0)
    while margin_left(high) >= -EPS:
        if high >= COST_LIMIT:
            raise ValueError("цель выполняется при любой себестоимости — проверьте входы")
        high *= 2
    while high - low > 0.001:
        mid = (low + high) / 2
        if margin_left(mid) >= -EPS:
            low = mid
        else:
            high = mid
    cost = math.floor(low * 100 + 1e-6) / 100
    return cost, compute(formula, env_at(row, price=price, cost=cost))


# ── вывод ─────────────────────────────────────────────────────────────

def rub(value):
    return "—" if value is None else f"{value:,.2f} ₽".replace(",", " ")


def whole(value):
    return f"{int(value):,} ₽".replace(",", " ")


def frac(value):
    return "—" if value is None else f"{value * 100:.1f} %"


def goal(kind, target):
    if kind == "profit":
        return f"прибыль ≥ {target:g} ₽"
    return f"{LABELS[kind]} ≥ {target * 100:g} %"


def now_line(formula, row, price=None, cost=None):
    env = env_at(row, price=price, cost=cost)
    computed = compute(formula, env)
    return (f"Сейчас: цена P {rub(env.get('price'))}, себестоимость {rub(env.get('cost'))}, прибыль "
            f"{rub(computed.get('profit'))}, маржа {frac(computed.get('margin'))}, рентабельность "
            f"{frac(computed.get('profitability'))}.")


def render_price(results, header):
    lines = [header, "", "Цена P под цель (на выкупленную единицу; P — цена с акциями продавца):", "",
             "| Цель | Цена P | Прибыль | Маржа | Рентабельность |", "|---|---|---|---|---|"]
    notes = []
    for (kind, target), res in results.items():
        if res["price"] is None:
            lines.append(f"| {goal(kind, target)} | не достигается до {whole(res['max_price'])} | — | — | — |")
            continue
        c = res["computed"]
        lines.append(f"| {goal(kind, target)} | {whole(res['price'])} | {rub(c.get('profit'))} | {frac(c.get('margin'))} | "
                     f"{frac(c.get('profitability'))} |")
        if res["until"] is not None:
            tail = f", снова — с {whole(res['again'])}" if res["again"] is not None else ""
            notes.append(f"{goal(kind, target)}: выполняется до {whole(res['until'])}, выше теряется (ставка вознаграждения "
                         f"или бакет логистики){tail}.")
    return "\n".join(lines + ([""] + notes if notes else [])) + "\n"


def render_cost(results, header, price):
    lines = [header, "", f"Предельная себестоимость при цене P {rub(price)} (на выкупленную единицу):", "",
             "| Цель | Себестоимость до | Прибыль | Маржа | Рентабельность |", "|---|---|---|---|---|"]
    for (kind, target), (cost, c) in results.items():
        if cost is None:
            lines.append(f"| {goal(kind, target)} | не выполняется и при нулевой | — | — | — |")
        else:
            lines.append(f"| {goal(kind, target)} | {rub(cost)} | {rub(c.get('profit'))} | {frac(c.get('margin'))} | "
                         f"{frac(c.get('profitability'))} |")
    return "\n".join(lines) + "\n"


def brief(computed):
    if computed is None:
        return None
    return {k: computed.get(k) for k in ("profit", "margin", "profitability", "commission_rate", "logistics")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--row", required=True)
    ap.add_argument("--product")
    ap.add_argument("--formula")
    ap.add_argument("--solve", choices=("price", "cost"), required=True)
    ap.add_argument("--target-profit")
    ap.add_argument("--target-margin")
    ap.add_argument("--target-profitability")
    ap.add_argument("--price", type=float)
    ap.add_argument("--cost", type=float)
    ap.add_argument("--max-price", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        data = load(args.row)
        row = pick_row(data, args.product)
        formula = pick_formula(data, args.formula)
        targets = parse_targets(args)
        header = now_line(formula, row, price=args.price, cost=args.cost)
        if args.solve == "price":
            results = solve_price(formula, row, targets, cost=args.cost, max_price=args.max_price)
            out = [{"target": goal(k, t), "kind": k, "value": t, "price": r["price"], "until": r["until"],
                    "again": r["again"], "computed": brief(r["computed"])} for (k, t), r in results.items()]
            text = render_price(results, header)
        else:
            results = {(k, t): solve_cost(formula, row, k, t, price=args.price) for k, t in targets}
            out = [{"target": goal(k, t), "kind": k, "value": t, "cost": c, "computed": brief(comp)}
                   for (k, t), (c, comp) in results.items()]
            text = render_cost(results, header, env_at(row, price=args.price).get("price"))
    except Unknown as exc:
        names = exc.args[0] if exc.args else []
        sys.stderr.write(("прибыль неизвестна: нет входов " + ", ".join(names)) if names
                         else "прибыль неизвестна при этих входах (выкуп, отмены)")
        sys.stderr.write("\n")
        return 2
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if args.json:
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
