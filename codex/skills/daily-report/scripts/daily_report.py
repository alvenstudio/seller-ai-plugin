#!/usr/bin/env python3
"""Ежедневный отчёт по рекламе и продажам из ответов инструментов Seller AI.

Скрипт ничего не запрашивает: агент сохраняет ответы инструментов в файлы и передаёт их сюда.

    python3 daily_report.py --products products.json --plan plan.json \
        [--cpc cpc.json] [--campaign-products c1.json c2.json …] [--cpo cpo-1.json cpo-2.json …] \
        [--previous products_earlier.json] [--alerts alerts.json] [--strategy strategy.json] [--store-id ID] \
        [--store-name NAME] [--json]

--products  ответ promotion_analytics_get со slice=product (все типы продвижения) за день или период;
--cpc       тот же запрос с types=cpc_search_reco,cpc_search,special — чтобы разделить расход на CPC и CPO;
--plan      ответ unit_economics_plan_list (целевой ДРР товара — inputs.drr, доля);
--campaign-products  страницы promotion_campaign_products_list активных кампаний за клики (файл на страницу): текущая
            ставка товара в каждой («CPC тек.»: рубли; у «Целевого ДРР» Ozon — «ДРР N %», у топ-позиций — «Топ-N», у
            автостратегии — «авто») и цель биддера (`bidder`);
--cpo       страницы promotion_cpo_products_list: ставка «Оплаты за заказ» товара в % и ₽;
--previous  более ранний ответ --products за тот же период (промежуточный срез);
--alerts    ответ promotion_bidder_alerts_list (actionable=true): текущие алерты, которые решает продавец, — аномалии
            «БИДДЕР ПРОСИТ РЕШЕНИЯ», замолчавший биддер (bidder_silent) — первой строкой;
--strategy  references/store-promotion-strategies.json (пороги и коридор цели).

Цель товара: в кампании биддера («Целевой общий расход») — его цель в силе (`bidder.target_total_drr`: своя, из плана
или разгона), иначе ДРР плана юнит-экономики. `warnings` ответов и списки, переданные не всеми страницами (товаров
меньше `total`), — аномалии «ДАННЫЕ НЕПОЛНЫЕ».

Печатает Markdown (или JSON с --json): аномалии, итог, таблица по товарам, ДРР к цели, наблюдения, срез.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

# ── Пороги аномалий (владелец: «отчёт читает сам себя», текст — КАПСОМ) ──
MIN_SPEND = 300.0           # ₽: ниже расход — шум, ДРР скачет случайно
MIN_OVERSPEND = 300.0       # ₽: перерасход сверх цели, начиная с которого кричим
SPEND_NO_SALES_MIN = 500.0  # ₽: расход при нулевой выручке
NO_AD_ORDERS_MIN = 1000.0   # ₽: расход при нулевых рекламных продажах (Ozon отдаёт их с задержкой)
HIGH_DRR_NO_TARGET = 40.0   # %: ДРР товара без цели
DRR_OVER_TARGET_PCT = 1.0   # п. п. сверх цели — перерасход
SPEND_SHARE_ALERT = 50.0    # %: доля одного товара в расходе магазина
STORE_MIN_SPEND = 1000.0    # ₽: минимальный расход магазина для итоговых аномалий
DELTA_MIN_SPEND = 300.0     # ₽: прирост расхода между срезами
MAX_LINES = 12
HIGH_DRR_MARK = 40.0        # %: пометка ⚠️ в таблице
DELTA_DRR_MARK = 25.0       # %: пометка ⚠️ в срезе

DEFAULT_STRATEGY = {"target_band": {"below": -2.0, "above": 1.0}, "min_spend_for_action_rub": 300}
# Стратегии со ставкой товара в рублях: ручные Ozon (TARGET_BIDS и NO_AUTO_STRATEGY — «без автостратегии») и биддер
# Seller AI («Целевой общий расход», Ozon видит TARGET_BIDS). «Целевой ДРР» Ozon (TARGET_CIR) — цель в %, топ-позиции —
# «Топ-N»; остальные — автостратегии: ставки Ozon ставит сам, `bid` = 0.
BID_STRATEGIES = {"TARGET_BIDS", "NO_AUTO_STRATEGY", "TARGET_TOTAL_DRR"}
MSK = timezone(timedelta(hours=3))


def load(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def num(value):
    """Число из ответа Seller AI (число или десятичная строка); None — неизвестно."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value):
    if value is None:
        return "—"
    return f"{value:,.0f} ₽".replace(",", " ").replace("-", "−")


