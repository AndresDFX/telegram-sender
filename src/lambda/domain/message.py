"""Composición del mensaje a difundir (regla de negocio pura).

Pipeline: quitar líneas de ubicación → aplicar markup → añadir footer (WhatsApp).
Todo parametrizable (los valores los resuelve la capa de config y se inyectan).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from domain.markup import DEFAULT_CURRENCY_SYMBOLS, DEFAULT_MARKUP_PERCENTAGE, aplicar_markup

# Patrones por defecto para quitar líneas del canal fuente: marca/branding + ubicación/horario.
# Robusto a variantes (el formato del origen no siempre es el mismo). Se aplica por LÍNEA
# (case-insensitive); los patrones de dirección exigen un número para no tocar líneas de
# producto. NO marcan precios (eso lo hace el markup aparte).
DEFAULT_LOCATION_PATTERNS = (
    r"ipro[\s_\-]*parts",                  # marca: IPRO PARTS / IPROPARTS / ipro_parts / ipro-parts
    r"@?\s*iproparts",                     # handle del canal (@iproparts)
    r"ubicad",                             # UBICADOS EN ...
    r"direcci[oó]n",                       # Dirección: ...
    r"estamos\s+en",                       # "Estamos en ..."
    r"centro\s+comercial",                 # Centro Comercial ...
    r"\bc\.?\s?c\b",                       # C.C / CC
    r"fortuna",                            # C.C. La Fortuna
    r"s[oó]tano",                          # SÓTANO
    r"\blocal\s+[a-z]?\.?-?\s?\d",         # LOCAL C1-C4 / LOCAL 5
    r"\bpiso\s+\d",                        # PISO 2
    r"\btorre\s+(?:\d+|[a-z])\b",          # TORRE A / TORRE 3 (no "S24 TORRE Edition")
    r"horario",                            # HORARIO: 8:30AM ...
    # Direcciones con número (carrera/calle/avenida/... + opcional cardinal + dígito). Solo
    # palabras/abreviaturas inequívocas (evita 2-letras ambiguas como "av"/"cl" que serían specs);
    # el dígito adyacente evita borrar líneas de producto que solo mencionen la palabra.
    r"\b(?:carrera|cra|calle|cll|avenida|diagonal|transversal|autopista)\b"
    r"(?:\s+(?:norte|sur|este|oeste|nte|occidente|oriente))?\.?\s*#?\s*\d",
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
