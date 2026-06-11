"""Adapters: TelethonUserSender / TelethonContacts (cliente mockeado)."""

import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

try:
    import telethon  # noqa: F401

    HAS_TELETHON = True
except ImportError:  # pragma: no cover
    HAS_TELETHON = False

logging.disable(logging.CRITICAL)


@unittest.skipUnless(HAS_TELETHON, "requiere telethon")
class TelethonUserSenderTests(unittest.TestCase):
    def _sender(self):
        from adapters.telethon_user import TelethonUserSender

        s = TelethonUserSender(api_id=1, api_hash="h", session="s")
        s._client = MagicMock()  # evita conectar
        return s

    def test_enviar_ok(self):
        s = self._sender()
        r = s.enviar("123", "hola")
        self.assertTrue(r.ok)
        s._client.send_message.assert_called_once_with(123, "hola")

    def test_enviar_foto_descarga_y_envia(self):
        s = self._sender()
        with patch("requests.get") as get:
            get.return_value.content = b"IMG"
            r = s.enviar_foto("123", "http://img/p.jpg", caption="cap")
        self.assertTrue(r.ok)
        args, kwargs = s._client.send_file.call_args
        self.assertEqual(args[0], 123)
        self.assertEqual(args[1], b"IMG")
        self.assertEqual(kwargs["caption"], "cap")

    def test_error_generico_se_propaga(self):
        s = self._sender()
        s._client.send_message.side_effect = ValueError("boom")
        with self.assertRaises(ValueError):
            s.enviar("123", "hola")


@unittest.skipUnless(HAS_TELETHON, "requiere telethon")
class TelethonContactsTests(unittest.TestCase):
    def test_listar_contactos(self):
        from adapters.telethon_user import ContactRecipients, TelethonContacts

        contacts = TelethonContacts(api_id=1, api_hash="h", session="s")
        client = MagicMock()
        client.return_value.users = [
            MagicMock(id=123, first_name="Ana", last_name="Lopez", username="ana"),
            MagicMock(id=456, first_name="Beto", last_name="", username="beto"),
        ]
        contacts._client = client

        lista = contacts.listar()
        self.assertEqual([c["id"] for c in lista], ["123", "456"])
        self.assertEqual(lista[0]["name"], "Ana Lopez")

        # ContactRecipients adapta a la interfaz de destinatarios
        rec = ContactRecipients(contacts)
        self.assertEqual(rec.listar_activos(), ["123", "456"])
        rec.marcar_inactivo("123")  # no-op, no debe fallar


class CachedContactsTests(unittest.TestCase):
    def test_listar_todos_propaga_telefono(self):
        from adapters.telethon_user import CachedContacts

        class FakeCfg:
            def get_contacts(self):
                return [
                    {"id": "123", "name": "Ana", "phone": "573188468892"},
                    {"id": "456", "name": "Beto"},  # sin teléfono -> ""
                ]

        cc = CachedContacts(FakeCfg())
        todos = cc.listar_todos()
        self.assertEqual(todos[0], {"chatId": "123", "name": "Ana", "phone": "573188468892"})
        self.assertEqual(todos[1]["phone"], "")  # tolera contactos viejos sin teléfono
        self.assertEqual(cc.listar_activos(), ["123", "456"])


if __name__ == "__main__":
    unittest.main()
