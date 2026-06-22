"""Entrypoint admin: Basic Auth y dispatch de la API (config, suscriptores, cola)."""

import base64
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from entrypoints import admin  # noqa: E402

logging.disable(logging.CRITICAL)


class FakeConfig:
    def __init__(self):
        self.saved = None
        self.cfg = {"source_channel": "iproparts", "markup_percentage": 15.0, "currency_symbols": "$",
                    "strip_patterns": ["ubicad"], "whatsapp_footer": "", "image_url": "",
                    "send_mode": "bot", "telethon_api_id": "", "telethon_api_hash": "", "telethon_session": ""}
        self.users = {}

    def get(self):
        return dict(self.cfg)

    def set(self, cambios):
        self.saved = cambios
        self.cfg.update(cambios)
        return dict(self.cfg)

    def get_users(self):
        return dict(self.users)

    def set_users(self, users):
        self.users = dict(users)


class FakeSubs:
    def __init__(self):
        self.registros = []

    def listar_todos(self):
        return [{"chatId": "1", "status": "active"}, {"chatId": "2", "status": "inactive"}]

    def registrar(self, chat_id, status):
        self.registros.append((chat_id, status))


class FakeQueueStats:
    def __init__(self):
        self.redriven = False
        self.purged = False

    def profundidades(self):
        return {"broadcast": 3, "dlq": 1}

    def dlq_muestra(self, n=5):
        return [{"broadcast_id": "b1", "batch_index": 0, "chat_ids": 2, "text": "hola"}]

    def dlq_redrive(self):
        self.redriven = True
        return {"ok": True, "redrive": "iniciado"}

    def dlq_purgar(self):
        self.purged = True
        return {"ok": True, "purged": True}


class FakeAudit:
    def __init__(self):
        self.entries = []

    def registrar(self, action, detail="", user="admin"):
        self.entries.append({"ts": 1, "action": action, "detail": detail, "user": user})

    def listar(self, limit=50):
        return list(reversed(self.entries))[:limit]


class FakeImageStore:
    def __init__(self):
        self.saved = None

    def guardar(self, data, content_type="image/jpeg"):
        self.saved = (data, content_type)
        return "images/broadcast.jpg"

    def url_temporal(self, key, expira=3600):
        return "http://signed/" + key


def _event(method, path, body=None, auth=True):
    headers = {}
    if auth:
        token = base64.b64encode(b"admin:secret123").decode()
        headers["authorization"] = f"Basic {token}"
    return {
        "rawPath": f"/dev{path}",
        "requestContext": {"http": {"method": method, "path": f"/dev{path}"}},
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
    }


