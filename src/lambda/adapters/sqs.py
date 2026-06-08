"""Adapters de la cola de broadcast: SQS real e inline (dev local)."""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Iterator, Sequence

from application.ports import BroadcastQueue, PartialEnqueueError


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
        self._size = batch_size or int(os.environ.get("BROADCAST_BATCH_SIZE", "100"))
        self._max_retries = max_retries
        self._endpoint = endpoint or os.environ.get("SQS_ENDPOINT")
        self._sleep = sleep

    def _client(self):
        import boto3

        kwargs = {"endpoint_url": self._endpoint} if self._endpoint else {}
        return boto3.client("sqs", **kwargs)

    def encolar(self, text: str, chat_ids: Sequence[str]) -> int:
        if not self._url:
            raise RuntimeError("BROADCAST_QUEUE_URL no configurado")
        if self._size <= 0:
            raise ValueError("batch_size debe ser > 0")

        lotes = list(_chunk(chat_ids, self._size))
        total = len(lotes)
        client = self._client()
        enqueued = 0

        for index, lote in enumerate(lotes):
            body = json.dumps({"text": text, "chat_ids": lote, "batch_index": index})
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


class InlineBroadcastQueue(BroadcastQueue):
    """Entrega de inmediato (dev local sin SQS) delegando en una función de entrega."""

    def __init__(self, deliver: Callable[[str, Sequence[str]], object]):
        self._deliver = deliver

    def encolar(self, text: str, chat_ids: Sequence[str]) -> int:
        self._deliver(text, list(chat_ids))
        return 1
