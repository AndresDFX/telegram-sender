"""Tests de Fase 0: secret_token (fail-closed), parseo seguro, dedup, compensación y comandos."""

import json
import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import handler  # noqa: E402
from sqs_client import PartialEnqueueError  # noqa: E402

logging.disable(logging.CRITICAL)


def _event(update, headers=None):
    return {"body": json.dumps(update), "headers": headers or {}}


def _clean_env():
    for var in ("WEBHOOK_SECRET_TOKEN", "ALLOW_INSECURE_WEBHOOK", "SOURCE_CHANNEL_ID"):
        os.environ.pop(var, None)


# --- secret_token (fail-closed) ---------------------------------------------

class SecretUnitTests(unittest.TestCase):
    """_secret_valido directamente, sin pasar por todo el handler."""

    def setUp(self):
        _clean_env()

    def test_sin_secreto_y_sin_flag_es_fail_closed(self):
        self.assertFalse(handler._secret_valido({"headers": {}}))

    def test_sin_secreto_con_flag_inseguro_permite(self):
        with patch.dict(os.environ, {"ALLOW_INSECURE_WEBHOOK": "1"}):
            self.assertTrue(handler._secret_valido({"headers": {}}))

    def test_secreto_correcto_constante(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET_TOKEN": "s3cr3t"}):
            ev = {"headers": {"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"}}
            self.assertTrue(handler._secret_valido(ev))

    def test_secreto_incorrecto(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET_TOKEN": "s3cr3t"}):
            self.assertFalse(handler._secret_valido({"headers": {"X-Telegram-Bot-Api-Secret-Token": "x"}}))

    def test_usa_compare_digest(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET_TOKEN": "s3cr3t"}), patch.object(
            handler.hmac, "compare_digest", wraps=handler.hmac.compare_digest
        ) as cmp:
            handler._secret_valido({"headers": {"x-telegram-bot-api-secret-token": "s3cr3t"}})
        cmp.assert_called_once()


class SecretHandlerTests(unittest.TestCase):
    def setUp(self):
        _clean_env()

    def test_fail_closed_rechaza_sin_secreto(self):
        with patch.object(handler, "obtener_usuarios_activos") as usuarios:
            resp = handler.lambda_handler(
                _event({"channel_post": {"chat": {"id": -1}, "text": "x"}}), None
            )
        self.assertEqual(resp["statusCode"], 403)
        usuarios.assert_not_called()

    def test_secreto_correcto_pasa(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET_TOKEN": "s3cr3t"}), patch.object(
            handler, "obtener_usuarios_activos", return_value=[]
        ), patch.object(handler, "procesar_lote", return_value={"total": 0, "sent": 0, "blocked": 0, "failed": 0, "failed_ids": []}):
            resp = handler.lambda_handler(
                _event(
                    {"channel_post": {"chat": {"id": -1}, "text": "x"}},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
                ),
                None,
            )
        self.assertEqual(resp["statusCode"], 200)

    def test_secreto_incorrecto_rechaza(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET_TOKEN": "s3cr3t"}), patch.object(
            handler, "obtener_usuarios_activos"
        ) as usuarios:
            resp = handler.lambda_handler(
                _event(
                    {"channel_post": {"chat": {"id": -1}, "text": "x"}},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "mal"},
                ),
                None,
            )
        self.assertEqual(resp["statusCode"], 403)
        usuarios.assert_not_called()


# --- parseo seguro del body --------------------------------------------------

class BodyParsingTests(unittest.TestCase):
    def setUp(self):
        _clean_env()
        os.environ["ALLOW_INSECURE_WEBHOOK"] = "1"

    def tearDown(self):
        os.environ.pop("ALLOW_INSECURE_WEBHOOK", None)

    def test_json_malformado_devuelve_400(self):
        with patch.object(handler, "marcar_update_procesado") as marcar:
            resp = handler.lambda_handler({"body": "no-es-json", "headers": {}}, None)
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(json.loads(resp["body"])["status"], "bad_request")
        marcar.assert_not_called()

    def test_body_none_no_revienta(self):
        resp = handler.lambda_handler({"body": None, "headers": {}}, None)
        self.assertEqual(resp["statusCode"], 200)

    def test_body_dict_directo(self):
        with patch.object(handler, "registrar_suscriptor"), patch.object(handler, "_responder"):
            resp = handler.lambda_handler(
                {"body": {"message": {"chat": {"id": 5, "type": "private"}, "text": "/start"}}, "headers": {}},
                None,
            )
        self.assertEqual(json.loads(resp["body"])["command"], "/start")

    def test_json_no_objeto_devuelve_400(self):
        # Listas, números, bool, null y strings JSON no son updates → 400 sin marcar dedup.
        for raw in ("[]", "[1,2,3]", "123", "true", "null", '"hola"'):
            with self.subTest(raw=raw), patch.object(handler, "marcar_update_procesado") as marcar:
                resp = handler.lambda_handler({"body": raw, "headers": {}}, None)
                self.assertEqual(resp["statusCode"], 400, raw)
                self.assertEqual(json.loads(resp["body"])["status"], "bad_request")
                marcar.assert_not_called()

    def test_body_bytes_no_soportado_devuelve_400(self):
        with patch.object(handler, "marcar_update_procesado") as marcar:
            resp = handler.lambda_handler({"body": b"{}", "headers": {}}, None)
        self.assertEqual(resp["statusCode"], 400)
        marcar.assert_not_called()

    def test_body_vacio_y_ausente_son_200(self):
        for event in ({"body": "", "headers": {}}, {"headers": {}}):
            with self.subTest(event=event):
                resp = handler.lambda_handler(event, None)
                self.assertEqual(resp["statusCode"], 200)
                self.assertEqual(resp["body"], "OK")


# --- dedup de update_id ------------------------------------------------------

class DedupTests(unittest.TestCase):
    def setUp(self):
        _clean_env()
        self._env = patch.dict(os.environ, {"BROADCAST_QUEUE_URL": "https://q", "ALLOW_INSECURE_WEBHOOK": "1"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_duplicado_se_ignora(self):
        with patch.object(handler, "marcar_update_procesado", return_value=False), patch.object(
            handler, "obtener_usuarios_activos"
        ) as usuarios, patch.object(handler, "encolar_lotes") as encolar:
            resp = handler.lambda_handler(
                _event({"update_id": 42, "channel_post": {"chat": {"id": -1}, "text": "x"}}), None
            )
        self.assertEqual(json.loads(resp["body"])["status"], "duplicate")
        usuarios.assert_not_called()
        encolar.assert_not_called()

    def test_primera_vez_se_procesa(self):
        with patch.object(handler, "marcar_update_procesado", return_value=True), patch.object(
            handler, "obtener_usuarios_activos", return_value=["1"]
        ), patch.object(handler, "encolar_lotes", return_value=1) as encolar:
            resp = handler.lambda_handler(
                _event({"update_id": 43, "channel_post": {"chat": {"id": -1}, "text": "Precio $10.00"}}),
                None,
            )
        self.assertEqual(json.loads(resp["body"])["status"], "queued")
        encolar.assert_called_once()

    def test_fallo_pre_efecto_revierte_dedup(self):
        # Falla la consulta de suscriptores (antes de encolar) → seguro revertir.
        with patch.object(handler, "marcar_update_procesado", return_value=True), patch.object(
            handler, "obtener_usuarios_activos", side_effect=RuntimeError("ddb caído")
        ), patch.object(handler, "borrar_update_procesado") as borrar:
            with self.assertRaises(RuntimeError):
                handler.lambda_handler(
                    _event({"update_id": 44, "channel_post": {"chat": {"id": -1}, "text": "$1.00"}}),
                    None,
                )
        borrar.assert_called_once_with("44")

    def test_encolado_parcial_no_revierte(self):
        # Ya se encolaron lotes → NO revertir (evita duplicados en el reintento).
        with patch.object(handler, "marcar_update_procesado", return_value=True), patch.object(
            handler, "obtener_usuarios_activos", return_value=["1", "2"]
        ), patch.object(handler, "encolar_lotes", side_effect=PartialEnqueueError(1, 3)), patch.object(
            handler, "borrar_update_procesado"
        ) as borrar:
            with self.assertRaises(PartialEnqueueError):
                handler.lambda_handler(
                    _event({"update_id": 45, "channel_post": {"chat": {"id": -1}, "text": "$1.00"}}),
                    None,
                )
        borrar.assert_not_called()

    def test_encolado_cero_si_revierte(self):
        # Falló el primer lote (nada encolado) → seguro revertir.
        with patch.object(handler, "marcar_update_procesado", return_value=True), patch.object(
            handler, "obtener_usuarios_activos", return_value=["1"]
        ), patch.object(handler, "encolar_lotes", side_effect=PartialEnqueueError(0, 3)), patch.object(
            handler, "borrar_update_procesado"
        ) as borrar:
            with self.assertRaises(PartialEnqueueError):
                handler.lambda_handler(
                    _event({"update_id": 46, "channel_post": {"chat": {"id": -1}, "text": "$1.00"}}),
                    None,
                )
        borrar.assert_called_once_with("46")

    def test_fallo_al_revertir_no_enmascara_el_error_original(self):
        with patch.object(handler, "marcar_update_procesado", return_value=True), patch.object(
            handler, "obtener_usuarios_activos", side_effect=RuntimeError("original")
        ), patch.object(handler, "borrar_update_procesado", side_effect=RuntimeError("delete falló")):
            with self.assertRaises(RuntimeError) as ctx:
                handler.lambda_handler(
                    _event({"update_id": 47, "channel_post": {"chat": {"id": -1}, "text": "$1.00"}}),
                    None,
                )
        self.assertEqual(str(ctx.exception), "original")  # se re-propaga el error real

    def test_sin_update_id_no_llama_dedup(self):
        with patch.object(handler, "marcar_update_procesado") as marcar, patch.object(
            handler, "obtener_usuarios_activos", return_value=["1"]
        ), patch.object(handler, "encolar_lotes", return_value=1):
            handler.lambda_handler(
                _event({"channel_post": {"chat": {"id": -1}, "text": "Precio $10.00"}}), None
            )
        marcar.assert_not_called()

    def test_inline_fallo_tras_enviar_no_revierte(self):
        # Sin cola (inline): el envío es efecto secundario; si procesar_lote revienta
        # tras enviar a algunos, NO se revierte el dedup (evita duplicados en reintento).
        os.environ.pop("BROADCAST_QUEUE_URL", None)
        with patch.object(handler, "marcar_update_procesado", return_value=True), patch.object(
            handler, "obtener_usuarios_activos", return_value=["1", "2"]
        ), patch.object(handler, "procesar_lote", side_effect=RuntimeError("crash a mitad")), patch.object(
            handler, "borrar_update_procesado"
        ) as borrar:
            resp = handler.lambda_handler(
                _event({"update_id": 48, "channel_post": {"chat": {"id": -1}, "text": "$1.00"}}),
                None,
            )
        self.assertEqual(resp["statusCode"], 500)
        borrar.assert_not_called()


# --- comandos /start /stop ---------------------------------------------------

class CommandTests(unittest.TestCase):
    def setUp(self):
        _clean_env()
        os.environ["ALLOW_INSECURE_WEBHOOK"] = "1"

    def tearDown(self):
        os.environ.pop("ALLOW_INSECURE_WEBHOOK", None)

    def _msg(self, text, chat_type="private", chat_id=555):
        return {"message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}

    def test_start_da_de_alta_y_responde(self):
        with patch.object(handler, "registrar_suscriptor") as registrar, patch.object(
            handler, "_responder"
        ) as responder:
            resp = handler.lambda_handler(_event(self._msg("/start")), None)
        self.assertEqual(json.loads(resp["body"])["command"], "/start")
        registrar.assert_called_once_with("555", "active")
        responder.assert_called_once()

    def test_stop_da_de_baja(self):
        with patch.object(handler, "registrar_suscriptor") as registrar, patch.object(handler, "_responder"):
            handler.lambda_handler(_event(self._msg("/stop")), None)
        registrar.assert_called_once_with("555", "inactive")

    def test_start_con_mencion_de_bot(self):
        with patch.object(handler, "registrar_suscriptor") as registrar, patch.object(handler, "_responder"):
            handler.lambda_handler(_event(self._msg("/start@MiCanalBot")), None)
        registrar.assert_called_once_with("555", "active")

    def test_comando_desconocido_envia_ayuda(self):
        with patch.object(handler, "registrar_suscriptor") as registrar, patch.object(
            handler, "_responder"
        ) as responder:
            handler.lambda_handler(_event(self._msg("/loquesea")), None)
        registrar.assert_not_called()
        responder.assert_called_once()

    def test_fallo_al_responder_no_revierte_el_alta(self):
        # _responder lanza tras registrar_suscriptor → el alta queda; NO se compensa; 200.
        with patch.object(handler, "registrar_suscriptor") as registrar, patch.object(
            handler, "_responder", side_effect=RuntimeError("telegram caído")
        ), patch.object(handler, "borrar_update_procesado") as borrar, patch.object(
            handler, "marcar_update_procesado", return_value=True
        ):
            resp = handler.lambda_handler(_event({"update_id": 9, **self._msg("/start")}), None)
        self.assertEqual(json.loads(resp["body"])["command"], "/start")
        registrar.assert_called_once_with("555", "active")
        borrar.assert_not_called()

    def test_mensaje_no_privado_se_ignora(self):
        with patch.object(handler, "registrar_suscriptor") as registrar, patch.object(
            handler, "_responder"
        ) as responder:
            resp = handler.lambda_handler(_event(self._msg("/start", chat_type="group")), None)
        self.assertEqual(resp["body"], "OK")
        registrar.assert_not_called()
        responder.assert_not_called()

    def test_mensaje_privado_sin_comando_se_ignora(self):
        with patch.object(handler, "registrar_suscriptor") as registrar, patch.object(
            handler, "_responder"
        ) as responder:
            resp = handler.lambda_handler(_event(self._msg("hola, una pregunta")), None)
        self.assertEqual(resp["body"], "OK")
        registrar.assert_not_called()
        responder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
