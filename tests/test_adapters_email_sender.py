"""Adapters: enviar_resend (POST a la API de Resend, mockeado)."""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.email_sender import enviar_resend  # noqa: E402


class FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ResendSenderTests(unittest.TestCase):
    def test_envia_y_construye_payload(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode())
            captured["method"] = req.get_method()
            return FakeResp(200)

        with patch("adapters.email_sender.urllib.request.urlopen", side_effect=fake_urlopen):
            ok = enviar_resend("key_123", "Replica <a@b.com>", "dest@x.com", "Asunto", "texto", "<b>html</b>")

        self.assertTrue(ok)
        self.assertEqual(captured["method"], "POST")
        self.assertIn("api.resend.com", captured["url"])
        # Authorization Bearer presente (las claves de header se normalizan a Title-Case).
        auth = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(auth.get("authorization"), "Bearer key_123")
        self.assertEqual(captured["body"]["from"], "Replica <a@b.com>")
        self.assertEqual(captured["body"]["to"], ["dest@x.com"])
        self.assertEqual(captured["body"]["subject"], "Asunto")
        self.assertEqual(captured["body"]["html"], "<b>html</b>")

    def test_sin_html_no_incluye_campo(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp(200)

        with patch("adapters.email_sender.urllib.request.urlopen", side_effect=fake_urlopen):
            enviar_resend("k", "a@b.com", "dest@x.com", "S", "t")

        self.assertNotIn("html", captured["body"])

    def test_status_no_2xx_devuelve_false(self):
        with patch("adapters.email_sender.urllib.request.urlopen", return_value=FakeResp(403)):
            self.assertFalse(enviar_resend("k", "a@b.com", "dest@x.com", "S", "t"))

    def test_falta_api_key_o_destino_lanza(self):
        with self.assertRaises(ValueError):
            enviar_resend("", "a@b.com", "dest@x.com", "S", "t")
        with self.assertRaises(ValueError):
            enviar_resend("k", "a@b.com", "", "S", "t")


if __name__ == "__main__":
    unittest.main()
