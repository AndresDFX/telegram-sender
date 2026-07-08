"""Adapters: TmePreviewChannelReader.parse (extracción de posts del preview público)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.tme import FallbackChannelReader, TmePreviewChannelReader  # noqa: E402
from domain.models import Post  # noqa: E402


def _msg(post_id, text):
    return (
        'tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message" data-post="iproparts/{post_id}">'
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
        '<div class="tgme_widget_message_footer compact js-message_footer">15:00</div>'
        "</div></div>"
    )


FIXTURE = "<main>" + _msg(1001, "SAMSUNG<br/>A06 4-64GB $325.000") + _msg(1002, "IPHONE $1.150.000") + "</main>"


class ParseTests(unittest.TestCase):
    def test_extrae_id_y_texto(self):
        posts = TmePreviewChannelReader.parse(FIXTURE)
        self.assertEqual([p.message_id for p in posts], [1001, 1002])
        self.assertIn("A06 4-64GB $325.000", posts[0].text)
        self.assertIn("SAMSUNG\nA06", posts[0].text)

    def test_ignora_sin_texto(self):
        self.assertEqual(TmePreviewChannelReader.parse("<main>" + _msg(5, "") + "</main>"), [])


class _FakeReader:
    def __init__(self, posts):
        self.posts, self.calls = posts, 0

    def leer_publicaciones(self, channel):
        self.calls += 1
        return list(self.posts)


class FallbackReaderTests(unittest.TestCase):
    def test_preview_vacio_usa_el_respaldo(self):
        # El preview murió (t.me redirige a la tarjeta → 0 posts): se lee con el userbot.
        preview, userbot = _FakeReader([]), _FakeReader([Post(1001, "A $100.000")])
        posts = FallbackChannelReader(preview, userbot).leer_publicaciones("ch")
        self.assertEqual([p.message_id for p in posts], [1001])
        self.assertEqual((preview.calls, userbot.calls), (1, 1))

    def test_preview_con_posts_no_toca_el_respaldo(self):
        preview, userbot = _FakeReader([Post(1, "x")]), _FakeReader([Post(2, "y")])
        posts = FallbackChannelReader(preview, userbot).leer_publicaciones("ch")
        self.assertEqual([p.message_id for p in posts], [1])
        self.assertEqual(userbot.calls, 0)  # no se abre Telethon si el preview funciona

    def test_todos_vacios_devuelve_vacio(self):
        self.assertEqual(FallbackChannelReader(_FakeReader([]), _FakeReader([]), None).leer_publicaciones("ch"), [])


if __name__ == "__main__":
    unittest.main()
