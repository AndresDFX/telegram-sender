"""Composición del mensaje a difundir (regla de negocio pura).

Pipeline: quitar líneas de ubicación → aplicar markup → añadir footer (WhatsApp).
Todo parametrizable (los valores los resuelve la capa de config y se inyectan).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from domain.markup import DEFAULT_CURRENCY_SYMBOLS, DEFAULT_MARKUP_PERCENTAGE, aplicar_markup

# Patrones por defecto para quitar líneas del canal fuente: marca/branding + ubicación/horario.
DEFAULT_LOCATION_PATTERNS = (
    r"ipro\s*parts",    # marca del canal fuente ("🔥 IPRO PARTS 🔥") — no reenviarla
    r"ubicad",          # UBICADOS EN EL C.C ...
    r"s[oó]tano",       # SÓTANO LOCAL C1-C4
    r"local\s+c\d",     # LOCAL C1-C4
    r"horario",         # HORARIO: 8:30AM ...
    r"c\.c\b",          # C.C
    r"fortuna",         # CENTRO COMERCIAL LA FORTUNA
)


def quitar_lineas(texto: str, patrones: Sequence[str]) -> str:
    """Elimina las líneas que casen (case-insensitive) con cualquiera de los patrones."""
    if not patrones:
        return texto
    regexes = [re.compile(p, re.IGNORECASE) for p in patrones]
    conservadas = [ln for ln in texto.splitlines() if not any(r.search(ln) for r in regexes)]
    # Colapsa 3+ saltos de línea seguidos (que deja la limpieza) en uno doble.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(conservadas)).strip()


def componer_mensaje(
    texto: str,
    *,
    markup_percentage: float = DEFAULT_MARKUP_PERCENTAGE,
    currency_symbols: str = DEFAULT_CURRENCY_SYMBOLS,
    strip_patterns: Sequence[str] = DEFAULT_LOCATION_PATTERNS,
    footer: str = "",
) -> str:
    limpio = quitar_lineas(texto, strip_patterns)
    con_markup = aplicar_markup(limpio, markup_percentage, currency_symbols=currency_symbols)
    if footer:
        con_markup = f"{con_markup}\n\n{footer}"
    return con_markup