def pct(value, digits=1):
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def cell_money(value):
    """Ячейка таблицы: нули и неизвестное — «—»."""
    return "—" if value is None or abs(value) < 0.5 else money(value)


def cell_pct(value, digits=1):
    return "—" if not value else pct(value, digits)


def pages_of(answer):
    """Ответ одним файлом или страницами — список страниц."""
    if answer is None:
        return []
    return answer if isinstance(answer, list) else [answer]


def product_rows(answer):
    """Строки товаров ответа promotion_analytics_get (slice=product) по SKU."""
    rows = {}
    for row in (answer or {}).get("rows") or []:
        if row.get("kind") != "product" or row.get("sku") is None:
            continue
        rows[str(row["sku"])] = row
    return rows


def targets_from_plan(plan):
    """SKU → целевой ДРР в процентах. ДРР плана задаёт только продавец: не задан — 0 в inputs.drr, такой товар без цели."""
    out = {}
    for item in (plan or {}).get("items") or []:
        sku = item.get("sku")
        drr = num((item.get("inputs") or {}).get("drr"))
        if sku is None or drr is None or drr <= 0:
            continue
        out[str(sku)] = drr * 100
    return out


def bids_from_campaigns(answers):
    """SKU → ставки по кампаниям и цель биддера из страниц promotion_campaign_products_list; страница, переданная
    дважды, товар не повторяет."""
    out, seen = {}, set()
    for a in answers or []:
        strategy = (a.get("strategy") or "").upper() or None
        for item in a.get("items") or []:
            sku = str(item.get("sku"))
            if (a.get("campaign_id"), sku) in seen:
                continue
            seen.add((a.get("campaign_id"), sku))
            e = out.setdefault(sku, {"campaigns": [], "bidder": None, "bidder_spend": None})
            bidder = item.get("bidder")
            manual = strategy is None or strategy in BID_STRATEGIES or bool(bidder)
            cir = strategy == "TARGET_CIR"
            top = None if manual or cir else (item.get("top_position") or None)
            e["campaigns"].append({"campaign_id": a.get("campaign_id"), "strategy": strategy,
                                   "auto": not manual and not cir and not top,
                                   "bid": num(item.get("bid")) if manual else None,
                                   "target_cir": num(item.get("target_cir")) if cir else None, "cir": cir,
                                   "top_position": top, "bidder": bool(bidder)})
            spend = num((item.get("stats") or {}).get("expense")) or 0.0
            if bidder and bidder.get("target_total_drr") is not None and (e["bidder_spend"] is None
                                                                           or spend > e["bidder_spend"]):
                e["bidder"] = {"target": num(bidder["target_total_drr"]), "source": bidder.get("target_source")}
                e["bidder_spend"] = spend
    return out


def cpo_bids(pages):
    """SKU → ставка «Оплаты за заказ» из страниц promotion_cpo_products_list: включена ли, % и ₽ (как в кабинете:
    display_bid_pct / display_bid_rub)."""
    out = {}
    for page in pages_of(pages):
        for item in (page or {}).get("items") or []:
            out[str(item.get("sku"))] = {"enabled": item.get("enabled"), "pct": num(item.get("display_bid_pct")),
                                         "rub": num(item.get("display_bid_rub")), "reduced": bool(item.get("reduced"))}
    return out


