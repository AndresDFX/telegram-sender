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
from domain.recipients import filtrar_destinatarios, ids_de_listas_activas, ids_excluidos_por_patron

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

    def _resolver_wa_total(self, wa_on: bool, mode: str, list_ids, exclude, exclude_patterns=(), pattern_exceptions=()) -> tuple[int, bool]:
        """(total, resuelto). Si el servicio no responde, el dispatcher lo resolverá luego."""
        if not (wa_on and self._whatsapp):
            return 0, True
        try:
            return int(self._whatsapp.contar(mode=mode, list_ids=list(list_ids or []), exclude=list(exclude or []), exclude_patterns=list(exclude_patterns or []), pattern_exceptions=list(pattern_exceptions or []))), True
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
        wa_image_key: str | None = None,
        not_before: int = 0,
        source: str = "channel",
    ) -> None:
        bs = int(cfg.get("batch_size", 150))
        # Dedup de destinatarios: un mismo id NO debe recibir el mensaje dos veces aunque aparezca
        # repetido (solape lista+ad-hoc, id duplicado en una lista, etc.). Preserva el orden.
        clientes = list(dict.fromkeys(str(c) for c in clientes))
        tg_lotes = self._chunk(clientes, bs) if tg_on else []
        wa_exclude = cfg.get("whatsapp_excluded", []) if wa_on else []
        wa_total, wa_resolved = self._resolver_wa_total(
            wa_on, wa_mode, wa_list_ids, wa_exclude,
            cfg.get("whatsapp_exclude_patterns", []), cfg.get("whatsapp_pattern_exceptions", [])
        )
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
            wa_image_key=wa_image_key,
            not_before=int(not_before or 0),
            source=source,
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

    def _excluidos_patron_tg(self, cfg: dict) -> set[str]:
        """IDs a auto-excluir porque su nombre coincide con algún patrón configurado
        (p. ej. 'FAM'). En modo bot no hay nombres -> conjunto vacío."""
        patrones = cfg.get("telegram_exclude_patterns", []) or []
        if not patrones:
            return set()
        try:
            excepciones = {str(x) for x in (cfg.get("telegram_pattern_exceptions") or [])}
            return ids_excluidos_por_patron(self._subscribers.listar_todos(), patrones) - excepciones
        except Exception:
            logger.exception("No se pudieron aplicar patrones de exclusión de Telegram")
            return set()

    def _destinatarios_telegram(self, cfg: dict, target: dict | None = None) -> list:
        tgt = target if target is not None else cfg.get("telegram_target", {})
        # Las exclusiones MANUALES por id/número (excluded_ids) aplican siempre. La auto-exclusión
        # por PATRÓN DE NOMBRE solo aplica a envíos AMPLIOS (all/except): en modo "only" (lista
        # explícita) los destinatarios se validan por id/número y NO se descartan por el nombre —
        # si un contacto de la lista se renombró, igual se le envía.
        excluidos = list(cfg.get("excluded_ids", []))
        if (tgt or {}).get("mode") != "only":
            excluidos += list(self._excluidos_patron_tg(cfg))
        return filtrar_destinatarios(
            self._subscribers.listar_activos(),
            cfg.get("telegram_lists", []),
            tgt,
            excluidos=excluidos,
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
                exclude_patterns=cfg.get("whatsapp_exclude_patterns", []),
                pattern_exceptions=cfg.get("whatsapp_pattern_exceptions", []),
            )
            logger.info("WhatsApp forward: %s", resultado)
            aceptado = isinstance(resultado, dict) and bool(resultado.get("accepted"))
        except Exception:
            logger.exception("Fallo reenviando a WhatsApp (no afecta el broadcast de Telegram)")
        # Si el servicio no aceptó el envío, cierra el canal WhatsApp del job (evita 'enviando' eterno).
        if not aceptado and self._broadcasts:
            try:
                self._broadcasts.marcar_whatsapp_fallido(broadcast_id)
                try:
                    self._broadcasts.registrar_error(broadcast_id, "WhatsApp — el servicio no aceptó el envío (¿conectado/URL/token?)")
                except Exception:
                    pass
            except Exception:
                logger.exception("No se pudo marcar WhatsApp fallido en el job %s", broadcast_id)

    # --- difusión desde el canal (con markup/footer) ---------------------------

    def __call__(self, text: str) -> dict[str, int]:
        cfg = self._config.get()
        # CAPTURA SIEMPRE: la info del canal (iproparts) se guarda aunque los envíos estén
        # pausados — se crea el plan EN ESPERA y el dispatcher lo enviará al activar el interruptor.
        # Nunca se pierde un post; lo opcional es el envío, no la captura.
        habilitado = bool(cfg.get("sending_enabled", True))
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
                wa_image_key=cfg.get("image_key") or None,
            )
            estado = "PROGRAMADA" if habilitado else "EN ESPERA (envíos pausados)"
            logger.info("Difusión %s %s (fraccionada) para %d clientes", bid, estado, len(clientes))
            return {"scheduled": True, "subscribers": len(clientes), "broadcast_id": bid, "held": not habilitado}

        if not habilitado:
            # Modo inline (sin scheduler, p.ej. dev): no hay plan que retener; no se envía mientras esté pausado.
            logger.info("Difusión %s registrada pero NO enviada (envíos pausados, modo inline)", bid)
            return {"paused": True, "subscribers": len(clientes), "broadcast_id": bid}
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
            # Selección EXPLÍCITA por id/número: se envía a EXACTAMENTE esos contactos. NO se aplica
            # la auto-exclusión por patrón de NOMBRE (el usuario los eligió por número; si el nombre
            # del contacto cambió, igual se le envía).
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
        image_key: str | None = None,
    ) -> dict:
        """Envía un mensaje propio (texto tal cual) por los canales elegidos. Destinatarios:
        contactos ad-hoc elegidos (telegram_ids/whatsapp_ids) > lista (telegram_list/whatsapp_list)
        > target configurado. WhatsApp manual EXIGE destinatarios concretos (no manda a todos).
        ``image_key`` (clave S3 de la imagen subida) se propaga al plan para RE-FIRMAR la URL al
        despachar: las URL prefirmadas caducan en 1h y un envío fraccionado/programado las dejaría
        muertas (la imagen no llegaría)."""
        cfg = self._config.get()
        # La PAUSA (interruptor maestro) solo frena los envíos AUTOMÁTICOS (captura del canal y
        # difusión programada). El envío MANUAL desde «Componer → Enviar» SIEMPRE sale: el usuario
        # lo pidió explícitamente. Por eso aquí NO se valida sending_enabled; el plan se marca
        # source="manual" para que el dispatcher y el worker lo dejen pasar aunque esté en pausa.
        # WhatsApp manual EXIGE que el SERVICIO esté CONFIGURADO (URL + token). NO exigimos
        # `whatsapp_enabled` (ese es el interruptor de auto-reenvío del canal): un envío manual es una
        # acción explícita del usuario y debe poder salir aunque el auto-reenvío esté apagado. Si el
        # servicio no está configurado, fallamos RUIDOSAMENTE (no en silencio). La liveness real
        # (servicio caído/desconectado) la detecta el dispatcher y marca el job como fallido.
        if whatsapp:
            wa_configurado = bool(
                self._whatsapp and cfg.get("whatsapp_service_url") and cfg.get("whatsapp_token")
            )
            if not wa_configurado:
                raise ValueError(
                    "WhatsApp no está configurado: conéctalo (Ajustes → WhatsApp: URL, token y QR) antes de elegirlo como canal."
                )
        wa_on = bool(whatsapp and self._whatsapp)
        wa_mode, wa_ids = self._wa_destino(cfg, whatsapp_list, whatsapp_ids)
        if wa_on and (wa_mode != "only" or not wa_ids):
            raise ValueError(
                "Elige contactos o una lista de WhatsApp en 'Enviar a' (evita mandar a todos por error)."
            )
        clientes = self._tg_clientes(cfg, telegram, telegram_list, telegram_ids)
        # Sin destinatarios reales en NINGÚN canal no hay envío. En vez de fallar en silencio
        # (crear un plan vacío que "no envía nada"), avisamos con la causa probable para que se
        # vea EN LA APP por qué no salió: casi siempre, patrones de exclusión que dejan fuera a todos.
        tg_count = len(clientes) if telegram else 0
        wa_count = len(wa_ids) if wa_on else 0
        if tg_count == 0 and wa_count == 0:
            if telegram:
                raise ValueError(
                    "No hay destinatarios para este envío: la selección de Telegram quedó vacía. "
                    "Revisa los patrones de exclusión (pestaña «Fuentes y listas» → Destinatarios): "
                    "es probable que estén excluyendo a todos los contactos."
                )
            raise ValueError("No hay destinatarios para este envío: elige al menos un canal con contactos.")
        channels = (["telegram"] if telegram else []) + (["whatsapp"] if wa_on else [])
        bid = self._nuevo_id()
        self._registrar(bid, text, "manual", channels, len(clientes))

        if self._usar_scheduler(cfg):
            self._crear_plan(
                bid, cfg=cfg, text=text, image_url=image_url or None, image_key=image_key or None,
                clientes=clientes, tg_on=bool(telegram), wa_on=wa_on, wa_mode=wa_mode, wa_list_ids=wa_ids,
                wa_text=text, wa_image_url=image_url or None, wa_image_key=image_key or None,
                not_before=int(scheduled_at or 0), source="manual",
            )
            return {"scheduled": True, "broadcast_id": bid, "channels": channels,
                    "telegram_total": len(clientes), "not_before": int(scheduled_at or 0)}

        if telegram:
            self._queue.encolar(text, clientes, image_url=image_url or None, image_key=image_key or None, broadcast_id=bid)
        if wa_on:
            self._forward_whatsapp(cfg, text, image_url or None, bid, wa_mode, wa_ids)
        return {"broadcast_id": bid, "channels": channels, "telegram_total": len(clientes)}
