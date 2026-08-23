"""Entrypoint del webhook de Telegram (onboarding + difusión si el bot fuera admin).

Controller fino: autentica (secret_token, fail-closed), parsea seguro, deduplica por
update_id (marca-antes con compensación acotada) y delega en los casos de uso. La
ingesta real del canal público la hace el poller; aquí lo vivo es el onboarding.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import wiring
from adapters import config
from application import hub_entrada
from application.ports import PartialEnqueueError
from domain import hub_firma

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_POST_KEYS = ("channel_post", "edited_channel_post")

# Dependencias (casos de uso) cableadas perezosamente; los tests pueden inyectarlas.
dedup = None
handle_command = None
broadcast = None


def _ensure() -> None:
    global dedup, handle_command, broadcast
    if dedup is None:
        dedup = wiring.build_dedup()
    if handle_command is None:
        handle_command = wiring.build_handle_command()
    if broadcast is None:
        broadcast = wiring.build_broadcast_list()


class _BadRequest(Exception):
    pass


def _response(status_code: int = 200, body: str = "OK") -> dict[str, Any]:
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json"}, "body": body}


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body", "{}")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise _BadRequest(str(error)) from error
        if not isinstance(parsed, dict):
            raise _BadRequest("el body JSON no es un objeto")
        return parsed
    raise _BadRequest("body de tipo no soportado")


def _comando_privado(message: dict[str, Any]) -> str | None:
    if message.get("chat", {}).get("type") != "private":
        return None
    text = message.get("text", "")
    if not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0].split("@", 1)[0].lower()


def _extraer_post(body: dict[str, Any]) -> dict[str, Any] | None:
    for key in _POST_KEYS:
        post = body.get(key)
        if post:
            return post
    return None


def _es_canal_fuente(post: dict[str, Any]) -> bool:
    source = os.environ.get("SOURCE_CHANNEL_ID")
    if not source:
        return True
    return str(post.get("chat", {}).get("id", "")) == str(source)


def _enrutar(body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message")
    if message:
        comando = _comando_privado(message)
        if comando:
            handle_command(str(message["chat"]["id"]), comando)
            return _response(body=json.dumps({"status": "command", "command": comando}))
        return _response()

    post = _extraer_post(body)
    if post:
        texto = post.get("text") or post.get("caption")
        if texto and _es_canal_fuente(post):
            # M18: pasamos el update_id como dedup_key → broadcast_id DETERMINISTA. Si crear el plan
            # falla a mitad y Telegram reintenta el webhook (tras revertir el dedup), el reintento
            # reusa el mismo id y sobrescribe el plan en vez de crear uno nuevo (no duplica la difusión).
            uid = body.get("update_id")
            resultado = broadcast(texto, dedup_key=str(uid) if uid is not None else None)
            return _response(body=json.dumps({"status": "queued", **resultado}))
    return _response()


def _revertir_dedup(update_id: str) -> None:
    try:
        dedup.borrar(update_id)
    except Exception:
        logger.exception("No se pudo revertir el dedup del update %s", update_id)


#: La ruta de la entrada del hub. Va DENTRO de la firma (ver `domain/hub_firma.py`), así que
#: cambiarla acá obliga a cambiar la URL del proyecto en el panel del hub o la firma deja de
#: cuadrar y el único síntoma es un 401 en el lado del hub.
RUTA_HUB = "/hub/entrada"


def _ruta(event: dict[str, Any]) -> str:
    """La ruta de la petición, sin query. Como en `entrypoints/admin.py`."""
    http = event.get("requestContext", {}).get("http", {})
    return str(event.get("rawPath") or http.get("path") or "").split("?")[0]


def _cuerpo_crudo(event: dict[str, Any]) -> bytes:
    """Los bytes TAL COMO LLEGARON.

    ⚠️ La firma se calcula sobre estos bytes, nunca sobre un dict reserializado:
    `json.dumps` de lo parseado cambia el orden de las claves y el espaciado, así que el
    sha256 sale distinto aunque el objeto sea el mismo — y el 401 que resulta de eso no se
    puede depurar desde el otro lado. `isBase64Encoded` se descodifica ANTES, o el hash
    sería el del base64.
    """
    crudo = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(crudo)
    return crudo.encode("utf-8") if isinstance(crudo, str) else bytes(crudo or b"")


def _entrada_hub(event: dict[str, Any]) -> dict[str, Any]:
    """`POST /hub/entrada` — un grupo de WhatsApp como fuente, leído por wa-hub.

    ⚠️ TIENE SU PROPIA PUERTA y por eso se atiende ANTES del `secret_token` de Telegram:
    el hub autentica con HMAC, no con esa cabecera. Si cayera por la puerta de Telegram, el
    hub se llevaría un 403 y no habría forma de saber desde allá que el problema era la
    cabecera equivocada.

    ⚠️ Lo que entra se CAPTURA, nunca se difunde. Ver `application/hub_entrada.py`.
    """
    cuerpo_bytes = _cuerpo_crudo(event)
    token = wiring.build_config_store().get().get("hub_token", "")
    ok, motivo = hub_firma.verificar(event.get("headers"), cuerpo_bytes, token, RUTA_HUB)
    if not ok:
        if motivo == "apagado":
            # 503 y no 403: fail-closed, pero DICIENDO que es una variable sin poner y no
            # un problema de credencial. Un 403 acá manda a revisar el token durante media
            # hora. Y 503 es reintentable para el hub, así que no pierde el mensaje.
            logger.warning("Entrada del hub sin `hub_token` configurado: no se ingiere")
            return _response(503, json.dumps({"status": "apagado"}))
        # ⚠️ El motivo va al LOG y NO al cuerpo: decirle a quien llama si falló el kid o la
        # firma le confirma qué mitad del token acertó.
        logger.warning("Entrada del hub rechazada: %s", motivo)
        return _response(403, json.dumps({"status": "forbidden"}))

    try:
        cuerpo = json.loads(cuerpo_bytes.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        # Sin el texto de la excepción en la respuesta: el cuerpo es dato de terceros.
        logger.warning("Cuerpo del hub ilegible: %s", error)
        return _response(400, json.dumps({"status": "bad_request"}))

    capturar = hub_entrada.CapturarDesdeHub(dedup, broadcast, log=logger)
    try:
        r = capturar(cuerpo)
    except Exception:
        # 500 para que el hub REINTENTE: su cola es at-least-once y un 5xx es reintentable.
        # Un 200 acá perdería el mensaje en silencio, que es lo único que no puede pasar.
        logger.exception("No pude capturar lo que entró por el hub")
        return _response(500, json.dumps({"status": "error"}))
    if r.get("error"):
        logger.warning("Cuerpo del hub no válido: %s", r.get("detalle"))
        return _response(400, json.dumps({"status": "bad_request"}))
    return _response(body=json.dumps(r))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()

    # ⚠️ ANTES del `secret_token`: la entrada del hub trae firma, no esa cabecera.
    if _ruta(event).endswith(RUTA_HUB):
        return _entrada_hub(event)

    if not config.webhook_secret_valido(event.get("headers")):
        logger.warning("Petición rechazada: secret_token inválido o ausente")
        return _response(403, "forbidden")

    try:
        body = _parse_body(event)
    except _BadRequest as error:
        logger.warning("Body inválido descartado: %s", error)
        return _response(400, json.dumps({"status": "bad_request"}))

    update_id = body.get("update_id")
    if update_id is not None:
        try:
            nuevo = dedup.marcar_estricto(str(update_id))
        except Exception:
            # A8: el dedup NO pudo confirmar (throttle/permiso/tabla ausente). NO asumir 'duplicado'
            # y descartar en silencio un update legítimo (post del canal u onboarding) que Telegram
            # daría por entregado (200) sin reintentar. Se procesa igual: a lo sumo se reprocesa un
            # duplicado raro (preferible a perder el update); la marca se reintenta en el camino feliz.
            logger.exception("dedup no pudo confirmar el update %s; lo proceso igual (no asumir duplicado)", update_id)
            nuevo = True
        if not nuevo:
            logger.info("Update %s ya procesado; se omite (duplicado)", update_id)
            return _response(body=json.dumps({"status": "duplicate"}))

    try:
        return _enrutar(body)
    except PartialEnqueueError as error:
        if update_id is not None and error.enqueued == 0:
            _revertir_dedup(str(update_id))
        else:
            logger.error("Encolado parcial (%s); se mantiene dedup de %s", error, update_id)
        raise
    except Exception:
        if update_id is not None:
            _revertir_dedup(str(update_id))
        raise
