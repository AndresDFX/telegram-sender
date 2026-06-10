"""Caso de uso: difundir una lista (compone el mensaje según config, consulta activos y encola).

Agnóstico de SQS vs envío inline: eso depende de qué BroadcastQueue se inyecte.
La composición (quitar ubicación, markup, footer WhatsApp) usa la config en runtime.
"""

from __future__ import annotations

import logging

from application.ports import BroadcastQueue, ConfigStore, SubscriberRepository
from domain.message import componer_mensaje

logger = logging.getLogger(__name__)


class BroadcastList:
    def __init__(self, subscribers: SubscriberRepository, queue: BroadcastQueue, config: ConfigStore) -> None:
        self._subscribers = subscribers
        self._queue = queue
        self._config = config

    def __call__(self, text: str) -> dict[str, int]:
        cfg = self._config.get()
        mensaje = componer_mensaje(
            text,
            markup_percentage=cfg["markup_percentage"],
            currency_symbols=cfg["currency_symbols"],
            strip_patterns=cfg["strip_patterns"],
            footer=cfg["whatsapp_footer"],
        )
        excluidos = set(cfg.get("excluded_ids", []))
        clientes = [c for c in self._subscribers.listar_activos() if str(c) not in excluidos]
        lotes = self._queue.encolar(
            mensaje, clientes, image_url=cfg.get("image_url") or None, image_key=cfg.get("image_key") or None
        )
        logger.info("Difusión: %d lotes para %d clientes (%d excluidos)", lotes, len(clientes), len(excluidos))
        return {"batches": lotes, "subscribers": len(clientes)}
