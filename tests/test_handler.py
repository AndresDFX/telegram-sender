"""Tests del receptor: enrutado de updates, markup, encolado y modo inline."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import handler  # noqa: E402


def _event(update):
    return {"body": json.dumps(update)}


class HandlerTests(unittest.TestCase):
    def setUp(self):
        # Entorno base: cola configurada, sin filtro de canal, auth de dev (sin secreto).
        self._env = patch.dict(
            os.environ,
            {
                "BROADCAST_QUEUE_URL": "https://q",
                "MARKUP_PERCENTAGE": "15",
                "ALLOW_INSECURE_WEBHOOK": "1",
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("SOURCE_CHANNEL_ID", None)
        os.environ.pop("WEBHOOK_SECRET_TOKEN", None)

    def tearDown(self):
        self._env.stop()

    def test_channel_post_texto_encola_con_markup(self):
        with patch.object(handler, "obtener_usuarios_activos", return_value=["1", "2"]), patch.object(
            handler, "encolar_lotes", return_value=1
        ) as encolar:
            resp = handler.lambda_handler(
                _event({"channel_post": {"chat": {"id": -100}, "text": "Zapatos $100.00"}}), None
            )

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["status"], "queued")
        encolar.assert_called_once()
        mensaje = encolar.call_args.args[0]
        self.assertIn("115.00", mensaje)  # markup del 15% aplicado
        self.assertEqual(encolar.call_args.args[1], ["1", "2"])

    def test_caption_es_procesado(self):
        with patch.object(handler, "obtener_usuarios_activos", return_value=["1"]), patch.object(
            handler, "encolar_lotes", return_value=1
        ) as encolar:
            resp = handler.lambda_handler(
                _event({"channel_post": {"chat": {"id": -100}, "caption": "Foto $50.00"}}), None
            )
        self.assertEqual(resp["statusCode"], 200)
        encolar.assert_called_once()
        self.assertIn("57.50", encolar.call_args.args[0])

    def test_edited_channel_post_es_procesado(self):
        with patch.object(handler, "obtener_usuarios_activos", return_value=["1"]), patch.object(
            handler, "encolar_lotes", return_value=1
        ) as encolar:
            resp = handler.lambda_handler(
                _event({"edited_channel_post": {"chat": {"id": -100}, "text": "Precio $10.00"}}),
                None,
            )
        self.assertEqual(resp["statusCode"], 200)
        encolar.assert_called_once()

    def test_filtra_canal_no_fuente(self):
        with patch.dict(os.environ, {"SOURCE_CHANNEL_ID": "-100999"}), patch.object(
            handler, "obtener_usuarios_activos"
        ) as usuarios, patch.object(handler, "encolar_lotes") as encolar:
            resp = handler.lambda_handler(
                _event({"channel_post": {"chat": {"id": -100123}, "text": "Precio $10.00"}}), None
            )
        self.assertEqual(resp["body"], "OK")
        usuarios.assert_not_called()
        encolar.assert_not_called()

    def test_update_sin_post_se_ignora(self):
        with patch.object(handler, "encolar_lotes") as encolar:
            resp = handler.lambda_handler(_event({"message": {"text": "/start"}}), None)
        self.assertEqual(resp["body"], "OK")
        encolar.assert_not_called()

    def test_modo_inline_sin_cola(self):
        os.environ.pop("BROADCAST_QUEUE_URL", None)
        stats = {"total": 1, "sent": 1, "blocked": 0, "failed": 0, "failed_ids": []}
        with patch.object(handler, "obtener_usuarios_activos", return_value=["1"]), patch.object(
            handler, "procesar_lote", return_value=stats
        ) as procesar:
            resp = handler.lambda_handler(
                _event({"channel_post": {"chat": {"id": -100}, "text": "Precio $10.00"}}), None
            )
        self.assertEqual(json.loads(resp["body"])["status"], "sent")
        procesar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
