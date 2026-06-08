"""Aplica markup porcentual SOLO a precios con contexto de moneda.

El canal fuente publica listas de productos donde los números de modelo y specs
(p.ej. "A06 4-64GB", "5G 8-256GB") NO son precios. Para no alterarlos, solo se
marca un número precedido por un símbolo de moneda (`$`), en formato colombiano
(punto como separador de miles, sin centavos): `$325.000`, `$1.150.000`.

El resultado se redondea al **mil hacia arriba** (configurable) y se reformatea con
puntos de miles: `$325.000` + 15% = 373.750 → `$374.000`.
"""

from __future__ import annotations

import os
import re
from decimal import ROUND_CEILING, Decimal

# Símbolos de moneda que marcan un número como precio (configurable por env).
CURRENCY_SYMBOLS = os.environ.get("CURRENCY_SYMBOLS", "$")
_SYM = "".join(re.escape(c) for c in CURRENCY_SYMBOLS)

# Precio = moneda + número colombiano: miles separados por punto (1+ grupos .NNN)
# o un entero "pelado" de 4+ dígitos. Evita marcar $5, $99, modelos y specs.
PRICE_PATTERN = re.compile(rf"(?P<sym>[{_SYM}])\s*(?P<num>\d{{1,3}}(?:\.\d{{3}})+|\d{{4,}})")

# Granularidad del redondeo hacia arriba (al mil).
_ROUND_TO = 1000


def _formatear_cop(pesos: int, simbolo: str) -> str:
    return f"{simbolo}{pesos:,}".replace(",", ".")


def aplicar_markup(texto: str, porcentaje: float | None = None) -> str:
    markup = porcentaje if porcentaje is not None else float(os.environ.get("MARKUP_PERCENTAGE", "15"))
    factor = Decimal("1") + (Decimal(str(markup)) / Decimal("100"))

    def replacer(match: re.Match[str]) -> str:
        raw = match.group("num").replace(".", "")
        try:
            base = int(raw)
        except ValueError:
            return match.group(0)

        con_markup = Decimal(base) * factor
        # Redondeo al mil hacia arriba.
        miles = (con_markup / _ROUND_TO).to_integral_value(rounding=ROUND_CEILING)
        pesos = int(miles) * _ROUND_TO
        return _formatear_cop(pesos, match.group("sym"))

    return PRICE_PATTERN.sub(replacer, texto)
