"""Adapters de la cola de broadcast: SQS real e inline (dev local)."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Callable, Iterator, Sequence

from application.ports import BroadcastQueue, PartialEnqueueError, QueueStats


def _chunk(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


class SqsBroadcastQueue(BroadcastQueue):
    """Encola un mensaje SQS por lote de N chat IDs, con reintentos por lote."""

    def __init__(
        self,
        queue_url: str | None = None,
        batch_size: int | None = None,
        max_retries: int = 2,
        endpoint: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._url = queue_url or os.environ.get("BROADCAST_QUEUE_URL")
        self._size = batch_size or int(os.environ.get("BROADCAST_BATCH_SIZE", "150"))
        self._max_retries = max_retries
        self._endpoint = endpoint or os.environ.get("SQS_ENDPOINT")
        self._sleep = sleep

    def _client(self):
        import boto3

        kwargs = {"endpoint_url": self._endpoint} if self._endpoint else {}
        return boto3.client("sqs", **kwargs)

    def encolar(
        self,
        text: str,
        chat_ids: Sequence[str],
        image_url: str | None = None,
        image_key: str | None = None,
        broadcast_id: str | None = None,
    ) -> int:
        if not self._url:
            raise RuntimeError("BROADCAST_QUEUE_URL no configurado")
        if self._size <= 0:
            raise ValueError("batch_size debe ser > 0")

        lotes = list(_chunk(chat_ids, self._size))
        total = len(lotes)
        client = self._client()
        enqueued = 0

        for index, lote in enumerate(lotes):
            body = json.dumps(
                {
                    "text": text,
                    "chat_ids": lote,
                    "batch_index": index,
                    "image_url": image_url,
                    "image_key": image_key,
                    "broadcast_id": broadcast_id,
                }
            )
            attempt = 0
            while True:
                try:
                    client.send_message(QueueUrl=self._url, MessageBody=body)
                    enqueued += 1
                    break
                except Exception:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise PartialEnqueueError(enqueued, total)
                    self._sleep(min(2 ** (attempt - 1), 4))

        return enqueued

    def encolar_uno(
        self,
        text: str,
        chat_ids: Sequence[str],
        image_url: str | None = None,
        image_key: str | None = None,
        broadcast_id: str | None = None,
        batch_index: int = 0,
        pid: str | None = None,
        manual: bool = False,
    ) -> None:
        """Encola UN lote ya formado (sin trocear). Lo usa el dispatcher para liberar
        exactamente un lote por tick (envío fraccionado y secuencial). ``pid`` permite al
        worker descartar el lote si su plan fue cancelado tras encolarlo. ``manual`` marca los
        lotes de envíos manuales: el worker los entrega aun con los envíos en pausa (la pausa
        solo frena lo automático)."""
        if not self._url:
            raise RuntimeError("BROADCAST_QUEUE_URL no configurado")
        body = json.dumps(
            {
                "text": text,
                "chat_ids": list(chat_ids),
                "batch_index": batch_index,
                "image_url": image_url,
                "image_key": image_key,
                "broadcast_id": broadcast_id,
                "pid": pid,
                "manual": bool(manual),
                "batch_id": uuid.uuid4().hex,  # idempotencia: el worker no reenvía un lote ya entregado
            }
        )
        self._client().send_message(QueueUrl=self._url, MessageBody=body)


class InlineBroadcastQueue(BroadcastQueue):
    """Entrega de inmediato (dev local sin SQS) delegando en una función de entrega."""

    def __init__(self, deliver: Callable[..., object]):
        self._deliver = deliver

    def encolar(
        self,
        text: str,
        chat_ids: Sequence[str],
        image_url: str | None = None,
        image_key: str | None = None,
        broadcast_id: str | None = None,
    ) -> int:
        self._deliver(text, list(chat_ids), image_url)  # inline (dev) usa solo image_url
        return 1


class SqsQueueStats(QueueStats):
    """Profundidad aproximada de la cola de broadcast y de la DLQ (para el admin)."""

    def __init__(self, queue_url: str | None = None, dlq_url: str | None = None, endpoint: str | None = None):
        self._url = queue_url or os.environ.get("BROADCAST_QUEUE_URL")
        self._dlq = dlq_url or os.environ.get("BROADCAST_DLQ_URL")
        self._endpoint = endpoint or os.environ.get("SQS_ENDPOINT")

    def _client(self):
        import boto3

        kwargs = {"endpoint_url": self._endpoint} if self._endpoint else {}
        return boto3.client("sqs", **kwargs)

    def profundidades(self) -> dict:
        client = self._client()

        def depth(url: str | None) -> int:
            if not url:
                return 0
            resp = client.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
            return int(resp["Attributes"].get("ApproximateNumberOfMessages", 0))

        return {"broadcast": depth(self._url), "dlq": depth(self._dlq)}

    def dlq_muestra(self, n: int = 5) -> list[dict]:
        """Muestra (sin borrar) hasta n mensajes de la DLQ para inspección en el panel."""
        if not self._dlq:
            return []
        client = self._client()
        resp = client.receive_message(
            QueueUrl=self._dlq, MaxNumberOfMessages=min(max(int(n), 1), 10), VisibilityTimeout=1
        )
        out = []
        for m in resp.get("Messages", []):
            try:
                b = json.loads(m.get("Body", "{}"))
            except Exception:
                b = {}
            out.append(
                {
                    "broadcast_id": b.get("broadcast_id"),
                    "pid": b.get("pid"),
                    "batch_index": b.get("batch_index"),
                    "chat_ids": len(b.get("chat_ids", [])),
                    "text": (b.get("text") or "")[:80],
                }
            )
        return out

    def dlq_redrive(self) -> dict:
        """Reprocesa la DLQ devolviéndola a la cola principal (start-message-move-task)."""
        if not self._dlq:
            return {"error": "sin DLQ"}
        client = self._client()
        arn = client.get_queue_attributes(QueueUrl=self._dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        client.start_message_move_task(SourceArn=arn)
        return {"ok": True, "redrive": "iniciado"}

    def dlq_purgar(self) -> dict:
        """Vacía la DLQ (descarta los mensajes fallidos)."""
        if not self._dlq:
            return {"error": "sin DLQ"}
        self._client().purge_queue(QueueUrl=self._dlq)
        return {"ok": True, "purged": True}
