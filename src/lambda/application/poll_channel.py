"""Caso de uso: sondear el canal público y difundir solo las publicaciones nuevas.

Usa un high-water mark por message_id para no re-difundir el backlog del preview.
La primera ejecución siembra el HWM sin difundir nada.
"""

from __future__ import annotations

import logging

from application.broadcasting import BroadcastList
from application.ports import ChannelReader, HighWaterMarkStore

logger = logging.getLogger(__name__)


class PollChannel:
    def __init__(
        self,
        reader: ChannelReader,
        hwm: HighWaterMarkStore,
        broadcast: BroadcastList,
        channel: str,
    ) -> None:
        self._reader = reader
        self._hwm = hwm
        self._broadcast = broadcast
        self._channel = channel

    def __call__(self) -> dict[str, int]:
        posts = self._reader.leer_publicaciones(self._channel)
        if not posts:
            logger.info("Preview de %s sin publicaciones con texto", self._channel)
            return {"checked": 0, "new": 0}

        max_id = max(p.message_id for p in posts)
        mark = self._hwm.obtener(self._channel)

        # Primera ejecución: sembrar el HWM sin difundir el backlog.
        if mark is None:
            self._hwm.guardar(self._channel, max_id)
            logger.info("Sembrado HWM de %s en %d (sin difundir backlog)", self._channel, max_id)
            return {"checked": len(posts), "new": 0, "seeded": max_id}

        nuevos = sorted((p for p in posts if p.message_id > mark), key=lambda p: p.message_id)
        for post in nuevos:
            self._broadcast(post.text)
            self._hwm.guardar(self._channel, post.message_id)  # avanzar tras cada uno (idempotente)

        logger.info("Canal %s: %d revisados, %d nuevos difundidos", self._channel, len(posts), len(nuevos))
        return {"checked": len(posts), "new": len(nuevos)}
