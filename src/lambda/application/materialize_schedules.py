"""Caso de uso: materializar mensajes programados vencidos en envíos reales.

Lo invoca el dispatcher (EventBridge, cada minuto) ANTES de despachar lotes. Por cada
horario activo cuyo ``next_run`` ya pasó, crea un envío manual reutilizando
``BroadcastList.enviar_manual`` (→ plan → dispatcher), de modo que hereda TODO el pipeline
existente: envío fraccionado/secuencial, ventana horaria, jitter anti-baneo, ambos canales
(Telegram y WhatsApp) y el interruptor maestro.

Luego avanza el horario:
  * ``once``  -> se desactiva (no vuelve a disparar).
  * recurrente -> recalcula su próximo ``next_run``.

Si los envíos están PAUSADOS (``sending_enabled=False``) no dispara, pero igual avanza los
recurrentes (evita acumular un backlog que explotaría al reactivar); los ``once`` quedan
pendientes hasta que se reactiven los envíos.
"""

from __future__ import annotations

import logging
import time

from domain.schedules import ONCE, proximo_run

logger = logging.getLogger(__name__)


class MaterializeSchedules:
    def __init__(self, schedules, broadcasting, config, now=time.time) -> None:
        self._schedules = schedules
        self._broadcasting = broadcasting
        self._config = config
        self._now = now

    def __call__(self) -> dict:
        ahora = int(self._now())
        vencidos = self._schedules.vencidos(ahora)
        if not vencidos:
            return {"vencidos": 0}
        cfg = self._config.get()
        activos = bool(cfg.get("sending_enabled", True))
        tz = int(cfg.get("window_tz", -300))
        disparados = omitidos = errores = 0
        for s in vencidos:
            try:
                # A2: AVANZAR el horario ANTES de enviar. Si _avanzar va bien pero el envío falla,
                # el horario ya quedó avanzado y NO se re-dispara cada minuto (antes: enviar→avanzar,
                # y un fallo de _avanzar tras un envío OK reenviaba el mismo horario en cada tick).
                # A lo sumo se pierde UNA ocurrencia (registrada), en vez de duplicar en bucle.
                self._avanzar(s, ahora, tz, salto=not activos)
                if activos:
                    self._broadcasting.enviar_manual(
                        s.get("text", ""),
                        image_url=s.get("image_url") or None,
                        telegram=bool(s.get("telegram")),
                        whatsapp=bool(s.get("whatsapp")),
                        telegram_list=s.get("telegram_list") or None,
                        whatsapp_list=s.get("whatsapp_list") or None,
                    )
                    disparados += 1
                else:
                    omitidos += 1
            except Exception:
                errores += 1
                logger.exception("No se pudo materializar el horario %s", s.get("sid"))
        return {
            "vencidos": len(vencidos),
            "disparados": disparados,
            "omitidos_pausa": omitidos,
            "errores": errores,
        }

    def _avanzar(self, s: dict, ahora: int, tz: int, salto: bool) -> None:
        sid = s["sid"]
        if str(s.get("type")) == ONCE:
            if salto:
                return  # pausado: el 'once' queda pendiente hasta reactivar los envíos
            self._schedules.actualizar(sid, enabled=False, last_run=ahora, runs=int(s.get("runs", 0)) + 1)
            return
        nxt = proximo_run(s.get("type"), s.get("at"), s.get("days"), tz, ahora)
        campos: dict = {}
        if not salto:
            campos["last_run"] = ahora
            campos["runs"] = int(s.get("runs", 0)) + 1
        if nxt:
            campos["next_run"] = nxt
        else:
            campos["enabled"] = False  # recurrente mal formado: desactiva para no reintentar en bucle
        self._schedules.actualizar(sid, **campos)
