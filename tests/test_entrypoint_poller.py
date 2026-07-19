"""Entrypoint poller: cacheo del estado de sesión de Telegram (refactor sesión-concurrente)."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from entrypoints import poller  # noqa: E402

logging.disable(logging.CRITICAL)


class FakeConfigStore:
    def __init__(self):
        self.tg_status = None

    def set_tg_status(self, connected, me=None):
        self.tg_status = {"connected": connected, "me": me}


class FakeAccount:
    def __init__(self, est=None, boom=False):
        self._est = est or {"authorized": True, "me": {"phone": "57300"}}
        self._boom = boom
        self.desconectado = False

    def estado(self):
        if self._boom:
            raise RuntimeError("telethon caído")
        return self._est

    def desconectar(self):
        self.desconectado = True


class RefreshTgStatusTests(unittest.TestCase):
    def setUp(self):
        self._orig = poller.wiring.build_telethon_account
        poller.config_store = FakeConfigStore()

    def tearDown(self):
        poller.wiring.build_telethon_account = self._orig
        poller.config_store = None

    def test_cachea_estado_en_userbot(self):
        poller.wiring.build_telethon_account = lambda: FakeAccount()
        poller._refresh_tg_status()
        self.assertEqual(poller.config_store.tg_status, {"connected": True, "me": {"phone": "57300"}})

    def test_modo_bot_no_hace_nada(self):
        poller.wiring.build_telethon_account = lambda: None  # None en modo bot
        poller._refresh_tg_status()
        self.assertIsNone(poller.config_store.tg_status)

    def test_error_no_rompe_y_desconecta(self):
        acc = FakeAccount(boom=True)
        poller.wiring.build_telethon_account = lambda: acc
        poller._refresh_tg_status()  # no debe lanzar
        self.assertIsNone(poller.config_store.tg_status)  # no se cacheó nada
        self.assertTrue(acc.desconectado)                 # se cerró la conexión ante el fallo


if __name__ == "__main__":
    unittest.main()
