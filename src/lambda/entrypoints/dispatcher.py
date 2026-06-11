"""Entrypoint del dispatcher (EventBridge cada minuto): libera UN lote del plan activo.

Es el corazón del envío fraccionado/secuencial: en cada invocación despacha como mucho
un lote (Telegram o WhatsApp), respetando la ventana horaria y esperando a que el lote
anterior termine. La concurrencia reservada = 1 garantiza que no haya dos ticks a la vez.
"""

from __future__ import annotations

import logging
from typing import Any

import wiring

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dispatch = None  # caso de uso DispatchCampaigns; inyectable en tests


def _ensure() -> None:
    global dispatch
    if dispatch is None:
        dispatch = wiring.build_dispatch_campaigns()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    resultado = dispatch()
    logger.info("dispatcher: %s", resultado)
    return resultado
