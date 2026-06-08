"""Cliente HTTP para la Telegram Bot API con reintentos y respeto de rate-limit."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

DEFAULT_MAX_RETRIES = 3
# Si Telegram devuelve 429 sin parameters.retry_after, espera este mínimo.
DEFAULT_RETRY_AFTER_SECONDS = 1.0
# Tope del backoff exponencial ante errores 5xx.
MAX_BACKOFF_SECONDS = 8.0


class TelegramClient:
    def __init__(
        self,
        bot_token: str | None = None,
        timeout: float = 10.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bot_token = bot_token or os.environ["TELEGRAM_BOT_TOKEN"]
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self.session = requests.Session()

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        """Envía un mensaje, honrando 429 (retry_after) y reintentando 5xx con backoff.

        Devuelve el payload de Telegram en éxito, o ``{"blocked": True}`` si el
        usuario bloqueó el bot (403). Lanza ``RuntimeError`` si se agotan los
        reintentos o la API responde un error no recuperable.
        """
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method="sendMessage")
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        attempt = 0
        while True:
            attempt += 1
            response = self.session.post(url, json=payload, timeout=self.timeout)
            status = response.status_code
            data = response.json() if response.content else {}

            if status == 403:
                logger.warning("Usuario %s bloqueó el bot o no puede recibir mensajes", chat_id)
                return {"ok": False, "blocked": True, "result": data}

            if status == 429:
                retry_after = float(
                    data.get("parameters", {}).get("retry_after", DEFAULT_RETRY_AFTER_SECONDS)
                )
                if attempt > self.max_retries:
                    raise RuntimeError(
                        f"Rate limited en chat {chat_id} tras {self.max_retries} reintentos: {data}"
                    )
                logger.warning(
                    "429 para %s; esperando %.2fs antes del reintento %d/%d",
                    chat_id,
                    retry_after,
                    attempt,
                    self.max_retries,
                )
                self._sleep(retry_after)
                continue

            if status >= 500:
                if attempt > self.max_retries:
                    response.raise_for_status()
                backoff = min(2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Telegram %d para %s; backoff %.2fs (reintento %d/%d)",
                    status,
                    chat_id,
                    backoff,
                    attempt,
                    self.max_retries,
                )
                self._sleep(backoff)
                continue

            response.raise_for_status()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            return data