def bid_text(bids):
    """«CPC тек.» по кампаниям: «12 ₽ / авто»; биддер — «12 ₽ (б)»; «Целевой ДРР» Ozon — «ДРР 12 %»; топ — «Топ-4»."""
    if not bids:
        return "—"
    parts = []
    for c in bids["campaigns"]:
        if c["cir"]:
            parts.append("ДРР" if c["target_cir"] is None else f"ДРР {c['target_cir']:g} %")
        elif c["top_position"]:
            parts.append(str(c["top_position"]))
        elif c["auto"]:
            parts.append("авто")
        elif c["bid"] is None:
            parts.append("—")
        else:
            parts.append(f"{c['bid']:,.0f} ₽".replace(",", " ") + (" (б)" if c["bidder"] else ""))
    return " / ".join(parts)


def missing_pages(campaign_answers, cpo_pages):
    """Списки, переданные не всеми страницами: товаров меньше `total` ответа."""
    errors = []
    got, total = {}, {}
    for a in campaign_answers or []:
        cid = a.get("campaign_id")
        got.setdefault(cid, set()).update(str(i.get("sku")) for i in a.get("items") or [])
        if a.get("total") is not None:
            total[cid] = max(total.get(cid, 0), int(a["total"]))
    for cid, n in total.items():
        if len(got[cid]) < n:
            errors.append(f"кампания {cid}: переданы не все страницы товаров ({len(got[cid])} из {n}) — ставки и цели "
                          "биддера остальных неизвестны")
    pages = pages_of(cpo_pages)
    cpo_total = max((int(p["total"]) for p in pages if (p or {}).get("total") is not None), default=None)
    cpo_got = {str(i.get("sku")) for p in pages for i in (p or {}).get("items") or []}
    if cpo_total is not None and len(cpo_got) < cpo_total:
        errors.append(f"«Оплата за заказ»: переданы не все страницы ({len(cpo_got)} из {cpo_total}) — ставки остальных "
                      "товаров неизвестны")
    return errors


def answer_warnings(*answers):
    """`warnings` ответов Seller AI (данные неполные или не свежие), без повторов."""
    out = []
    for answer in answers:
        for page in pages_of(answer):
            for w in (page or {}).get("warnings") or []:
                text = w if isinstance(w, str) else json.dumps(w, ensure_ascii=False)
                if text not in out:
                    out.append(text)
    return out


def build_rows(products, cpc, targets, bids=None, cpo=None):
    cpc_rows = product_rows(cpc) if cpc else {}
    bids, cpo = bids or {}, cpo or {}
    out = []
    for sku, row in product_rows(products).items():
        m = row.get("metrics") or {}
        spend = num(m.get("expense")) or 0.0
        c = (cpc_rows.get(sku) or {}).get("metrics") or {}
        cpc_spend = num(c.get("expense")) if cpc else None
        clicks = num(m.get("clicks"))
        orders = num(m.get("orders"))
        b = bids.get(sku)
        bidder = (b or {}).get("bidder")
        target, source = (bidder["target"], f"bidder:{bidder['source']}") if bidder else (targets.get(sku), "plan")
        c_bid = cpo.get(sku)
        out.append({
            "sku": sku,
            "name": row.get("name") or "",
            "spend": spend,
            "cpc_spend": cpc_spend,
            "cpo_spend": None if cpc_spend is None else round(spend - cpc_spend, 2),
            "ad_revenue": num(m.get("sales")) or 0.0,
            "revenue": num(m.get("gmv")),           # вся выручка товара по заказам; None — не загружена
            "orders": orders,
            "ctr": num(m.get("ctr")),
            "conversion": (orders / clicks * 100) if clicks and orders is not None else None,
            "drr": num(m.get("drr_promo")),
            "drr_total": num(m.get("drr")),
            "target": target,
            "target_source": source if target is not None else None,
            "avg_cpc": num(c.get("click_cost")) if cpc else None,
            "bids": b["campaigns"] if b else None,
            "cpc_now": bid_text(b) if b else None,
            "cpo": c_bid,
        })
    out.sort(key=lambda r: -r["spend"])
    return out


