"""Receptor del webhook Telegram → Telegram.

Responsabilidades:
  1. Autenticar la petición vía ``secret_token`` (header X-Telegram-Bot-Api-Secret-Token).
     Fail-closed: si no hay secreto configurado se rechaza, salvo que ALLOW_INSECURE_WEBHOOK
     esté activo (servidor de desarrollo local; nunca en un endpoint expuesto).
  2. Parsear el body de forma segura (body inválido → 400, no 500).
  3. Deduplicar por ``update_id`` (idempotencia). Marca-antes con compensación acotada:
     solo se revierte el dedup ante fallos de la fase de preparación (markup, consulta de
     suscriptores) o de un efecto idempotente (alta/baja, que es un upsert). El encolado
     parcial (``PartialEnqueueError`` con lotes ya enviados) y el broadcast inline NO
     revierten, para no reenviar la lista a quienes ya la habrían recibido.
  4. Enrutar: comando privado (/start, /stop) → alta/baja; channel_post / edited_channel_post
     del canal fuente → markup + broadcast desacoplado vía SQS.
  5. Responder 200 de inmediato (el broadcast lo hace el worker desde SQS).

Si no hay cola configurada (``BROADCAST_QUEUE_URL`` vacío) cae a modo inline — útil
para el servidor de desarrollo local sin SQS.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Any

from broadcaster import procesar_lote
from dynamodb_client import (
    borrar_update_procesado,
    marcar_update_procesado,
    obtener_usuarios_activos,
    registrar_suscriptor,
)
from markup import aplicar_markup
from sqs_client import PartialEnqueueError, encolar_lotes
from telegram_client import TelegramClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Claves de update que pueden traer una lista de precios del canal.
_POST_KEYS = ("channel_post", "edited_channel_post")
_SECRET_HEADER = "x-telegram-bot-api-secret-token"
_TRUTHY = ("1", "true", "yes", "on")

_WELCOME = "✅ Te suscribiste. Recibirás aquí las listas en cuanto se publiquen.\nEnvía /stop para darte de baja."
_GOODBYE = "🚫 Te diste de baja. No recibirás más listas.\nEnvía /start cuando quieras volver."
_HELP = "Comandos disponibles:\n/start — suscribirte\n/stop — darte de baja"


class _BadRequest(Exception):
    """Body que no se puede parsear como update de Telegram."""


def _response(status_code: int = 200, body: str = "OK") -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": body,
    }


def _headers_lower(event: dict[str, Any]) -> dict[str, str]:
    """Headers con claves en minúscula (API Gateway las envía así; Flask no)."""
    return {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}


def _secret_valido(event: dict[str, Any]) -> bool:
    expected = os.environ.get("WEBHOOK_SECRET_TOKEN")
    if not expected:
        # Sin secreto configurado solo se permite con el flag de dev y NUNCA dentro de
        # Lambda: si ALLOW_INSECURE_WEBHOOK se colara en el env de una función desplegada,
        # AWS_LAMBDA_FUNCTION_NAME (que el runtime siempre define) la mantiene fail-closed.
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            return False
        return os.environ.get("ALLOW_INSECURE_WEBHOOK", "").strip().lower() in _TRUTHY
    received = _headers_lower(event).get(_SECRET_HEADER, "")
    return hmac.compare_digest(str(received), str(expected))


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


def _extraer_post(body: dict[str, Any]) -> dict[str, Any] | None:
    for key in _POST_KEYS:
        post = body.get(key)
        if post:
            return post
    return None


def _texto_de_post(post: dict[str, Any]) -> str | None:
    """Texto plano o caption (listas publicadas como imagen)."""
    return post.get("text") or post.get("caption")


def _es_canal_fuente(post: dict[str, Any]) -> bool:
    source_channel_id = os.environ.get("SOURCE_CHANNEL_ID")
    if not source_channel_id:
        return True
    chat_id = str(post.get("chat", {}).get("id", ""))
    return chat_id == str(source_channel_id)


def _comando_privado(message: dict[str, Any]) -> str | None:
    """Devuelve el comando normalizado (/start, /stop) de un mensaje privado, o None."""
    if message.get("chat", {}).get("type") != "private":
        return None
    text = message.get("text", "")
    if not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0]  # primer token: "/start@MiBot"
    return token.split("@", 1)[0].lower()


def _responder(chat_id: str, text: str) -> None:
    TelegramClient().send_message(chat_id, text)


def _responder_seguro(chat_id: str, text: str) -> None:
    """Responde al usuario sin propagar errores: el alta/baja ya quedó persistida."""
    try:
        _responder(chat_id, text)
    except Exception:
        logger.exception("No se pudo responder a %s (el alta/baja ya está persistida)", chat_id)


def _manejar_comando(message: dict[str, Any], comando: str) -> dict[str, Any]:
    chat_id = str(message["chat"]["id"])
    if comando == "/start":
        registrar_suscriptor(chat_id, "active")
        _responder_seguro(chat_id, _WELCOME)
    elif comando == "/stop":
        registrar_suscriptor(chat_id, "inactive")
        _responder_seguro(chat_id, _GOODBYE)
    else:
        _responder_seguro(chat_id, _HELP)
    logger.info("Comando %s de %s procesado", comando, chat_id)
    return _response(body=json.dumps({"status": "command", "command": comando}))


def _procesar_broadcast(texto: str) -> dict[str, Any]:
    mensaje = aplicar_markup(texto)
    clientes = obtener_usuarios_activos()

    queue_url = os.environ.get("BROADCAST_QUEUE_URL")
    if queue_url:
        lotes = encolar_lotes(mensaje, clientes, queue_url=queue_url)
        logger.info("Encolados %d lotes para %d clientes", lotes, len(clientes))
        return _response(
            body=json.dumps({"status": "queued", "batches": lotes, "subscribers": len(clientes)})
        )

    # Modo inline (desarrollo local sin SQS). El envío ES un efecto secundario ya
    # cometido: un fallo aquí NO debe revertir el dedup (reenviaría a quienes ya
    # recibieron), por eso se contiene y no se propaga al except que compensa.
    try:
        stats = procesar_lote(mensaje, clientes)
    except Exception:
        logger.exception("Fallo en broadcast inline tras posibles envíos; se mantiene el dedup")
        return _response(500, json.dumps({"status": "inline_error"}))
    resumen = {k: v for k, v in stats.items() if k != "failed_ids"}
    logger.info("Broadcast inline completado: %s", resumen)
    return _response(body=json.dumps({"status": "sent", "stats": resumen}))


def _enrutar(body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message")
    if message:
        comando = _comando_privado(message)
        if comando:
            return _manejar_comando(message, comando)
        return _response()  # mensaje privado sin comando: ignorar

    post = _extraer_post(body)
    if post:
        texto = _texto_de_post(post)
        if texto and _es_canal_fuente(post):
            return _procesar_broadcast(texto)

    return _response()  # nada que hacer


def _revertir_dedup(update_id: str) -> None:
    try:
        borrar_update_procesado(update_id)
    except Exception:
        logger.exception("No se pudo revertir el dedup del update %s", update_id)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _secret_valido(event):
        logger.warning("Petición rechazada: secret_token inválido o ausente")
        return _response(403, "forbidden")

    try:
        body = _parse_body(event)
    except _BadRequest as error:
        logger.warning("Body inválido descartado: %s", error)
        return _response(400, json.dumps({"status": "bad_request"}))

    update_id = body.get("update_id")
    # Marca-antes: si ya estaba procesado, es un duplicado → ignorar.
    if update_id is not None and not marcar_update_procesado(str(update_id)):
        logger.info("Update %s ya procesado; se omite (duplicado)", update_id)
        return _response(body=json.dumps({"status": "duplicate"}))

    try:
        return _enrutar(body)
    except PartialEnqueueError as error:
        # Solo es seguro reintentar (revertir el dedup) si NO se encoló ningún lote;
        # si ya se encoló alguno, mantener la marca evita broadcasts duplicados.
        if update_id is not None and error.enqueued == 0:
            _revertir_dedup(str(update_id))
        else:
            logger.error(
                "Encolado parcial (%s); se mantiene el dedup de %s para evitar duplicados",
                error,
                update_id,
            )
        raise
    except Exception:
        # Fallo antes de cualquier efecto secundario observable → permitir reintento.
        if update_id is not None:
            _revertir_dedup(str(update_id))
        raise
