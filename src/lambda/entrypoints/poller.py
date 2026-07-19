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
    finally:
        try:
            fuente.desconectar()  # M17: no dejar la conexión Telethon (GetContacts) abierta cada invoke
        except Exception:
            pass


def _refresh_tg_status() -> None:
    """Cachea el estado de sesión userbot para que el PANEL no abra su propia conexión Telethon cada
    60s (que se solaparía con la del poller usando la misma sesión). El poller ya abre Telethon cada
    run, así que verificar aquí y escribir la caché deja al panel en modo solo-lectura. None en modo bot."""
    cuenta = wiring.build_telethon_account()  # None en modo bot
    if cuenta is None:
        return
    try:
        est = cuenta.estado()  # estado() ya desconecta en su finally (M11)
        config_store.set_tg_status(connected=bool(est.get("authorized")), me=est.get("me"))
    except Exception:
        logger.exception("No se pudo cachear el estado de sesión de Telegram")
        try:
            cuenta.desconectar()
        except Exception:
            pass


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    # La RECOPILACIÓN es independiente del ENVÍO: el poller captura mientras capture_enabled esté
    # activo, aunque sending_enabled esté apagado. En ese caso PollChannel/BroadcastList solo
    # registran y previsualizan la lista (no la difunden). El gate de ENVÍO lo aplica BroadcastList.
    if not config_store.get().get("capture_enabled", True):
        logger.info("Recopilación automática PAUSADA (capture_enabled=False); el poller no captura.")
        _refresh_contacts()  # el panel sigue necesitando los contactos al día
        _refresh_tg_status()  # y el estado de sesión cacheado, para que el panel no abra Telethon
        return {"capture_paused": True}
    resultado = poll()
    _refresh_contacts()
    _refresh_tg_status()  # cachea el estado de sesión (panel en modo solo-lectura)
    return resultado
