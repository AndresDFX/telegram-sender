"""Worker SQS: consume lotes encolados y los entrega por la Bot API.

Disparado por una EventSourceMapping de SQS. Usa la respuesta parcial de lotes
(``batchItemFailures``) para que solo los mensajes que fallan de forma sistémica
vuelvan a la cola — y, tras agotar reintentos, caigan a la DLQ. Los fallos de
usuarios individuales (bloqueos, chats inválidos) se registran pero NO reencolan
el lote, evitando reenvíos duplicados a quienes sí recibieron.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from broadcaster import procesar_lote

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _resumen(stats: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in stats.items() if k != "failed_ids"}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    records = event.get("Records", [])
    batch_item_failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId")
        try:
            body = json.loads(record["body"])
            text = body["text"]
            chat_ids = body.get("chat_ids", [])

            stats = procesar_lote(text, chat_ids)
            logger.info("Lote %s procesado: %s", body.get("batch_index"), _resumen(stats))

            # Fallo sistémico (todo el lote falló) → reintentar el mensaje completo.
            if stats["total"] > 0 and stats["failed"] == stats["total"]:
                raise RuntimeError(f"Todos los envíos del lote {message_id} fallaron")

        except Exception:
            logger.exception("Fallo procesando el mensaje SQS %s", message_id)
            if message_id:
                batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
