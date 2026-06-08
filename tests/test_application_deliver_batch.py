"""Application: DeliverBatch (envío por lote con fakes de los puertos)."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.deliver_batch import DeliverBatch  # noqa: E402
from domain.models import SendResult  # noqa: E402

logging.disable(logging.CRITICAL)


class FakeSender:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def enviar(self, chat_id, text):
        self.calls.append((chat_id, text))
        outcome = self.outcomes.get(chat_id, SendResult(ok=True))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSubscribers:
    def __init__(self):
        self.inactivos = []

    def listar_activos(self):
        return []

    def registrar(self, chat_id, status):
        pass

    def marcar_inactivo(self, chat_id):
        self.inactivos.append(chat_id)


class DeliverBatchTests(unittest.TestCase):
    def test_clasifica_enviados_bloqueados_y_fallidos(self):
        sender = FakeSender({
            "1": SendResult(ok=True),
            "2": SendResult(ok=False, blocked=True),
            "3": RuntimeError("boom"),
        })
        subs = FakeSubscribers()
        deliver = DeliverBatch(sender, subs, delay=0, sleep=lambda _s: None)

        stats = deliver("lista", ["1", "2", "3"])

        self.assertEqual(stats.total, 3)
        self.assertEqual(stats.sent, 1)
        self.assertEqual(stats.blocked, 1)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.failed_ids, ["3"])
        self.assertEqual(subs.inactivos, ["2"])

    def test_respeta_el_delay(self):
        sleeps = []
        deliver = DeliverBatch(FakeSender(), FakeSubscribers(), delay=0.05, sleep=sleeps.append)
        deliver("x", ["1", "2"])
        self.assertEqual(sleeps, [0.05, 0.05])

    def test_fallo_al_inactivar_no_rompe(self):
        class BadSubs(FakeSubscribers):
            def marcar_inactivo(self, chat_id):
                raise RuntimeError("ddb")

        deliver = DeliverBatch(FakeSender({"1": SendResult(ok=False, blocked=True)}), BadSubs(), delay=0)
        stats = deliver("x", ["1"])
        self.assertEqual(stats.blocked, 1)
        self.assertEqual(stats.failed, 0)


if __name__ == "__main__":
    unittest.main()
