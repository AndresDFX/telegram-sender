"""Adapter: derivación de estado del BroadcastStore (lógica pura, sin DynamoDB)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.dynamodb import DynamoDbBroadcastStore  # noqa: E402

E = DynamoDbBroadcastStore._estado


def job(**kw):
    base = {
        "channels": ["telegram"],
        "tg_total": 0,
        "tg_sent": 0,
        "tg_failed": 0,
        "wa_total": 0,
        "wa_sent": 0,
        "wa_failed": 0,
        "wa_started": False,
    }
    base.update(kw)
    return base


class EstadoTests(unittest.TestCase):
    def test_queued_sin_progreso(self):
        self.assertEqual(E(job(tg_total=10)), "queued")

    def test_sending_parcial(self):
        self.assertEqual(E(job(tg_total=10, tg_sent=4)), "sending")

    def test_done_completo_sin_fallos(self):
        self.assertEqual(E(job(tg_total=10, tg_sent=10)), "done")

    def test_partial_completo_con_fallos(self):
        self.assertEqual(E(job(tg_total=10, tg_sent=7, tg_failed=3)), "partial")

    def test_whatsapp_pendiente_es_sending(self):
        # canal whatsapp pero el servicio aún no reportó wa_total → en curso
        self.assertEqual(E(job(channels=["whatsapp"], wa_started=False)), "sending")

    def test_ambos_canales_done(self):
        j = job(channels=["telegram", "whatsapp"], tg_total=5, tg_sent=5, wa_total=3, wa_sent=3, wa_started=True)
        self.assertEqual(E(j), "done")

    def test_ambos_canales_uno_en_curso(self):
        j = job(channels=["telegram", "whatsapp"], tg_total=5, tg_sent=5, wa_total=3, wa_sent=1, wa_started=True)
        self.assertEqual(E(j), "sending")


if __name__ == "__main__":
    unittest.main()
