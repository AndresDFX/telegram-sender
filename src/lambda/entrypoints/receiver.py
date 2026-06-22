"""Entrypoint del webhook de Telegram (onboarding + difusión si el bot fuera admin).

Controller fino: autentica (secret_token, fail-closed), parsea seguro, deduplica por
update_id (marca-antes con compensación acotada) y delega en los casos de uso. La
ingesta real del canal público la hace el poller; aquí lo vivo es el onboarding.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import wiring
from adapters import config
from application.ports import PartialEnqueueError

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


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()

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
