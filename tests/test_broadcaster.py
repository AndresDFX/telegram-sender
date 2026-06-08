"""Tests del envío por lote: éxito, bloqueo (403 → inactivo) y fallo."""

import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import broadcaster  # noqa: E402

logging.disable(logging.CRITICAL)  # silencia los logs de rutas de error esperadas


class FakeTelegram:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def send_message(self, chat_id, text):
        self.calls.append(chat_id)
        outcome = self.outcomes.get(chat_id, {"ok": True})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BroadcasterTests(unittest.TestCase):
    def test_clasifica_enviados_bloqueados_y_fallidos(self):
        telegram = FakeTelegram(
            {
                "1": {"ok": True},
                "2": {"ok": False, "blocked": True},
                "3": RuntimeError("boom"),
            }
        )
        with patch.object(broadcaster, "marcar_inactivo") as marcar:
            stats = broadcaster.procesar_lote(
                "lista", ["1", "2", "3"], telegram=telegram, sleep=lambda _s: None
            )

        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["failed_ids"], ["3"])
        marcar.assert_called_once_with("2")
        self.assertEqual(telegram.calls, ["1", "2", "3"])

    def test_respeta_el_delay_entre_envios(self):
        telegram = FakeTelegram({})
        sleeps = []
        with patch.object(broadcaster, "marcar_inactivo"):
            broadcaster.procesar_lote(
                "lista", ["1", "2"], telegram=telegram, delay=0.05, sleep=sleeps.append
            )
        self.assertEqual(sleeps, [0.05, 0.05])

    def test_fallo_al_marcar_inactivo_no_rompe_el_lote(self):
        telegram = FakeTelegram({"1": {"ok": False, "blocked": True}})
        with patch.object(broadcaster, "marcar_inactivo", side_effect=RuntimeError("ddb")):
            stats = broadcaster.procesar_lote(
                "lista", ["1"], telegram=telegram, sleep=lambda _s: None
            )
        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(stats["failed"], 0)


if __name__ == "__main__":
    unittest.main()
