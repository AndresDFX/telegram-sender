"""Adapters: TmePreviewChannelReader.parse (extracción de posts del preview público)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.tme import TmePreviewChannelReader  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
