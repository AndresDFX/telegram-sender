"""Composición del mensaje a difundir (regla de negocio pura).

Pipeline: quitar líneas de ubicación → aplicar markup → añadir footer (WhatsApp).
Todo parametrizable (los valores los resuelve la capa de config y se inyectan).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from domain.markup import DEFAULT_CURRENCY_SYMBOLS, DEFAULT_MARKUP_PERCENTAGE, aplicar_markup, desglosar_precios

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


# Teléfono colombiano: móvil 3XX o fijo 60X (10 dígitos nacionales). Para NO borrar por error
# referencias/costos/cantidades (números de 10 dígitos PEGADOS sin formato), se exige una
# SEÑAL de teléfono: separador entre grupos, indicativo +57, paréntesis, o una palabra de
# contacto adyacente. Un número de 10 dígitos pelado y sin etiqueta NO se toca (es ambiguo).
_AREA = r"(?:3\d{2}|60\d)"          # móvil 3XX o fijo 60X
_S = r"[\s.\-]"                     # separadores admitidos (espacio, punto, guion)

# (1) Teléfono FORMATEADO: +57, paréntesis, o al menos un separador entre los grupos.
_TEL_FORMATEADO = (
    r"(?<![\d$\U0001F4B2\U0001F4B8])"        # no tras dígito ni símbolo de moneda
    r"(?:"
    rf"\+?\s?57{_S}*\(?\s*{_AREA}\s*\)?{_S}*\d{{3}}{_S}*\d{{4}}"   # indicativo +57/57
    rf"|\(\s*{_AREA}\s*\){_S}*\d{{3}}{_S}*\d{{4}}"                  # (área) entre paréntesis
    rf"|{_AREA}{_S}+\d{{3}}{_S}*\d{{4}}"                            # >=1 separador tras el área
    rf"|{_AREA}{_S}*\d{{3}}{_S}+\d{{4}}"                            # >=1 separador antes del final
    r")"
    r"(?!\d)"
)
# (2) Teléfono ETIQUETADO: palabra de contacto inmediatamente seguida del número (aunque vaya
# pegado). Cubre "Cel 3001234567", "WhatsApp: 3001234567", "Pedidos 3001234567".
_KW_CONTACTO = r"(?:whats?app|wsp|wpp|cel(?:ular)?|tel[eé]fonos?|tel|pedidos?|domicilios?|contacto|cont[aá]ctanos|ll[aá]manos)"
_TEL_ETIQUETADO = (
    rf"\b{_KW_CONTACTO}\b[\s:.\-]*"
    rf"(?:\+?57{_S}*)?\(?\s*{_AREA}\s*\)?{_S}*\d{{3}}{_S}*\d{{4}}(?!\d)"
)
DEFAULT_PHONE_PATTERNS = (_TEL_FORMATEADO, _TEL_ETIQUETADO)


def quitar_lineas(texto: str, patrones: Sequence[str]) -> str:
    """Elimina las líneas que casen (case-insensitive) con cualquiera de los patrones."""
    if not patrones:
        return texto
    regexes = [re.compile(p, re.IGNORECASE) for p in patrones]
    conservadas = [ln for ln in texto.splitlines() if not any(r.search(ln) for r in regexes)]
    # Colapsa 3+ saltos de línea seguidos (que deja la limpieza) en uno doble.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(conservadas)).strip()


_ALFANUM = re.compile(r"[0-9A-Za-zÁÉÍÓÚáéíóúÑñ]")


def quitar_telefonos(texto: str, patrones: Sequence[str]) -> str:
    """Quita SOLO la coincidencia de teléfono de cada línea (no la línea entera).

    M3: una línea de catálogo suele mezclar producto + precio + teléfono; borrar la línea entera
    perdía el producto y su PRECIO. Aquí se sustituye solo el match del teléfono; si tras quitarlo la
    línea queda sin contenido útil (sin letras ni dígitos, p. ej. era solo "Cel 300…"), se elimina."""
    if not patrones:
        return texto
    # Un SOLO regex (una pasada izquierda-a-derecha): si se aplicaran por separado, el patrón del
    # número "pelado" borraría el dígito antes que el patrón ETIQUETADO, dejando la etiqueta huérfana
    # ("Pedidos:"). Combinados, en la posición de la etiqueta gana la alternativa que consume
    # keyword+número juntos.
    regex = re.compile("|".join(f"(?:{p})" for p in patrones), re.IGNORECASE)
    salida = []
    for ln in texto.splitlines():
        limpia = regex.sub("", ln)
        if limpia != ln and not _ALFANUM.search(limpia):
            continue  # la línea era solo teléfono/etiqueta: se descarta entera
        # Colapsa los espacios dobles que deja la sustitución dentro de la línea.
        salida.append(re.sub(r"[ \t]{2,}", " ", limpia).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(salida)).strip()


def componer_con_desglose(
    texto: str,
    *,
    markup_percentage: float = DEFAULT_MARKUP_PERCENTAGE,
    currency_symbols: str = DEFAULT_CURRENCY_SYMBOLS,
    strip_patterns: Sequence[str] = DEFAULT_LOCATION_PATTERNS,
    footer: str = "",
) -> tuple[str, list[dict]]:
    """Como ``componer_mensaje`` pero devuelve también el DESGLOSE de precios (anterior→nuevo por
    producto), calculado sobre el mismo texto limpio que se marca (fuente única, sin desalineación).
    Devuelve ``(mensaje, desglose)``; ``("", [])`` si tras la limpieza no queda contenido."""
    limpio = quitar_lineas(texto, strip_patterns)
    limpio = quitar_telefonos(limpio, DEFAULT_PHONE_PATTERNS)  # M3: quita el teléfono, conserva producto/precio
    con_markup = aplicar_markup(limpio, markup_percentage, currency_symbols=currency_symbols)
    # M4: si la limpieza dejó el cuerpo vacío, NO componer (devolver solo el footer sería spam);
    # el llamador (captura del canal) salta los mensajes vacíos.
    if not con_markup.strip():
        return "", []
    desglose = desglosar_precios(limpio, con_markup, currency_symbols=currency_symbols)
    if footer:
        con_markup = f"{con_markup}\n\n{footer}"
    return con_markup, desglose


def componer_mensaje(
    texto: str,
    *,
    markup_percentage: float = DEFAULT_MARKUP_PERCENTAGE,
    currency_symbols: str = DEFAULT_CURRENCY_SYMBOLS,
    strip_patterns: Sequence[str] = DEFAULT_LOCATION_PATTERNS,
    footer: str = "",
) -> str:
    mensaje, _ = componer_con_desglose(
        texto, markup_percentage=markup_percentage, currency_symbols=currency_symbols,
        strip_patterns=strip_patterns, footer=footer,
    )
    return mensaje
