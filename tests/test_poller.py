"""Tests del poller del canal público: parseo, siembra de HWM y difusión de solo lo nuevo."""

import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import poller  # noqa: E402

logging.disable(logging.CRITICAL)


def _msg(post_id, text):
    return (
        'tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message" data-post="iproparts/{post_id}">'
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
        '<div class="tgme_widget_message_footer compact js-message_footer">15:00</div>'
        "</div></div>"
    )


FIXTURE = (
    "<main>"
    + _msg(1001, "SAMSUNG<br/>A06 4-64GB $325.000")
    + _msg(1002, "IPHONE $1.150.000")
    + "</main>"
)


class ParsePostsTests(unittest.TestCase):
    def test_extrae_id_y_texto(self):
        posts = poller._parse_posts(FIXTURE)
        self.assertEqual([mid for mid, _ in posts], [1001, 1002])
        self.assertIn("A06 4-64GB $325.000", posts[0][1])
        self.assertIn("SAMSUNG\nA06", posts[0][1])  # <br/> → salto de línea

    def test_ignora_bloques_sin_texto(self):
        html_text = "<main>" + _msg(5, "") + "</main>"
        self.assertEqual(poller._parse_posts(html_text), [])


class LambdaHandlerTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"BROADCAST_QUEUE_URL": "https://q", "SOURCE_CHANNEL_USERNAME": "iproparts"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_primera_ejecucion_siembra_sin_difundir(self):
        with patch.object(poller, "_fetch", return_value=FIXTURE), patch.object(
            poller, "obtener_hwm", return_value=None
        ), patch.object(poller, "guardar_hwm") as guardar, patch.object(
            poller, "encolar_lotes"
        ) as encolar, patch.object(poller, "obtener_usuarios_activos", return_value=["1"]):
            res = poller.lambda_handler({}, None)
        self.assertEqual(res["seeded"], 1002)
        guardar.assert_called_once_with("iproparts", 1002)
        encolar.assert_not_called()  # no difunde el backlog

    def test_solo_difunde_nuevos_con_markup(self):
        with patch.object(poller, "_fetch", return_value=FIXTURE), patch.object(
            poller, "obtener_hwm", return_value=1001
        ), patch.object(poller, "guardar_hwm") as guardar, patch.object(
            poller, "encolar_lotes", return_value=1
        ) as encolar, patch.object(poller, "obtener_usuarios_activos", return_value=["1", "2"]):
            res = poller.lambda_handler({}, None)
        self.assertEqual(res["new"], 1)  # solo el 1002
        encolar.assert_called_once()
        mensaje = encolar.call_args.args[0]
        self.assertIn("$1.323.000", mensaje)  # markup aplicado al precio nuevo
        guardar.assert_called_once_with("iproparts", 1002)

    def test_sin_nuevos_no_difunde(self):
        with patch.object(poller, "_fetch", return_value=FIXTURE), patch.object(
            poller, "obtener_hwm", return_value=1002
        ), patch.object(poller, "guardar_hwm") as guardar, patch.object(
            poller, "encolar_lotes"
        ) as encolar, patch.object(poller, "obtener_usuarios_activos", return_value=["1"]):
            res = poller.lambda_handler({}, None)
        self.assertEqual(res["new"], 0)
        encolar.assert_not_called()
        guardar.assert_not_called()

    def test_preview_vacio(self):
        with patch.object(poller, "_fetch", return_value="<main></main>"), patch.object(
            poller, "obtener_hwm", return_value=1
        ), patch.object(poller, "encolar_lotes") as encolar:
            res = poller.lambda_handler({}, None)
        self.assertEqual(res, {"checked": 0, "new": 0})
        encolar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
