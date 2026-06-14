"""Caso de uso: DESPACHAR planes de envío fraccionado, UN lote a la vez (secuencial).

Lo invoca el dispatcher (EventBridge, cada minuto). En cada tick:
  1. Toma el plan activo MÁS ANTIGUO (un plan a la vez = secuencialidad global).
  2. Si hay un lote en vuelo, comprueba si terminó leyendo los contadores de progreso
     (que ya actualizan el worker de Telegram y el servicio de WhatsApp). Si lleva
     demasiado tiempo, lo da por cerrado para no bloquear la cola.
  3. Si la ventana horaria está cerrada, no despacha nada (encolamiento diferido).
  4. Libera el SIGUIENTE lote (Telegram primero, luego WhatsApp) y registra a qué hora
     se programó y cuántos destinatarios lleva acumulados (para el panel de monitoreo).

Así nunca se "vuelca" toda la difusión de golpe: se gotea un lote por tick, respetando
horario y ritmo, evitando patrones que disparen baneos.
"""

from __future__ import annotations

import logging
import os
import time

from domain.scheduling import total_lotes, ventana_abierta

logger = logging.getLogger(__name__)


class DispatchCampaigns:
    def __init__(
        self,
        plans,
        broadcasts,
        queue,
        whatsapp,
        config,
        broadcasts_table: str | None = None,
        stale_seconds: int = 900,
        now=time.time,
    ) -> None:
        self._plans = plans
        self._broadcasts = broadcasts
        self._queue = queue
        self._whatsapp = whatsapp
        self._config = config
        self._broadcasts_table = broadcasts_table or os.environ.get("BROADCASTS_TABLE")
        self._stale = stale_seconds
        self._now = now

    def __call__(self) -> dict:
        cfg = self._config.get()
        # Interruptor maestro: en PAUSA solo se frena lo AUTOMÁTICO (captura del canal y
        # difusión programada → planes source="channel"). Los envíos MANUALES (Componer → Enviar,
        # planes source="manual") SÍ se despachan: el usuario los pidió explícitamente.
        paused = not cfg.get("sending_enabled", True)
        now = int(self._now())
        planes = self._plans.activos()
        if paused:
            planes = [p for p in planes if p.get("source") == "manual"]
        if not planes:
            return {"paused": True} if paused else {"planes": 0}

        # Programación a hora exacta: solo despachamos planes cuyo not_before ya pasó.
        listos = [p for p in planes if int(p.get("not_before", 0)) <= now]
        if not listos:
            return {"planes": len(planes), "diferido_horario": True}

        # Keep-alive: hay trabajo LISTO -> mantenemos despierto el servicio de WhatsApp
        # (Render Free duerme a los 15 min y el primer envío tras dormir podría expirar).
        try:
            self._whatsapp.ping()
        except Exception:
            pass

        plan = listos[0]  # secuencial GLOBAL: el plan LISTO más antiguo primero
        pid = plan["pid"]
        bid = plan.get("broadcast_id")

        # 1) ¿Hay un lote en vuelo? ¿Terminó (según contadores) o se estancó?
        in_flight = plan.get("in_flight") or ""
        if in_flight:
            ch = plan.get("in_flight_channel") or ("tg" if in_flight.startswith("TG") else "wa")
            target = int(plan.get("in_flight_target", 0))
            prog = self._broadcasts.progreso(bid) if bid else {"tg": target, "wa": target}
            edad = now - int(plan.get("in_flight_at", 0))
            if prog.get(ch, 0) >= target:
                self._plans.limpiar_inflight(pid)
            elif edad > self._stale:
                logger.warning("Lote %s del plan %s estancado (%ss); libero el cursor", in_flight, pid, edad)
                self._plans.limpiar_inflight(pid)
            else:
                return {"plan": pid, "esperando": in_flight, "progreso": prog.get(ch, 0), "target": target}

        bs = int(plan.get("batch_size", 150)) or 150

        # Ventanas horarias POR CANAL (independientes): el horario de Telegram y el de WhatsApp se
        # evalúan por separado, así una ventana cerrada en un canal NO frena al otro. Si un canal no
        # tiene horario propio configurado, hereda el global (compatibilidad).
        tg_open = self._ventana_canal(cfg, "tg", now)
        wa_open = self._ventana_canal(cfg, "wa", now)

        # 2) Telegram: independiente de WhatsApp. Se despacha si tiene lotes pendientes y su ventana
        #    está abierta. NO se resuelve nada de WhatsApp antes, para que una caída de WhatsApp
        #    NUNCA bloquee los envíos de Telegram.
        tg_next = int(plan.get("tg_next", 0))
        tg_pendiente = tg_next < int(plan.get("tg_batches", 0))
        if tg_pendiente and tg_open:
            ids = self._plans.ids_lote_tg(pid, tg_next)
            target = int(plan.get("tg_dispatched", 0)) + len(ids)
            # Reclama el lote ANTES de encolar: si un cancel concurrente ganó, no se envía.
            if not self._plans.registrar_dispatch(pid, channel="tg", index=tg_next, n=len(ids), target=target, now=now):
                logger.info("Plan %s cancelado en carrera; no se despacha TG#%d", pid, tg_next)
                return {"plan": pid, "cancelado": True}
            self._queue.encolar_uno(
                plan.get("text", ""),
                ids,
                image_url=plan.get("image_url") or None,
                image_key=plan.get("image_key") or None,
                broadcast_id=bid,
                batch_index=tg_next,
                pid=pid,
                # el worker lee este flag: en pausa entrega solo los lotes manuales (no los automáticos).
                manual=(plan.get("source") == "manual"),
            )
            logger.info("Plan %s: despachado TG#%d (%d destinatarios)", pid, tg_next, len(ids))
            return {"plan": pid, "despachado": f"TG#{tg_next}", "n": len(ids)}

        # 3) WhatsApp: independiente de Telegram. Resolvemos su total AQUÍ (no antes), de modo que un
        #    WhatsApp caído solo afecta a WhatsApp. Se despacha si su ventana está abierta.
        if plan.get("wa_enabled") and wa_open:
            if not plan.get("wa_resolved"):
                try:
                    total = self._whatsapp.contar(
                        mode=plan.get("wa_mode", "all"),
                        list_ids=plan.get("wa_list_ids", []),
                        exclude=plan.get("wa_exclude", []),
                        exclude_patterns=cfg.get("whatsapp_exclude_patterns", []),
                        pattern_exceptions=cfg.get("whatsapp_pattern_exceptions", []),
                    )
                except Exception:
                    logger.exception("No se pudo resolver el total de WhatsApp del plan %s; reintento luego", pid)
                    return {"plan": pid, "esperando_wa": True}
                self._plans.resolver_wa(pid, total, bs)
                plan["wa_total"] = total
                plan["wa_batches"] = total_lotes(total, bs)
                plan["wa_resolved"] = True

            wa_next = int(plan.get("wa_next", 0))
            if wa_next < int(plan.get("wa_batches", 0)):
                offset = wa_next * bs
                wa_total = int(plan.get("wa_total", 0))
                limit = max(0, min(bs, wa_total - offset))
                target = int(plan.get("wa_dispatched", 0)) + limit
                # Reclama el lote ANTES de llamar al servicio: si se canceló en carrera, no se envía.
                if not self._plans.registrar_dispatch(pid, channel="wa", index=wa_next, n=limit, target=target, now=now):
                    logger.info("Plan %s cancelado en carrera; no se despacha WA#%d", pid, wa_next)
                    return {"plan": pid, "cancelado": True}
                self._whatsapp.forward(
                    plan.get("wa_text", "") or plan.get("text", ""),
                    plan.get("wa_image_url") or None,
                    plan.get("wa_exclude", []),
                    mode=plan.get("wa_mode", "all"),
                    list_ids=plan.get("wa_list_ids", []),
                    broadcast_id=bid,
                    broadcasts_table=self._broadcasts_table,
                    offset=offset,
                    limit=limit,
                    bc_total=wa_total,
                    delay_min_ms=int(cfg.get("wa_delay_min", 3000)),
                    delay_max_ms=int(cfg.get("wa_delay_max", 9000)),
                    exclude_patterns=cfg.get("whatsapp_exclude_patterns", []),
                    pattern_exceptions=cfg.get("whatsapp_pattern_exceptions", []),
                )
                logger.info("Plan %s: despachado WA#%d (offset %d, %d destinatarios)", pid, wa_next, offset, limit)
                return {"plan": pid, "despachado": f"WA#{wa_next}", "n": limit}

        # 4) Nada despachable AHORA: si hay pendientes pero su ventana está cerrada -> diferir.
        wa_pendiente = bool(plan.get("wa_enabled")) and (
            not plan.get("wa_resolved") or int(plan.get("wa_next", 0)) < int(plan.get("wa_batches", 0))
        )
        if (tg_pendiente and not tg_open) or (wa_pendiente and not wa_open):
            return {"plan": pid, "diferido": "fuera de ventana"}

        # 5) Nada pendiente -> plan terminado.
        self._plans.finalizar(pid)
        logger.info("Plan %s finalizado", pid)
        return {"plan": pid, "finalizado": True}

    def _ventana_canal(self, cfg: dict, canal: str, now: int) -> bool:
        """¿Está abierta la ventana horaria del canal (``tg``/``wa``) ahora? Cada canal tiene su
        propio horario; si no está configurado, hereda el horario global (compatibilidad)."""
        enabled = cfg.get(f"{canal}_window_enabled")
        if enabled is None:
            enabled = cfg.get("window_enabled")
        start = cfg.get(f"{canal}_window_start") or cfg.get("window_start", "08:00")
        end = cfg.get(f"{canal}_window_end") or cfg.get("window_end", "20:00")
        return ventana_abierta(
            now,
            enabled=bool(enabled),
            start=start,
            end=end,
            tz_offset_min=int(cfg.get("window_tz", -300)),
        )