class AdminTests(unittest.TestCase):
    def setUp(self):
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        admin.queue_stats = FakeQueueStats()
        admin.image_store = FakeImageStore()
        admin.audit_store = FakeAudit()
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"

    def tearDown(self):
        admin.config = admin.subscribers = admin.queue_stats = admin.image_store = admin.audit_store = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_api_sin_auth_401(self):
        resp = admin.lambda_handler(_event("GET", "/admin/api/config", auth=False), None)
        self.assertEqual(resp["statusCode"], 401)

    def test_api_password_incorrecta_401(self):
        ev = _event("GET", "/admin/api/config", auth=False)
        ev["headers"]["authorization"] = "Basic " + base64.b64encode(b"admin:mala").decode()
        self.assertEqual(admin.lambda_handler(ev, None)["statusCode"], 401)

    def test_sin_password_configurada_401(self):
        os.environ.pop("ADMIN_PASSWORD", None)  # fail-closed
        self.assertEqual(admin.lambda_handler(_event("GET", "/admin/api/config"), None)["statusCode"], 401)

    def test_shell_publico_sin_auth(self):
        resp = admin.lambda_handler(_event("GET", "/admin", auth=False), None)  # la página es pública
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("text/html", resp["headers"]["Content-Type"])
        self.assertIn("wordmark", resp["body"])  # marca "Sender" (logo + wordmark)

    def test_me(self):
        resp = admin.lambda_handler(_event("GET", "/admin/api/me"), None)
        self.assertTrue(json.loads(resp["body"])["ok"])

    def test_post_image(self):
        img_b64 = base64.b64encode(b"PNGDATA").decode()
        resp = admin.lambda_handler(_event("POST", "/admin/api/image", {"image": img_b64, "content_type": "image/png"}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(admin.image_store.saved, (b"PNGDATA", "image/png"))
        self.assertEqual(admin.config.cfg["image_key"], "images/broadcast.jpg")

    def test_get_config(self):
        resp = admin.lambda_handler(_event("GET", "/admin/api/config"), None)
        self.assertEqual(json.loads(resp["body"])["source_channel"], "iproparts")

    def test_post_config_sanea(self):
        body = {"source_channel": "otro", "markup_percentage": "20", "strip_patterns": "a\nb", "basura": "x"}
        resp = admin.lambda_handler(_event("POST", "/admin/api/config", body), None)
        self.assertEqual(resp["statusCode"], 200)
        guardado = admin.config.saved
        self.assertEqual(guardado["source_channel"], "otro")
        self.assertEqual(guardado["markup_percentage"], 20.0)       # coercionado a float
        self.assertEqual(guardado["strip_patterns"], ["a", "b"])    # textarea -> lista
        self.assertNotIn("basura", guardado)                         # ignorado

    def test_get_subscribers(self):
        resp = admin.lambda_handler(_event("GET", "/admin/api/subscribers"), None)
        subs = json.loads(resp["body"])["subscribers"]
        self.assertEqual(len(subs), 2)

    def test_post_subscriber_toggle(self):
        admin.lambda_handler(_event("POST", "/admin/api/subscribers", {"chat_id": "7", "status": "inactive"}), None)
        self.assertEqual(admin.subscribers.registros, [("7", "inactive")])

    def test_get_queue(self):
        resp = admin.lambda_handler(_event("GET", "/admin/api/queue"), None)
        self.assertEqual(json.loads(resp["body"]), {"broadcast": 3, "dlq": 1})

    def test_preview_process_aplica_markup(self):
        resp = admin.lambda_handler(
            _event("POST", "/admin/api/preview/process", {"text": "A06 4-64GB $325.000"}), None
        )
        self.assertEqual(resp["statusCode"], 200)
        # markup 15% aplicado (config fake: 15%, símbolo $)
        self.assertIn("$374.000", json.loads(resp["body"])["processed"])

    def test_auditoria_registra_y_lista(self):
        # guardar config -> queda auditado; /api/audit lo lista
        admin.lambda_handler(_event("POST", "/admin/api/config", {"source_channel": "x"}), None)
        acciones = [e["action"] for e in admin.audit_store.entries]
        self.assertIn("config", acciones)
        resp = admin.lambda_handler(_event("GET", "/admin/api/audit"), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(len(json.loads(resp["body"])["audit"]) >= 1)

    def test_dlq_get_y_acciones(self):
        g = admin.lambda_handler(_event("GET", "/admin/api/dlq"), None)
        self.assertEqual(g["statusCode"], 200)
        self.assertEqual(json.loads(g["body"])["depth"], 1)
        admin.lambda_handler(_event("POST", "/admin/api/dlq/redrive", {}), None)
        self.assertTrue(admin.queue_stats.redriven)
        admin.lambda_handler(_event("POST", "/admin/api/dlq/purge", {}), None)
        self.assertTrue(admin.queue_stats.purged)

    def test_lockout_tras_muchos_fallos(self):
        from unittest.mock import patch

        admin._AUTH["fails"] = 0
        admin._AUTH["locked_until"] = 0.0
        ev = _event("GET", "/admin/api/config", auth=False)
        ev["headers"]["authorization"] = "Basic " + base64.b64encode(b"admin:mala").decode()
        for _ in range(admin._AUTH_MAX_FAILS):
            self.assertEqual(admin.lambda_handler(ev, None)["statusCode"], 401)
        # ahora bloqueado: incluso con credenciales correctas, rechaza durante el cooldown.
        # Devuelve 429 (no 401) para que el front distinga "espera N min" de credencial inválida.
        self.assertEqual(admin.lambda_handler(_event("GET", "/admin/api/config"), None)["statusCode"], 429)
        admin._AUTH["fails"] = 0
        admin._AUTH["locked_until"] = 0.0  # limpiar para no afectar otros tests

    def test_config_enmascara_sesion(self):
        admin.config.cfg["telethon_session"] = "SECRETO_TOTAL"
        admin.config.cfg["bot_token"] = "123:SECRETO_BOT"
        resp = admin.lambda_handler(_event("GET", "/admin/api/config"), None)
        body = json.loads(resp["body"])
        self.assertEqual(body["telethon_session"], "")          # nunca se expone
        self.assertTrue(body["telethon_session_set"])           # pero se indica que está
        self.assertEqual(body["bot_token"], "")                 # el token del bot tampoco se expone
        self.assertTrue(body["bot_token_set"])

    def test_post_cuenta_userbot(self):
        body = {"send_mode": "userbot", "telethon_api_id": "123", "telethon_api_hash": "abc", "telethon_session": "SESS"}
        admin.lambda_handler(_event("POST", "/admin/api/config", body), None)
        self.assertEqual(admin.config.saved["send_mode"], "userbot")
        self.assertEqual(admin.config.saved["telethon_session"], "SESS")

    def test_post_no_borra_sesion_con_vacio(self):
        admin.config.cfg["telethon_session"] = "YA_EXISTE"
        admin.lambda_handler(_event("POST", "/admin/api/config", {"source_channel": "x", "telethon_session": ""}), None)
        self.assertNotIn("telethon_session", admin.config.saved)  # vacío → no se guarda (no borra)


class RoleTests(unittest.TestCase):
    """Roles: admin gestiona usuarios; los usuarios normales hacen todo MENOS eso."""

    def setUp(self):
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        admin.audit_store = FakeAudit()
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"
        admin._AUTH["fails"] = 0; admin._AUTH["locked_until"] = 0.0
        # Un usuario normal (rol "user") con contraseña válida para que pase el Basic Auth.
        admin.config.users = {
            "ana": {"email": "ana@x.com", "hash": admin.auth_dom.hash_password("clave1234"), "role": "user"},
        }

    def tearDown(self):
        admin.config = admin.subscribers = admin.audit_store = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def _ev(self, method, path, body=None, user="admin", pw="secret123"):
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {
            "rawPath": f"/dev{path}",
            "requestContext": {"http": {"method": method, "path": f"/dev{path}"}},
            "headers": {"authorization": f"Basic {token}"},
            "body": json.dumps(body) if body is not None else None,
        }

    def test_me_admin_es_admin(self):
        b = json.loads(admin.lambda_handler(self._ev("GET", "/admin/api/me"), None)["body"])
        self.assertTrue(b["is_admin"]); self.assertEqual(b["role"], "admin")

    def test_me_usuario_no_es_admin(self):
        b = json.loads(admin.lambda_handler(self._ev("GET", "/admin/api/me", user="ana", pw="clave1234"), None)["body"])
        self.assertFalse(b["is_admin"]); self.assertEqual(b["role"], "user")

    def test_usuario_no_lista_usuarios_403(self):
        r = admin.lambda_handler(self._ev("GET", "/admin/api/users", user="ana", pw="clave1234"), None)
        self.assertEqual(r["statusCode"], 403)

    def test_usuario_no_crea_usuarios_403(self):
        r = admin.lambda_handler(self._ev("POST", "/admin/api/users",
                                          {"username": "x", "password": "abcdefgh", "role": "user"},
                                          user="ana", pw="clave1234"), None)
        self.assertEqual(r["statusCode"], 403)
        self.assertNotIn("x", admin.config.users)

    def test_admin_crea_usuario_con_rol(self):
        r = admin.lambda_handler(self._ev("POST", "/admin/api/users",
                                          {"username": "leo", "password": "abcdefgh", "role": "user"}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(admin.config.users["leo"]["role"], "user")

    def test_admin_puede_promover_y_degradar(self):
        admin.lambda_handler(self._ev("POST", "/admin/api/users/role", {"username": "ana", "role": "admin"}), None)
        self.assertEqual(admin.config.users["ana"]["role"], "admin")
        admin.lambda_handler(self._ev("POST", "/admin/api/users/role", {"username": "ana", "role": "user"}), None)
        self.assertEqual(admin.config.users["ana"]["role"], "user")

    def test_no_degrada_al_admin_principal(self):
        admin.config.users["admin"] = {"email": "", "hash": admin.auth_dom.hash_password("secret123"), "role": "admin"}
        r = admin.lambda_handler(self._ev("POST", "/admin/api/users/role", {"username": "admin", "role": "user"}), None)
        self.assertEqual(r["statusCode"], 400)

    def test_no_borra_al_admin_principal(self):
        admin.config.users["admin"] = {"email": "", "hash": admin.auth_dom.hash_password("secret123"), "role": "admin"}
        r = admin.lambda_handler(self._ev("POST", "/admin/api/users/delete", {"username": "admin"}), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertIn("admin", admin.config.users)


class TelegramAccountTests(unittest.TestCase):
    """/api/telegram/account: estado de la identidad que envía (userbot: ¿válida o renovar?)."""

    def setUp(self):
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()  # evita que _ensure() haga I/O real (build_subscribers)
        admin.audit_store = FakeAudit()
        os.environ["ADMIN_USER"] = "admin"; os.environ["ADMIN_PASSWORD"] = "secret123"
        admin._AUTH["fails"] = 0; admin._AUTH["locked_until"] = 0.0
        self._wiring_orig = admin.wiring.build_telethon_account

    def tearDown(self):
        admin.wiring.build_telethon_account = self._wiring_orig
        admin.config = admin.subscribers = admin.audit_store = None
        os.environ.pop("ADMIN_USER", None); os.environ.pop("ADMIN_PASSWORD", None)

    def _ev(self):
        token = base64.b64encode(b"admin:secret123").decode()
        return {"rawPath": "/dev/admin/api/telegram/account",
                "requestContext": {"http": {"method": "GET", "path": "/dev/admin/api/telegram/account"}},
                "headers": {"authorization": f"Basic {token}"}, "body": None}

    def test_userbot_sin_sesion_pide_renovar(self):
        admin.config.cfg["send_mode"] = "userbot"; admin.config.cfg["telethon_session"] = ""
        b = json.loads(admin.lambda_handler(self._ev(), None)["body"])
        self.assertEqual(b["mode"], "userbot"); self.assertFalse(b["configured"]); self.assertTrue(b["needs_renew"])

    def test_userbot_conectada(self):
        admin.config.cfg["send_mode"] = "userbot"; admin.config.cfg["telethon_session"] = "SESS"

        class FakeAcc:
            def estado(self):
                return {"authorized": True, "me": {"id": "1", "name": "Yo", "username": "yo", "phone": "573001112233"}}
        admin.wiring.build_telethon_account = lambda: FakeAcc()
        b = json.loads(admin.lambda_handler(self._ev(), None)["body"])
        self.assertTrue(b["connected"]); self.assertFalse(b["needs_renew"]); self.assertEqual(b["me"]["phone"], "573001112233")

    def test_userbot_sesion_revocada_pide_renovar(self):
        admin.config.cfg["send_mode"] = "userbot"; admin.config.cfg["telethon_session"] = "SESS"

        class FakeAcc:
            def estado(self):
                return {"authorized": False, "me": None}
        admin.wiring.build_telethon_account = lambda: FakeAcc()
        b = json.loads(admin.lambda_handler(self._ev(), None)["body"])
        self.assertFalse(b["connected"]); self.assertTrue(b["needs_renew"])

    def test_userbot_error_no_afirma_renovar(self):
        admin.config.cfg["send_mode"] = "userbot"; admin.config.cfg["telethon_session"] = "SESS"

        def _boom():
            raise RuntimeError("telethon caído")
        admin.wiring.build_telethon_account = _boom
        b = json.loads(admin.lambda_handler(self._ev(), None)["body"])
        self.assertIsNone(b["connected"]); self.assertFalse(b["needs_renew"])  # estado desconocido, no falsa alarma


if __name__ == "__main__":
    unittest.main()
