"""Adapters: HttpWhatsAppForwarder (POST al servicio WhatsApp, mockeado)."""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.whatsapp import HttpWhatsAppForwarder, NullWhatsAppForwarder  # noqa: E402


class FakeResp:
    def __init__(self, body):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class WhatsAppForwarderTests(unittest.TestCase):
    def test_forward_postea_y_parsea(self):
        fwd = HttpWhatsAppForwarder("https://wa.example.com/", "tok")
        with patch("urllib.request.urlopen", return_value=FakeResp('{"accepted": true, "targets": 5}')) as op:
            res = fwd.forward("hola", "http://img", ["1", "2"])
        self.assertEqual(res, {"accepted": True, "targets": 5})
        req = op.call_args.args[0]
        self.assertEqual(req.full_url, "https://wa.example.com/send")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(
            json.loads(req.data.decode()),
            {
                "text": "hola",
                "image_url": "http://img",
                "exclude": ["1", "2"],
                "exclude_patterns": [],
                "pattern_exceptions": [],
                "mode": "all",
                "list_ids": [],
                "broadcast_id": None,
                "broadcasts_table": None,
            },
        )

    def test_forward_pasa_mode_y_list_ids(self):
        fwd = HttpWhatsAppForwarder("https://wa.example.com", "tok")
        with patch("urllib.request.urlopen", return_value=FakeResp("{}")) as op:
            fwd.forward("hola", None, [], mode="only", list_ids=["57300@s.whatsapp.net"])
        payload = json.loads(op.call_args.args[0].data.decode())
        self.assertEqual(payload["mode"], "only")
        self.assertEqual(payload["list_ids"], ["57300@s.whatsapp.net"])

    def test_contar_usa_endpoint_count_dedicado(self):
        # M16: contar() pega a /count (solo cuenta, nunca envía), no a /send con un flag.
        fwd = HttpWhatsAppForwarder("https://wa.example.com", "tok")
        with patch("urllib.request.urlopen", return_value=FakeResp('{"count": 7, "mode": "all"}')) as op:
            n = fwd.contar(mode="all")
        self.assertEqual(n, 7)
        self.assertEqual(op.call_args.args[0].full_url, "https://wa.example.com/count")
        self.assertNotIn("count_only", json.loads(op.call_args.args[0].data.decode()))

    def test_contar_cae_a_send_count_only_si_no_hay_count(self):
        # Servicio viejo sin /count (404): cae al flag count_only de /send (compatibilidad).
        import urllib.error
        fwd = HttpWhatsAppForwarder("https://wa.example.com", "tok")
        err = urllib.error.HTTPError("https://wa.example.com/count", 404, "Not Found", {}, None)
        with patch("urllib.request.urlopen", side_effect=[err, FakeResp('{"count": 3}')]) as op:
            n = fwd.contar(mode="all")
        self.assertEqual(n, 3)
        self.assertEqual(op.call_args_list[-1].args[0].full_url, "https://wa.example.com/send")
        self.assertTrue(json.loads(op.call_args_list[-1].args[0].data.decode())["count_only"])

    def test_sin_config_no_postea(self):
        with patch("urllib.request.urlopen") as op:
            res = HttpWhatsAppForwarder("", "").forward("x", None, [])
        op.assert_not_called()
        self.assertIn("skipped", res)

    def test_null_forwarder(self):
        self.assertIn("skipped", NullWhatsAppForwarder().forward("x", None, []))


if __name__ == "__main__":
    unittest.main()
