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
        plans=None,
    ) -> None:
        self._subscribers = subscribers
        self._queue = queue
        self._config = config
        self._whatsapp = whatsapp
        self._image_store = image_store
        self._broadcasts = broadcasts
        self._plans = plans

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _nuevo_id() -> str:
        return "b-" + uuid.uuid4().hex[:16]

    @staticmethod
    def _chunk(items, size: int) -> list[list]:
        size = max(1, int(size))
        return [list(items[i : i + size]) for i in range(0, len(items), size)]

    def _usar_scheduler(self, cfg: dict) -> bool:
        """Fracciona y secuencia (un lote a la vez) si está activo y hay store de planes.
        Si falta el store (p.ej. tests) cae al envío inmediato (compatibilidad)."""
        return bool(cfg.get("scheduling_enabled")) and self._plans is not None

    def _resolver_wa_total(self, wa_on: bool, mode: str, list_ids, exclude) -> tuple[int, bool]:
        """(total, resuelto). Si el servicio no responde, el dispatcher lo resolverá luego."""
        if not (wa_on and self._whatsapp):
            return 0, True
        try:
            return int(self._whatsapp.contar(mode=mode, list_ids=list(list_ids or []), exclude=list(exclude or []))), True
        except Exception:
            logger.exception("No se pudo contar WhatsApp al crear el plan; el dispatcher lo resolverá")
            return 0, False

    def _crear_plan(
        self,
        bid: str,
        *,
        cfg: dict,
        text: str,
        image_url: str | None,
        image_key: str | None,
        clientes: list,
        tg_on: bool,
        wa_on: bool,
        wa_mode: str,
        wa_list_ids,
        wa_text: str,
        wa_image_url: str | None,
        not_before: int = 0,
    ) -> None:
        bs = int(cfg.get("batch_size", 150))
        tg_lotes = self._chunk(clientes, bs) if tg_on else []
        wa_exclude = cfg.get("whatsapp_excluded", []) if wa_on else []
        wa_total, wa_resolved = self._resolver_wa_total(wa_on, wa_mode, wa_list_ids, wa_exclude)
        self._plans.crear(
            bid,
            broadcast_id=bid,
            text=text,
            image_url=image_url,
            image_key=image_key,
            batch_size=bs,
            tg_lotes=tg_lotes,
            wa_enabled=wa_on,
            wa_total=wa_total,
            wa_resolved=wa_resolved,
            wa_mode=wa_mode,
            wa_list_ids=list(wa_list_ids or []),
            wa_exclude=wa_exclude,
            wa_text=wa_text,
            wa_image_url=wa_image_url,
            not_before=int(not_before or 0),
        )

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
        self, cfg: dict, mensaje: str, image_url: str | None, broadcast_id: str, mode: str, list_ids
    ) -> None:
        aceptado = False
        try:
            resultado = self._whatsapp.forward(
                mensaje,
                image_url,
                cfg.get("whatsapp_excluded", []),
                mode=mode,
                list_ids=sorted(list_ids),
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
        if not cfg.get("sending_enabled", True):
            logger.info("Envíos PAUSADOS (sending_enabled=False); difusión de canal omitida")
            return {"paused": True, "subscribers": 0}
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

        wa_t = cfg.get("whatsapp_target", {})
        wa_mode = wa_t.get("mode", "all")
        wa_list_ids = ids_de_listas_activas(cfg.get("whatsapp_lists", []), wa_t)

        if self._usar_scheduler(cfg):
            # Envío fraccionado: se crea el plan y el dispatcher gotea un lote a la vez.
            self._crear_plan(
                bid, cfg=cfg, text=mensaje,
                image_url=cfg.get("image_url") or None, image_key=cfg.get("image_key") or None,
                clientes=clientes, tg_on=True, wa_on=wa_on, wa_mode=wa_mode, wa_list_ids=wa_list_ids,
                wa_text=mensaje, wa_image_url=self._image_url_para_whatsapp(cfg),
            )
            logger.info("Difusión %s PROGRAMADA (fraccionada) para %d clientes", bid, len(clientes))
            return {"scheduled": True, "subscribers": len(clientes), "broadcast_id": bid}

        lotes = self._queue.encolar(
            mensaje,
            clientes,
            image_url=cfg.get("image_url") or None,
            image_key=cfg.get("image_key") or None,
            broadcast_id=bid,
        )
        logger.info("Difusión %s: %d lotes para %d clientes", bid, lotes, len(clientes))
        if wa_on:
            self._forward_whatsapp(cfg, mensaje, self._image_url_para_whatsapp(cfg), bid, wa_mode, wa_list_ids)
        return {"batches": lotes, "subscribers": len(clientes), "broadcast_id": bid}

    # --- envío manual (mensaje propio, no capturado del canal) -----------------

    def _target_para(self, cfg: dict, canal: str, lista: str | None) -> dict:
        if lista:
            return {"mode": "only", "lists": [lista]}
        return cfg.get(f"{canal}_target", {})

    def _wa_destino(self, cfg: dict, whatsapp_list, whatsapp_ids):
        """(mode, list_ids) para WhatsApp: contactos ad-hoc elegidos > lista elegida > config."""
        if whatsapp_ids:
            return "only", [str(x) for x in whatsapp_ids]
        t = self._target_para(cfg, "whatsapp", whatsapp_list)
        return t.get("mode", "all"), sorted(ids_de_listas_activas(cfg.get("whatsapp_lists", []), t))

    def _tg_clientes(self, cfg: dict, telegram, telegram_list, telegram_ids) -> list:
        if not telegram:
            return []
        if telegram_ids:
            sel = {str(x) for x in telegram_ids}
            return [c for c in self._subscribers.listar_activos() if str(c) in sel]
        return self._destinatarios_telegram(cfg, self._target_para(cfg, "telegram", telegram_list))

    def previsualizar(
        self,
        telegram: bool,
        whatsapp: bool,
        telegram_list: str | None = None,
        whatsapp_list: str | None = None,
        telegram_ids=None,
        whatsapp_ids=None,
    ) -> dict:
        """Cuántos destinatarios recibirían (sin enviar)."""
        cfg = self._config.get()
        out: dict = {}
        if telegram:
            out["telegram"] = len(self._tg_clientes(cfg, True, telegram_list, telegram_ids))
        if whatsapp:
            out["whatsapp"] = len(self._wa_destino(cfg, whatsapp_list, whatsapp_ids)[1])
        return out

    def enviar_manual(
        self,
        text: str,
        image_url: str | None = None,
        telegram: bool = True,
        whatsapp: bool = False,
        telegram_list: str | None = None,
        whatsapp_list: str | None = None,
        telegram_ids=None,
        whatsapp_ids=None,
        scheduled_at: int | None = None,
    ) -> dict:
        """Envía un mensaje propio (texto tal cual) por los canales elegidos. Destinatarios:
        contactos ad-hoc elegidos (telegram_ids/whatsapp_ids) > lista (telegram_list/whatsapp_list)
        > target configurado. WhatsApp manual EXIGE destinatarios concretos (no manda a todos)."""
        cfg = self._config.get()
        if not cfg.get("sending_enabled", True):
            raise ValueError("Envíos pausados: actívalos en la pestaña Programación para poder enviar.")
        wa_on = bool(whatsapp and self._whatsapp)
        wa_mode, wa_ids = self._wa_destino(cfg, whatsapp_list, whatsapp_ids)
        if wa_on and (wa_mode != "only" or not wa_ids):
            raise ValueError(
                "Elige contactos o una lista de WhatsApp en 'Enviar a' (evita mandar a todos por error)."
            )
        clientes = self._tg_clientes(cfg, telegram, telegram_list, telegram_ids)
        channels = (["telegram"] if telegram else []) + (["whatsapp"] if wa_on else [])
        bid = self._nuevo_id()
        self._registrar(bid, text, "manual", channels, len(clientes))

        if self._usar_scheduler(cfg):
            self._crear_plan(
                bid, cfg=cfg, text=text, image_url=image_url or None, image_key=None,
                clientes=clientes, tg_on=bool(telegram), wa_on=wa_on, wa_mode=wa_mode, wa_list_ids=wa_ids,
                wa_text=text, wa_image_url=image_url or None, not_before=int(scheduled_at or 0),
            )
            return {"scheduled": True, "broadcast_id": bid, "channels": channels,
                    "telegram_total": len(clientes), "not_before": int(scheduled_at or 0)}

        if telegram:
            self._queue.encolar(text, clientes, image_url=image_url or None, image_key=None, broadcast_id=bid)
        if wa_on:
            self._forward_whatsapp(cfg, text, image_url or None, bid, wa_mode, wa_ids)
        return {"broadcast_id": bid, "channels": channels, "telegram_total": len(clientes)}