def totals_of(products, cpc=None):
    m = ((products or {}).get("total") or {}).get("metrics") or {}
    gmv = products.get("gmv") or {}
    spend = num(m.get("expense")) or 0.0
    cpc_spend = num((((cpc or {}).get("total") or {}).get("metrics") or {}).get("expense")) if cpc else None
    return {
        "spend": spend,
        "cpc_spend": cpc_spend,
        "cpo_spend": None if cpc_spend is None else round(spend - cpc_spend, 2),
        "ad_revenue": num(m.get("sales")) or 0.0,
        "orders": num(m.get("orders")),
        "ctr": num(m.get("ctr")),
        "drr": num(m.get("drr_promo")),
        "drr_total": num(m.get("drr")),
        "revenue": num(gmv.get("total")) if gmv.get("covered", True) else None,
        "covered": gmv.get("covered", True),
    }


def store_target(rows):
    """Цель магазина: целевой ДРР товаров, взвешенный по их выручке; None — ни у одного товара с выручкой нет цели."""
    weighted, revenue = 0.0, 0.0
    for r in rows:
        if r["target"] is not None and r["revenue"]:
            weighted += r["target"] * r["revenue"]
            revenue += r["revenue"]
    return weighted / revenue if revenue > 0 else None


def _label(r):
    return (r.get("name") or r["sku"])[:40].upper()


def bidder_alerts(answer):
    """Текущие алерты биддера, которые решает продавец (promotion_bidder_alerts_list): (вид, текст)."""
    out = []
    for a in (answer or {}).get("items") or []:
        if not a.get("actionable") or a.get("state") != "current":
            continue
        where = " — ".join(x for x in (a.get("campaign_title") or f"кампания {a.get('campaign_id')}",
                                       a.get("name") or a.get("sku")) if x)
        out.append((a.get("kind"), f"БИДДЕР ПРОСИТ РЕШЕНИЯ: {where}: {a.get('message') or a.get('kind')}"))
    return out


