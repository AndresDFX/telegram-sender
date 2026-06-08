"""Tests del worker SQS: respuesta parcial de lotes (batchItemFailures)."""

import json
import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import worker  # noqa: E402

logging.disable(logging.CRITICAL)  # silencia los logs de rutas de error esperadas


def _record(message_id, body):
    return {"messageId": message_id, "body": body if isinstance(body, str) else json.dumps(body)}


def _stats(total, sent=0, blocked=0, failed=0):
    return {"total": total, "sent": sent, "blocked": blocked, "failed": failed, "failed_ids": []}


class WorkerTests(unittest.TestCase):
    def test_lote_exitoso_sin_fallos(self):
        event = {"Records": [_record("m1", {"text": "hola", "chat_ids": ["1", "2"], "batch_index": 0})]}
        with patch.object(worker, "procesar_lote", return_value=_stats(2, sent=2)):
            result = worker.lambda_handler(event, None)
        self.assertEqual(result["batchItemFailures"], [])

    def test_fallo_sistemico_reporta_el_mensaje(self):
        event = {"Records": [_record("m1", {"text": "hola", "chat_ids": ["1", "2"]})]}
        with patch.object(worker, "procesar_lote", return_value=_stats(2, failed=2)):
            result = worker.lambda_handler(event, None)
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "m1"}])

    def test_excepcion_en_procesamiento_reporta_el_mensaje(self):
        event = {"Records": [_record("m1", {"text": "hola", "chat_ids": ["1"]})]}
        with patch.object(worker, "procesar_lote", side_effect=RuntimeError("boom")):
            result = worker.lambda_handler(event, None)
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "m1"}])

    def test_body_malformado_reporta_el_mensaje(self):
        event = {"Records": [_record("m1", "no-es-json")]}
        result = worker.lambda_handler(event, None)
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "m1"}])

    def test_fallo_parcial_no_reencola(self):
        # Algunos usuarios fallan pero no todo el lote → no se reporta el mensaje.
        event = {"Records": [_record("m1", {"text": "hola", "chat_ids": ["1", "2", "3"]})]}
        with patch.object(worker, "procesar_lote", return_value=_stats(3, sent=2, failed=1)):
            result = worker.lambda_handler(event, None)
        self.assertEqual(result["batchItemFailures"], [])

    def test_mezcla_de_mensajes_solo_reporta_los_fallidos(self):
        event = {
            "Records": [
                _record("ok", {"text": "a", "chat_ids": ["1"]}),
                _record("bad", {"text": "b", "chat_ids": ["2"]}),
            ]
        }

        def fake(text, chat_ids):
            return _stats(1, sent=1) if text == "a" else _stats(1, failed=1)

        with patch.object(worker, "procesar_lote", side_effect=fake):
            result = worker.lambda_handler(event, None)
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "bad"}])


if __name__ == "__main__":
    unittest.main()
