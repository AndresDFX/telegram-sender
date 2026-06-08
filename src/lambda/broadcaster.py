"""Lógica de envío por lote, compartida entre el worker SQS y el modo inline local."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Sequence

from dynamodb_client import marcar_inactivo
from telegram_client import TelegramClient

logger = logging.getLogger(__name__)
SEND_DELAY_SECONDS = float(os.environ.get("SEND_DELAY_SECONDS", "0.05"))


def procesar_lote(
    text: str,
    chat_ids: Sequence[str],
    *,
    telegram: TelegramClient | None = None,
    delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Envía ``text`` a cada chat respetando el delay anti-rate-limit.

    - 403 (bot bloqueado) → marca al suscriptor inactivo y cuenta como ``blocked``.
    - Cualquier otra excepción → cuenta como ``failed`` y registra el chatId.
    El delay entre envíos mantiene el ritmo global por debajo de 30 msg/seg.
    """
    telegram = telegram or TelegramClient()
    delay = SEND_DELAY_SECONDS if delay is None else delay

    stats: dict[str, Any] = {
        "total": len(chat_ids),
        "sent": 0,
        "blocked": 0,
        "failed": 0,
        "failed_ids": [],
    }

    for chat_id in chat_ids:
        try:
            result = telegram.send_message(chat_id, text)
            if result.get("blocked"):
                stats["blocked"] += 1
                try:
                    marcar_inactivo(chat_id)
                except Exception:
                    logger.exception("No se pudo marcar inactivo al chat %s", chat_id)
            else:
                stats["sent"] += 1
        except Exception:
            stats["failed"] += 1
            stats["failed_ids"].append(chat_id)
            logger.exception("Error enviando mensaje al chat %s", chat_id)

        if delay:
            sleep(delay)

    return stats