def detect_anomalies(rows, totals, single_day=True, previous=None, data_errors=(), alerts=()):
    """Аномалии: список {kind, text, impact} по убыванию денег (impact, ₽); замолчавший биддер — первым: кампания
    крутится на последних ставках без контроля ДРР."""
    found = []

    def add(kind, text, impact):
        found.append({"kind": kind, "text": text.upper(), "impact": round(impact, 2)})

    for err in data_errors:
        add("data", f"ДАННЫЕ НЕПОЛНЫЕ: {err}", 0)
    for kind, text in alerts:
        add("bidder_silent" if kind == "bidder_silent" else "bidder", text, 0)
    store_spend = totals["spend"]
    for r in rows:
        spend, revenue, label = r["spend"], r["revenue"], _label(r)
        if spend < MIN_SPEND or revenue is None:
            continue
        target, drr_total = r["target"], r["drr_total"] or 0.0
        if revenue <= 0:
            if spend >= SPEND_NO_SALES_MIN:
                add("spend_no_sales", f"РАСХОД БЕЗ ПРОДАЖ: {label} — ПОТРАЧЕНО {money(spend)}, ВЫРУЧКА 0", spend)
            continue
        if target is not None:
            overspend = spend - revenue * target / 100
            if drr_total > target + DRR_OVER_TARGET_PCT and overspend >= MIN_OVERSPEND:
                add("overspend", f"ПЕРЕРАСХОД: {label} — ДРР {pct(drr_total)} ПРИ ЦЕЛИ {pct(target)}, ЛИШНИЕ "
                    f"{money(overspend)} (РАСХОД {money(spend)} / ВЫРУЧКА {money(revenue)})", overspend)
        elif drr_total > HIGH_DRR_NO_TARGET:
            add("high_drr", f"ВЫСОКИЙ ДРР БЕЗ ЦЕЛИ: {label} — ДРР {pct(drr_total)} (РАСХОД {money(spend)} / ВЫРУЧКА "
                f"{money(revenue)})", spend * (drr_total - HIGH_DRR_NO_TARGET) / max(drr_total, 1))
        if r["ad_revenue"] <= 0 and spend >= NO_AD_ORDERS_MIN:
            add("no_ad_orders", f"РЕКЛАМА БЕЗ ЗАКАЗОВ: {label} — РАСХОД {money(spend)}, РЕКЛАМНЫХ ПРОДАЖ 0 "
                f"(ВЫРУЧКА ВСЕГО {money(revenue)})", spend)
        if store_spend > 0 and spend >= STORE_MIN_SPEND:
            share = spend / store_spend * 100
            over = target is not None and drr_total > target + DRR_OVER_TARGET_PCT
            if share >= SPEND_SHARE_ALERT and (over or drr_total > HIGH_DRR_NO_TARGET):
                add("concentration", f"КОНЦЕНТРАЦИЯ БЮДЖЕТА: {label} — {share:.0f}% РАСХОДА МАГАЗИНА ({money(spend)}) "
                    f"ПРИ ДРР {pct(drr_total)}", 0)
    for d in deltas(rows, previous) if previous else []:
        if d["spend"] < DELTA_MIN_SPEND:
            continue
        if d["revenue"] is None:
            continue
        if d["revenue"] <= 0:
            add("delta_no_revenue", f"С ПРОШЛОГО СРЕЗА: {d['label']} — РАСХОД +{money(d['spend'])}, ВЫРУЧКА НЕ ВЫРОСЛА",
                d["spend"])
            continue
        d_drr = d["spend"] / d["revenue"] * 100
        limit = d["target"] * 2 if d["target"] is not None else HIGH_DRR_NO_TARGET
        if d_drr > limit:
            add("delta_high_drr", f"С ПРОШЛОГО СРЕЗА: {d['label']} — ДРР ПРИРОСТА {pct(d_drr)} (+{money(d['spend'])} "
                f"РАСХОДА НА +{money(d['revenue'])} ВЫРУЧКИ)", d["spend"] - d["revenue"] * limit / 100)
    store_revenue = totals["revenue"]
    target = store_target(rows)
    if single_day and store_spend >= STORE_MIN_SPEND and store_revenue and target is not None:
        store_drr = store_spend / store_revenue * 100
        overspend = store_spend - store_revenue * target / 100
        if store_drr > target + DRR_OVER_TARGET_PCT and overspend >= MIN_OVERSPEND:
            add("store_overspend", f"ПО МАГАЗИНУ: ДРР {pct(store_drr)} ПРИ ЦЕЛИ {pct(target)}, ПЕРЕРАСХОД "
                f"{money(overspend)} ЗА ДЕНЬ", overspend)
    found.sort(key=lambda a: (a["kind"] != "bidder_silent", -a["impact"]))
    return found


def deltas(rows, previous_rows):
    """Прирост между более ранним срезом и текущим по каждому товару."""
    prev = {r["sku"]: r for r in previous_rows}
    out = []
    for r in rows:
        p = prev.get(r["sku"])
        if not p:
            continue
        d_rev = None if r["revenue"] is None or p["revenue"] is None else r["revenue"] - p["revenue"]
        out.append({"sku": r["sku"], "label": _label(r), "name": r["name"], "spend": r["spend"] - p["spend"],
                    "revenue": d_rev, "target": r["target"]})
    return out


