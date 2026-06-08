"""Tests del cliente Telegram: 403 (bloqueado), 429 (retry_after) y 5xx (backoff)."""

import logging
import os
import sys
import unittest
from unittest.mock import MagicMock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from telegram_client import TelegramClient  # noqa: E402

logging.disable(logging.CRITICAL)  # silencia los logs de rutas de error esperadas


class FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = b"{}"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def make_client(responses, sleep=None, max_retries=3):
    client = TelegramClient(bot_token="t", max_retries=max_retries, sleep=sleep or (lambda _s: None))
    client.session = MagicMock()
    client.session.post.side_effect = responses
    return client


class TelegramClientTests(unittest.TestCase):
    def test_envio_ok(self):
        client = make_client([FakeResponse(200, {"ok": True, "result": {"message_id": 1}})])
        result = client.send_message("123", "hola")
        self.assertTrue(result["ok"])
        self.assertEqual(client.session.post.call_count, 1)

    def test_403_marca_bloqueado_sin_lanzar(self):
        client = make_client([FakeResponse(403, {"ok": False, "error_code": 403})])
        result = client.send_message("123", "hola")
        self.assertTrue(result["blocked"])
        self.assertFalse(result["ok"])

    def test_429_respeta_retry_after_y_reintenta(self):
        sleep = MagicMock()
        client = make_client(
            [
                FakeResponse(429, {"ok": False, "parameters": {"retry_after": 2}}),
                FakeResponse(200, {"ok": True, "result": {}}),
            ],
            sleep=sleep,
        )
        result = client.send_message("123", "hola")
        self.assertTrue(result["ok"])
        self.assertEqual(client.session.post.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_429_agota_reintentos_y_lanza(self):
        client = make_client(
            [
                FakeResponse(429, {"ok": False, "parameters": {"retry_after": 1}}),
                FakeResponse(429, {"ok": False, "parameters": {"retry_after": 1}}),
            ],
            max_retries=1,
        )
        with self.assertRaises(RuntimeError):
            client.send_message("123", "hola")

    def test_5xx_reintenta_con_backoff(self):
        sleep = MagicMock()
        client = make_client(
            [FakeResponse(500), FakeResponse(200, {"ok": True, "result": {}})],
            sleep=sleep,
        )
        result = client.send_message("123", "hola")
        self.assertTrue(result["ok"])
        self.assertEqual(client.session.post.call_count, 2)
        sleep.assert_called_once()

    def test_respuesta_no_ok_lanza(self):
        client = make_client([FakeResponse(200, {"ok": False, "description": "Bad Request"})])
        with self.assertRaises(RuntimeError):
            client.send_message("123", "hola")


if __name__ == "__main__":
    unittest.main()
