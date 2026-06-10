"""Entrypoint del poller (EventBridge cron): difunde lo nuevo del canal y, en modo
userbot, refresca el caché de contactos para que el panel los muestre rápido."""

from __future__ import annotations

import logging
from typing import Any

import wiring

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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
        config_store.set_contacts(fuente.listar())
    except Exception:
        logger.exception("No se pudo refrescar el caché de contactos")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    resultado = poll()
    _refresh_contacts()
    return resultado
