"""Encolado de lotes de chat IDs en SQS para el broadcast desacoplado.

Cada mensaje SQS lleva un lote de chat IDs (no un solo usuario): así el worker
procesa un grupo por invocación y la cola amortigua picos sin romper el timeout
de Lambda. El import de ``boto3`` es perezoso para no exigirlo en las pruebas.

Idempotencia: si el encolado falla a mitad (algunos lotes ya enviados a SQS),
se lanza ``PartialEnqueueError`` con cuántos lotes se encolaron, para que el
receptor decida si es seguro revertir el dedup (solo si no se encoló nada) y
evitar reenviar la lista a quienes ya la habrían recibido.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Iterator, Sequence

DEFAULT_BATCH_SIZE = int(os.environ.get("BROADCAST_BATCH_SIZE", "100"))
DEFAULT_MAX_RETRIES = 2


class PartialEnqueueError(RuntimeError):
    """El encolado falló tras haber enviado ``enqueued`` de ``total`` lotes."""

    def __init__(self, enqueued: int, total: int) -> None:
        super().__init__(f"Encolado parcial: {enqueued}/{total} lotes")
        self.enqueued = enqueued
        self.total = total


def _client():
    import boto3

    kwargs = {}
    endpoint = os.environ.get("SQS_ENDPOINT")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("sqs", **kwargs)


def _chunk(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def encolar_lotes(
    text: str,
    chat_ids: Sequence[str],
    *,
    queue_url: str | None = None,
    batch_size: int | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Divide ``chat_ids`` en lotes y encola un mensaje por lote. Devuelve cuántos encoló.

    Cada envío se reintenta hasta ``max_retries`` veces ante errores transitorios.
    Si un lote falla definitivamente, lanza ``PartialEnqueueError`` indicando cuántos
    lotes alcanzaron la cola antes del fallo.
    """
    url = queue_url or os.environ.get("BROADCAST_QUEUE_URL")
    if not url:
        raise RuntimeError("BROADCAST_QUEUE_URL no configurado")

    size = batch_size or DEFAULT_BATCH_SIZE
    if size <= 0:
        raise ValueError("batch_size debe ser > 0")

    lotes = list(_chunk(chat_ids, size))
    total = len(lotes)
    client = _client()
    enqueued = 0

    for index, lote in enumerate(lotes):
        body = json.dumps({"text": text, "chat_ids": lote, "batch_index": index})
        attempt = 0
        while True:
            try:
                client.send_message(QueueUrl=url, MessageBody=body)
                enqueued += 1
                break
            except Exception:
                attempt += 1
                if attempt > max_retries:
                    raise PartialEnqueueError(enqueued, total)
                sleep(min(2 ** (attempt - 1), 4))

    return enqueued
