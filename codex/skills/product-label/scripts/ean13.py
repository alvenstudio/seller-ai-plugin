"""EAN-13 для этикеток: проверка, дописывание контрольной цифры, новый код. Чистые функции, без зависимостей."""
import random


def ean13_check_digit(code12: str) -> str:
    """Контрольная цифра EAN-13: цифры на нечётных местах ×1, на чётных ×3, дополнение до кратного 10."""
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(code12))
    return str((10 - total % 10) % 10)


def normalize_ean13(value) -> tuple[str, str | None]:
    """→ (13 цифр или "", предупреждение или None). 12 цифр — EAN-13 без контрольной: дописать её."""
    v = "".join(str(value or "").split())
    if not v:
        return "", None
    if not v.isdigit() or len(v) not in (12, 13):
        return "", f"штрихкод {v!r} не EAN-13 — на этикетку не выведен"
    if len(v) == 12:
        return v + ean13_check_digit(v), None
    if v[-1] != ean13_check_digit(v[:12]):
        return "", (f"штрихкод {v!r}: контрольная цифра не сходится (должна быть {ean13_check_digit(v[:12])}) — "
                    f"на этикетку не выведен")
    return v, None


def make_ean13(prefix: str = "200", rng=None) -> str:
    """Новый EAN-13 для разовой этикетки (к карточке Ozon не привязан — так и сказать пользователю)."""
    rng = rng or random.Random()
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(12 - len(prefix)))
    return body + ean13_check_digit(body)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="EAN-13: проверить код или сделать новый (не привязан к карточке Ozon)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check"); p.add_argument("code")
    p = sub.add_parser("new"); p.add_argument("--prefix", default="200")
    a = ap.parse_args(argv)
    if a.command == "new":
        print(make_ean13(a.prefix))
        return 0
    code, warning = normalize_ean13(a.code)
    print(code or warning)
    return 0 if code else 1


if __name__ == "__main__":
    raise SystemExit(main())
