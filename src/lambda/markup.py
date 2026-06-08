"""Aplica markup porcentual a precios detectados en el texto del mensaje."""

from __future__ import annotations

import os
import re
from decimal import Decimal, ROUND_HALF_UP

PRICE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})|\d+(?:\.\d{2})?)(?!\d)"
)


def _parse_price(raw: str) -> Decimal | None:
    normalized = raw.strip()
    if not normalized:
        return None

    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        parts = normalized.split(",")
        normalized = parts[0].replace(".", "") + "." + parts[1] if len(parts) == 2 else normalized.replace(",", ".")
    else:
        normalized = normalized.replace(",", "")

    try:
        value = Decimal(normalized)
    except Exception:
        return None

    return value if value > 0 else None


def _format_price(value: Decimal, template: str) -> str:
    if "," in template and "." in template:
        if template.rfind(",") > template.rfind("."):
            integer_part, decimal_part = f"{value:.2f}".split(".")
            grouped = f"{int(integer_part):,}".replace(",", ".")
            return f"{grouped},{decimal_part}"
        integer_part, decimal_part = f"{value:.2f}".split(".")
        grouped = f"{int(integer_part):,}"
        return f"{grouped}.{decimal_part}"

    if "," in template:
        integer_part, decimal_part = f"{value:.2f}".split(".")
        return f"{integer_part},{decimal_part}"

    if "." in template:
        return f"{value:.2f}"

    return str(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def aplicar_markup(texto: str, porcentaje: float | None = None) -> str:
    markup = porcentaje if porcentaje is not None else float(os.environ.get("MARKUP_PERCENTAGE", "15"))

    def replacer(match: re.Match[str]) -> str:
        original = match.group(0)
        parsed = _parse_price(original)
        if parsed is None:
            return original

        factor = Decimal("1") + (Decimal(str(markup)) / Decimal("100"))
        updated = (parsed * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return _format_price(updated, original)

    return PRICE_PATTERN.sub(replacer, texto)
