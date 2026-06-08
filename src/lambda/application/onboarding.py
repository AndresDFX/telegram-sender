"""Caso de uso: alta/baja de suscriptores por comandos privados (/start, /stop)."""

from __future__ import annotations

import logging

from application.ports import MessageSender, SubscriberRepository
from domain.models import ACTIVE, INACTIVE

logger = logging.getLogger(__name__)

WELCOME = "✅ Te suscribiste. Recibirás aquí las listas en cuanto se publiquen.\nEnvía /stop para darte de baja."
GOODBYE = "🚫 Te diste de baja. No recibirás más listas.\nEnvía /start cuando quieras volver."
HELP = "Comandos disponibles:\n/start — suscribirte\n/stop — darte de baja"


class HandleCommand:
    def __init__(self, subscribers: SubscriberRepository, sender: MessageSender) -> None:
        self._subscribers = subscribers
        self._sender = sender

    def __call__(self, chat_id: str, comando: str) -> str:
        if comando == "/start":
            self._subscribers.registrar(chat_id, ACTIVE)
            self._responder(chat_id, WELCOME)
        elif comando == "/stop":
            self._subscribers.registrar(chat_id, INACTIVE)
            self._responder(chat_id, GOODBYE)
        else:
            self._responder(chat_id, HELP)
        logger.info("Comando %s de %s procesado", comando, chat_id)
        return comando

    def _responder(self, chat_id: str, text: str) -> None:
        """Best-effort: un fallo al responder NO debe revertir el alta/baja ya persistida."""
        try:
            self._sender.enviar(chat_id, text)
        except Exception:
            logger.exception("No se pudo responder a %s (el alta/baja ya está persistida)", chat_id)
