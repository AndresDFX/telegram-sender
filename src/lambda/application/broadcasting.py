"""Caso de uso: difundir una lista (compone el mensaje según config, consulta activos y encola).

Agnóstico de SQS vs envío inline: eso depende de qué BroadcastQueue se inyecte.
La composición (quitar ubicación, markup, footer WhatsApp) usa la config en runtime.
Si WhatsApp está activo, reenvía la misma lista al servicio de WhatsApp (fire-and-forget).

Cada difusión crea un "job" con estado (BroadcastStore) y propaga su ``broadcast_id`` a la
cola (Telegram) y al servicio (WhatsApp) para que reporten progreso. ``enviar_manual`` permite
componer y enviar un mensaje propio (no capturado del canal), reutilizando las mismas listas.
"""

from __future__ import annotations

import logging
import os
import uuid

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
        broadcasts=None,
    ) -> None:
        self._subscribers = subscribers
        self._queue = queue
        self._config = config
        self._whatsapp = whatsapp
        self._image_store = image_store
        self._broadcasts = broadcasts

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _nuevo_id() -> str:
        return "b-" + uuid.uuid4().hex[:16]

    @staticmethod
    def _broadcasts_table() -> str | None:
        return os.environ.get("BROADCASTS_TABLE")

    def _registrar(self, broadcast_id: str, text: str, source: str, channels: list[str], tg_total: int) -> None:
        if not self._broadcasts:
            return
        try:
            self._broadcasts.crear(broadcast_id, text, source, channels, tg_total=tg_total)
        except Exception:
            logger.exception("No se pudo registrar el job %s (no afecta el envío)", broadcast_id)

    def _image_url_para_whatsapp(self, cfg: dict) -> str | None:
        if cfg.get("image_url"):
            return cfg["image_url"]
        if cfg.get("image_key") and self._image_store:
            try:
                return self._image_store.url_temporal(cfg["image_key"])
            except Exception:
                logger.exception("No se pudo firmar la imagen para WhatsApp")
        return None

    def _destinatarios_telegram(self, cfg: dict, target: dict | None = None) -> list:
        return filtrar_destinatarios(
            self._subscribers.listar_activos(),
            cfg.get("telegram_lists", []),
            target if target is not None else cfg.get("telegram_target", {}),
            excluidos=cfg.get("excluded_ids", []),
        )

    def _forward_whatsapp(
        self, cfg: dict, mensaje: str, image_url: str | None, broadcast_id: str, wa_target: dict | None = None
    ) -> None:
        wa_target = wa_target if wa_target is not None else cfg.get("whatsapp_target", {})
        aceptado = False
        try:
            resultado = self._whatsapp.forward(
                mensaje,
                image_url,
                cfg.get("whatsapp_excluded", []),
                mode=wa_target.get("mode", "all"),
                list_ids=sorted(ids_de_listas_activas(cfg.get("whatsapp_lists", []), wa_target)),
                broadcast_id=broadcast_id,
                broadcasts_table=self._broadcasts_table(),
            )
            logger.info("WhatsApp forward: %s", resultado)
            aceptado = isinstance(resultado, dict) and bool(resultado.get("accepted"))
        except Exception:
            logger.exception("Fallo reenviando a WhatsApp (no afecta el broadcast de Telegram)")
        # Si el servicio no aceptó el envío, cierra el canal WhatsApp del job (evita 'enviando' eterno).
        if not aceptado and self._broadcasts:
            try:
                self._broadcasts.marcar_whatsapp_fallido(broadcast_id)
            except Exception:
                logger.exception("No se pudo marcar WhatsApp fallido en el job %s", broadcast_id)

    # --- difusión desde el canal (con markup/footer) ---------------------------

    def __call__(self, text: str) -> dict[str, int]:
        cfg = self._config.get()
        mensaje = componer_mensaje(
            text,
            markup_percentage=cfg["markup_percentage"],
            currency_symbols=cfg["currency_symbols"],
            strip_patterns=cfg["strip_patterns"],
            footer=cfg["whatsapp_footer"],
        )
        clientes = self._destinatarios_telegram(cfg)
        wa_on = bool(self._whatsapp and cfg.get("whatsapp_enabled"))
        channels = ["telegram"] + (["whatsapp"] if wa_on else [])
        bid = self._nuevo_id()
        self._registrar(bid, mensaje, "channel", channels, len(clientes))

        lotes = self._queue.encolar(
            mensaje,
            clientes,
            image_url=cfg.get("image_url") or None,
            image_key=cfg.get("image_key") or None,
            broadcast_id=bid,
        )
        logger.info("Difusión %s: %d lotes para %d clientes", bid, lotes, len(clientes))
        if wa_on:
            self._forward_whatsapp(cfg, mensaje, self._image_url_para_whatsapp(cfg), bid)
        return {"batches": lotes, "subscribers": len(clientes), "broadcast_id": bid}

    # --- envío manual (mensaje propio, no capturado del canal) -----------------

    def _target_para(self, cfg: dict, canal: str, lista: str | None) -> dict:
        """Target efectivo de un canal: si se eligió una lista en el compositor, esa lista en
        modo 'only'; si no, el target configurado del canal."""
        if lista:
            return {"mode": "only", "lists": [lista]}
        return cfg.get(f"{canal}_target", {})

    def previsualizar(
        self, telegram: bool, whatsapp: bool, telegram_list: str | None = None, whatsapp_list: str | None = None
    ) -> dict:
        """Cuántos destinatarios recibirían (sin enviar). WhatsApp se aproxima por los ids de
        la lista (el servicio tiene los contactos reales)."""
        cfg = self._config.get()
        out: dict = {}
        if telegram:
            out["telegram"] = len(self._destinatarios_telegram(cfg, self._target_para(cfg, "telegram", telegram_list)))
        if whatsapp:
            wa_t = self._target_para(cfg, "whatsapp", whatsapp_list)
            out["whatsapp"] = len(ids_de_listas_activas(cfg.get("whatsapp_lists", []), wa_t))
        return out

    def enviar_manual(
        self,
        text: str,
        image_url: str | None = None,
        telegram: bool = True,
        whatsapp: bool = False,
        telegram_list: str | None = None,
        whatsapp_list: str | None = None,
    ) -> dict:
        """Envía un mensaje compuesto por el usuario (texto tal cual, sin markup), por los
        canales elegidos. Puede dirigirse a una lista concreta (telegram_list/whatsapp_list);
        si no, usa el target configurado del canal.

        Seguridad: por WhatsApp manual EXIGE una lista con destinatarios (no difunde a TODA la
        agenda por error). Lanza ValueError si no hay a quién enviar.
        """
        cfg = self._config.get()
        wa_on = bool(whatsapp and self._whatsapp)
        wa_target = self._target_para(cfg, "whatsapp", whatsapp_list)
        if wa_on:
            wa_ids = ids_de_listas_activas(cfg.get("whatsapp_lists", []), wa_target)
            if wa_target.get("mode") != "only" or not wa_ids:
                raise ValueError(
                    "Elige una lista de WhatsApp con destinatarios en 'Enviar a' (evita mandar a "
                    "todos por error). Crea/activa una lista en la pestaña WhatsApp."
                )
        tg_target = self._target_para(cfg, "telegram", telegram_list)
        channels = (["telegram"] if telegram else []) + (["whatsapp"] if wa_on else [])
        clientes = self._destinatarios_telegram(cfg, tg_target) if telegram else []
        bid = self._nuevo_id()
        self._registrar(bid, text, "manual", channels, len(clientes))

        if telegram:
            self._queue.encolar(text, clientes, image_url=image_url or None, image_key=None, broadcast_id=bid)
        if wa_on:
            self._forward_whatsapp(cfg, text, image_url or None, bid, wa_target)
        return {"broadcast_id": bid, "channels": channels, "telegram_total": len(clientes)}
