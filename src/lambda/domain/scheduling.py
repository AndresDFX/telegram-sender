"""Políticas puras de envío anti-baneo: tope de lote, ventana horaria y delay aleatorio.

Sin dependencias de infraestructura para poder probarlas de forma aislada. El dispatcher
(secuencial, un lote a la vez) y los workers (Telegram/WhatsApp) las consumen.
"""

from __future__ import annotations

import math
import random

# Tope duro de contactos por lote (editable a la baja desde el panel, nunca por encima).
BATCH_MAX = 150


def cap_batch_size(value, maximo: int = BATCH_MAX) -> int:
    """Normaliza el tamaño de lote al rango [1, maximo]. Default = maximo si es inválido."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return maximo
    if not math.isfinite(n):   # A1: 'inf'/'1e400'/'nan' no deben reventar (int(inf) lanza OverflowError)
        return maximo
    return max(1, min(int(n), maximo))


def total_lotes(total: int, batch_size: int) -> int:
    """Cuántos lotes hacen falta para 'total' destinatarios con el tamaño dado."""
    if total <= 0:
        return 0
    bs = max(1, int(batch_size))
    return (total + bs - 1) // bs


def _hhmm_a_minutos(texto: str, fallback: int) -> int:
    try:
        h, m = str(texto).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:   # M1: rechazar minutos/horas fuera de rango (antes "08:90" pasaba)
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return fallback


def ventana_abierta(now_epoch: float, *, enabled: bool, start: str, end: str, tz_offset_min: int) -> bool:
    """¿La hora LOCAL (now_epoch UTC + offset) cae dentro de [start, end)?

    Soporta ventanas que cruzan medianoche (p.ej. 22:00–06:00). Si 'enabled' es False,
    o la ventana es degenerada (start==end), se considera siempre abierta (envío 24h).
    """
    if not enabled:
        return True
    local = (int(now_epoch) + int(tz_offset_min) * 60) % 86400
    minuto = local // 60
    ini = _hhmm_a_minutos(start, 0)
    fin = _hhmm_a_minutos(end, 24 * 60)
    if ini == fin:
        return True
    if ini < fin:
        return ini <= minuto < fin
    return minuto >= ini or minuto < fin  # cruza medianoche


def delay_aleatorio(minimo: float, maximo: float, rng=random.uniform) -> float:
    """Delay aleatorio en [min, max] (evita patrones predecibles). Tolera min>max y negativos."""
    lo, hi = float(minimo), float(maximo)
    if hi < lo:
        lo, hi = hi, lo
    if hi <= 0:
        return 0.0
    if lo < 0:
        lo = 0.0
    return rng(lo, hi)
