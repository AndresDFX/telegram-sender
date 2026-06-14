"""Programación de mensajes recurrentes (lógica pura, sin I/O).

Calcula el próximo instante (epoch UTC) en que un horario debe dispararse, dado el tipo
(``once``/``daily``/``weekly``), la hora local ``"HH:MM"``, los días de la semana (weekly,
0=lunes … 6=domingo, convenio de ``datetime.weekday()``) y el offset de zona horaria en
minutos (mismo convenio que ``window_tz``: ``-300`` = UTC-5, Colombia).

``once`` no se recalcula: su ``next_run`` se fija al crearlo (la fecha/hora elegida).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ONCE, DAILY, WEEKLY = "once", "daily", "weekly"


def hhmm(at: str) -> tuple[int, int] | None:
    """Parsea ``"HH:MM"`` -> (hora, minuto) válidos, o ``None``."""
    try:
        hh_s, mm_s = str(at).split(":")
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError):
        return None
    return (hh, mm) if 0 <= hh < 24 and 0 <= mm < 60 else None


def _local(epoch: int, tz_off_min: int) -> datetime:
    """Reloj de pared local (naive) para un epoch UTC dado y un offset en minutos."""
    return datetime.fromtimestamp(int(epoch) + int(tz_off_min) * 60, tz=timezone.utc).replace(tzinfo=None)


def _epoch(local_dt: datetime, tz_off_min: int) -> int:
    """Inverso de :func:`_local`: de un reloj de pared local (naive) a epoch UTC."""
    return int(local_dt.replace(tzinfo=timezone.utc).timestamp()) - int(tz_off_min) * 60


def proximo_run(tipo: str, at: str, dias, tz_off_min: int, desde_epoch: int) -> int | None:
    """Próximo disparo (epoch UTC) ESTRICTAMENTE posterior a ``desde_epoch``.

    Devuelve ``None`` para ``once`` (no se recalcula) o si los parámetros son inválidos.
    """
    tipo = str(tipo or "").lower()
    if tipo == ONCE:
        return None
    hm = hhmm(at)
    if not hm:
        return None
    hh, mm = hm
    base = _local(desde_epoch, tz_off_min)
    cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tipo == DAILY:
        if cand <= base:
            cand += timedelta(days=1)
        return _epoch(cand, tz_off_min)
    if tipo == WEEKLY:
        dset = sorted({int(d) for d in (dias or []) if 0 <= int(d) <= 6})
        if not dset:
            return None
        for add in range(0, 8):  # hoy .. +7 días: garantiza encontrar el próximo día válido
            c = cand + timedelta(days=add)
            if c.weekday() in dset and c > base:
                return _epoch(c, tz_off_min)
        return None
    return None
