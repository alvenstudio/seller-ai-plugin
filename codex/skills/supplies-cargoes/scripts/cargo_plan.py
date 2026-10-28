#!/usr/bin/env python3
"""Грузоместа поставки FBO — чистый расчёт над ответом `supplies_orders_get`; ничего не вызывает.

Готовит `body` для `supplies_cargoes_set` (в ответе — поле `body` и сводка `summary`): полный набор мест поставки
(`mode: replace`), только новые места рядом с прежними (`mode: add`) или удаление выбранных мест (`mode: delete`, прочие
места сохраняют номера, этикетки и «Годен до»), и, где нужно, `body` нового состава для `supplies_supply_items_set`
(`items_body`).

    python3 cargo_plan.py layout   in.json   # места поставки из количества и фасовки → body
    python3 cargo_plan.py boxplan  in.json   # округлить заявку по имеющимся коробкам (один размер на поставку,
                                             #   с allow_mixed — и смешанно, если по одному размеру не выходит)
    python3 cargo_plan.py remove   in.json   # убрать места по cargo_id → удаление этих мест, затем новое количество
    python3 cargo_plan.py add      in.json   # добавить места к поставке → новое количество, затем новые места
    python3 cargo_plan.py check    in.json   # сверка мест с составом поставок заявки

Входы (JSON-файл или - для stdin):
  layout:  {"sku": "…", "quantity": 960, "box_size": 20, "pallet_size": 600 | null,
            "breakdown": {"20": 30, "12": 5} | null, "expires": "2027-02-25" | {"20": "2027-02-25"},
            "expected_cargo_ids": [], "max_boxes": 30, "max_pallets": 40}
  boxplan: {"supplies": [{"supply_id": "…", "quantity": 96}], "inventory": {"12": 70, "10": 26},
            "allow_mixed": false, "expires": "2027-02-25" | {"12": "…", "10": "…"}}
           или {"supplies": [...], "box_size": 12} — общий объём округляется до целых коробок;
           allow_mixed — коробки разных размеров в одной поставке, только при одном сроке годности
  remove:  {"order": <ответ supplies_orders_get>, "cargo_ids": ["…"], "keep_quantity": false,
            "replace": false, "expires": …}   # replace: true — прежний путь, новый полный набор (нужен срок)
  add:     {"order": <ответ supplies_orders_get>, "supply_id": "…", "sku": "…",
            "places": [{"type": "BOX", "quantity": 8}], "expires": …}   # срок — у новых мест
  check:   {"order": <ответ supplies_orders_get>}
"""
import argparse
import json
import math
import sys

PALLET_FROM = 100  # штук: место такого размера в перечне «размер: мест» — палета


class CargoError(ValueError):
    pass


def _pos(v, name):
    if isinstance(v, bool):
        raise CargoError(f"{name}: нужно целое больше нуля")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise CargoError(f"{name}: нужно целое больше нуля") from None
    if n <= 0 or str(n) != str(v).strip():
        raise CargoError(f"{name}: нужно целое больше нуля")
    return n


