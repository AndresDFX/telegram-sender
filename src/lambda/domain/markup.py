"""Regla de negocio pura: aplicar markup porcentual a los precios de un texto.

Sin I/O ni lectura de entorno (eso lo resuelve la capa de aplicación/config y se
inyecta como parámetros). Solo marca números con símbolo de moneda (`$`) en formato
colombiano (punto = miles, sin centavos) y redondea el resultado al **mil hacia
arriba**, sin tocar modelos ni specs (p.ej. "A06 4-64GB").
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, Decimal

DEFAULT_CURRENCY_SYMBOLS = "$"
DEFAULT_MARKUP_PERCENTAGE = 15.0
_ROUND_TO = 1000


def _price_pattern(currency_symbols: str) -> re.Pattern[str]:
    sym = "".join(re.escape(c) for c in currency_symbols)
    # moneda + número colombiano: miles separados por punto (1+ grupos .NNN) o entero de 4+ dígitos.
    return re.compile(rf"(?P<sym>[{sym}])\s*(?P<num>\d{{1,3}}(?:\.\d{{3}})+|\d{{4,}})")


def _formatear_cop(pesos: int, simbolo: str) -> str:
    return f"{simbolo}{pesos:,}".replace(",", ".")


def aplicar_markup(
    texto: str,
    porcentaje: float = DEFAULT_MARKUP_PERCENTAGE,
    *,
    currency_symbols: str = DEFAULT_CURRENCY_SYMBOLS,
) -> str:
    factor = Decimal("1") + (Decimal(str(porcentaje)) / Decimal("100"))

    def replacer(match: re.Match[str]) -> str:
        raw = match.group("num").replace(".", "")
        try:
            base = int(raw)
        except ValueError:
            return match.group(0)

        con_markup = Decimal(base) * factor
        miles = (con_markup / _ROUND_TO).to_integral_value(rounding=ROUND_CEILING)
        return _formatear_cop(int(miles) * _ROUND_TO, match.group("sym"))

    return _price_pattern(currency_symbols).sub(replacer, texto)
