"""Caso de uso: entregar un lote de mensajes a una lista de chats (con delay anti-rate-limit).

Si hay imagen, se envía primero la foto y luego el texto (las listas superan el límite
de caption de Telegram, ~1024, así que van como dos mensajes).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Sequence

from application.ports import MessageSender, SubscriberRepository
from domain.models import BroadcastStats

logger = logging.getLogger(__name__)


class DeliverBatch:
    def __init__(
        self,
        sender: MessageSender,
        subscribers: SubscriberRepository,
        delay: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sender = sender
        self._subscribers = subscribers
        self._delay = delay
        self._sleep = sleep

    def __call__(self, text: str, chat_ids: Sequence[str], image_url: str | None = None) -> BroadcastStats:
        stats = BroadcastStats(total=len(chat_ids))
        for chat_id in chat_ids:
            try:
                if image_url:
                    foto = self._sender.enviar_foto(chat_id, image_url)
                    if foto.blocked:
                        stats.blocked += 1
                        self._inactivar(chat_id)
                        self._wait()
                        continue
                    self._wait()

                result = self._sender.enviar(chat_id, text)
                if result.blocked:
                    stats.blocked += 1
                    self._inactivar(chat_id)
                else:
                    stats.sent += 1
            except Exception:
                stats.failed += 1
                stats.failed_ids.append(chat_id)
                logger.exception("Error enviando mensaje al chat %s", chat_id)

            self._wait()

        return stats

    def _inactivar(self, chat_id: str) -> None:
        try:
            self._subscribers.marcar_inactivo(chat_id)
        except Exception:
            logger.exception("No se pudo marcar inactivo al chat %s", chat_id)

    def _wait(self) -> None:
        if self._delay:
            self._sleep(self._delay)
