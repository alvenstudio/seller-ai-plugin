#!/usr/bin/env python3
"""Справочник вознаграждения Ozon из XLSX («Таблица категорий и типов для расчёта вознаграждения», лист «Прайс РФ (БЗ)») в TSV
навыка — только stdlib (zipfile + xml), ничего не вызывает.

    python3 table_from_xlsx.py <файл.xlsx> --from 2026-10-07 [--out references/commissions-2026-10-07.tsv] [--sheet "…"]

Шапка Ozon — две строки (группы FBO / FBO Fresh / FBS / RFBS / «RFBS мототехника» и диапазоны цены), данные — с третьей:
«Основная категория», «Категория», «Тип товара» и 11 ставок долями (0.4 = 40 %). В TSV — проценты без хвостов float
(0.43000000000000005 → 43), пустая ячейка — пустая (у Ozon ставки нет), строки без «Типа товара» пропускаются. Другой состав
колонок — код 1 с текстом: файл не тот или Ozon поменял таблицу.
"""
import argparse
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
HEADER = ["Основная категория", "Категория", "Тип товара", "FBO до 100", "FBO 100-300", "FBO свыше 300", "FBO Fresh до 100",
          "FBO Fresh 100-300", "FBO Fresh свыше 300", "FBS до 100", "FBS 100-300", "FBS свыше 300", "RFBS",
          "RFBS мототехника свыше 500 тыс."]
GROUPS = {3: "fbo", 6: "fbo fresh", 9: "fbs", 12: "rfbs"}
REFERENCES = Path(__file__).resolve().parent.parent / "references"


class TableError(Exception):
    pass


def _text(node):
    return "".join(t.text or "" for t in node.iter(M + "t"))


def _norm(value):
    return " ".join(str(value or "").split())


def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_rows(path, sheet=None):
    """Строки листа: списки значений (str — текст, Decimal — число, None — пусто)."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = [_text(si) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(M + "si")]
        sheets = ET.fromstring(z.read("xl/workbook.xml")).iter(M + "sheet")
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(REL)}
        chosen = [s for s in sheets if sheet is None or s.get("name") == sheet]
        if not chosen:
            raise TableError(f"Нет листа «{sheet}»")
        target = rels[chosen[0].get(R_ID)].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(target)).iter(M + "row"):
            cells, nxt = {}, 0
            for c in row.iter(M + "c"):
                i = _col(c.get("r")) if c.get("r") else nxt
                nxt = i + 1
                kind, v = c.get("t"), c.find(M + "v")
                if kind == "s":
                    cells[i] = shared[int(v.text)]
                elif kind == "inlineStr":
                    cells[i] = _text(c)
                elif v is None or v.text is None:
                    cells[i] = None
                elif kind in ("str", "e", "b"):
                    cells[i] = v.text
                else:
                    cells[i] = Decimal(v.text)
            rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
        return rows


def _check_header(rows):
    if len(rows) < 2:
        raise TableError("Меньше двух строк шапки")
    top, names = [_norm(x).lower() for x in rows[0]] + [""] * 14, [_norm(x) for x in rows[1]] + [""] * 14
    bad = [f"{i + 1}: «{top[i]}»" for i, g in GROUPS.items() if top[i] != g]
    if "мототехника" not in top[13]:
        bad.append(f"14: «{top[13]}»")
    if names[:3] != HEADER[:3]:
        bad.append(f"названия: {names[:3]}")
    for i in (3, 6, 9):
        if not (names[i].startswith("до 100") and "300" in names[i + 1] and names[i + 2].startswith("свыше 300")):
            bad.append(f"диапазоны с колонки {i + 1}: {names[i:i + 3]}")
    if bad:
        raise TableError("Шапка не как у таблицы вознаграждения Ozon — " + "; ".join(bad))


def _pct(value, where):
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    try:
        if isinstance(value, str):
            s = value.strip().replace(",", ".")
            share = Decimal(s[:-1]) / 100 if s.endswith("%") else Decimal(s)
        else:
            share = value
    except InvalidOperation:
        raise TableError(f"{where}: не число «{value}»")
    return format((share * 100).quantize(Decimal("0.000001")).normalize(), "f")


def convert(rows):
    _check_header(rows)
    out = [HEADER]
    for n, row in enumerate(rows[2:], start=3):
        row = list(row) + [None] * (14 - len(row))
        if not _norm(row[2]):
            continue
        out.append([_norm(x) for x in row[:3]] + [_pct(row[i], f"строка {n}, колонка {i + 1}") for i in range(3, 14)])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx")
    ap.add_argument("--from", dest="since", required=True, help="дата, с которой действует таблица, ГГГГ-ММ-ДД")
    ap.add_argument("--out")
    ap.add_argument("--sheet")
    a = ap.parse_args(argv)
    try:
        since = date.fromisoformat(a.since)
        table = convert(read_rows(a.xlsx, a.sheet))
    except (TableError, ValueError, KeyError, zipfile.BadZipFile) as e:
        print(f"Не получилось: {e}", file=sys.stderr)
        return 1
    out = Path(a.out) if a.out else REFERENCES / f"commissions-{since.isoformat()}.tsv"
    out.write_text("".join("\t".join(r) + "\n" for r in table), encoding="utf-8")
    print(f"{out}: {len(table) - 1} строк")
    return 0


if __name__ == "__main__":
    sys.exit(main())
