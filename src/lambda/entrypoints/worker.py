"""Entrypoint del worker (consumidor SQS): entrega cada lote y reporta fallos parciales.

Respuesta parcial de lotes (``batchItemFailures``): solo los mensajes con fallo
sistémico (todo el lote falló) vuelven a la cola y, agotados los reintentos, caen a
la DLQ. Los fallos por usuario individual se registran pero NO reencolan el lote.

Si el lote trae ``image_key`` (imagen subida al panel), se resuelve a una URL
temporal (presigned) justo antes de enviar; ``image_url`` (URL externa) se usa tal cual.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import wiring

logger = logging.getLogger()
logger.setLevel(logging.INFO)

deliver = None  # caso de uso DeliverBatch; inyectable en tests
image_store = None  # S3ImageStore; inyectable en tests


def _ensure() -> None:
    global deliver, image_store
    if deliver is None:
        deliver = wiring.build_deliver_batch()
    if image_store is None:
        image_store = wiring.build_image_store()


def _resolver_imagen(body: dict) -> str | None:
    key = body.get("image_key")
    if key:
        try:
            return image_store.url_temporal(key)
        except Exception:
            logger.exception("No se pudo generar URL temporal para %s", key)
    return body.get("image_url")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    batch_item_failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId")
        try:
            body = json.loads(record["body"])
            stats = deliver(body["text"], body.get("chat_ids", []), _resolver_imagen(body))
            logger.info("Lote %s procesado: %s", body.get("batch_index"), stats.resumen())
            if stats.total > 0 and stats.failed == stats.total:
                raise RuntimeError(f"Todos los envíos del lote {message_id} fallaron")
        except Exception:
            logger.exception("Fallo procesando el mensaje SQS %s", message_id)
            if message_id:
                batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
