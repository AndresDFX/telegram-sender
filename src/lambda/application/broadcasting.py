"""Caso de uso: difundir una lista (compone el mensaje según config, consulta activos y encola).

Agnóstico de SQS vs envío inline: eso depende de qué BroadcastQueue se inyecte.
La composición (quitar ubicación, markup, footer WhatsApp) usa la config en runtime.
Si WhatsApp está activo, reenvía la misma lista al servicio de WhatsApp (fire-and-forget).
"""

from __future__ import annotations

import logging

from application.ports import BroadcastQueue, ConfigStore, ImageStore, SubscriberRepository, WhatsAppForwarder
from domain.message import componer_mensaje
from domain.recipients import filtrar_destinatarios, ids_de_listas_activas

logger = logging.getLogger(__name__)


class BroadcastList:
    def __init__(
        self,
        subscribers: SubscriberRepository,
        queue: BroadcastQueue,
        config: ConfigStore,
        whatsapp: WhatsAppForwarder | None = None,
        image_store: ImageStore | None = None,
    ) -> None:
        self._subscribers = subscribers
        self._queue = queue
        self._config = config
        self._whatsapp = whatsapp
        self._image_store = image_store

    def _image_url_para_whatsapp(self, cfg: dict) -> str | None:
        if cfg.get("image_url"):
            return cfg["image_url"]
        if cfg.get("image_key") and self._image_store:
            try:
                return self._image_store.url_temporal(cfg["image_key"])
            except Exception:
                logger.exception("No se pudo firmar la imagen para WhatsApp")
        return None

    def __call__(self, text: str) -> dict[str, int]:
        cfg = self._config.get()
        mensaje = componer_mensaje(
            text,
            markup_percentage=cfg["markup_percentage"],
            currency_symbols=cfg["currency_symbols"],
            strip_patterns=cfg["strip_patterns"],
            footer=cfg["whatsapp_footer"],
        )
        # Telegram: el Lambda conoce los contactos → resuelve las listas aquí mismo.
        clientes = filtrar_destinatarios(
            self._subscribers.listar_activos(),
            cfg.get("telegram_lists", []),
            cfg.get("telegram_target", {}),
            excluidos=cfg.get("excluded_ids", []),
        )
        lotes = self._queue.encolar(
            mensaje, clientes, image_url=cfg.get("image_url") or None, image_key=cfg.get("image_key") or None
        )
        logger.info(
            "Difusión: %d lotes para %d clientes (modo %s)",
            lotes,
            len(clientes),
            cfg.get("telegram_target", {}).get("mode", "all"),
        )

        if self._whatsapp and cfg.get("whatsapp_enabled"):
            try:
                wa_target = cfg.get("whatsapp_target", {})
                resultado = self._whatsapp.forward(
                    mensaje,
                    self._image_url_para_whatsapp(cfg),
                    cfg.get("whatsapp_excluded", []),
                    mode=wa_target.get("mode", "all"),
                    list_ids=sorted(ids_de_listas_activas(cfg.get("whatsapp_lists", []), wa_target)),
                )
                logger.info("WhatsApp forward: %s", resultado)
            except Exception:
                logger.exception("Fallo reenviando a WhatsApp (no afecta el broadcast de Telegram)")

        return {"batches": lotes, "subscribers": len(clientes)}
