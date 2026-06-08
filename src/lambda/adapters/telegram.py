"""Adapter de envío por la Telegram Bot API (implementa MessageSender)."""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

import requests

from application.ports import MessageSender
from domain.models import SendResult

logger = logging.getLogger(__name__)
_API = "https://api.telegram.org/bot{token}/{method}"
_DEFAULT_RETRY_AFTER = 1.0
_MAX_BACKOFF = 8.0


class TelegramSender(MessageSender):
    def __init__(
        self,
        bot_token: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._token = bot_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep
        self._session = requests.Session()

    def _resolve_token(self) -> str:
        if not self._token:
            self._token = os.environ["TELEGRAM_BOT_TOKEN"]
        return self._token

    def enviar(self, chat_id: str, text: str) -> SendResult:
        url = _API.format(token=self._resolve_token(), method="sendMessage")
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}

        attempt = 0
        while True:
            attempt += 1
            response = self._session.post(url, json=payload, timeout=self._timeout)
            status = response.status_code
            data = response.json() if response.content else {}

            if status == 403:
                logger.warning("Usuario %s bloqueó el bot o no puede recibir mensajes", chat_id)
                return SendResult(ok=False, blocked=True)

            if status == 429:
                retry_after = float(data.get("parameters", {}).get("retry_after", _DEFAULT_RETRY_AFTER))
                if attempt > self._max_retries:
                    raise RuntimeError(f"Rate limited en chat {chat_id} tras {self._max_retries} reintentos: {data}")
                logger.warning("429 para %s; esperando %.2fs (reintento %d)", chat_id, retry_after, attempt)
                self._sleep(retry_after)
                continue

            if status >= 500:
                if attempt > self._max_retries:
                    response.raise_for_status()
                self._sleep(min(2 ** (attempt - 1), _MAX_BACKOFF))
                continue

            response.raise_for_status()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            return SendResult(ok=True)
