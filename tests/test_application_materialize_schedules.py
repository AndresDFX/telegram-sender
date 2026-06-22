"""Application: MaterializeSchedules (dispara los horarios vencidos y avanza next_run)."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.materialize_schedules import MaterializeSchedules  # noqa: E402

logging.disable(logging.CRITICAL)

NOW = 1704103200  # 2024-01-01 10:00:00 UTC (lunes)
H = 3600
D = 86400


def _sched(**over):
    s = {
        "sid": "s1", "type": "daily", "at": "09:00", "days": [], "text": "hola",
        "image_url": "", "telegram": True, "telegram_list": "", "whatsapp": False,
        "whatsapp_list": "", "next_run": NOW - 60, "enabled": True, "runs": 0, "last_run": 0,
    }
    s.update(over)
    return s


class FakeSchedules:
    def __init__(self, vencidos):
        self._venc = vencidos
        self.updates = []

    def vencidos(self, now):
        return list(self._venc)

    def actualizar(self, sid, **campos):
        self.updates.append((sid, campos))


class FakeBroadcasting:
    def __init__(self, boom=False):
        self.calls = []
        self._boom = boom

    def enviar_manual(self, text, **kw):
        self.calls.append((text, kw))
        if self._boom:
            raise ValueError("pausado")
        return {"ok": True}


class FakeConfig:
    def __init__(self, enabled=True, tz=0):
        self._c = {"sending_enabled": enabled, "window_tz": tz}

    def get(self):
        return dict(self._c)


def _run(schedules, broadcasting, config):
    return MaterializeSchedules(schedules, broadcasting, config, now=lambda: NOW)()


class MaterializeTests(unittest.TestCase):
    def test_sin_vencidos_no_hace_nada(self):
        b = FakeBroadcasting()
        res = _run(FakeSchedules([]), b, FakeConfig())
        self.assertEqual(res["vencidos"], 0)
        self.assertEqual(b.calls, [])

    def test_daily_dispara_y_avanza_next_run(self):
        sch = FakeSchedules([_sched()])
        b = FakeBroadcasting()
        res = _run(sch, b, FakeConfig(tz=0))
        self.assertEqual(res["disparados"], 1)
        self.assertEqual(b.calls[0][0], "hola")
        self.assertTrue(b.calls[0][1]["telegram"])
        sid, campos = sch.updates[0]
        self.assertEqual(campos["next_run"], NOW - H + D)  # mañana 09:00 UTC (ya pasaron las 10:00 de hoy)
        self.assertEqual(campos["runs"], 1)
        self.assertEqual(campos["last_run"], NOW)

    def test_once_se_desactiva_tras_disparar(self):
        sch = FakeSchedules([_sched(type="once", sid="o1")])
        b = FakeBroadcasting()
        _run(sch, b, FakeConfig())
        self.assertEqual(len(b.calls), 1)
        sid, campos = sch.updates[0]
        self.assertFalse(campos["enabled"])
        self.assertEqual(campos["runs"], 1)

    def test_pausado_no_dispara_pero_avanza_recurrente(self):
        sch = FakeSchedules([_sched()])
        b = FakeBroadcasting()
        res = _run(sch, b, FakeConfig(enabled=False, tz=0))
        self.assertEqual(b.calls, [])  # nada se envía estando pausado
        self.assertEqual(res["omitidos_pausa"], 1)
        sid, campos = sch.updates[0]
        self.assertEqual(campos["next_run"], NOW - H + D)  # igual avanza (evita backlog)
        self.assertNotIn("runs", campos)  # no cuenta como ejecución
        self.assertNotIn("last_run", campos)

    def test_pausado_once_queda_pendiente(self):
        sch = FakeSchedules([_sched(type="once", sid="o1")])
        b = FakeBroadcasting()
        _run(sch, b, FakeConfig(enabled=False))
        self.assertEqual(b.calls, [])
        self.assertEqual(sch.updates, [])  # el 'once' NO se toca: dispara al reactivar

    def test_error_al_enviar_igual_avanza(self):
        # A2: ahora se AVANZA el horario antes de enviar. Si el envío falla, el horario YA avanzó,
        # así NO se re-dispara cada minuto (a lo sumo se pierde esta ocurrencia, registrada como error).
        sch = FakeSchedules([_sched()])
        b = FakeBroadcasting(boom=True)
        res = _run(sch, b, FakeConfig())
        self.assertEqual(res["errores"], 1)
        self.assertEqual(len(sch.updates), 1)            # avanzó (no re-dispara en bucle)
        self.assertIn("next_run", sch.updates[0][1])


if __name__ == "__main__":
    unittest.main()