def places(quantity, box_size, pallet_size=None):
    """Сколько влезло целых палет, остаток — коробками; последняя коробка неполная."""
    q, box = _pos(quantity, "quantity"), _pos(box_size, "box_size")
    out = []
    if pallet_size:
        p = _pos(pallet_size, "pallet_size")
        out += [{"type": "PALLET", "quantity": p}] * (q // p)
        q %= p
    while q > 0:
        out.append({"type": "BOX", "quantity": min(box, q)})
        q -= box
    return out


def places_from_breakdown(quantity, breakdown, pallet_from=PALLET_FROM):
    """Перечень «штук в месте: мест»; сумма должна совпасть с количеством поставки."""
    out = []
    for size, n in sorted(((_pos(s, "размер"), _pos(c, "мест")) for s, c in breakdown.items()), reverse=True):
        out += [{"type": "PALLET" if size >= pallet_from else "BOX", "quantity": size}] * n
    total = sum(p["quantity"] for p in out)
    if quantity is not None and total != int(quantity):
        raise CargoError(f"Места дают {total} шт, а в поставке {quantity}: раскладка не сходится")
    return out


def _expires_for(expires, qty):
    if expires is None or isinstance(expires, str):
        return expires
    for key in (str(qty), qty):
        if key in expires:
            return expires[key]
    raise CargoError(f"Нет срока годности для мест по {qty} шт: назовите срок для каждого размера")


def body(sku, place_list, expires=None, expected_cargo_ids=(), max_boxes=30, max_pallets=40, mode=None):
    boxes = sum(1 for p in place_list if p["type"] == "BOX")
    pallets = sum(1 for p in place_list if p["type"] == "PALLET")
    if boxes > max_boxes or pallets > max_pallets:
        raise CargoError(f"{boxes} коробок и {pallets} палет: в поставку — не больше {max_boxes} коробок и "
                         f"{max_pallets} палет, остаток — отдельной заявкой")
    if not place_list:
        raise CargoError("Нет ни одного места")
    cargoes = []
    for p in place_list:
        items = p.get("items") or [{"sku": str(sku), "quantity": p["quantity"]}]
        exp = _expires_for(expires, p["quantity"])
        cargoes.append({"type": p["type"], "items": [dict(i, expires_at=exp) if exp else dict(i) for i in items]})
    out = {"cargoes": cargoes, "expected": {"cargo_ids": [str(c) for c in expected_cargo_ids]}}
    if mode:
        out = {"mode": mode, **out}
    return {"body": out,
            "summary": {"boxes": boxes, "pallets": pallets, "units": sum(p["quantity"] for p in place_list),
                        "units_per_place": sorted({p["quantity"] for p in place_list})}}


def layout(data):
    if data.get("breakdown"):
        pl = places_from_breakdown(data.get("quantity"), data["breakdown"])
    else:
        pl = places(data["quantity"], data["box_size"], data.get("pallet_size"))
    return body(data["sku"], pl, data.get("expires"), data.get("expected_cargo_ids") or [],
                int(data.get("max_boxes", 30)), int(data.get("max_pallets", 40)))


# --- Округлить поданную заявку по коробкам: пропорции заявки, один размер коробки на поставку ---

def _apportion(targets, n):
    alloc = [max(1, math.floor(t)) for t in targets]
    while sum(alloc) > n:
        i = max((i for i, a in enumerate(alloc) if a > 1), key=lambda i: alloc[i] - targets[i])
        alloc[i] -= 1
    while sum(alloc) < n:
        i = max(range(len(alloc)), key=lambda i: targets[i] - alloc[i])
        alloc[i] += 1
    return alloc


def _closest_subset(targets, goal):
    reach = {0: None}
    cap = int(goal + max(targets.values(), default=0)) + 1
    for key, t in sorted(targets.items(), key=lambda kv: -kv[1]):
        w = int(round(t))
        for s in list(reach):
            ns = s + w
            if ns <= cap and ns not in reach:
                reach[ns] = (s, key)
    best = min((s for s in reach if s > 0), key=lambda s: abs(s - goal), default=0)
    chosen = []
    while best:
        best, key = reach[best]
        chosen.append(key)
    return chosen


def box_plan(supplies, inventory=None, box_size=None, allow_mixed=False, expires=None):
    """{supply_id: {размер: коробок}}; все коробки инвентаря расходятся, у поставки один размер (allow_mixed — если по
    одному размеру не раскладывается, коробки смешиваются в поставке; при разных сроках по размерам — нельзя)."""
    qty = {str(s["supply_id"]): int(s["quantity"]) for s in supplies if int(s["quantity"]) > 1}
    if not qty:
        raise CargoError("Нет поставок с количеством больше 1 шт")
    if inventory is None:
        box = _pos(box_size, "box_size")
        inventory = {box: max(len(qty), round(sum(qty.values()) / box))}
    inv = {_pos(k, "размер"): _pos(v, "коробок") for k, v in inventory.items()}
    if len(qty) > sum(inv.values()):
        raise CargoError(f"Поставок {len(qty)}, а коробок {sum(inv.values())}: каждой нужна хотя бы одна")
    scale = sum(s * n for s, n in inv.items()) / sum(qty.values())
    target = {sid: q * scale for sid, q in qty.items()}
    skipped = [str(s["supply_id"]) for s in supplies if int(s["quantity"]) <= 1]
    try:
        return _single_size_plan(qty, inv, target, skipped)
    except CargoError:
        if not allow_mixed:
            raise
        if isinstance(expires, dict) and len({str(v) for v in expires.values()}) > 1:
            raise CargoError("Сроки годности у размеров разные — смешивать размеры в поставке нельзя: "
                             "задайте раскладку сами") from None
        return _mixed_plan(qty, inv, target, skipped)


def _mixed_plan(qty, inv, target, skipped):
    """Каждую коробку (от больших к меньшим) — поставке с наибольшей недостачей до цели; у каждой хотя бы одна."""
    got = {sid: [] for sid in qty}
    for size in sorted((s for s, n in inv.items() for _ in range(n)), reverse=True):
        sid = max(got, key=lambda k: (target[k] - sum(got[k]), target[k], k))
        got[sid].append(size)
    for sid in [k for k in got if not got[k]]:
        donor = max((k for k in got if len(got[k]) > 1), key=lambda k: (sum(got[k]) - target[k], k))
        got[donor].sort()
        got[sid].append(got[donor].pop(0))
    plan = {}
    for sid, sizes in got.items():
        breakdown = {}
        for size in sizes:
            breakdown[str(size)] = breakdown.get(str(size), 0) + 1
        plan[sid] = {"breakdown": breakdown, "quantity": sum(sizes), "was": qty[sid]}
    return {"supplies": plan, "units": sum(p["quantity"] for p in plan.values()), "skipped": skipped, "mixed": True}


def _single_size_plan(qty, inv, target, skipped):
    sizes = sorted(inv, key=lambda s: inv[s] * s, reverse=True)
    free, groups = dict(target), {}
    for size in sizes[1:]:
        members = _closest_subset(free, inv[size] * size)
        members.sort(key=lambda sid: target[sid], reverse=True)
        del members[inv[size]:]
        groups[size] = members
        for sid in members:
            free.pop(sid)
    groups[sizes[0]] = list(free)
    if len(groups[sizes[0]]) > inv[sizes[0]] or (inv[sizes[0]] and not groups[sizes[0]]):
        raise CargoError(f"Коробки по {sizes[0]} шт не раскладываются по поставкам: задайте раскладку сами")
    plan = {}
    for size, members in groups.items():
        if members:
            for sid, n in zip(members, _apportion([target[s] / size for s in members], inv[size])):
                plan[sid] = {"breakdown": {str(size): n}, "quantity": size * n, "was": qty[sid]}
    return {"supplies": plan, "units": sum(p["quantity"] for p in plan.values()), "skipped": skipped, "mixed": False}


# --- Правка мест по ответу supplies_orders_get ---

def _supply(order, supply_id):
    for s in order.get("supplies") or []:
        if str(s.get("supply_id")) == str(supply_id):
            return s
    raise CargoError(f"Поставки {supply_id} нет в заявке")


def _composition(order, supply_id):
    out = {}
    for item in order.get("items") or []:
        for s in item.get("supplies") or []:
            if str(s.get("supply_id")) == str(supply_id) and s.get("quantity") is not None:
                out[str(item["sku"])] = out.get(str(item["sku"]), 0) + int(s["quantity"])
    return out


def _cargoes(order, supply_id):
    return [c for c in order.get("cargoes") or [] if str(c.get("supply_id")) == str(supply_id)]


def _place(c):
    items = [{"sku": str(i["sku"]), "quantity": int(i["quantity"])} for i in c.get("items") or []]
    if not items or any(i["quantity"] <= 0 for i in items):
        raise CargoError(f"Состав грузоместа {c.get('cargo_id')} неизвестен — правку по нему не собрать")
    return {"type": c.get("type") or "BOX", "quantity": sum(i["quantity"] for i in items), "items": items}


def _items_body(supply, composition):
    return {"items": [{"sku": k, "quantity": v} for k, v in sorted(composition.items()) if v > 0],
            "expected": {"bundle_id": supply.get("bundle_id")}}


def remove(data):
    order, ids = data["order"], {str(c) for c in data["cargo_ids"]}
    found = [c for c in order.get("cargoes") or [] if str(c.get("cargo_id")) in ids]
    missing = ids - {str(c["cargo_id"]) for c in found}
    if missing:
        raise CargoError(f"Грузомест нет в заявке: {', '.join(sorted(missing))}")
    result = []
    for sid in sorted({str(c["supply_id"]) for c in found}):
        supply, mine = _supply(order, sid), _cargoes(order, sid)
        keep = [c for c in mine if str(c["cargo_id"]) not in ids]
        if not keep:
            raise CargoError(f"В поставке {sid} не останется мест: пустую поставку Ozon не примет")
        removed = {}
        for c in mine:
            if str(c["cargo_id"]) in ids:
                for i in _place(c)["items"]:
                    removed[i["sku"]] = removed.get(i["sku"], 0) + i["quantity"]
        comp = _composition(order, sid)
        new_comp = {k: v - removed.get(k, 0) for k, v in comp.items()}
        if any(v < 0 for v in new_comp.values()):
            raise CargoError(f"Поставка {sid}: убираем больше, чем в составе")
        gone = sorted(str(c["cargo_id"]) for c in mine if str(c["cargo_id"]) in ids)
        if data.get("replace"):
            cargo_body = body(None, [_place(c) for c in keep], data.get("expires"), [c["cargo_id"] for c in mine])
        else:
            # Только выбранные места: прочие сохраняют номера, этикетки и «Годен до» (`mode: delete`).
            cargo_body = {"body": {"mode": "delete", "cargo_ids": gone,
                                   "expected": {"cargo_ids": [str(c["cargo_id"]) for c in mine]}},
                          "summary": {"deleted": len(gone), "left": len(keep),
                                      "units_left": sum(_place(c)["quantity"] for c in keep)}}
        result.append({"supply_id": sid, "cargoes_body": cargo_body,
                       "items_body": None if data.get("keep_quantity") else _items_body(supply, new_comp),
                       "quantity": {"before": comp, "after": new_comp, "removed": removed},
                       "removed_cargo_ids": gone})
    return {"supplies": result, "warnings": _expiry_warning(data) if data.get("replace") else [],
            "order": "сначала места (cargoes_body), после их succeeded — перечитать заявку и состав (items_body, "
                     "expected.bundle_id из свежего чтения): мест меньше состава Seller AI принимает, а поднять состав "
                     "у поставки с местами Ozon не даёт"}


def add(data):
    order, sid = data["order"], str(data["supply_id"])
    supply, mine = _supply(order, sid), _cargoes(order, sid)
    new = []
    for p in data["places"]:
        q = _pos(p["quantity"], "quantity")
        new.append({"type": p.get("type", "BOX"), "quantity": q, "items": [{"sku": str(data["sku"]), "quantity": q}]})
    comp = _composition(order, sid)
    added = sum(p["quantity"] for p in new)
    new_comp = dict(comp)
    new_comp[str(data["sku"])] = new_comp.get(str(data["sku"]), 0) + added
    # Только новые места рядом с прежними (`mode: add`): прежние сохраняют номера, этикетки и «Годен до».
    cargo_body = body(None, new, data.get("expires"), [c["cargo_id"] for c in mine], mode="add")
    warnings = [] if data.get("expires") else ["Срок годности не задан: у новых мест его не будет (Ozon его не "
                                               "отдаёт и не печатает). Спросите срок у пользователя"]
    return {"supply_id": sid, "items_body": _items_body(supply, new_comp), "cargoes_body": cargo_body,
            "quantity": {"before": comp, "after": new_comp}, "warnings": warnings + [
                "Увеличение количества проходит только пока у поставки нет грузомест; после — INBOUND_NO_CAPACITY"]}


def _expiry_warning(data):
    if data.get("expires"):
        return []
    return ["Срок годности не задан: Seller AI его не отдаёт обратно, а новый набор заменяет все места — "
            "без срока у оставшихся мест он пропадёт. Спросите срок у пользователя"]


def check(data):
    order, out = data["order"], []
    for s in order.get("supplies") or []:
        if s.get("state") in ("CANCELLED", "OVERDUE", "REJECTED_AT_SUPPLY_WAREHOUSE"):
            continue
        sid = str(s["supply_id"])
        comp, in_cargo, unknown = _composition(order, sid), {}, 0
        mine = _cargoes(order, sid)
        for c in mine:
            if not c.get("items"):
                unknown += 1
            for i in c.get("items") or []:
                if i.get("quantity") is not None:
                    in_cargo[str(i["sku"])] = in_cargo.get(str(i["sku"]), 0) + int(i["quantity"])
        sizes = sorted({sum(int(i.get("quantity") or 0) for i in c.get("items") or []) for c in mine if c.get("items")})
        diff = {k: in_cargo.get(k, 0) - comp.get(k, 0) for k in set(comp) | set(in_cargo)
                if in_cargo.get(k, 0) != comp.get(k, 0)}
        out.append({"supply_id": sid, "cluster": (s.get("cluster") or {}).get("name"), "composition": comp,
                    "in_cargoes": in_cargo, "places": len(mine),
                    "boxes": sum(1 for c in mine if c.get("type") == "BOX"),
                    "pallets": sum(1 for c in mine if c.get("type") == "PALLET"),
                    "units_per_place": sizes, "unknown_places": unknown,
                    "ok": bool(mine) and not diff and not unknown, "diff": diff})
    return {"supplies": out, "ok": all(r["ok"] for r in out)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["layout", "boxplan", "remove", "add", "check"])
    ap.add_argument("input")
    a = ap.parse_args()
    data = json.loads(sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read())
    try:
        if a.command == "boxplan":
            r = box_plan(data["supplies"], data.get("inventory"), data.get("box_size"), bool(data.get("allow_mixed")),
                         data.get("expires"))
        else:
            r = {"layout": layout, "remove": remove, "add": add, "check": check}[a.command](data)
    except (CargoError, KeyError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
