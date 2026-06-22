"""Application: DeliverBatch (envío por lote con fakes de los puertos)."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.deliver_batch import DeliverBatch  # noqa: E402
from domain.models import SendResult  # noqa: E402

logging.disable(logging.CRITICAL)


class FakeSender:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []
        self.fotos = []

    def enviar(self, chat_id, text):
        self.calls.append((chat_id, text))
        outcome = self.outcomes.get(chat_id, SendResult(ok=True))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def enviar_foto(self, chat_id, image_url, caption=""):
        self.fotos.append((chat_id, image_url, caption))
        return SendResult(ok=True)


class FakeSubscribers:
    def __init__(self):
        self.inactivos = []

    def listar_activos(self):
        return []

    def registrar(self, chat_id, status):
        pass

    def marcar_inactivo(self, chat_id):
        self.inactivos.append(chat_id)


class FakeDedup:
    def __init__(self, ya=()):
        self.ya = set(ya)
        self.marcados = []

    def procesado(self, key):
        return key in self.ya

    def marcar(self, key):
        self.marcados.append(key)
        return True


class DeliverBatchTests(unittest.TestCase):
    def test_clasifica_enviados_bloqueados_y_fallidos(self):
        sender = FakeSender({
            "1": SendResult(ok=True),
            "2": SendResult(ok=False, blocked=True),
            "3": RuntimeError("boom"),
        })
        subs = FakeSubscribers()
        deliver = DeliverBatch(sender, subs, delay=0, sleep=lambda _s: None)

        stats = deliver("lista", ["1", "2", "3"])

        self.assertEqual(stats.total, 3)
        self.assertEqual(stats.sent, 1)
        self.assertEqual(stats.blocked, 1)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.failed_ids, ["3"])
        self.assertEqual(subs.inactivos, ["2"])

    def test_respeta_el_delay(self):
        sleeps = []
        deliver = DeliverBatch(FakeSender(), FakeSubscribers(), delay=0.05, sleep=sleeps.append)
        deliver("x", ["1", "2"])
        self.assertEqual(sleeps, [0.05, 0.05])

    def test_delay_aleatorio_en_rango(self):
        sleeps = []
        deliver = DeliverBatch(FakeSender(), FakeSubscribers(), delay_min=1.0, delay_max=4.0, sleep=sleeps.append)
        deliver("x", ["1", "2", "3"])
        self.assertEqual(len(sleeps), 3)
        for s in sleeps:
            self.assertGreaterEqual(s, 1.0)
            self.assertLessEqual(s, 4.0)

    def test_imagen_con_texto_corto_va_en_un_solo_mensaje_caption(self):
        # Texto que cabe en caption (<=1024): la foto LLEVA el texto y NO hay mensaje de texto aparte.
        sender = FakeSender()
        deliver = DeliverBatch(sender, FakeSubscribers(), delay=0)
        stats = deliver("lista corta", ["1", "2"], image_url="http://img/p.jpg")
        self.assertEqual(stats.sent, 2)
        self.assertEqual(sender.fotos, [("1", "http://img/p.jpg", "lista corta"), ("2", "http://img/p.jpg", "lista corta")])
        self.assertEqual(sender.calls, [])  # NO se envió texto por separado (un solo mensaje)

    def test_idempotencia_por_destinatario_no_reenvia_en_reentrega(self):
        # Reentrega del lote: el contacto que YA recibió ("b1:2") se salta (no se reenvía); los
        # demás se entregan y se marcan. Evita duplicados aunque el lote se reentregue (timeout).
        sender = FakeSender()
        dd = FakeDedup({"b1:2"})
        deliver = DeliverBatch(sender, FakeSubscribers(), delay=0, dedup=dd)
        stats = deliver("hola", ["1", "2", "3"], batch_id="b1")
        self.assertEqual(stats.sent, 3)  # 1 y 3 enviados ahora + 2 ya contaba como entregado
        self.assertEqual([c[0] for c in sender.calls], ["1", "3"])  # al 2 NO se le reenvió
        self.assertIn("b1:1", dd.marcados)
        self.assertIn("b1:3", dd.marcados)
        self.assertNotIn("b1:2", dd.marcados)

    def test_imagen_con_texto_largo_va_en_dos_mensajes(self):
        # Texto > 1024 no cabe en caption: foto SIN caption + texto completo aparte.
        sender = FakeSender()
        deliver = DeliverBatch(sender, FakeSubscribers(), delay=0)
        largo = "x" * 1100
        stats = deliver(largo, ["1"], image_url="http://img/p.jpg")
        self.assertEqual(stats.sent, 1)
        self.assertEqual(sender.fotos, [("1", "http://img/p.jpg", "")])  # foto sin caption
        self.assertEqual([c[0] for c in sender.calls], ["1"])  # y el texto completo aparte

    def test_a3_foto_no_se_reenvia_en_reentrega_si_el_texto_fallo(self):
        # A3: imagen + texto largo (>1024) van en 2 mensajes. Si la foto se envió pero el texto
        # falló/timeout, en la REENTREGA la foto NO debe reenviarse (sub-paso 'b1:1:photo' marcado).
        largo = "x" * 1100
        dd = FakeDedup()
        # 1ª entrega: foto OK, texto FALLA.
        s1 = FakeSender({"1": RuntimeError("timeout")})
        DeliverBatch(s1, FakeSubscribers(), delay=0, dedup=dd)(largo, ["1"], image_url="http://img", batch_id="b1")
        self.assertEqual(len(s1.fotos), 1)            # foto enviada
        self.assertIn("b1:1:photo", dd.marcados)      # sub-paso foto marcado
        self.assertNotIn("b1:1", dd.marcados)         # el destinatario NO completó (texto falló)
        # 2ª entrega (reentrega): lo marcado ya es "procesado"; el texto ahora va bien.
        dd.ya |= set(dd.marcados)
        s2 = FakeSender()
        stats = DeliverBatch(s2, FakeSubscribers(), delay=0, dedup=dd)(largo, ["1"], image_url="http://img", batch_id="b1")
        self.assertEqual(len(s2.fotos), 0)            # ¡foto NO reenviada! (A3)
        self.assertEqual([c[0] for c in s2.calls], ["1"])  # solo el texto
        self.assertEqual(stats.sent, 1)
        self.assertIn("b1:1", dd.marcados)            # ahora sí completa

    def test_fallo_al_inactivar_no_rompe(self):
        class BadSubs(FakeSubscribers):
            def marcar_inactivo(self, chat_id):
                raise RuntimeError("ddb")

        deliver = DeliverBatch(FakeSender({"1": SendResult(ok=False, blocked=True)}), BadSubs(), delay=0)
        stats = deliver("x", ["1"])
        self.assertEqual(stats.blocked, 1)
        self.assertEqual(stats.failed, 0)


if __name__ == "__main__":
    unittest.main()
