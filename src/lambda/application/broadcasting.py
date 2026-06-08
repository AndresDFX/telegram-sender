"""Caso de uso: difundir una lista (aplica markup, consulta activos y encola).

Agnóstico de SQS vs envío inline: eso depende de qué BroadcastQueue se inyecte.
"""

from __future__ import annotations

import logging

from application.ports import BroadcastQueue, SubscriberRepository
from domain.markup import DEFAULT_CURRENCY_SYMBOLS, DEFAULT_MARKUP_PERCENTAGE, aplicar_markup

logger = logging.getLogger(__name__)


class BroadcastList:
    def __init__(
        self,
        subscribers: SubscriberRepository,
        queue: BroadcastQueue,
        percentage: float = DEFAULT_MARKUP_PERCENTAGE,
        currency_symbols: str = DEFAULT_CURRENCY_SYMBOLS,
    ) -> None:
        self._subscribers = subscribers
        self._queue = queue
        self._percentage = percentage
        self._currency_symbols = currency_symbols

    def __call__(self, text: str) -> dict[str, int]:
        mensaje = aplicar_markup(text, self._percentage, currency_symbols=self._currency_symbols)
        clientes = self._subscribers.listar_activos()
        lotes = self._queue.encolar(mensaje, clientes)
        logger.info("Difusión: %d lotes para %d clientes", lotes, len(clientes))
        return {"batches": lotes, "subscribers": len(clientes)}
