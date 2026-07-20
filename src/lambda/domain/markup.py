"""Regla de negocio pura: aplicar markup porcentual a los precios de un texto.

Sin I/O ni lectura de entorno (eso lo resuelve la capa de aplicación/config y se
inyecta como parámetros). Marca SOLO números acompañados de un marcador de moneda
(símbolo ``$`` / ``💸`` / ``💲`` o la palabra ``COP``) para NO tocar modelos, specs,
teléfonos ni fechas. Es robusto a variantes del canal fuente (los originales no
siempre llegan igual):

  $325.000   $ 325.000   $325000   $1.150.000   $1'150.000   $1’150.000   $1'150'000
  💲2.400.000   COP 325.000   COP $325.000   325.000$   325.000 COP

Redondea el resultado al **mil hacia arriba** y conserva el símbolo líder cuando lo hay
(en los demás casos normaliza a ``$``).
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, Decimal

# El canal marca precios con varios símbolos: "$" y los emojis 💸 (billete) y 💲 (dólar).
DEFAULT_CURRENCY_SYMBOLS = "$\U0001F4B8\U0001F4B2"
DEFAULT_MARKUP_PERCENTAGE = 15.0
_ROUND_TO = 1000

# Separadores de miles colombianos: punto, apóstrofo recto (') y tipográfico (’ U+2019).
# NO incluye la coma aquí: en Colombia la coma es DECIMAL (no mezclar con miles).
_MILES = r"[.’']"
# Número en pesos, con grupos de miles CONSISTENTES (no se mezclan separadores):
#   1.150.000 / 1'150.000 / 1’150’000  (colombiano)  ·  1,150,000 (formato US)  ·  325000 (4+ dígitos)
_NUM = rf"\d{{1,3}}(?:{_MILES}\d{{3}})+|\d{{1,3}}(?:,\d{{3}})+|\d{{4,}}"
# "COP" como palabra suelta (no dentro de otra, p.ej. SCOPE), insensible a may/min.
_COP = r"(?<![A-Za-z])[Cc][Oo][Pp](?![A-Za-z])"
# Al escalar quitamos cualquier separador de miles (punto, coma o apóstrofo).
_STRIP = re.compile(r"[.,’']")


def _price_pattern(currency_symbols: str) -> re.Pattern[str]:
    sym = "".join(re.escape(c) for c in currency_symbols)
    s = rf"[{sym}]"
    # 4 formas, TODAS exigen un marcador de moneda adyacente (símbolo o COP). El orden importa:
    # símbolo-antes primero (caso dominante), luego COP-antes, símbolo-después y COP-después.
    # El separador entre marcador y número es [ \t]* (NO \s*) para que un precio NUNCA cruce
    # un salto de línea y "robe" el símbolo del precio de la línea siguiente.
    g = r"[ \t]*"
    return re.compile(
        rf"(?P<sym>{s}){g}(?P<num>{_NUM})"            # $1.150.000 / 💲 1150000
        rf"|{_COP}{g}\$?{g}(?P<num2>{_NUM})"          # COP 1.150.000 / COP $1.150.000
        rf"|(?P<num3>{_NUM}){g}(?P<sym3>{s})"         # 1.150.000$
        rf"|(?P<num4>{_NUM}){g}{_COP}"                # 1.150.000 COP
    )


def _escalar(raw: str, factor: Decimal) -> int | None:
    limpio = _STRIP.sub("", raw)
    if len(limpio) > 1 and limpio[0] == "0":
        return None  # precio mal formado (cero a la izquierda): no tocar, evita corromperlo
    try:
        base = int(limpio)
    except ValueError:
        return None
    con_markup = Decimal(base) * factor
    miles = (con_markup / _ROUND_TO).to_integral_value(rounding=ROUND_CEILING)
    return int(miles) * _ROUND_TO


def _formatear_cop(pesos: int, simbolo: str = "$") -> str:
    return f"{simbolo}{pesos:,}".replace(",", ".")


def desglosar_precios(
    limpio: str,
    marcado: str,
    *,
    currency_symbols: str = DEFAULT_CURRENCY_SYMBOLS,
) -> list[dict]:
    """Empareja el precio ANTERIOR con el NUEVO por cada producto, para mostrarlo en el panel.

    Recibe el texto YA LIMPIO (pre-markup) y el MARCADO (post-markup): como el markup reemplaza cada
    precio en su sitio 1:1, la N-ésima coincidencia de precio en ``limpio`` corresponde a la N-ésima
    en ``marcado``. Devuelve una fila por precio: ``{"producto": <línea>, "anterior": "$325.000",
    "nuevo": "$374.000"}``. Si por alguna razón los conteos no cuadran, empareja hasta el menor
    (best-effort) para no inventar correspondencias."""
    pat = _price_pattern(currency_symbols)

    def _linea(texto: str, pos: int) -> str:
        ini = texto.rfind("\n", 0, pos) + 1
        fin = texto.find("\n", pos)
        return texto[ini:(fin if fin != -1 else len(texto))].strip()

    ant = [(m.group(0).strip(), _linea(limpio, m.start())) for m in pat.finditer(limpio)]
    nue = [m.group(0).strip() for m in pat.finditer(marcado)]
    filas = []
    for (anterior, producto), nuevo in zip(ant, nue):
        if anterior == nuevo:
            continue  # el precio no cambió (p.ej. no era escalable): no aporta al comparador
        filas.append({"producto": producto, "anterior": anterior, "nuevo": nuevo})
    return filas


def aplicar_markup(
    texto: str,
    porcentaje: float = DEFAULT_MARKUP_PERCENTAGE,
    *,
    currency_symbols: str = DEFAULT_CURRENCY_SYMBOLS,
) -> str:
    factor = Decimal("1") + (Decimal(str(porcentaje)) / Decimal("100"))

    def replacer(match: re.Match[str]) -> str:
        # B3: si tras el número entero sigue una COMA + dígito (decimal CO ',50' o separador mixto
        # ',000'), NO escalar: el patrón solo casa la parte entera y dejaría la fracción suelta,
        # corrompiendo el precio ('$1.500,50' -> '$2.000,50'). Mejor no tocarlo que difundir basura.
        if re.match(r",\d", match.string[match.end():]):
            return match.group(0)
        num = match.group("num") or match.group("num2") or match.group("num3") or match.group("num4")
        pesos = _escalar(num, factor)
        if pesos is None:
            return match.group(0)
        simbolo = match.group("sym") or match.group("sym3") or "$"  # conserva símbolo líder; si no, "$"
        return _formatear_cop(pesos, simbolo)

    return _price_pattern(currency_symbols).sub(replacer, texto)
