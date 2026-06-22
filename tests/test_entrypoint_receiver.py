"""Entrypoint receiver: autenticación, parseo, dedup y ruteo (con casos de uso falsos)."""

import json
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from entrypoints import receiver  # noqa: E402
from application.ports import PartialEnqueueError  # noqa: E402

logging.disable(logging.CRITICAL)

_ENV_VARS = ("WEBHOOK_SECRET_TOKEN", "ALLOW_INSECURE_WEBHOOK", "SOURCE_CHANNEL_ID", "AWS_LAMBDA_FUNCTION_NAME")


class FakeDedup:
    def __init__(self, marca=True, infra_error=False):
        self._marca = marca
        self._infra_error = infra_error  # A8: simula fallo de infra (throttle/permiso)
        self.borrados = []

    def marcar(self, key):
        return self._marca

    def marcar_estricto(self, key):
        if self._infra_error:
            raise RuntimeError("ProvisionedThroughputExceeded")
        return self._marca

    def borrar(self, key):
        self.borrados.append(key)


def _event(update, headers=None):
    return {"body": json.dumps(update), "headers": headers or {}}


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        receiver.dedup = FakeDedup(marca=True)
        receiver.handle_command = MagicMock()
        receiver.broadcast = MagicMock(return_value={"batches": 1, "subscribers": 2})
        for v in _ENV_VARS:
            os.environ.pop(v, None)
        os.environ["ALLOW_INSECURE_WEBHOOK"] = "1"  # dev: sin secreto se acepta

    def tearDown(self):
        receiver.dedup = receiver.handle_command = receiver.broadcast = None
        for v in _ENV_VARS:
            os.environ.pop(v, None)

    # --- secret_token ---
    def test_fail_closed_sin_secreto_ni_flag(self):
        os.environ.pop("ALLOW_INSECURE_WEBHOOK", None)
        resp = receiver.lambda_handler(_event({"channel_post": {"chat": {"id": -1}, "text": "x"}}), None)
        self.assertEqual(resp["statusCode"], 403)
        receiver.broadcast.assert_not_called()

    def test_fail_closed_en_lambda_ignora_flag(self):
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "telegram-sync-dev-receiver"  # dentro de Lambda
        resp = receiver.lambda_handler(_event({"message": {"chat": {"id": 1, "type": "private"}, "text": "/start"}}), None)
        self.assertEqual(resp["statusCode"], 403)

    def test_secreto_correcto(self):
        os.environ.pop("ALLOW_INSECURE_WEBHOOK", None)
        os.environ["WEBHOOK_SECRET_TOKEN"] = "s3cr3t"
        resp = receiver.lambda_handler(
            _event({"message": {"chat": {"id": 9, "type": "private"}, "text": "/start"}},
                   headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"}),
            None,
        )
        self.assertEqual(resp["statusCode"], 200)
        receiver.handle_command.assert_called_once_with("9", "/start")

    # --- parseo ---
    def test_json_malformado_400_sin_dedup(self):
        receiver.dedup = MagicMock()
        resp = receiver.lambda_handler({"body": "no-json", "headers": {}}, None)
        self.assertEqual(resp["statusCode"], 400)
        receiver.dedup.marcar.assert_not_called()

    def test_body_none_200(self):
        self.assertEqual(receiver.lambda_handler({"body": None, "headers": {}}, None)["statusCode"], 200)

    # --- dedup ---
    def test_duplicado(self):
        receiver.dedup = FakeDedup(marca=False)
        resp = receiver.lambda_handler(_event({"update_id": 7, "channel_post": {"chat": {"id": -1}, "text": "x"}}), None)
        self.assertEqual(json.loads(resp["body"])["status"], "duplicate")
        receiver.broadcast.assert_not_called()

    def test_a8_fallo_de_infra_del_dedup_no_descarta_como_duplicado(self):
        # A8: si el dedup no puede confirmar (throttle/permiso), el receiver NO debe responder
        # 'duplicate' y descartar el post; debe procesarlo igual (encolar el broadcast).
        receiver.dedup = FakeDedup(infra_error=True)
        resp = receiver.lambda_handler(_event({"update_id": 42, "channel_post": {"chat": {"id": -1}, "text": "x"}}), None)
        self.assertEqual(json.loads(resp["body"])["status"], "queued")  # NO 'duplicate'
        receiver.broadcast.assert_called_once_with("x")

    # --- ruteo ---
    def test_start_llama_handle_command(self):
        resp = receiver.lambda_handler(_event({"message": {"chat": {"id": 555, "type": "private"}, "text": "/start@bot"}}), None)
        self.assertEqual(json.loads(resp["body"])["command"], "/start")
        receiver.handle_command.assert_called_once_with("555", "/start")

    def test_mensaje_privado_sin_comando_ignora(self):
        resp = receiver.lambda_handler(_event({"message": {"chat": {"id": 1, "type": "private"}, "text": "hola"}}), None)
        self.assertEqual(resp["body"], "OK")
        receiver.handle_command.assert_not_called()

    def test_channel_post_llama_broadcast_con_texto_crudo(self):
        resp = receiver.lambda_handler(_event({"channel_post": {"chat": {"id": -1}, "text": "Lista $100.000"}}), None)
        self.assertEqual(json.loads(resp["body"])["status"], "queued")
        receiver.broadcast.assert_called_once_with("Lista $100.000")  # el markup vive en el caso de uso

    def test_caption_llama_broadcast(self):
        receiver.lambda_handler(_event({"channel_post": {"chat": {"id": -1}, "caption": "Foto $50.000"}}), None)
        receiver.broadcast.assert_called_once_with("Foto $50.000")

    # --- compensación del dedup ---
    def test_encolado_parcial_no_revierte(self):
        receiver.dedup = FakeDedup(marca=True)
        receiver.broadcast = MagicMock(side_effect=PartialEnqueueError(1, 3))
        with self.assertRaises(PartialEnqueueError):
            receiver.lambda_handler(_event({"update_id": 8, "channel_post": {"chat": {"id": -1}, "text": "x"}}), None)
        self.assertEqual(receiver.dedup.borrados, [])

    def test_encolado_cero_revierte(self):
        receiver.dedup = FakeDedup(marca=True)
        receiver.broadcast = MagicMock(side_effect=PartialEnqueueError(0, 3))
        with self.assertRaises(PartialEnqueueError):
            receiver.lambda_handler(_event({"update_id": 8, "channel_post": {"chat": {"id": -1}, "text": "x"}}), None)
        self.assertEqual(receiver.dedup.borrados, ["8"])

    def test_fallo_generico_revierte(self):
        receiver.dedup = FakeDedup(marca=True)
        receiver.broadcast = MagicMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            receiver.lambda_handler(_event({"update_id": 8, "channel_post": {"chat": {"id": -1}, "text": "x"}}), None)
        self.assertEqual(receiver.dedup.borrados, ["8"])


if __name__ == "__main__":
    unittest.main()
