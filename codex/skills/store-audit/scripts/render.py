#!/usr/bin/env python3
"""
Презентация аудита по report.json (build_report.py): PDF + HTML-дек + вертикальные картинки 1080×1920.

    python3 render.py --report ./audit/report.json --out ./audit-out
    python3 render.py --report ... --out ... --no-images   # PDF и HTML без PNG
    python3 render.py --report ... --out ... --no-pdf      # без PDF

Рендер локальный: HTML пишет сам скрипт, PDF и PNG печатает headless Chrome (путь — --chrome или CHROME_PATH,
по умолчанию ищутся Google Chrome и Chromium). Без Chrome остаётся HTML.
"""
import argparse
import html
import json
import os
import shutil
import subprocess
import sys

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
]

MONTH_RU = {"01": "январь", "02": "февраль", "03": "март", "04": "апрель", "05": "май", "06": "июнь",
            "07": "июль", "08": "август", "09": "сентябрь", "10": "октябрь", "11": "ноябрь", "12": "декабрь"}
MONTH_SHORT = {"01": "янв", "02": "фев", "03": "мар", "04": "апр", "05": "май", "06": "июн",
               "07": "июл", "08": "авг", "09": "сен", "10": "окт", "11": "ноя", "12": "дек"}


def n(v, digits=0):
    """Число по-русски: неразрывный пробел в тысячах, запятая в дробной части; None — «—»."""
    if v is None:
        return "—"
    s = f"{v:,.{digits}f}"
    s = s.replace(",", " ")
    s = s.replace(".", ",")
    return s.replace("-", "−")


def e(s):
    return html.escape(str(s if s is not None else ""))


def month_label(ym, short=False):
    names = MONTH_SHORT if short else MONTH_RU
    label = names.get(ym[5:7], ym)
    return label if short else f"{label} {ym[:4]}"


