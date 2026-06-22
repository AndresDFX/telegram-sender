"""Entrypoint del dispatcher (EventBridge cada minuto): libera UN lote del plan activo.

Es el corazón del envío fraccionado/secuencial: en cada invocación despacha como mucho
un lote (Telegram o WhatsApp), respetando la ventana horaria y esperando a que el lote
anterior termine.

M28: la NO-duplicación NO depende de la concurrencia reservada (su default es 0). La
garantía real es el LOCK OPTIMISTA de ``registrar_dispatch`` (ConditionExpression sobre el
cursor): aunque EventBridge entregue un tick duplicado (at-least-once) o dos invocaciones se
solapen, solo una puede reclamar un índice dado. El único efecto de dos ticks simultáneos es
que podrían reclamar TG y WA del MISMO plan a la vez (no es duplicado, pero rompe el ritmo
"un lote a la vez"); si se quiere esa secuencialidad estricta, fijar
``DispatcherReservedConcurrency=1`` en el stack.
"""

from __future__ import annotations

import logging
from typing import Any

import wiring

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dispatch = None  # caso de uso DispatchCampaigns; inyectable en tests
materialize = None  # caso de uso MaterializeSchedules (mensajes programados)


def _ensure() -> None:
    global dispatch, materialize
    if dispatch is None:
        dispatch = wiring.build_dispatch_campaigns()
    if materialize is None:
        materialize = wiring.build_materialize_schedules()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    # 1) Materializa los mensajes programados vencidos en envíos (planes). Aislado: si falla,
    #    NO debe impedir el despacho de los planes ya existentes.
    schedules = {}
    try:
        schedules = materialize()
    except Exception:
        logger.exception("Materializar mensajes programados falló (no afecta el despacho)")
    # 2) Despacha UN lote del plan activo más antiguo.
    resultado = dispatch()
    logger.info("dispatcher: schedules=%s dispatch=%s", schedules, resultado)
    return {"schedules": schedules, **resultado}
