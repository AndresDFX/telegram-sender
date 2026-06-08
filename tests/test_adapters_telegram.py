"""Adapters: TelegramSender (403/429/5xx, devuelve SendResult)."""

import logging
import os
import sys
import unittest
from unittest.mock import MagicMock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.telegram import TelegramSender  # noqa: E402

logging.disable(logging.CRITICAL)


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


def make(responses, sleep=None, max_retries=3):
    s = TelegramSender(bot_token="t", max_retries=max_retries, sleep=sleep or (lambda _s: None))
    s._session = MagicMock()
    s._session.post.side_effect = responses
    return s


class TelegramSenderTests(unittest.TestCase):
    def test_ok(self):
        r = make([FakeResponse(200, {"ok": True, "result": {}})]).enviar("1", "hi")
        self.assertTrue(r.ok)
        self.assertFalse(r.blocked)

    def test_403_blocked(self):
        r = make([FakeResponse(403, {"ok": False})]).enviar("1", "hi")
        self.assertTrue(r.blocked)
        self.assertFalse(r.ok)

    def test_429_retry_after_then_ok(self):
        sleep = MagicMock()
        s = make([
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 2}}),
            FakeResponse(200, {"ok": True, "result": {}}),
        ], sleep=sleep)
        self.assertTrue(s.enviar("1", "hi").ok)
        sleep.assert_called_once_with(2.0)

    def test_429_exhausts(self):
        s = make([FakeResponse(429, {"parameters": {"retry_after": 1}})] * 2, max_retries=1)
        with self.assertRaises(RuntimeError):
            s.enviar("1", "hi")

    def test_5xx_backoff_then_ok(self):
        sleep = MagicMock()
        s = make([FakeResponse(500), FakeResponse(200, {"ok": True, "result": {}})], sleep=sleep)
        self.assertTrue(s.enviar("1", "hi").ok)
        sleep.assert_called_once()

    def test_not_ok_raises(self):
        with self.assertRaises(RuntimeError):
            make([FakeResponse(200, {"ok": False, "description": "bad"})]).enviar("1", "hi")


if __name__ == "__main__":
    unittest.main()
