"""La firma del hub: la MISMA cadena canónica en las dos direcciones.

    v1\\n<MÉTODO>\\n<RUTA>\\n<TS>\\n<sha256hex(cuerpo)>

con las cabeceras `x-hv-puente-{key,ts,sig}` y ventana de ±300 s.

VIVE EN `domain/` porque es puro —entra texto y bytes, sale un hexadecimal— y porque lo
usan los DOS lados de Réplica: `adapters/whatsapp_hub.py` para FIRMAR lo que sale hacia
`POST /v1/enviar`, y `application/hub_entrada.py` para VERIFICAR lo que entra por el
webhook. Tenerlo dos veces sería tener dos cadenas canónicas que se pueden separar sin que
nada falle hasta que aparece un 401 que no se puede depurar desde el otro lado.

No es un invento de este proyecto: es la que firma `wa-hub/src/dominio/entrega.ts`, la que
verifica `plataforma-hv/app.py` en `_puente_auth()` y la que implementa
`doble-hub/gateway/src/domain/hub-entrada.ts`. Hay un vector de prueba con los
hexadecimales que produjo el cliente real del hub, en
`tests/test_adapters_whatsapp_hub.py`.

Sin dependencias: solo stdlib, para poder pegarse en una Lambda que no lleva `requests`.
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Las tres cabeceras. No se estrenan nombres.
CAB_KID = "x-hv-puente-key"
CAB_TS = "x-hv-puente-ts"
CAB_SIG = "x-hv-puente-sig"

VENTANA_DEF_S = 300   # tolerancia de reloj, la misma que PUENTE_VENTANA en la plataforma HV
MIN_SECRETO = 32      # por debajo es un HMAC de juguete: se rechaza y se dice


def cadena_canonica(metodo: str, ruta: str, ts, cuerpo: bytes) -> str:
    """La cadena que se firma.

    Devuelve `str` y no `bytes`: es la forma del cliente de referencia
    (`wa-hub/clientes/python/hub.py`), y hay una prueba que compara las dos funciones. Si
    difirieran en el tipo, esa comparación habría que convertirla — y entonces deja de ser
    una igualdad, que es justo lo que la hace valer.

    El método y la ruta van DENTRO porque, sin ellos, una firma capturada contra un
    endpoint se reenvía contra otro.

    ⚠️ `.upper()` es un extra sobre la referencia. Para el uso real no cambia nada (siempre
    es "POST"), pero un llamador que pasara "post" produciría una firma que el otro lado
    rechaza con un 401 sin más pista. Normalizar no puede estropear una firma correcta.
    """
    h = hashlib.sha256(cuerpo).hexdigest()
    return "v1\n%s\n%s\n%s\n%s" % (metodo.upper(), ruta, ts, h)


def firmar(secreto: str, metodo: str, ruta: str, ts, cuerpo: bytes) -> str:
    return hmac.new(
        secreto.encode("utf-8"),
        cadena_canonica(metodo, ruta, ts, cuerpo).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def partir_token(token: str) -> tuple[str, str]:
    """`(kid, secreto)` de un `"kid:secreto"`.

    Sin dos puntos NO se adivina un kid: se devuelve vacío y quien llama lo rechaza. Un kid
    inventado produce un 401 que se pasa media hora buscando en el token equivocado.
    """
    crudo = (token or "").strip()
    if ":" not in crudo:
        return "", ""
    kid, _, secreto = crudo.partition(":")
    return kid.strip()[:16], secreto.strip()


def _cab(cabeceras, nombre: str) -> str:
    """Una cabecera, sin distinguir mayúsculas.

    API Gateway v2 las entrega en minúsculas, pero un `curl` de prueba o un proxy por medio
    pueden mandarlas capitalizadas; buscar solo la forma exacta daría un 401 por «falta la
    cabecera» con la cabecera puesta.
    """
    if not cabeceras:
        return ""
    for k, v in cabeceras.items():
        if str(k).lower() == nombre:
            return str(v or "").strip()
    return ""


def verificar(
    cabeceras: dict | None,
    cuerpo: bytes,
    token: str,
    ruta: str,
    metodo: str = "POST",
    ventana_s: int = VENTANA_DEF_S,
    ahora: float | None = None,
) -> tuple[bool, str]:
    """¿Viene del hub? Devuelve `(ok, motivo)`.

    El MOTIVO no es decorativo: es lo único que hay al otro lado cuando algo no cuadra, y
    la diferencia entre «kid», «reloj» y «firma» es la diferencia entre revisar el token,
    el reloj de la Lambda o la cadena canónica. Un «401» a secas son horas.

    ⚠️ El motivo va al LOG, nunca al cuerpo de la respuesta: decirle a quien llama si falló
    el kid o la firma le confirma qué mitad del token acertó.

    ⚠️ Sin token configurado devuelve `False` con «apagado». Es fail-closed a propósito: la
    ruta no existe a efectos prácticos hasta que hay secreto, y quien la llame se lleva un
    403 en vez de entrar.
    """
    kid_esperado, secreto = partir_token(token)
    if not kid_esperado or not secreto:
        return False, "apagado" if not (token or "").strip() else "token sin la forma kid:secreto"
    if len(secreto) < MIN_SECRETO:
        return False, "el secreto tiene %d caracteres y el mínimo son %d (¿se pegó cortado?)" % (
            len(secreto), MIN_SECRETO,
        )

    if _cab(cabeceras, CAB_KID) != kid_esperado:
        return False, "kid"

    crudo_ts = _cab(cabeceras, CAB_TS)
    try:
        ts = int(crudo_ts)
    except (TypeError, ValueError):
        return False, "reloj"
    ahora_s = time.time() if ahora is None else ahora
    if abs(ahora_s - ts) > ventana_s:
        return False, "reloj (desvío de %d s, ventana %d)" % (int(abs(ahora_s - ts)), ventana_s)

    # ⚠️ En tiempo constante. Comparar hexadecimales con `==` filtra, por el tiempo que
    # tarda en salir, cuántos caracteres del principio acertó quien lo intenta.
    esperada = firmar(secreto, metodo, ruta, crudo_ts, cuerpo)
    if not hmac.compare_digest(_cab(cabeceras, CAB_SIG).lower(), esperada):
        return False, "firma"
    return True, ""