def signal(row, band):
    """above 🔴 выше цели, ok 🟢 в коридоре, below 🔵 ниже; no_spend ⚪ — у товара с целью расхода за день нет (статистика
    Ozon по товару ещё не пришла или реклама не крутилась: это не «ниже цели»); no_target ⚪ — цели нет, а расход или
    выручка есть; None — без выручки (общий ДРР неизвестен) или без активности."""
    if row["target"] is None:
        return "no_target" if row["spend"] > 0 or (row["revenue"] or 0) > 0 else None
    if row["spend"] <= 0:
        return "no_spend"
    if row["drr_total"] is None:
        return None
    diff = row["drr_total"] - row["target"]
    if diff > band["above"]:
        return "above"
    if diff < band["below"]:
        return "below"
    return "ok"


def observations(rows, totals):
    out = []
    active = [r for r in rows if r["spend"] > 0]
    if active and totals["spend"] > 0:
        top = active[0]
        out.append(f"Больше всего расхода — {top['name'][:50]}: {money(top['spend'])} "
                   f"({top['spend'] / totals['spend'] * 100:.0f}% бюджета), ДРР общ. {pct(top['drr_total'])}.")
    high = [r for r in active if (r["drr_total"] or 0) > HIGH_DRR_MARK]
    if high:
        out.append("ДРР общ. выше 40% — пересмотреть ставки: " + ", ".join(r["name"][:30] for r in high[:3]) + ".")
    with_rev = [r for r in active if r["revenue"] and r["drr_total"]]
    if with_rev:
        best = min(with_rev, key=lambda r: r["drr_total"])
        out.append(f"Лучший ДРР общ. — {best['name'][:50]}: {pct(best['drr_total'])}.")
    with_conv = [r for r in active if r["conversion"]]
    if with_conv:
        best = max(with_conv, key=lambda r: r["conversion"])
        out.append(f"Лучшая конверсия клика в заказ — {best['name'][:50]}: {pct(best['conversion'])}.")
    return out


def build(products, plan, cpc=None, previous=None, strategy=None, store_id=None, campaign_products=None, cpo=None,
          alerts=None):
    strategy_all = strategy or {}
    merged = dict(DEFAULT_STRATEGY)
    merged.update(strategy_all.get("default_strategy") or {})
    merged.update((strategy_all.get("stores") or {}).get(str(store_id), {}) if store_id else {})
    band = merged.get("target_band") or DEFAULT_STRATEGY["target_band"]
    targets = targets_from_plan(plan)
    bids = bids_from_campaigns(campaign_products)
    rows = build_rows(products, cpc, targets, bids, cpo_bids(cpo))
    totals = totals_of(products, cpc)
    period = products.get("period") or {}
    single_day = period.get("from") == period.get("to")
    errors = []
    if not totals["covered"]:
        errors.append("ОТПРАВЛЕНИЯ ПЕРИОДА ЕЩЁ НЕ ЗАГРУЖЕНЫ — ОБЩИЙ ДРР НЕИЗВЕСТЕН")
    if (products.get("models") or {}).get("unknown_days"):
        errors.append("ЗАКАЗЫ МОДЕЛЕЙ НЕИЗВЕСТНЫ ЗА " + ", ".join(products["models"]["unknown_days"]))
    errors += missing_pages(campaign_products, cpo)
    errors += answer_warnings(products, cpc, campaign_products, cpo, alerts)
    prev_rows = build_rows(previous, None, targets, bids) if previous else None
    anomalies = detect_anomalies(rows, totals, single_day, prev_rows, errors, bidder_alerts(alerts))
    for r in rows:
        r["signal"] = signal(r, band) if single_day else None
    totals["target"] = store_target(rows)
    return {
        "period": period, "store_id": store_id, "observed_at": products.get("observed_at"), "totals": totals,
        "rows": rows, "anomalies": anomalies, "observations": observations(rows, totals), "band": band,
        "bids_known": campaign_products is not None, "cpo_known": cpo is not None,
        "deltas": deltas(rows, prev_rows) if prev_rows else None,
    }