# ── общий CSS для HTML-дека и для картинок ────────────────────────────
DECK_CSS = """
:root{--ground:#EFEBFB;--ink:#1B1533;--ink-soft:#4A3F73;--muted:#6B5FA8;--card:#fff;
--a:#BCA9F4;--b:#CFC3F9;--pos:#1B8A63;--neg:#D63B63;--line:rgba(27,21,51,.12)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#15102A}}
:root[data-theme="dark"]{--ground:#15102A}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);padding:28px 20px 72px;
font-family:-apple-system,"SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.deck{max-width:1020px;margin:0 auto;display:flex;flex-direction:column;gap:26px}
.slide{background:linear-gradient(158deg,var(--a) 0%,var(--b) 58%,#DAD2FB 100%);border-radius:34px;
padding:52px 48px 56px;position:relative;overflow:hidden;box-shadow:0 18px 40px -24px rgba(27,21,51,.45)}
.slide::after{content:"";position:absolute;right:-140px;top:-120px;width:420px;height:420px;
border-radius:50%;background:rgba(255,255,255,.16)}
.slide>*{position:relative;z-index:1}
h1,h2{font-family:-apple-system,"SF Pro Display",Arial,sans-serif;font-weight:900;letter-spacing:-.035em;
line-height:.93;margin:0;text-transform:uppercase;text-wrap:balance}
h1{font-size:clamp(38px,6.4vw,68px)}h2{font-size:clamp(30px,4.9vw,52px)}
.chip{display:inline-block;padding:.1em .34em .17em;border-radius:16px;background:var(--card)}
.chip.dark{background:var(--ink);color:#F3EFFF}
.kicker{font-size:13px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-soft);margin:0 0 14px}
.lede{font-size:17px;line-height:1.5;color:var(--ink-soft);margin:22px 0 0;max-width:60ch}
.note{font-size:13.5px;line-height:1.55;color:var(--ink-soft);margin:22px 0 0;max-width:72ch}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:34px}
.brand .mark{width:44px;height:44px;border-radius:13px;background:var(--card);display:grid;place-items:center;
font-weight:900;font-size:17px;color:#7C5CE0}
.brand b{display:block;font-size:20px;font-weight:900;letter-spacing:-.02em}
.brand span{display:block;font-size:12.5px;color:var(--ink-soft)}
.hero{font-size:clamp(56px,10.5vw,120px);font-weight:900;letter-spacing:-.05em;line-height:.86;
font-variant-numeric:tabular-nums;margin:26px 0 0}
.hero small{font-size:.34em}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:12px;margin-top:34px}
.tile{background:var(--card);border-radius:22px;padding:20px 22px}
.tile .v{font-size:29px;font-weight:900;letter-spacing:-.025em;font-variant-numeric:tabular-nums;line-height:1}
.tile .l{font-size:13.5px;color:var(--muted);margin-top:8px;line-height:1.4}
.tablewrap{margin-top:30px;background:var(--card);border-radius:22px;padding:8px 6px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;min-width:540px}
th,td{padding:11px 14px;text-align:right;font-size:14.5px}
th{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:800;
border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left;font-weight:800}
tbody tr+tr td{border-top:1px solid var(--line)}
tr.total td{border-top:2px solid var(--ink);font-weight:900;font-size:15.5px;white-space:nowrap}
td.neg{color:var(--neg);font-weight:800}td.q{color:var(--ink-soft);white-space:nowrap}
.steps{margin-top:32px;display:flex;flex-direction:column;gap:10px}
.step{background:var(--card);border-radius:22px;padding:18px 22px;display:grid;
grid-template-columns:1fr auto;gap:18px;align-items:center}
.step.dark{background:var(--ink);color:#F3EFFF}
.step .l{font-weight:800;font-size:16.5px}
.step .s{font-size:13px;color:var(--muted);margin-top:4px;line-height:1.45}
.step.dark .s{color:#B7ADD8}
.step .v{font-size:23px;font-weight:900;font-variant-numeric:tabular-nums;white-space:nowrap}
.neg{color:var(--neg)}.pos{color:var(--pos)}
.bars{margin-top:32px;display:flex;flex-direction:column;gap:9px}
.br{display:grid;grid-template-columns:56px 1fr 130px;align-items:center;gap:14px}
.br .m{font-size:13px;font-weight:800;color:var(--ink-soft);text-transform:uppercase}
.tr{height:26px;border-radius:9px;background:rgba(255,255,255,.42);position:relative;overflow:hidden}
.tr i{position:absolute;top:0;bottom:0;border-radius:9px;background:var(--ink)}
.tr i.n{background:var(--neg)}
.br .v{font-size:14.5px;font-weight:900;text-align:right;font-variant-numeric:tabular-nums}
.share{margin-top:32px;display:flex;flex-direction:column;gap:22px}
.sh .row{display:flex;justify-content:space-between;gap:16px}
.sh .n{font-size:16px;font-weight:800}.sh .a{font-size:16px;font-weight:900;font-variant-numeric:tabular-nums}
.sh .meter{height:10px;border-radius:6px;background:rgba(255,255,255,.5);overflow:hidden;margin-top:8px}
.sh .meter i{display:block;height:100%;border-radius:6px;background:var(--ink)}
.foot{max-width:1020px;margin:34px auto 0;font-size:12.5px;color:var(--muted);line-height:1.6;padding:0 8px}
@media(max-width:640px){.slide{padding:36px 24px 40px}.br{grid-template-columns:44px 1fr 100px}}
"""

