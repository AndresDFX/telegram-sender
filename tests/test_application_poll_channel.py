"""Application: PollChannel (siembra de HWM y difusión de solo lo nuevo, con fakes)."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.poll_channel import PollChannel  # noqa: E402
from domain.models import Post  # noqa: E402

logging.disable(logging.CRITICAL)


class FakeReader:
    def __init__(self, posts):
        self._posts = posts

    def leer_publicaciones(self, channel):
        return list(self._posts)


class FakeHwm:
    def __init__(self, value=None):
        self.value = value

    def obtener(self, channel):
        return self.value

    def guardar(self, channel, value):
        self.value = value


class FakeBroadcast:
    def __init__(self):
        self.textos = []

    def __call__(self, text):
        self.textos.append(text)
        return {"batches": 1, "subscribers": 1}


class FakeConfig:
    def __init__(self, channel="ch"):
        self._channel = channel

    def get(self):
        return {"source_channel": self._channel}

    def set(self, cambios):
        return self.get()


POSTS = [Post(1001, "A $325.000"), Post(1002, "B $100.000")]
CFG = FakeConfig("ch")


class PollChannelTests(unittest.TestCase):
    def test_primera_ejecucion_siembra_sin_difundir(self):
        hwm, bc = FakeHwm(None), FakeBroadcast()
        res = PollChannel(FakeReader(POSTS), hwm, bc, CFG)()
        self.assertEqual(res["seeded"], 1002)
        self.assertEqual(res["new"], 0)
        self.assertEqual(bc.textos, [])
        self.assertEqual(hwm.value, 1002)

    def test_solo_difunde_nuevos(self):
        hwm, bc = FakeHwm(1001), FakeBroadcast()
        res = PollChannel(FakeReader(POSTS), hwm, bc, CFG)()
        self.assertEqual(res["new"], 1)
        self.assertEqual(bc.textos, ["B $100.000"])
        self.assertEqual(hwm.value, 1002)

    def test_sin_nuevos(self):
        hwm, bc = FakeHwm(1002), FakeBroadcast()
        res = PollChannel(FakeReader(POSTS), hwm, bc, CFG)()
        self.assertEqual(res["new"], 0)
        self.assertEqual(bc.textos, [])

    def test_preview_vacio(self):
        res = PollChannel(FakeReader([]), FakeHwm(1), FakeBroadcast(), CFG)()
        self.assertEqual(res, {"checked": 0, "new": 0})


if __name__ == "__main__":
    unittest.main()