def observed_label(value):
    """«данные Ozon на 25.10 14:05 МСК» из observed_at ответа (UTC или со смещением); None — не показывать."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return f"данные Ozon на {value}"
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return "данные Ozon на " + t.astimezone(MSK).strftime("%d.%m %H:%M") + " МСК"


def render(report, store_name=""):
    p = report["period"]
    when = p.get("from") if p.get("from") == p.get("to") else f"{p.get('from')} — {p.get('to')}"
    t = report["totals"]
    lines = [f"# Отчёт по рекламе — {store_name} — {when}".replace(" —  —", " —"), ""]
    seen = observed_label(report.get("observed_at"))
    if seen:
        lines += [f"_{seen[0].upper() + seen[1:]}_", ""]
    lines += ["## 🚨 АНОМАЛИИ", ""]
    anomalies = report["anomalies"]
    if not anomalies:
        lines.append("АНОМАЛИЙ НЕ НАЙДЕНО")
    for a in anomalies[:MAX_LINES]:
        lines.append(f"- **{a['text']}**")
    if len(anomalies) > MAX_LINES:
        lines.append(f"- …И ЕЩЁ {len(anomalies) - MAX_LINES} АНОМАЛИЙ")
    split = t.get("cpc_spend") is not None
    summary = [("Расход на рекламу", cell_money(t["spend"]))]
    if split:
        summary += [("Расход CPC", cell_money(t["cpc_spend"])), ("Расход CPO", cell_money(t["cpo_spend"]))]
    summary += [("Продажи с рекламы", cell_money(t["ad_revenue"])),
                ("Вся выручка (заказы)", cell_money(t["revenue"])),
                ("Продано с рекламы, шт.", "—" if not t["orders"] else f"{int(t['orders'])}"),
                ("ДРР в продвижении", cell_pct(t["drr"])),
                ("ДРР общий", cell_pct(t["drr_total"])),
                ("Цель магазина (средневзвешенная по выручке)", cell_pct(t.get("target"))),
                ("CTR", cell_pct(t["ctr"], 2))]
    lines += ["", "## Итого", "", "| Показатель | Значение |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary]
    lines += ["", "## По товарам", ""]
    extra = (["CPC тек."] if report.get("bids_known") else []) + (["CPO %", "CPO ₽"] if report.get("cpo_known") else [])
    head = ["Товар", "Расход CPC", "Расход CPO", "Расход ∑", "Прод. рекл.", "Прод. итого", "CTR", "Конв.", "ДРР",
            "ДРР общ.", "Цел. ДРР", "CPC ср."] + extra
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in report["rows"]:
        mark = " ⚠️" if (r["drr_total"] or 0) > HIGH_DRR_MARK else ""
        star = "*" if (r.get("target_source") or "").startswith("bidder") else ""
        cells = [f"{r['name'][:50]} ({r['sku']})", cell_money(r['cpc_spend']), cell_money(r['cpo_spend']),
                 cell_money(r['spend']), cell_money(r['ad_revenue']), cell_money(r['revenue']), cell_pct(r['ctr'], 2),
                 cell_pct(r['conversion']), cell_pct(r['drr']), f"{cell_pct(r['drr_total'])}{mark}",
                 f"{cell_pct(r['target'])}{star}", cell_money(r['avg_cpc'])]
        if report.get("bids_known"):
            cells.append(r.get("cpc_now") or "—")
        if report.get("cpo_known"):
            c = r.get("cpo")
            if not c:
                cells += ["—", "—"]
            elif c["enabled"] is False:
                cells += ["выкл.", "—"]
            else:
                cells += [cell_pct(c["pct"]), cell_money(c["rub"])]
        lines.append("| " + " | ".join(cells) + " |")

    def bold(text):
        return f"**{text}**"

    lines.append("| " + " | ".join(["**ИТОГО**", bold(cell_money(t["cpc_spend"])) if split else "",
                                    bold(cell_money(t["cpo_spend"])) if split else "", bold(cell_money(t["spend"])),
                                    bold(cell_money(t["ad_revenue"])), bold(cell_money(t["revenue"])),
                                    bold(cell_pct(t["ctr"], 2)), "", bold(cell_pct(t["drr"])),
                                    bold(cell_pct(t["drr_total"])), bold(cell_pct(t.get("target"))), ""]
                                   + [""] * len(extra)) + " |")
    if any((r.get("target_source") or "").startswith("bidder") for r in report["rows"]):
        lines += ["", "\\* цель биддера («Целевой общий расход»: своя, из плана или разгон) — сигналы и перерасход "
                      "считаются к ней. «(б)» — ставку ставит биддер."]
    groups = {"above": [], "ok": [], "below": [], "no_spend": [], "no_target": []}
    for r in report["rows"]:
        if r.get("signal"):
            groups[r["signal"]].append(r)
    if any(groups.values()):
        band = report["band"]
        lines += ["", "## ДРР к цели", ""]
        for key, title in (("above", f"🔴 Выше цели (больше +{band['above']:g} п. п.)"),
                           ("ok", f"🟢 В коридоре ({band['below']:g}…+{band['above']:g} п. п.)"),
                           ("below", f"🔵 Ниже цели (меньше {band['below']:g} п. п.)")):
            if groups[key]:
                lines.append(f"- {title}: " + "; ".join(
                    f"{r['name'][:40]} {pct(r['drr_total'])} при цели {pct(r['target'])}" for r in groups[key]))
        if groups["no_spend"]:
            lines.append("- ⚪ Расхода за день нет (статистика Ozon по товару ещё не пришла или реклама не крутилась — "
                         "это не «ниже цели»): " + "; ".join(f"{r['name'][:40]} при цели {pct(r['target'])}"
                                                           for r in groups["no_spend"]))
        if groups["no_target"]:
            lines.append("- ⚪ Без цели (ДРР плана не задан — ни сигнала, ни «ПЕРЕРАСХОДА»): " + "; ".join(
                f"{r['name'][:40]} ДРР общ. {pct(r['drr_total'])}" for r in groups["no_target"]))
    if report["observations"]:
        lines += ["", "## Наблюдения", ""] + [f"- {o}" for o in report["observations"]]
    if report.get("deltas"):
        changed = [d for d in report["deltas"] if abs(d["spend"]) >= 1 or (d["revenue"] or 0) >= 1]
        if changed:
            lines += ["", "## Промежуточный срез (с прошлого отчёта)", "",
                      "| Товар | Выручка | Расход | ДРР прироста |", "|---|---|---|---|"]
            ts, tr = 0.0, 0.0
            for d in changed:
                ts += d["spend"]
                tr += d["revenue"] or 0.0
                drr = d["spend"] / d["revenue"] * 100 if d["revenue"] and d["revenue"] > 0 else None
                mark = " ⚠️" if drr is not None and drr > DELTA_DRR_MARK else ""
                lines.append(f"| {d['name'][:50]} | {money(d['revenue'])} | {money(d['spend'])} | {pct(drr)}{mark} |")
            lines.append(f"| **ИТОГО** | **{money(tr)}** | **{money(ts)}** | **{pct(ts / tr * 100 if tr > 0 else None)}** |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--products", required=True)
    ap.add_argument("--plan")
    ap.add_argument("--cpc")
    ap.add_argument("--campaign-products", nargs="*", help="страницы promotion_campaign_products_list")
    ap.add_argument("--cpo", nargs="+", help="страницы promotion_cpo_products_list")
    ap.add_argument("--previous")
    ap.add_argument("--alerts", help="ответ promotion_bidder_alerts_list (actionable=true)")
    ap.add_argument("--strategy")
    ap.add_argument("--store-id")
    ap.add_argument("--store-name", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    campaigns = [load(f) for f in args.campaign_products] if args.campaign_products is not None else None
    cpo = [load(f) for f in args.cpo] if args.cpo else None
    report = build(load(args.products), load(args.plan), load(args.cpc), load(args.previous), load(args.strategy),
                   args.store_id, campaigns, cpo, load(args.alerts))
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(report, args.store_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
