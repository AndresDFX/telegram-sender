"""Application: BroadcastList (composición del mensaje + encolado, con fakes)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.broadcasting import BroadcastList  # noqa: E402


class FakeSubs:
    def __init__(self, ids):
        self._ids = ids

    def listar_activos(self):
        return list(self._ids)

    def registrar(self, *a):
        pass

    def marcar_inactivo(self, *a):
        pass


class FakeQueue:
    def __init__(self):
        self.calls = []

    def encolar(self, text, chat_ids, image_url=None):
        self.calls.append((text, list(chat_ids), image_url))
        return 1


class FakeConfig:
    def __init__(self, **overrides):
        self.cfg = {
            "source_channel": "ch",
            "markup_percentage": 15.0,
            "currency_symbols": "$",
            "strip_patterns": ["ubicad"],
            "whatsapp_footer": "📲 WhatsApp 300",
            "image_url": "",
        }
        self.cfg.update(overrides)

    def get(self):
        return dict(self.cfg)

    def set(self, c):
        self.cfg.update(c)
        return dict(self.cfg)


class BroadcastListTests(unittest.TestCase):
    def test_compone_quita_ubicacion_markup_y_footer(self):
        queue = FakeQueue()
        bl = BroadcastList(FakeSubs(["1", "2"]), queue, FakeConfig())
        res = bl("UBICADOS aqui\nA06 4-64GB $100.000")

        self.assertEqual(res, {"batches": 1, "subscribers": 2})
        text, ids, image_url = queue.calls[0]
        self.assertNotIn("UBICADOS", text)            # ubicación fuera
        self.assertIn("$115.000", text)               # markup aplicado
        self.assertTrue(text.endswith("📲 WhatsApp 300"))  # footer
        self.assertEqual(ids, ["1", "2"])
        self.assertIsNone(image_url)

    def test_pasa_image_url_si_configurada(self):
        queue = FakeQueue()
        BroadcastList(FakeSubs(["1"]), queue, FakeConfig(image_url="http://img/p.jpg"))("A $100.000")
        self.assertEqual(queue.calls[0][2], "http://img/p.jpg")


if __name__ == "__main__":
    unittest.main()
