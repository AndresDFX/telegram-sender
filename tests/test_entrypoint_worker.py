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
        # Inyectamos config_store/plans/dedup para ejercitar el gate real (no la rama fail-open
        # por falta de AWS). Por defecto: envíos ACTIVOS, planes no descartables, nada deduplicado.
        worker.config_store = MagicMock()
        worker.config_store.get.return_value = {"sending_enabled": True}
        worker.config_store.incr_ban_strikes.return_value = 1
        worker.plans = MagicMock()
        worker.plans.descartar.return_value = False
        worker.dedup = MagicMock()
        worker.dedup.procesado.return_value = False

    def tearDown(self):
        worker.deliver = None
        worker.image_store = None
        worker.broadcasts = None
        worker.config_store = None
        worker.plans = None
        worker.dedup = None

    def test_lote_exitoso(self):
        worker.deliver = MagicMock(return_value=_stats(2, sent=2))
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"]})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])
        worker.deliver.assert_called_once_with("x", ["1", "2"], None)

    def test_pausado_descarta_lote_automatico(self):
        # En pausa, un lote AUTOMÁTICO (sin flag manual) se descarta sin enviar.
        worker.config_store.get.return_value = {"sending_enabled": False}
        worker.deliver = MagicMock()
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"]})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])  # ack sin reencolar
        worker.deliver.assert_not_called()  # no se intenta enviar

    def test_pausado_entrega_lote_manual(self):
        # En pausa, un lote MANUAL (body["manual"]=True) SÍ se entrega: la pausa es solo automática.
        worker.config_store.get.return_value = {"sending_enabled": False}
        worker.deliver = MagicMock(return_value=_stats(2, sent=2))
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"], "manual": True})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])
        worker.deliver.assert_called_once_with("x", ["1", "2"], None)

    def test_plan_cancelado_descarta_lote(self):
        worker.plans.descartar.return_value = True
        worker.deliver = MagicMock()
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1"], "pid": "b-xyz"})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])  # ack, no envía
        worker.deliver.assert_not_called()
        worker.plans.descartar.assert_called_once_with("b-xyz")

    def test_dedup_omite_lote_ya_entregado(self):
        worker.dedup.procesado.return_value = True  # ya entregado antes
        worker.deliver = MagicMock()
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"], "batch_id": "abc"})]}
        self.assertEqual(worker.lambda_handler(event, None)["batchItemFailures"], [])
        worker.deliver.assert_not_called()  # no reenvía

    def test_marca_dedup_tras_exito(self):
        worker.deliver = MagicMock(return_value=_stats(2, sent=2))
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"], "batch_id": "abc"})]}
        worker.lambda_handler(event, None)
        worker.dedup.marcar.assert_called_once_with("abc")          # marca entregado
        worker.config_store.reset_ban_strikes.assert_called_once()  # hubo envíos -> reinicia strikes

    def test_auto_pausa_tras_fallos_totales(self):
        worker.config_store.incr_ban_strikes.return_value = 2  # alcanza el umbral
        worker.deliver = MagicMock(return_value=_stats(2, failed=2))  # fallo total
        event = {"Records": [_record("m1", {"text": "x", "chat_ids": ["1", "2"]})]}
        res = worker.lambda_handler(event, None)
        self.assertEqual(res["batchItemFailures"], [{"itemIdentifier": "m1"}])  # reencola
        worker.config_store.set.assert_called_once_with({"sending_enabled": False})  # AUTO-PAUSA

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
