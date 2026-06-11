"""Entrypoint worker: respuesta parcial de lotes (batchItemFailures) con DeliverBatch falso."""

import json
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from entrypoints import worker  # noqa: E402
from domain.models import BroadcastStats  # noqa: E402

logging.disable(logging.CRITICAL)


def _record(mid, body):
    return {"messageId": mid, "body": body if isinstance(body, str) else json.dumps(body)}


def _stats(total, sent=0, failed=0):
    return BroadcastStats(total=total, sent=sent, failed=failed)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        # Inyectamos config_store y plans para ejercitar el gate real (no la rama fail-open
        # por falta de AWS). Por defecto: envíos ACTIVOS y planes no descartables.
        worker.config_store = MagicMock()
        worker.config_store.get.return_value = {"sending_enabled": True}
        worker.plans = MagicMock()
        worker.plans.descartar.return_value = False

    def tearDown(self):
        worker.deliver = None
        worker.image_store = None
        worker.broadcasts = None
        worker.config_store = None
        worker.plans = None

    def test_lote_exitoso(self):
        worker.deliver = MagicMock(return_value=_stats(2, sent=2))
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"]})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])
        worker.deliver.assert_called_once_with("x", ["1", "2"], None)

    def test_pausado_descarta_sin_enviar(self):
        worker.config_store.get.return_value = {"sending_enabled": False}
        worker.deliver = MagicMock()
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"]})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])  # ack sin reencolar
        worker.deliver.assert_not_called()  # no se intenta enviar

    def test_plan_cancelado_descarta_lote(self):
        worker.plans.descartar.return_value = True
        worker.deliver = MagicMock()
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1"], "pid": "b-xyz"})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])  # ack, no envía
        worker.deliver.assert_not_called()
        worker.plans.descartar.assert_called_once_with("b-xyz")

    def test_fallo_sistemico_reporta(self):
        worker.deliver = MagicMock(return_value=_stats(2, failed=2))
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"]})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [{"itemIdentifier": "m1"}])

    def test_fallo_parcial_no_reencola(self):
        worker.deliver = MagicMock(return_value=_stats(3, sent=2, failed=1))
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2", "3"]})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])

    def test_excepcion_reporta(self):
        worker.deliver = MagicMock(side_effect=RuntimeError("boom"))
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1"]})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [{"itemIdentifier": "m1"}])

    def test_body_malformado_reporta(self):
        worker.deliver = MagicMock()
        event = {"Records": [_record("m1", "no-json")]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [{"itemIdentifier": "m1"}])


if __name__ == "__main__":
    unittest.main()
