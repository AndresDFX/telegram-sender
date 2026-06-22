"""Entrypoint del poller (EventBridge cron): difunde lo nuevo del canal y, en modo
userbot, refresca el caché de contactos para que el panel los muestre rápido."""

from __future__ import annotations

import logging
import time
from typing import Any

import wiring

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Telethon GetContacts es caro y dispara FloodWait si se llama seguido; refrescamos el
# caché como mucho cada 30 min (los contactos casi no cambian).
_CONTACTS_TTL = 1800

poll = None
config_store = None


def _ensure() -> None:
    global poll, config_store
    if poll is None:
        poll = wiring.build_poll_channel()
    if config_store is None:
        config_store = wiring.build_config_store()


def _refresh_contacts() -> None:
    fuente = wiring.build_contacts_source()  # None en modo bot
    if fuente is None:
        return
    try:
        if int(time.time()) - config_store.contacts_refreshed_at() < _CONTACTS_TTL:
            return  # refrescado hace poco: no llamar a GetContacts (evita FloodWait)
    except Exception:
        pass  # si no se puede leer el timestamp, intentamos refrescar igual
    try:
        config_store.set_contacts(fuente.listar())
    except Exception:
        logger.exception("No se pudo refrescar el caché de contactos")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    # La RECOPILACIÓN es independiente del ENVÍO: el poller captura mientras capture_enabled esté
    # activo, aunque sending_enabled esté apagado. En ese caso PollChannel/BroadcastList solo
    # registran y previsualizan la lista (no la difunden). El gate de ENVÍO lo aplica BroadcastList.
    if not config_store.get().get("capture_enabled", True):
        logger.info("Recopilación automática PAUSADA (capture_enabled=False); el poller no captura.")
        _refresh_contacts()  # el panel sigue necesitando los contactos al día
        return {"capture_paused": True}
    resultado = poll()
    _refresh_contacts()
    return resultado