STORY_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1080px;height:1920px;overflow:hidden}
body{font-family:-apple-system,"SF Pro Text","Helvetica Neue",Arial,sans-serif;color:#1B1533;
background:linear-gradient(158deg,#BCA9F4 0%,#CFC3F9 55%,#DDD5FC 100%);
padding:86px 76px 78px;display:flex;flex-direction:column;position:relative;-webkit-font-smoothing:antialiased}
body::after{content:"";position:absolute;right:-210px;top:-190px;width:640px;height:640px;
border-radius:50%;background:rgba(255,255,255,.17)}
.wrap{position:relative;z-index:1;display:flex;flex-direction:column;height:100%}
.brand{display:flex;align-items:center;gap:20px;margin-bottom:54px}
.mark{width:74px;height:74px;border-radius:22px;background:#fff;display:grid;place-items:center;
font-weight:900;font-size:26px;color:#7C5CE0}
.brand b{display:block;font-size:33px;font-weight:900;letter-spacing:-.02em}
.brand span{display:block;font-size:21px;color:#4A3F73;margin-top:3px}
.kicker{font-size:21px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#4A3F73;margin-bottom:24px}
h1,h2{font-family:-apple-system,"SF Pro Display",Arial,sans-serif;font-weight:900;letter-spacing:-.04em;
line-height:.9;text-transform:uppercase}
h1{font-size:104px}h2{font-size:82px}
.chip{display:inline-block;padding:.08em .3em .15em;border-radius:26px;background:#fff}
.chip.dark{background:#1B1533;color:#F3EFFF}
.hero{font-size:150px;font-weight:900;letter-spacing:-.055em;line-height:.85;
font-variant-numeric:tabular-nums;margin-top:48px}
.hero small{font-size:.35em}
.lede{font-size:30px;line-height:1.45;color:#4A3F73;margin-top:auto}
.note{font-size:24px;line-height:1.5;color:#4A3F73;margin-top:auto;padding-top:40px}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:38px}
.tile{background:#fff;border-radius:30px;padding:30px 28px}
.tile .v{font-size:44px;font-weight:900;letter-spacing:-.03em;line-height:1;font-variant-numeric:tabular-nums}
.tile .l{font-size:22px;color:#6B5FA8;margin-top:12px;line-height:1.3}
.steps,.cards{display:flex;flex-direction:column;gap:16px;margin-top:auto;margin-bottom:auto}
.step,.card{background:#fff;border-radius:30px;padding:30px 36px;display:grid;
grid-template-columns:1fr auto;gap:26px;align-items:center}
.step.dark,.card.dark{background:#1B1533;color:#F3EFFF}
.step .l,.card .l{font-size:29px;font-weight:800}
.step .s,.card .s{font-size:22px;color:#6B5FA8;margin-top:8px;line-height:1.35}
.step.dark .s,.card.dark .s{color:#B7ADD8}
.step .v,.card .v{font-size:40px;font-weight:900;font-variant-numeric:tabular-nums;white-space:nowrap}
.neg{color:#D63B63}.pos{color:#1B8A63}
.step.dark .v.neg,.card.dark .v.neg{color:#FF8FAE}
.step.dark .v.pos,.card.dark .v.pos{color:#5FE0A8}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;background:#fff;
border-radius:30px;overflow:hidden;margin-top:auto}
th,td{padding:25px 30px;text-align:right;font-size:27px}
th{font-size:19px;letter-spacing:.07em;text-transform:uppercase;color:#6B5FA8;font-weight:800;
border-bottom:2px solid rgba(27,21,51,.12)}
th:first-child,td:first-child{text-align:left;font-weight:800}
tbody tr+tr td{border-top:1px solid rgba(27,21,51,.1)}
tr.total td{border-top:3px solid #1B1533;font-weight:900;font-size:29px;white-space:nowrap}
td.q{color:#4A3F73;font-weight:600;white-space:nowrap}
.bars{display:flex;flex-direction:column;gap:15px;margin-top:auto}
.br{display:grid;grid-template-columns:86px 1fr 230px;align-items:center;gap:22px}
.br .m{font-size:23px;font-weight:800;color:#4A3F73;text-transform:uppercase}
.tr{height:46px;border-radius:15px;background:rgba(255,255,255,.45);position:relative;overflow:hidden}
.tr i{position:absolute;top:0;bottom:0;border-radius:15px;background:#1B1533}
.tr i.n{background:#D63B63}
.br .v{font-size:27px;font-weight:900;text-align:right;font-variant-numeric:tabular-nums}
.share{display:flex;flex-direction:column;gap:34px;margin-top:auto;margin-bottom:auto}
.sh .row{display:flex;justify-content:space-between;gap:20px}
.sh .n{font-size:29px;font-weight:800}.sh .a{font-size:29px;font-weight:900;font-variant-numeric:tabular-nums}
.sh .meter{height:16px;border-radius:9px;background:rgba(255,255,255,.5);overflow:hidden;margin-top:12px}
.sh .meter i{display:block;height:100%;border-radius:9px;background:#1B1533}
.page{position:absolute;left:76px;bottom:44px;font-size:20px;font-weight:700;color:#4A3F73;letter-spacing:.08em;z-index:1}
"""


# ── построение слайдов ────────────────────────────────────────────────

def _share(part, whole, digits=1):
    return n(100 * abs(part) / abs(whole), digits) if whole else "—"


def build_slides(rep):
    meta, months_, tot = rep["meta"], rep["months"], rep["totals"]
    name = meta.get("store_name") or meta.get("store_id") or ""
    initials = "".join(w[0] for w in str(name).split()[:2]).upper() or "OZ"
    period = f'{meta.get("date_from", "")} — {meta.get("date_to", "")}'
    brand = (f'<div class="brand"><div class="mark">{e(initials)}</div>'
             f'<div><b>{e(name)}</b><span>Ozon · {e(period)}</span></div></div>')
    slides = []
    sales = tot["sales"]

    # 1. обложка
    estimate = (f' С учётом рекламы, которую Ozon ещё не списал: {n(tot["estimate"])} ₽.'
                if tot.get("estimate") else "")
    slides.append(brand + f"""
<h1><span class="chip">Чистая</span><br>прибыль</h1>
<div class="hero">{n(tot["net"])} <small>₽</small></div>
<p class="lede">Расчёт по фактическим начислениям Ozon: реализация минус вознаграждение и услуги Ozon,
минус себестоимость проданного и утилизированного, минус НДС и налог.{e(estimate)}</p>
<div class="tiles">
  <div class="tile"><div class="v">{n(sales / 1e6, 1)} млн</div><div class="l">реализация</div></div>
  <div class="tile"><div class="v">{n(tot["accrued"] / 1e6, 1)} млн</div><div class="l">начислено Ozon</div></div>
  <div class="tile"><div class="v">{n(tot["sold_units"])}</div><div class="l">продано единиц</div></div>
</div>""")

    # 2. воронка
    slides.append(f"""
<p class="kicker">Что происходит с каждым рублём</p>
<h2>До вас доходит<br><span class="chip">{_share(tot["accrued"], sales)}%</span></h2>
<div class="steps">
  <div class="step"><div><div class="l">Реализация</div><div class="s">заплатили покупатели, включая баллы Ozon, за вычетом возвратов</div></div><div class="v">{n(sales)}</div></div>
  <div class="step"><div><div class="l">Вознаграждение Ozon</div><div class="s">{_share(tot["commission"], sales)}% от реализации</div></div><div class="v neg">{n(tot["commission"])}</div></div>
  <div class="step"><div><div class="l">Услуги Ozon</div><div class="s">{len(rep["services"]["detail"])} видов начислений</div></div><div class="v neg">{n(tot["services"])}</div></div>
  <div class="step dark"><div><div class="l">Начислено Ozon</div><div class="s">то, что площадка должна выплатить</div></div><div class="v">{n(tot["accrued"])}</div></div>
  <div class="step"><div><div class="l">Себестоимость и налог</div><div class="s">{n(tot["sold_units"])} проданных единиц, {n(tot["disposed_units"])} утилизировано</div></div><div class="v neg">{n(-(tot["cogs"] + tot["disposal_cogs"] + tot["tax"]))}</div></div>
  <div class="step dark"><div><div class="l">Чистая прибыль</div><div class="s">{_share(tot["net"], sales)}% от реализации</div></div><div class="v">{n(tot["net"])}</div></div>
</div>""")

    # 3. помесячные столбики
    if months_:
        vals = [r["net"] for r in months_]
        lo, hi = min(min(vals), 0), max(max(vals), 0)
        span = (hi - lo) or 1
        zero = 100 * (-lo) / span
        bars = []
        for r in months_:
            m = month_label(r["ym"], short=True)
            w = 100 * abs(r["net"]) / span
            if r["net"] >= 0:
                bars.append(f'<div class="br"><span class="m">{m}</span><div class="tr">'
                            f'<i style="left:{zero:.1f}%;width:{w:.1f}%"></i></div>'
                            f'<span class="v">{n(r["net"])}</span></div>')
            else:
                bars.append(f'<div class="br"><span class="m">{m}</span><div class="tr">'
                            f'<i class="n" style="right:{100 - zero:.1f}%;width:{w:.1f}%"></i></div>'
                            f'<span class="v neg">{n(r["net"])}</span></div>')
        best = max(months_, key=lambda r: r["net"])
        losses = [r for r in months_ if r["net"] < 0]
        note = (f'Лучший месяц — {month_label(best["ym"])}: {n(best["net"])} ₽, '
                f'это {_share(best["net"], tot["net"], 0)}% всей прибыли за период.')
        if losses:
            note += f" Убыточных месяцев: {len(losses)} — " + ", ".join(month_label(r["ym"]) for r in losses) + "."
        slides.append(f"""
<p class="kicker">Помесячная динамика</p>
<h2>Прибыль<br>по месяцам</h2>
<div class="bars">{''.join(bars)}</div>
<p class="note">{e(note)}</p>""")

        # 4. помесячная таблица
        trs = "".join(
            f'<tr><td>{month_label(r["ym"])}</td><td>{n(r["accrued"])}</td>'
            f'<td class="q">{n(-(r["cogs"] + r["disposal_cogs"]))}</td><td class="q">{n(-r["tax"])}</td>'
            f'<td class="{"neg" if r["net"] < 0 else ""}">{n(r["net"])}</td></tr>'
            for r in months_)
        slides.append(f"""
<p class="kicker">Помесячно · цифры</p>
<h2>Начислено<br>и заработано</h2>
<table><thead><tr><th>Месяц</th><th>Начислено</th><th>Себест.</th><th>Налог</th><th>Заработано</th></tr></thead>
<tbody>{trs}<tr class="total"><td>Итого</td><td>{n(tot["accrued"])}</td>
<td class="q">{n(-(tot["cogs"] + tot["disposal_cogs"]))}</td><td class="q">{n(-tot["tax"])}</td><td>{n(tot["net"])}</td></tr></tbody></table>
<p class="note">Начислено — то, что площадка должна выплатить после вознаграждения и всех услуг.
Заработано — после себестоимости проданного и утилизированного, НДС и налога.</p>""")

    # 5. услуги — обзор
    cats = list(rep["services"]["by_category"].items())
    if cats:
        mx = max(abs(v) for _, v in cats) or 1
        shares = "".join(
            f'<div class="sh"><div class="row"><span class="n{" pos" if v > 0 else ""}">{e(k)}</span>'
            f'<span class="a{" pos" if v > 0 else ""}">{n(v)}</span></div>'
            f'<div class="meter"><i style="width:{100 * abs(v) / mx:.1f}%{";background:#1B8A63" if v > 0 else ""}"></i></div></div>'
            for k, v in cats)
        slides.append(f"""
<p class="kicker">Услуги Ozon · {n(abs(tot["services"]))} ₽</p>
<h2>На что<br>уходят деньги</h2>
<div class="share">{shares}</div>
<p class="note">Всего {len(rep["services"]["detail"])} видов начислений Ozon.
Это {_share(tot["services"], sales)}% от реализации и {_share(tot["services"], tot["accrued"])}% от начисленного.
Положительные строки — компенсации Ozon, не расход.</p>""")

    # 6+. по слайду на крупную категорию услуг
    detail = rep["services"]["detail"]
    for cat, total in cats:
        rows_cat = [d for d in detail if d["category"] == cat and abs(d["amount"]) >= 1]
        if not rows_cat or abs(total) < abs(tot["services"]) * 0.005:
            continue
        rows_cat.sort(key=lambda d: d["amount"])
        trs = "".join(
            f'<tr><td>{e(d["name"])}</td><td class="{"neg" if d["amount"] < 0 else "pos"}">{n(d["amount"])}</td>'
            f'<td class="q">{_share(d["amount"], total)}%</td></tr>'
            for d in rows_cat[:14])
        slides.append(f"""
<p class="kicker">{e(cat)}</p>
<h2><span class="chip dark">{n(abs(total) / 1e6, 2)} млн</span><br>{e(cat.lower())}</h2>
<table><thead><tr><th>Начисление Ozon</th><th>Сумма</th><th>Доля</th></tr></thead>
<tbody>{trs}<tr class="total"><td>Итого</td><td>{n(total)}</td><td class="q">100%</td></tr></tbody></table>
<p class="note">Доля от всех услуг Ozon — {_share(total, tot["services"])}%.</p>""")

    # утилизация по товарам
    disp = rep["disposal"]["by_product"][:12]
    if disp:
        trs = "".join(f'<tr><td>{e(d["offer_id"])}</td><td class="q">{n(d["qty"])}</td>'
                      f'<td class="neg">{n(-d["cost"])}</td></tr>' for d in disp)
        slides.append(f"""
<p class="kicker">Что списывалось чаще всего</p>
<h2>Утилизировано<br><span class="chip">{n(tot["disposed_units"])}</span> единиц</h2>
<table><thead><tr><th>Товар</th><th>Штук</th><th>Себестоимость</th></tr></thead>
<tbody>{trs}<tr class="total"><td>Итого</td><td class="q">{n(tot["disposed_units"])}</td>
<td class="neg">{n(-tot["disposal_cogs"])}</td></tr></tbody></table>
<p class="note">Себестоимость списанного товара — прямой убыток сверх сбора Ozon за утилизацию
({n(rep["disposal"]["fee"])} ₽ за период).</p>""")

    # прибыль с единицы по товарам
    ue = rep.get("unit_economics") or []
    if ue:
        top = ue[:12]
        trs = "".join(
            f'<tr><td>{e(x["offer_id"])}</td><td class="q">{n(x["units"])}</td>'
            f'<td class="q">{n(x["accrual"])}</td><td class="q">{n(x["commission_pct"], 1)}%</td>'
            f'<td class="q">{n(x["logistics"])}</td><td class="q">{n(x["cost"])}</td>'
            f'<td class="{"neg" if x["margin"] < 0 else ""}">{n(x["margin"])}</td></tr>'
            for x in top)
        worst = min(top, key=lambda x: x["margin_pct"])
        best = max(top, key=lambda x: x["margin_pct"])
        slides.append(f"""
<p class="kicker">Сколько остаётся с одной продажи</p>
<h2>Прибыль<br>с единицы</h2>
<table><thead><tr><th>Товар</th><th>Шт</th><th>Чек</th><th>Вознагр.</th><th>Логист.</th><th>Себест.</th><th>Прибыль</th></tr></thead>
<tbody>{trs}</tbody></table>
<p class="note">Чек — реализация на проданную единицу (нетто возвратов); прибыль — чистая прибыль товара по начислениям
на единицу: все его начисления, себестоимость и налог. Лучший — {e(best["offer_id"])} ({n(best["margin"])} ₽,
{n(best["margin_pct"], 1)}%), худший — {e(worst["offer_id"])} ({n(worst["margin"])} ₽, {n(worst["margin_pct"], 1)}%).</p>""")

    # невыкупы и возвраты
    npk = rep.get("non_pickup") or {}
    st = rep.get("stock") or {}
    if npk.get("amount") or st.get("removal"):
        count = npk.get("count")
        avg = f'в среднем {n(abs(npk["amount"]) / count)} ₽ за невыкуп. ' if count else ""
        slides.append(f"""
<p class="kicker">Возвраты и вывоз</p>
<h2>Товар,<br>уехавший назад</h2>
<div class="tiles">
  <div class="tile"><div class="v">{n(abs(npk.get("amount") or 0))}</div><div class="l">₽ обратной логистики и обработки невыкупов</div></div>
  <div class="tile"><div class="v">{n(count)}</div><div class="l">{"строк обратной логистики" if npk.get("basis") == "rows" else "невыкупов и возвратов (отправлений)"}</div></div>
  <div class="tile"><div class="v">{n(st.get("removal"))}</div><div class="l">единиц к вывозу и в пути к вам сейчас</div></div>
</div>
<p class="note">Невыкуп — покупатель не забрал заказ: выручки нет, а логистика списывается; {avg}
Вывоз — товар, который вы забираете со складов Ozon; он остаётся вашим активом.</p>""")

    # остатки по кластерам
    if st.get("total"):
        cl = list((st.get("by_cluster") or {}).items())[:10]
        trs = "".join(f'<tr><td>{e(k)}</td><td class="q">{n(v)}</td>'
                      f'<td class="q">{_share(v, st["total"])}%</td></tr>' for k, v in cl)
        slides.append(f"""
<p class="kicker">Что лежит на складах сейчас</p>
<h2>Остаток<br><span class="chip">{n(st["total"])}</span> шт</h2>
<table><thead><tr><th>Кластер</th><th>Штук</th><th>Доля</th></tr></thead><tbody>{trs}</tbody></table>
<div class="tiles">
  <div class="tile"><div class="v">{n(st.get("inbound"))}</div><div class="l">едет на склады в поставках</div></div>
  <div class="tile"><div class="v">{n(st.get("goods_cost"))}</div><div class="l">₽ всего товара по себестоимости</div></div>
  <div class="tile"><div class="v">{n(st.get("assets_total"))}</div><div class="l">₽ всех активов с балансом</div></div>
</div>
<p class="note">Товар на складах Ozon на {e((st.get("as_of") or "")[:16].replace("T", " "))}, включая брак, излишки и
истекающий срок. Заявки на поставку ({n(st.get("requested"))} шт) — не активы.</p>""")

    # материальный баланс FBO
    mb = rep.get("material_balance")
    if mb and mb.get("ledger_units"):
        trs = "".join(
            f'<tr><td>{e(r["offer_id"])}</td><td class="q">{n(r["ledger"])}</td>'
            f'<td class="q">{n(r["ozon_stock"] + r["in_delivery"])}</td>'
            f'<td class="{"neg" if r["gap"] > 0 else "q"}">{n(r["gap"])}</td></tr>'
            for r in [r for r in mb["rows"] if r["gap"]][:10])
        slides.append(f"""
<p class="kicker">Материальный баланс FBO</p>
<h2>Куда делся<br>весь товар</h2>
<div class="steps">
  <div class="step dark"><div><div class="l">Должно быть на Ozon</div><div class="s">принято по актам, минус продажи, утилизация и вывоз, плюс возвраты</div></div><div class="v">{n(mb["ledger_units"])}</div></div>
  <div class="step"><div><div class="l">Есть у Ozon</div><div class="s">на складах и в доставке покупателям</div></div><div class="v">{n(mb["ozon_units"] + mb["in_delivery_units"])}</div></div>
  <div class="step"><div><div class="l">Недостача</div><div class="s">товар принят, но не продан, не списан и не вывезен</div></div><div class="v neg">{n(mb["shortage_units"])}</div></div>
  <div class="step"><div><div class="l">Излишек</div><div class="s">у Ozon больше, чем по журналу</div></div><div class="v">{n(mb["surplus_units"])}</div></div>
</div>
<table><thead><tr><th>Товар</th><th>По журналу</th><th>У Ozon</th><th>Разница</th></tr></thead><tbody>{trs}</tbody></table>
<p class="note">Недостача по себестоимости — {n(mb["shortage_cost"])} ₽. На здоровом магазине это доли процента от принятого;
2–3 % и больше — сначала ищите ошибку данных (акт приёмки ещё не пришёл, поставка учтена по заявке), а не потери.</p>""")

    # дополнительные строки от пользователя
    if rep.get("extra"):
        cards = "".join(
            f'<div class="step"><div><div class="l">{e(x.get("label", ""))}</div>'
            f'<div class="s">{e(x.get("note", ""))}</div></div><div class="v">{n(x.get("amount", 0))}</div></div>'
            for x in rep["extra"])
        total_extra = sum(x.get("amount", 0) for x in rep["extra"])
        slides.append(f"""
<p class="kicker">Дополнительно</p>
<h2>Нашлось<br><span class="chip">{n(total_extra / 1e6, 2)} млн</span></h2>
<div class="steps">{cards}
<div class="step dark"><div><div class="l">Итого</div><div class="s">строки, добавленные вручную</div></div><div class="v">{n(total_extra)}</div></div></div>""")

    return slides


def render_html(rep, slides, out_path):
    name = rep["meta"].get("store_name") or rep["meta"].get("store_id") or ""
    body = "".join(f'<section class="slide">{s}</section>' for s in slides)
    warn = "".join(f"<li>{e(w)}</li>" for w in rep.get("warnings") or [])
    doc = (f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Аудит {e(name)}</title>\n<style>{DECK_CSS}</style>\n"
           f'<div class="deck">{body}</div>\n'
           f'<div class="foot"><p>Источник — Seller AI: отчёт «Чистая прибыль» и «Юнит-экономика → По начислениям» '
           f'(итог сходится с «Начислениями» до копейки), «Активы» и партии поставок FBO.</p>'
           f'{"<ul>" + warn + "</ul>" if warn else ""}</div>')
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return out_path


def render_images(slides, out_dir, chrome, prefix):
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for i, body in enumerate(slides, 1):
        doc = (f"<!doctype html><meta charset='utf-8'><style>{STORY_CSS}</style>"
               f"<div class='wrap'>{body}</div><div class='page'>{i:02d} / {len(slides)}</div>")
        html_path = os.path.join(out_dir, f"_slide{i:02d}.html")
        png = os.path.join(out_dir, f"{prefix}-{i:02d}.png")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(doc)
        subprocess.run([chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", f"--screenshot={png}",
                        "--window-size=1080,1920", f"file://{html_path}"],
                       capture_output=True, timeout=120)
        os.remove(html_path)
        if os.path.exists(png):
            made.append(png)
            print(f"{png}", file=sys.stderr)
    return made


def render_pdf(slides, out_path, chrome):
    """Все слайды одним PDF: страница ровно 1080×1920, без полей и колонтитулов."""
    pages = "".join(
        f'<section class="page-slide"><div class="wrap">{body}</div>'
        f'<div class="page">{i:02d} / {len(slides)}</div></section>'
        for i, body in enumerate(slides, 1))
    doc = (f"<!doctype html><meta charset='utf-8'><style>{STORY_CSS}\n"
           "@page{size:1080px 1920px;margin:0}\n"
           "html,body{width:auto;height:auto;overflow:visible;padding:0;background:none}\n"
           "body::after{content:none}\n"
           ".page-slide{width:1080px;height:1920px;padding:86px 76px 78px;position:relative;"
           "display:flex;flex-direction:column;break-after:page;overflow:hidden;"
           "background:linear-gradient(158deg,#BCA9F4 0%,#CFC3F9 55%,#DDD5FC 100%);"
           "-webkit-print-color-adjust:exact;print-color-adjust:exact}\n"
           ".page-slide:last-child{break-after:auto}\n"
           ".page-slide::after{content:'';position:absolute;right:-210px;top:-190px;width:640px;"
           "height:640px;border-radius:50%;background:rgba(255,255,255,.17)}\n"
           f"</style>{pages}")
    tmp = out_path + ".html"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(doc)
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out_path}", f"file://{tmp}"],
                   capture_output=True, timeout=300)
    os.remove(tmp)
    return out_path if os.path.exists(out_path) else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Презентация по отчёту аудита")
    ap.add_argument("--report", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-images", action="store_true", help="не делать PNG")
    ap.add_argument("--no-pdf", action="store_true", help="не делать PDF")
    ap.add_argument("--no-html", action="store_true", help="не делать HTML-дек")
    ap.add_argument("--chrome")
    ap.add_argument("--prefix", default="audit")
    args = ap.parse_args(argv)

    with open(args.report, encoding="utf-8") as fh:
        rep = json.load(fh)
    os.makedirs(args.out, exist_ok=True)
    slides = build_slides(rep)
    name = rep["meta"].get("store_name") or rep["meta"].get("store_id") or "audit"
    if not args.no_html:
        html_path = render_html(rep, slides, os.path.join(args.out, f"Аудит {name}.html"))
        print(f"HTML: {html_path}", file=sys.stderr)

    chrome = None
    if not (args.no_images and args.no_pdf):
        chrome = args.chrome or next((c for c in CHROME_CANDIDATES if c and os.path.exists(c)), None)
        if not chrome:
            print("Chrome не найден — PDF и картинки пропущены (--chrome или CHROME_PATH)", file=sys.stderr)
    if chrome and not args.no_pdf:
        pdf = render_pdf(slides, os.path.join(args.out, f"Аудит {name}.pdf"), chrome)
        print(f"PDF: {pdf}", file=sys.stderr)
    if chrome and not args.no_images:
        render_images(slides, args.out, chrome, args.prefix)
    print(f"слайдов: {len(slides)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
