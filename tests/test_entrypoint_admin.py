"""Entrypoint admin: Basic Auth y dispatch de la API (config, suscriptores, cola)."""

import ast
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from html.parser import HTMLParser

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
        self.tg_status = {"connected": None, "me": None, "checked_at": 0}

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

    def get_tg_status(self):
        return dict(self.tg_status)

    def set_tg_status(self, connected, me=None):
        self.tg_status = {"connected": connected, "me": me if me is not None else self.tg_status.get("me"),
                          "checked_at": int(time.time())}


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

    def purgar_principal(self):
        self.main_purged = True
        return {"ok": True, "purged": True}


class FakeBroadcastStore:
    def __init__(self):
        self.borrados = []
        self.items = []

    def listar(self, limit=30):
        return list(self.items)

    def borrar(self, bid):
        self.borrados.append(bid)

    def borrar_terminados(self, excluir_ids=None):
        return 0


class FakePlanStoreAdmin:
    def __init__(self):
        self.borrados = []
        self.todos_borrado = 0

    def borrar(self, pid):
        self.borrados.append(pid)

    def borrar_todos(self):
        self.todos_borrado += 1
        return 3

    def activos(self):
        return []


class FakeAudit:
    def __init__(self):
        self.entries = []

    def registrar(self, action, detail="", user="admin"):
        self.entries.append({"ts": 1, "action": action, "detail": detail, "user": user})

    def listar(self, limit=50):
        return list(reversed(self.entries))[:limit]

    def borrar_todos(self):
        n = len(self.entries)
        self.entries = []
        return n


class FakeScheduleStore:
    """Programados en memoria: `actualizar` guarda lo pedido para poder afirmar QUÉ se escribió."""

    def __init__(self):
        self.items = []
        self.actualizados = []
        self.borrados = []

    def crear(self, **campos):
        sid = "s-%d" % (len(self.items) + 1)
        self.items.append({"sid": sid, "last_run": 0, "runs": 0, "created_at": 0, **campos})
        return sid

    def listar(self):
        return [dict(x) for x in self.items]

    def actualizar(self, sid, **campos):
        self.actualizados.append((sid, dict(campos)))
        for it in self.items:
            if it["sid"] == sid:
                it.update(campos)

    def borrar(self, sid):
        self.borrados.append(sid)
        self.items = [x for x in self.items if x["sid"] != sid]


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
        admin.broadcast_store = FakeBroadcastStore()
        admin.plan_store = FakePlanStoreAdmin()
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"

    def tearDown(self):
        admin.config = admin.subscribers = admin.queue_stats = admin.image_store = admin.audit_store = None
        admin.broadcast_store = admin.plan_store = None
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

    def test_a12_activar_envio_sin_lista_rechaza_400(self):
        # A12: activar sending_enabled sin auto_telegram_list se rechaza (400) y NO se guarda,
        # para no difundir a TODOS los contactos por error (guardia de backend, no solo del front).
        resp = admin.lambda_handler(_event("POST", "/admin/api/config", {"sending_enabled": True}), None)
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("lista", json.loads(resp["body"])["error"].lower())
        self.assertIsNone(admin.config.saved)  # no se persistió el cambio

    def test_a12_activar_envio_con_lista_ok(self):
        # Con la lista elegida en la misma petición, la activación se acepta.
        body = {"sending_enabled": True, "auto_telegram_list": "VIP"}
        resp = admin.lambda_handler(_event("POST", "/admin/api/config", body), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(admin.config.saved["sending_enabled"])
        self.assertEqual(admin.config.saved["auto_telegram_list"], "VIP")

    def test_get_subscribers(self):
        resp = admin.lambda_handler(_event("GET", "/admin/api/subscribers"), None)
        subs = json.loads(resp["body"])["subscribers"]
        self.assertEqual(len(subs), 2)

    def test_post_subscriber_toggle(self):
        admin.lambda_handler(_event("POST", "/admin/api/subscribers", {"chat_id": "7", "status": "inactive"}), None)
        self.assertEqual(admin.subscribers.registros, [("7", "inactive")])

    def test_queue_purge_llama_purgar_principal(self):
        resp = admin.lambda_handler(_event("POST", "/admin/api/queue/purge", {}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(getattr(admin.queue_stats, "main_purged", False))

    def test_borrar_difusion_detiene_el_plan(self):
        # Borrar una difusión también borra su plan (pid == broadcast_id) → desencola/detiene.
        admin.lambda_handler(_event("POST", "/admin/api/broadcasts/delete", {"id": "b-123"}), None)
        self.assertIn("b-123", admin.plan_store.borrados)     # detuvo el envío (plan)
        self.assertIn("b-123", admin.broadcast_store.borrados)  # y quitó el registro

    def test_borrar_masivo_detiene_cada_plan(self):
        admin.lambda_handler(_event("POST", "/admin/api/broadcasts/delete", {"ids": ["b1", "b2"]}), None)
        self.assertEqual(admin.plan_store.borrados, ["b1", "b2"])
        self.assertEqual(admin.broadcast_store.borrados, ["b1", "b2"])

    def test_broadcasts_delete_all(self):
        # {all:true} borra TODAS las difusiones (y desencola cada plan).
        admin.broadcast_store.items = [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}]
        resp = admin.lambda_handler(_event("POST", "/admin/api/broadcasts/delete", {"all": True}), None)
        self.assertEqual(json.loads(resp["body"])["deleted"], 3)
        self.assertEqual(sorted(admin.broadcast_store.borrados), ["b1", "b2", "b3"])
        self.assertEqual(sorted(admin.plan_store.borrados), ["b1", "b2", "b3"])  # cada uno desencolado

    def test_plans_delete_all(self):
        resp = admin.lambda_handler(_event("POST", "/admin/api/plans/delete", {"all": True}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(admin.plan_store.todos_borrado, 1)

    def test_audit_delete_all(self):
        admin.audit_store.entries = [{"ts": 1, "action": "x", "detail": "", "user": "admin"},
                                     {"ts": 2, "action": "y", "detail": "", "user": "admin"}]
        resp = admin.lambda_handler(_event("POST", "/admin/api/audit/delete", {"all": True}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["deleted"], 2)
        # Las 2 previas se borraron; la propia limpieza queda registrada (bitácora del borrado).
        self.assertEqual([e["action"] for e in admin.audit_store.entries], ["audit:borrar"])

    def test_users_delete_all_protege_admin_y_actual(self):
        # {all:true} borra todos MENOS el admin principal y el usuario actual (sin lockout).
        admin.config.users = {"admin": {"role": "admin"}, "otro": {"role": "user"}, "tercero": {"role": "user"}}
        resp = admin.lambda_handler(_event("POST", "/admin/api/users/delete", {"all": True}), None)
        self.assertEqual(resp["statusCode"], 200)
        # 'admin' es el usuario actual (auth admin:secret123) Y el principal → se conserva.
        self.assertIn("admin", admin.config.users)
        self.assertNotIn("otro", admin.config.users)
        self.assertNotIn("tercero", admin.config.users)

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
        self.assertIn("config:guardar", acciones)  # nomenclatura entidad:accion
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

    def test_userbot_usa_cache_sin_abrir_telethon(self):
        # Refactor sesión-concurrente: si el poller dejó un estado FRESCO en caché, el panel lo sirve
        # SIN abrir su propia conexión Telethon (evita el solape de dos clientes con la misma sesión).
        admin.config.cfg["send_mode"] = "userbot"; admin.config.cfg["telethon_session"] = "SESS"
        admin.config.tg_status = {"connected": True, "me": {"phone": "573001112233"}, "checked_at": int(time.time())}

        def _fail():
            raise AssertionError("el panel NO debe abrir Telethon con la caché fresca")
        admin.wiring.build_telethon_account = _fail
        b = json.loads(admin.lambda_handler(self._ev(), None)["body"])
        self.assertTrue(b["connected"]); self.assertTrue(b["cached"]); self.assertEqual(b["me"]["phone"], "573001112233")

    def test_userbot_cache_vencida_rechequea_y_cachea(self):
        # Caché vieja (poller caído): el panel hace UN chequeo en vivo y refresca la caché.
        admin.config.cfg["send_mode"] = "userbot"; admin.config.cfg["telethon_session"] = "SESS"
        admin.config.tg_status = {"connected": False, "me": None, "checked_at": int(time.time()) - 9999}

        class FakeAcc:
            def estado(self):
                return {"authorized": True, "me": {"id": "1", "name": "Yo", "phone": "57300"}}
        admin.wiring.build_telethon_account = lambda: FakeAcc()
        b = json.loads(admin.lambda_handler(self._ev(), None)["body"])
        self.assertTrue(b["connected"]); self.assertNotIn("cached", b)
        self.assertTrue(admin.config.tg_status["connected"])              # se refrescó la caché
        self.assertGreater(admin.config.tg_status["checked_at"], int(time.time()) - 5)


class PwaTests(unittest.TestCase):
    """PWA: manifest, service worker e iconos. Son PUBLICOS a proposito (el navegador los pide sin
    la cabecera Authorization) y deben resolver bien la raiz REAL del panel, que con API Gateway
    lleva el stage delante (/dev/admin/)."""

    def setUp(self):
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"

    def tearDown(self):
        admin.config = admin.subscribers = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_manifest_publico_y_rutas_con_stage(self):
        resp = admin.lambda_handler(_event("GET", "/admin/manifest.webmanifest", auth=False), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("application/manifest+json", resp["headers"]["Content-Type"])
        m = json.loads(resp["body"])
        self.assertEqual(m["start_url"], "/dev/admin/")
        self.assertEqual(m["scope"], "/dev/")          # cubre /dev/admin y /dev/admin/ (sin barra y con ella)
        self.assertEqual(m["display"], "standalone")
        self.assertTrue(all(i["src"].startswith("/dev/admin/") for i in m["icons"]))
        self.assertIn("maskable", [i["purpose"] for i in m["icons"]])
        self.assertTrue(all(s["url"].startswith("/dev/admin/?tab=") for s in m["shortcuts"]))

    def test_sw_publico_con_ambito_ampliado(self):
        resp = admin.lambda_handler(_event("GET", "/admin/sw.js", auth=False), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("text/javascript", resp["headers"]["Content-Type"])
        # Sin esta cabecera el navegador rechaza un ambito por encima de la ruta del propio sw.js.
        self.assertEqual(resp["headers"]["Service-Worker-Allowed"], "/dev/")
        self.assertIn("no-cache", resp["headers"]["Cache-Control"])
        self.assertIn("RAIZ='/dev/admin/'", resp["body"])
        self.assertNotIn("__VER__", resp["body"])
        self.assertIn("API", resp["body"])             # el fetch handler excluye /api/ de la cache

    def test_sw_nunca_cachea_la_api(self):
        js = admin.lambda_handler(_event("GET", "/admin/sw.js", auth=False), None)["body"]
        self.assertIn("API=RAIZ+'api/'", js)
        self.assertIn("indexOf(API) === 0", js)   # sale de la cache antes de tocar nada

    def test_iconos_publicos_en_base64(self):
        for ruta in ("/admin/icon-192.png", "/admin/icon-512.png",
                     "/admin/icon-maskable-512.png", "/admin/apple-touch-icon.png"):
            resp = admin.lambda_handler(_event("GET", ruta, auth=False), None)
            self.assertEqual(resp["statusCode"], 200, ruta)
            self.assertEqual(resp["headers"]["Content-Type"], "image/png")
            self.assertTrue(resp["isBase64Encoded"], ruta)
            crudo = base64.b64decode(resp["body"])
            self.assertTrue(crudo.startswith(bytes([0x89]) + b"PNG"), ruta)   # PNG de verdad, no un placeholder
            self.assertGreater(len(crudo), 2000, ruta)

    def test_shell_resuelve_la_raiz_real(self):
        body = admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]
        self.assertNotIn("__RAIZ__", body)             # sin sustituir, el navegador pediria /dev/manifest...
        self.assertIn('href="/dev/admin/manifest.webmanifest"', body)
        self.assertIn('href="/dev/admin/apple-touch-icon.png"', body)

    def test_rutas_sin_stage(self):
        # Invocacion directa de la funcion (o API Gateway sin stage): la raiz es /admin/ y el ambito /.
        ev = {"rawPath": "/admin/manifest.webmanifest",
              "requestContext": {"http": {"method": "GET", "path": "/admin/manifest.webmanifest"}},
              "headers": {}, "body": None}
        m = json.loads(admin.lambda_handler(ev, None)["body"])
        self.assertEqual(m["start_url"], "/admin/")
        self.assertEqual(m["scope"], "/")

    def test_version_cambia_con_el_html(self):
        # La version del cache sale del hash del shell: al desplegar un panel distinto cambia el
        # nombre del cache y el SW se renueva solo (dentro de un contenedor se memoiza).
        v = admin._pwa_version()
        self.assertEqual(len(v), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in v))
        self.assertEqual(admin._pwa_version(), v)      # memoizado: mismo shell, misma version
        original, memo = admin._PAGE, admin._PWA_VER
        try:
            admin._PAGE, admin._PWA_VER = original + "<!-- otro deploy -->", ""
            self.assertNotEqual(admin._pwa_version(), v)
        finally:
            admin._PAGE, admin._PWA_VER = original, memo
        self.assertEqual(admin._pwa_version(), v)

    # --- Instalar: un solo botón, y está DENTRO del panel ------------------------------------
    # El de la pantalla de acceso se quitó. El del header se queda: quitar los dos dejaría la PWA
    # sin ninguna forma propia de ofrecerse (y en iOS, sin sitio donde explicar el gesto).

    def test_el_acceso_ya_no_ofrece_instalar(self):
        html = admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]
        acceso = html[html.index('<div id="login">'):html.index('<div id="app">')]
        self.assertIn("doLogin()", acceso)          # vector: el trozo medido es el del acceso
        self.assertNotIn("install", acceso)         # ni el div, ni el botón, ni un resto
        self.assertNotIn("pwaInstalar", acceso)
        self.assertNotIn("Instalar", acceso)

    def test_el_boton_de_instalar_del_panel_sigue_ahi(self):
        # Se quitó SOLO el del acceso. Y su maquinaria no se toca: beforeinstallprompt suele llegar
        # ANTES de entrar y lo que importa es que guarde PROMPT; sin eso el primer clic al botón del
        # header caería en el modal de instrucciones aunque el navegador supiera instalar.
        html = admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]
        # El trozo es el header YA dentro de #app: ahí es donde tiene que estar (en el acceso #app
        # está en display:none, así que un botón suelto en el <script> no valdría de nada).
        header = html[html.index('<div id="app">'):html.index("</header>")]
        boton = re.search(r'<button[^>]*id="install_btn"[^>]*>', header)
        self.assertIsNotNone(boton)
        self.assertIn("hidden", boton.group(0))          # arranca oculto: lo destapa verInstalar
        self.assertIn('onclick="pwaInstalar()"', boton.group(0))
        self.assertIn('aria-label="Instalar Replica como app"', boton.group(0))
        for pieza in ("beforeinstallprompt", "appinstalled", "window.pwaInstalar", "PROMPT"):
            self.assertIn(pieza, html, pieza)
        self.assertGreaterEqual(html.count("verInstalar("), 4)   # sigue teniendo dueño

    def test_no_queda_ninguna_referencia_huerfana_al_boton_del_acceso(self):
        # Ni en el HTML, ni en el CSS, ni en el JS: los tres viajan en la misma respuesta, así que
        # una sola búsqueda los cubre. Una regla de CSS o una rama de JS sin dueño la lee el
        # siguiente y la cree viva.
        html = admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]
        self.assertNotIn("install_login", html)
        # La regla de "ya instalada" nombra solo al que queda (con los dos ids seguiría escondiendo
        # un id que no existe).
        self.assertIn("html.pwa #install_btn{display:none!important}", html)


class WhatsappPairTests(unittest.TestCase):
    """Vinculacion de WhatsApp «desde este telefono» (numero → codigo de 8 digitos → conectado).
    El servicio Node ya expone /pair y /status: el panel solo añade guia, sondeo y auditoria."""

    def setUp(self):
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        admin.audit_store = FakeAudit()
        self._proxy = admin._whatsapp_proxy
        self.llamadas = []
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"

    def tearDown(self):
        admin._whatsapp_proxy = self._proxy
        admin.config = admin.subscribers = admin.audit_store = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def _fake_proxy(self, respuesta=None, status=200):
        def _p(path, timeout=20.0, body=None):
            self.llamadas.append((path, timeout, body))
            cuerpo = respuesta if respuesta is not None else {"pairingCode": "12345678", "number": "573001234567"}
            return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(cuerpo)}
        admin._whatsapp_proxy = _p

    def test_pair_audita_con_el_numero_enmascarado(self):
        # La auditoria la lee cualquier usuario del panel: el numero completo no debe quedar ahi.
        self._fake_proxy()
        resp = admin.lambda_handler(_event("POST", "/admin/api/whatsapp/pair", {"number": "573001234567"}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["pairingCode"], "12345678")
        entrada = admin.audit_store.entries[-1]
        self.assertEqual(entrada["action"], "whatsapp:pair")
        self.assertIn("4567", entrada["detail"])          # solo los ultimos 4 digitos
        self.assertNotIn("573001234567", entrada["detail"])
        self.assertNotIn("57300", entrada["detail"])

    def test_pair_normaliza_el_numero_antes_de_enviarlo(self):
        # El campo acepta lo que el usuario tenga a mano ("+57 300 123 4567"); el servicio Node
        # espera solo digitos.
        self._fake_proxy()
        admin.lambda_handler(_event("POST", "/admin/api/whatsapp/pair", {"number": "+57 300 123-4567"}), None)
        path, _timeout, body = self.llamadas[-1]
        self.assertEqual(path, "/pair")
        self.assertEqual(body, {"number": "573001234567"})

    def test_pair_sin_numero_no_inventa_detalle(self):
        self._fake_proxy({"error": "numero_invalido"}, status=400)
        resp = admin.lambda_handler(_event("POST", "/admin/api/whatsapp/pair", {}), None)
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(admin.audit_store.entries[-1]["detail"], "vincular por código")

    def test_pair_propaga_el_fallo_del_servicio(self):
        # 504 sin_codigo es el caso tipico desde un datacenter: el panel necesita el codigo de error
        # para explicarlo en castellano, no un 200 vacio.
        self._fake_proxy({"error": "sin_codigo", "detalle": "no se generó el código a tiempo"}, status=504)
        resp = admin.lambda_handler(_event("POST", "/admin/api/whatsapp/pair", {"number": "573001234567"}), None)
        self.assertEqual(resp["statusCode"], 504)
        self.assertEqual(json.loads(resp["body"])["error"], "sin_codigo")

    def test_status_expone_lo_que_necesita_el_sondeo(self):
        self._fake_proxy({"connected": True, "contacts": 42, "pairingCode": None})
        resp = admin.lambda_handler(_event("GET", "/admin/api/whatsapp/status"), None)
        self.assertEqual(self.llamadas[-1][0], "/status")
        cuerpo = json.loads(resp["body"])
        self.assertTrue(cuerpo["connected"])
        self.assertEqual(cuerpo["contacts"], 42)

    def test_status_deja_pasar_el_sello_de_build_del_servicio(self):
        # Sin este dato no hay forma de saber desde el panel si el host (Render) ya desplego la
        # ultima version del servicio Node: /status es un proxy, asi que no debe filtrar campos.
        self._fake_proxy({"connected": True, "contacts": 7,
                          "build": {"commit": "329008b", "src": "99be42076190"}})
        resp = admin.lambda_handler(_event("GET", "/admin/api/whatsapp/status"), None)
        self.assertEqual(json.loads(resp["body"])["build"]["commit"], "329008b")

    def test_el_panel_muestra_el_build_del_servicio(self):
        html = admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]
        self.assertIn("s.build", html)
        self.assertIn("' · build '", html)

    def test_panel_trae_el_flujo_guiado_con_el_qr_como_secundario(self):
        html = admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]
        for pieza in ('id="wa_modo"', 'id="wa_pair_num"', 'id="wa_pair_btn"', 'id="wa_pair_box"',
                      'id="wa_code"', 'class="wa-pasos"', "Dispositivos vinculados",
                      "Vincular con número de teléfono", "function waSondear", "function waCopyCode"):
            self.assertIn(pieza, html, pieza)
        # El modo por defecto es el del mismo telefono; el QR queda detras del segundo boton.
        self.assertIn('data-m="tel" class="on"', html)
        self.assertIn('data-m="qr"', html)
        self.assertLess(html.index('data-m="tel"'), html.index('data-m="qr"'))
        self.assertIn("/api/whatsapp/status", html)     # el sondeo confirma solo, sin recargar


class SchedulesCrudTests(unittest.TestCase):
    """CRUD completo de los programados: crear, leer, EDITAR (nuevo), pausar y borrar.
    Editar reusa las mismas validaciones que crear (_campos_schedule): si divergieran, editar
    dejaría pasar cosas que crear rechaza (p. ej. WhatsApp sin lista → toda la agenda)."""

    def setUp(self):
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        admin.queue_stats = FakeQueueStats()
        admin.image_store = FakeImageStore()
        admin.audit_store = FakeAudit()
        admin.broadcast_store = FakeBroadcastStore()
        admin.plan_store = FakePlanStoreAdmin()
        admin.schedule_store = FakeScheduleStore()
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"

    def tearDown(self):
        admin.config = admin.subscribers = admin.queue_stats = admin.image_store = admin.audit_store = None
        admin.broadcast_store = admin.plan_store = admin.schedule_store = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def _crear(self, **extra):
        cuerpo = {"text": "hola", "telegram": True, "telegram_list": "Clientes",
                  "type": "daily", "at": "09:00"}
        cuerpo.update(extra)
        return admin.lambda_handler(_event("POST", "/admin/api/schedules", cuerpo), None)

    def test_crear_diario_y_auditar(self):
        resp = self._crear()
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(len(admin.schedule_store.items), 1)
        self.assertGreater(json.loads(resp["body"])["next_run"], 0)
        self.assertEqual(admin.audit_store.entries[-1]["action"], "schedules:crear")

    def test_crear_rechaza_whatsapp_sin_lista(self):
        resp = self._crear(whatsapp=True, whatsapp_list="")
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("lista", json.loads(resp["body"])["error"])

    def test_update_cambia_el_texto_y_recalcula_el_proximo_envio(self):
        sid = json.loads(self._crear()["body"])["sid"]
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": sid, "text": "texto nuevo"}), None)
        self.assertEqual(resp["statusCode"], 200)
        guardado = admin.schedule_store.items[0]
        self.assertEqual(guardado["text"], "texto nuevo")
        self.assertGreater(json.loads(resp["body"])["next_run"], 0)
        self.assertEqual(admin.audit_store.entries[-1]["action"], "schedules:actualizar")

    def test_update_hereda_lo_que_no_se_manda(self):
        # El panel manda solo lo que cambió: canales, listas y horario deben sobrevivir.
        sid = json.loads(self._crear()["body"])["sid"]
        admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                    {"sid": sid, "name": "Matutino"}), None)
        g = admin.schedule_store.items[0]
        self.assertEqual(g["name"], "Matutino")
        self.assertEqual(g["text"], "hola")
        self.assertTrue(g["telegram"])
        self.assertEqual(g["telegram_list"], "Clientes")
        self.assertEqual(g["at"], "09:00")

    def test_update_no_pisa_el_historial_de_ejecuciones(self):
        sid = json.loads(self._crear()["body"])["sid"]
        admin.schedule_store.items[0].update({"runs": 7, "last_run": 1700000000})
        admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                    {"sid": sid, "text": "otro"}), None)
        _sid, campos = admin.schedule_store.actualizados[-1]
        self.assertNotIn("runs", campos)
        self.assertNotIn("last_run", campos)
        self.assertEqual(admin.schedule_store.items[0]["runs"], 7)

    def test_update_valida_igual_que_crear(self):
        sid = json.loads(self._crear()["body"])["sid"]
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": sid, "whatsapp": True, "whatsapp_list": ""}), None)
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("lista", json.loads(resp["body"])["error"])
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": sid, "text": ""}), None)
        self.assertEqual(resp["statusCode"], 400)
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": sid, "image_url": "http://insegura/x.jpg"}), None)
        self.assertEqual(resp["statusCode"], 400)

    def test_update_de_una_vez_exige_fecha_futura(self):
        futuro = int(time.time()) + 3600
        sid = json.loads(self._crear(type="once", run_at=futuro)["body"])["sid"]
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": sid, "run_at": int(time.time()) - 60}), None)
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("futuras", json.loads(resp["body"])["error"])

    def test_update_de_una_vez_conserva_la_fecha_si_no_se_manda(self):
        futuro = int(time.time()) + 3600
        sid = json.loads(self._crear(type="once", run_at=futuro)["body"])["sid"]
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": sid, "text": "cambiado"}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(admin.schedule_store.items[0]["next_run"], futuro)

    def test_update_de_semanal_exige_dias(self):
        sid = json.loads(self._crear()["body"])["sid"]
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": sid, "type": "weekly", "days": []}), None)
        self.assertEqual(resp["statusCode"], 400)
        ok = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                         {"sid": sid, "type": "weekly", "days": [0, 3]}), None)
        self.assertEqual(ok["statusCode"], 200)
        self.assertEqual(admin.schedule_store.items[0]["days"], [0, 3])

    def test_update_sid_desconocido_400(self):
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                           {"sid": "s-inexistente", "text": "x"}), None)
        self.assertEqual(resp["statusCode"], 400)
        resp = admin.lambda_handler(_event("POST", "/admin/api/schedules/update", {"text": "x"}), None)
        self.assertEqual(resp["statusCode"], 400)

    def test_update_puede_reactivar(self):
        sid = json.loads(self._crear()["body"])["sid"]
        admin.lambda_handler(_event("POST", "/admin/api/schedules/update",
                                    {"sid": sid, "enabled": False}), None)
        self.assertFalse(admin.schedule_store.items[0]["enabled"])

    def test_toggle_audita_con_verbo_propio(self):
        sid = json.loads(self._crear()["body"])["sid"]
        admin.lambda_handler(_event("POST", "/admin/api/schedules/toggle", {"sid": sid, "enabled": False}), None)
        self.assertEqual(admin.audit_store.entries[-1]["action"], "schedules:pausar")
        admin.lambda_handler(_event("POST", "/admin/api/schedules/toggle", {"sid": sid, "enabled": True}), None)
        self.assertEqual(admin.audit_store.entries[-1]["action"], "schedules:activar")


class UsersUpdateTests(unittest.TestCase):
    """`POST /api/users/update`: editar el correo y RESTABLECER la contraseña de otra persona
    (lo que faltaba del CRUD de usuarios: antes solo se podía crear, cambiar rol y borrar)."""

    def setUp(self):
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        admin.audit_store = FakeAudit()
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"
        admin._AUTH["fails"] = 0
        admin._AUTH["locked_until"] = 0.0
        admin.config.users = {
            "ana": {"email": "ana@x.com", "hash": admin.auth_dom.hash_password("clave1234"), "role": "user"},
        }

    def tearDown(self):
        admin.config = admin.subscribers = admin.audit_store = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def _ev(self, body, user="admin", pw="secret123"):
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {
            "rawPath": "/dev/admin/api/users/update",
            "requestContext": {"http": {"method": "POST", "path": "/dev/admin/api/users/update"}},
            "headers": {"authorization": f"Basic {token}"},
            "body": json.dumps(body),
        }

    def test_cambia_el_correo(self):
        r = admin.lambda_handler(self._ev({"username": "ana", "email": "nueva@x.com"}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(admin.config.users["ana"]["email"], "nueva@x.com")
        self.assertEqual(admin.audit_store.entries[-1]["action"], "users:actualizar")
        self.assertIn("correo", admin.audit_store.entries[-1]["detail"])

    def test_restablece_la_contrasena_sin_pedir_la_actual(self):
        r = admin.lambda_handler(self._ev({"username": "ana", "password": "otraclave99"}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertTrue(admin._verificar("ana", "otraclave99"))
        self.assertFalse(admin._verificar("ana", "clave1234"))
        self.assertIn("contraseña", admin.audit_store.entries[-1]["detail"])

    def test_no_registra_la_contrasena_en_la_auditoria(self):
        admin.lambda_handler(self._ev({"username": "ana", "password": "otraclave99"}), None)
        self.assertNotIn("otraclave99", admin.audit_store.entries[-1]["detail"])

    def test_conserva_el_rol_y_los_datos_por_usuario(self):
        admin.config.users["ana"]["excluded"] = ["123"]
        admin.lambda_handler(self._ev({"username": "ana", "email": "n@x.com"}), None)
        self.assertEqual(admin.config.users["ana"]["role"], "user")
        self.assertEqual(admin.config.users["ana"]["excluded"], ["123"])

    def test_rechaza_contrasena_corta(self):
        r = admin.lambda_handler(self._ev({"username": "ana", "password": "corta"}), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertTrue(admin._verificar("ana", "clave1234"))  # no la cambió

    def test_rechaza_usuario_inexistente_y_cuerpo_vacio(self):
        self.assertEqual(admin.lambda_handler(self._ev({"username": "nadie", "email": "x@y.com"}), None)["statusCode"], 400)
        self.assertEqual(admin.lambda_handler(self._ev({"username": "ana"}), None)["statusCode"], 400)

    def test_un_usuario_normal_no_puede_editar_a_otros(self):
        r = admin.lambda_handler(self._ev({"username": "ana", "password": "otraclave99"},
                                          user="ana", pw="clave1234"), None)
        self.assertEqual(r["statusCode"], 403)
        self.assertTrue(admin._verificar("ana", "clave1234"))

    def test_users_get_dice_quien_es_el_principal(self):
        ev = {
            "rawPath": "/dev/admin/api/users",
            "requestContext": {"http": {"method": "GET", "path": "/dev/admin/api/users"}},
            "headers": {"authorization": "Basic " + base64.b64encode(b"admin:secret123").decode()},
            "body": None,
        }
        cuerpo = json.loads(admin.lambda_handler(ev, None)["body"])
        # El panel ya no adivina que el principal se llama "admin": lo dice el backend.
        self.assertEqual(cuerpo["principal"], "admin")


class NomenclaturaTests(unittest.TestCase):
    """Los nombres tienen que significar lo mismo en todas partes: la clave de auditoría usa la
    MISMA palabra que la ruta HTTP (`entidad:accion`) y el panel sabe traducir todas."""

    ENTIDADES = {"users", "config", "patterns", "subscribers", "broadcasts", "plans", "schedules",
                 "queue", "dlq", "audit", "telethon", "whatsapp"}

    @classmethod
    def setUpClass(cls):
        ruta = os.path.join(os.path.dirname(__file__), "..", "src", "lambda", "entrypoints", "admin.py")
        with open(ruta, encoding="utf-8") as fh:
            cls.codigo = fh.read()
        cls.acciones = []
        for nodo in ast.walk(ast.parse(cls.codigo)):
            if not (isinstance(nodo, ast.Call) and getattr(nodo.func, "id", "") == "_audit" and nodo.args):
                continue
            primero = nodo.args[0]
            ramas = [primero] if not isinstance(primero, ast.IfExp) else [primero.body, primero.orelse]
            for rama in ramas:
                cls.acciones.append(rama.value if isinstance(rama, ast.Constant) else None)

    def test_hay_acciones_y_todas_son_literales(self):
        self.assertGreater(len(self.acciones), 20)
        self.assertNotIn(None, self.acciones)  # una acción calculada no se puede auditar ni traducir

    def test_toda_accion_es_entidad_dos_puntos_verbo(self):
        for a in self.acciones:
            self.assertRegex(a, r"^[a-z]+:[a-z-]+$", a)
            self.assertIn(a.split(":")[0], self.ENTIDADES, a)

    def test_la_entidad_de_la_accion_existe_como_ruta(self):
        for a in self.acciones:
            entidad = a.split(":")[0]
            self.assertIn(f'"/api/{entidad}', self.codigo, entidad)

    def _bloque(self, nombre):
        i = self.codigo.index("const " + nombre + "={")
        return self.codigo[i:self.codigo.index("};", i)]

    def test_el_panel_traduce_todas_las_acciones(self):
        ents, vrbs = self._bloque("ACC_ENT"), self._bloque("ACC_VRB")
        for a in self.acciones:
            e, v = a.split(":", 1)
            self.assertIn(e + ":", ents, a)
            self.assertTrue(v + ":" in vrbs or "'" + v + "':" in vrbs, a)

    def test_las_rutas_de_escritura_usan_los_mismos_verbos(self):
        # Forma canónica: POST /api/<entidad>/update y /delete (más los verbos propios ya existentes).
        rutas = set(re.findall(r'sub == "(/api/[a-z/\-]+)"', self.codigo))
        for esperada in ("/api/users/update", "/api/users/delete",
                         "/api/schedules/update", "/api/schedules/delete",
                         "/api/broadcasts/delete", "/api/plans/delete", "/api/audit/delete"):
            self.assertIn(esperada, rutas, esperada)


class PanelCrudTests(unittest.TestCase):
    """El CRUD no existe si no se puede hacer desde el panel: cada operación nueva tiene su botón."""

    @classmethod
    def setUpClass(cls):
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        admin.audit_store = FakeAudit()
        cls.html = admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]

    @classmethod
    def tearDownClass(cls):
        admin.config = admin.subscribers = admin.audit_store = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_editar_un_programado(self):
        for pieza in ("function sgEdit", 'onclick="sgEdit(', "/api/schedules/update",
                      'id="se_text"', 'id="se_type"', 'id="se_days_box"', "schedLocalInput"):
            self.assertIn(pieza, self.html, pieza)

    def test_editar_un_usuario(self):
        for pieza in ("function editUser", 'onclick="editUser(', "/api/users/update",
                      'id="eu_email"', 'id="eu_pw"'):
            self.assertIn(pieza, self.html, pieza)

    def test_renombrar_una_lista(self):
        for pieza in ("function renameList", 'onclick="renameList(', "auto_'+ch+'_list"):
            self.assertIn(pieza, self.html, pieza)
        # Renombrar arrastra las referencias de los programados (si no, se quedan sin destino).
        self.assertIn("/api/schedules/update", self.html)

    def test_reincluir_un_solo_auto_excluido(self):
        self.assertIn("async function clearBlocked(id)", self.html)
        self.assertIn("clearBlocked(\\'", self.html)   # botón por contacto (con su jid)
        self.assertIn("clearBlocked()", self.html)     # y el de "reincluir a todos"
        self.assertIn("blk-row", self.html)

    def test_la_auditoria_se_lee_en_espanol(self):
        for pieza in ("const ACC_ENT=", "const ACC_VRB=", "function accLabel"):
            self.assertIn(pieza, self.html, pieza)

    def test_el_principal_no_se_adivina(self):
        self.assertIn("USR_PRINCIPAL", self.html)
        self.assertIn("r.principal", self.html)

    def test_enter_en_un_textarea_no_envia_el_modal(self):
        self.assertIn("TEXTAREA", self.html)


# --- Ver la contraseña: el ojo de cada campo secreto -----------------------------------------
# El ojo NO viene en el HTML: lo engancha el JS del final de la página sobre cada
# input[type=password] que ya esté cargado. Por eso estas pruebas van en dos niveles:
#   · PasswordRevealTests — mide la PÁGINA SERVIDA: cuántos campos de contraseña hay y que el
#     enganche sea uno solo y sin filtro (así alcanza a los diez). Python puro, corre siempre.
#   · PasswordRevealDomTests — EJECUTA ese mismo bloque, recortado de la página que sirve la
#     Lambda y sin retocarlo, contra un DOM mínimo en node. Es el único nivel que puede afirmar
#     «el aria-label cambia con el estado»: en el HTML servido todavía no hay ningún aria-label
#     que mirar, ni ningún botón que contar.

_SELECTOR = "querySelectorAll('input[type=password]')"

# Elementos sin cierre: si se apilaran, el árbol se quedaría colgando dentro del primer <input>.
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}


class _Nodo:
    __slots__ = ("tag", "attrs", "hijos")

    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.hijos = tag, dict(attrs), []


class _Arbol(HTMLParser):
    """Árbol mínimo de la página. Hace falta el árbol y no un regex porque lo que se comprueba
    es la VECINDAD: qué elemento va justo antes de cada campo (de ahí saca el JS la <label> que
    tiene que emparejar antes de envolver el input)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = _Nodo("#raiz")
        self.pila = [self.raiz]

    def handle_starttag(self, tag, attrs):
        nodo = _Nodo(tag, attrs)
        self.pila[-1].hijos.append(nodo)
        if tag not in _VOID:
            self.pila.append(nodo)

    def handle_startendtag(self, tag, attrs):
        self.pila[-1].hijos.append(_Nodo(tag, attrs))

    def handle_endtag(self, tag):
        # Se cierra hasta la ÚLTIMA que coincida, no a ciegas contra el tope: un cierre suelto en
        # una página de miles de líneas no debe descolgar el árbol y dejar campos sin vecino.
        for i in range(len(self.pila) - 1, 0, -1):
            if self.pila[i].tag == tag:
                del self.pila[i:]
                break


def _panel_html():
    """La página tal como la pide el navegador: el shell es público (se sirve sin credenciales)."""
    return admin.lambda_handler(_event("GET", "/admin", auth=False), None)["body"]


def _campos_password(pagina):
    """[{id, prev}] de cada input[type=password] que la página YA trae (no los que crea el JS)."""
    arbol = _Arbol()
    arbol.feed(pagina)
    campos = []

    def recorrer(nodo):
        previo = None
        for hijo in nodo.hijos:
            if hijo.tag == "input" and (hijo.attrs.get("type") or "").lower() == "password":
                campos.append({"id": hijo.attrs.get("id"),
                               "prev": None if previo is None
                               else {"tag": previo.tag, "for": previo.attrs.get("for")}})
            recorrer(hijo)
            previo = hijo

    recorrer(arbol.raiz)
    return campos


def _cuerpo_js(pagina, nombre):
    """Una función JS de primer nivel de la página, recortada hasta la siguiente.

    Se mide el TROZO y no la página entera porque lo que importa es DÓNDE está la llamada: un
    pwTapar() en cualquier otro sitio no salva a fpSend.
    """
    i = pagina.index("function " + nombre + "(")
    sigue = [p for p in (pagina.find("\nfunction ", i + 1),
                         pagina.find("\nasync function ", i + 1)) if p > 0]
    return pagina[i:min(sigue) if sigue else len(pagina)]


def _sin_comentarios_js(cuerpo):
    """Un trozo de JS sin sus comentarios.

    Existe porque la comprobacion de al lado busca una LLAMADA, y un comentario que la
    mencione la satisface sola: es el fallo que este proyecto repite. No tokeniza (eso
    seria una cuarta copia de un tokenizador), pero tampoco recorta a ciegas: solo trata
    un // como comentario si lo que va delante en la linea tiene las comillas cerradas,
    asi que un // dentro de una cadena no se lleva media linea de codigo.

    Y su correccion no se supone: cada prueba que lo usa exige que un centinela del
    comentario haya DESAPARECIDO. Si esto dejara de quitar, se pone rojo."""
    cuerpo = re.sub(r"/\*.*?\*/", " ", cuerpo, flags=re.S)
    fuera = []
    for linea in cuerpo.split(chr(10)):
        i = linea.find("//")
        while i >= 0:
            antes = linea[:i]
            if antes.count("'") % 2 == 0 and antes.count('\"') % 2 == 0:
                linea = antes
                break
            i = linea.find("//", i + 2)
        fuera.append(linea)
    return chr(10).join(fuera)


def _bloque_del_ojo(pagina):
    """El IIFE que engancha los ojos, recortado del <script> que sirve la Lambda."""
    script = re.findall(r"<script>(.*?)</script>", pagina, re.S)[-1]
    return script[script.rindex("(function(){", 0, script.index(_SELECTOR)):]


class PasswordRevealTests(unittest.TestCase):
    """Los campos secretos del panel llevan un ojo para ver lo que se escribe. Aquí se mide la
    página: cuántos campos hay y que el enganche sea único y sin filtro. El comportamiento del
    ojo (el botón, su type y el aria-label al alternar) lo prueba PasswordRevealDomTests."""

    # Los diez de la página, en orden de aparición. La lista es EXACTA a propósito: un campo
    # nuevo rompe la prueba y obliga a decidir si lleva ojo, en vez de colarse sin él.
    IDS = ["lp", "fp_new", "bot_token", "tl_password", "telethon_session", "whatsapp_token",
           "usr_new_pw", "resend_api_key", "cp_cur", "cp_new"]

    @classmethod
    def setUpClass(cls):
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        cls.html = _panel_html()
        cls.campos = _campos_password(cls.html)

    @classmethod
    def tearDownClass(cls):
        admin.config = admin.subscribers = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_la_prueba_mide_la_pagina_de_verdad(self):
        # Vector: si esto dejara de medir la página (una ruta que no es, un 401, la plantilla sin
        # sustituir), lo demás pasaría en verde sobre la nada. Falla aquí primero.
        self.assertGreater(len(self.html), 50000)
        self.assertIn('<div id="login">', self.html)
        self.assertIn('id="lp"', self.html)
        self.assertIn('onclick="doLogin()"', self.html)
        self.assertNotIn("__RAIZ__", self.html)      # está SERVIDA, no es el literal sin sustituir
        self.assertTrue(self.campos)                 # y el parser encontró campos, no cero

    def test_los_diez_campos_de_contrasena_siguen_siendo_diez(self):
        self.assertEqual([c["id"] for c in self.campos], self.IDS)
        self.assertEqual(len(self.campos), 10)

    def test_un_solo_enganche_sin_filtro_los_alcanza_a_todos(self):
        bloque = _bloque_del_ojo(self.html)
        # Un único punto de enganche: con el patrón copiado a mano en algún campo habría dos
        # sitios que mantener, y el segundo se quedaría atrás (empezando por el volver a tapar).
        self.assertEqual(self.html.count(_SELECTOR), 1)
        self.assertEqual(self.html.count("'pw-ojo'"), 1)
        # Y recorre TODO lo que devuelve el selector: sin recortes y sin buscar ids uno a uno
        # (con una lista de ids, un campo nuevo se quedaría sin ojo en silencio).
        self.assertIn(_SELECTOR + ".forEach", bloque)
        self.assertNotIn("getElementById", bloque)
        self.assertNotIn(".slice(", bloque)

    def test_el_acceso_no_hereda_la_contrasena_de_quien_acaba_de_salir(self):
        # Tapar el campo esconde el estado, no el secreto: el valor sobrevivía a logout(), así que
        # con el ojo el siguiente en sentarse LEÍA la contraseña de quien acababa de salir.
        cuerpo = _cuerpo_js(self.html, "logout")
        self.assertIn("$('lp').value=''", cuerpo)
        self.assertIn("$('lu').value=''", cuerpo)   # y sin usuario no se vuelve a entrar con Enter

    def test_los_bloques_de_secretos_se_tapan_antes_de_volver_a_mostrarse(self):
        # Estos tres no cambian de pantalla: ocultan y vuelven a mostrar un bloque con display, y
        # reaparece tal como quedó. Ninguna de las cinco funciones envueltas pasa por ahí.
        send = _cuerpo_js(self.html, "fpSend")
        self.assertLess(send.index("pwTapar()"), send.index("$('fp_step2').style.display='block'"))
        signin = _cuerpo_js(self.html, "tlSignIn")
        self.assertLess(signin.index("pwTapar()"),
                        signin.index("$('tl_pwd_wrap').style.display='block'"))
        reset = _cuerpo_js(self.html, "fpReset")
        self.assertIn("pwTapar()", reset)
        # Al acabar bien se vacía el paso 2: si no, pedir otro código (lo normal cuando el primero
        # no llega) lo vuelve a mostrar con la contraseña nueva dentro, y el ojo la lee.
        self.assertIn("$('fp_new').value=''", reset)
        self.assertIn("$('fp_code').value=''", reset)
        # Solo en la rama de éxito: en la de error eso es lo que la persona está escribiendo.
        self.assertLess(reset.index("$('fp_new').value=''"), reset.index("}catch(e){"))

    def test_los_dos_sitios_que_crean_un_modal_llaman_al_enganche(self):
        # LO QUE FALTABA. El arnés de node llama a `pwEnganchar` él mismo, así que prueba
        # que el ayudante funciona y NO que alguien lo use: quitar la llamada de `dsModal`
        # dejaba la suite en verde y #eu_pw sin ojo, o sea el hueco original intacto.
        # Son los dos únicos sitios del panel que meten un modal en el documento; el
        # segundo no tiene campos secretos hoy, y por eso mismo se fija: quien añada uno
        # no va a leer una nota que pida acordarse.
        for nombre in ("dsModal", "listMembers"):
            cuerpo = _sin_comentarios_js(_cuerpo_js(self.html, nombre))
            # El vector del quitador: el comentario de esa misma función tiene que haberse
            # ido. Si no, lo que sigue lo podría estar cumpliendo la prosa.
            self.assertNotIn("pwEnganchar` con su", cuerpo, nombre)
            self.assertNotIn("el enganche va aquí", cuerpo, nombre)
            self.assertIn("document.body.appendChild(ov)", cuerpo, nombre)   # sigue siendo el trozo bueno
            # Y la llamada, con SU nodo: con `document` se re-envolverían los diez campos
            # de la página y cada uno acabaría con dos ojos.
            self.assertIn("pwEnganchar(d)", cuerpo, nombre)
            self.assertNotIn("pwEnganchar(document)", cuerpo, nombre)

    def test_el_enganche_se_llama_antes_de_poner_el_foco(self):
        # Envolver mueve el input dentro de un <span>. Si el modal ya había enfocado el
        # campo, moverlo le quita el foco y quien abre «Editar usuario» se encuentra
        # escribiendo en ninguna parte.
        cuerpo = _sin_comentarios_js(_cuerpo_js(self.html, "dsModal"))
        self.assertLess(cuerpo.index("pwEnganchar(d)"), cuerpo.index(".focus()"))

    def test_el_details_de_la_stringsession_vuelve_a_tapar_al_abrir_y_cerrar(self):
        # Cerrarlo creyendo que así se guarda no tapaba nada: al volver a abrirlo la StringSession
        # seguía entera a la vista, y su valor solo lo borra saveAccount(). Da acceso TOTAL a la
        # cuenta de Telegram, así que es el peor de los diez para dejarlo destapado.
        det = self.html[self.html.index('<details style="margin-top:14px"'):]
        det = det[:det.index("</details>")]
        self.assertIn('id="telethon_session"', det)          # vector: es ESE details, no otro
        self.assertRegex(det, r'<details[^>]*ontoggle="[^"]*pwTapar\(\)')

    def test_el_patron_esta_escrito_una_vez(self):
        # Suelo mínimo por si falta node y PasswordRevealDomTests se salta entera: el botón se crea
        # en un solo sitio, con type explícito, y su etiqueta accesible se CALCULA (no es un literal).
        bloque = _bloque_del_ojo(self.html)
        self.assertEqual(bloque.count("createElement('button')"), 1)
        self.assertRegex(bloque, r"\.type\s*=\s*'button'")
        self.assertIn("'aria-label'", bloque)
        self.assertIn("Mostrar ", bloque)
        self.assertIn("Ocultar ", bloque)
        # El <span> que lo envuelve tiene su CSS: sin él el ojo no queda DENTRO del campo.
        self.assertIn(".pw{position:relative", self.html)
        self.assertIn(".pw>.pw-ojo{position:absolute", self.html)


# Arnés de PasswordRevealDomTests. Va aquí, en el fichero de la prueba, por lo mismo que el panel
# lleva su JS dentro del .py: un fichero suelto al lado se queda sin dueño. Las afirmaciones NO
# están aquí (se hacen en Python, sobre el JSON que esto imprime); esto solo mueve el DOM.
_ARNES_OJO = r"""/* DOM mínimo + el bloque del ojo TAL COMO LO SIRVE la Lambda. Entrada: la ruta de un JSON
   {campos, bloque} en argv[2]. Salida: una línea de JSON con el estado del DOM en cada momento.
   El DOM es de mentira a propósito: jsdom no está instalado y no se va a colgar una dependencia
   de npm de la suite de Python (lo de node ya vive en scripts/revisar_js_panel.py). Implementa
   solo lo que el bloque usa, y el selector está acotado a mano: si el bloque pasara a buscar otra
   cosa, esto REVIENTA en vez de devolver una lista vacía y dejar la prueba verde sobre cero. */
const fs = require('fs');

class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null; this._at = {}; }
  appendChild(n) { if (n.parentNode) n.parentNode._quitar(n); n.parentNode = this; this.children.push(n); return n; }
  insertBefore(n, ref) {
    if (n.parentNode) n.parentNode._quitar(n);
    n.parentNode = this;
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, n);
    return n;
  }
  _quitar(n) { const i = this.children.indexOf(n); if (i >= 0) this.children.splice(i, 1); }
  setAttribute(k, v) { this._at[k] = String(v); }
  getAttribute(k) { return k in this._at ? this._at[k] : null; }
  get previousElementSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.children.indexOf(this);
    return i > 0 ? this.parentNode.children[i - 1] : null;
  }
  // La definición del navegador, no una bandera: sube por parentNode y mira si llega a la
  // raíz. Hace falta porque `tapar()` poda por aquí los campos de un modal ya cerrado, y sin
  // esto `!undefined` es true y la poda se llevaba los DIEZ pares — pasó, y estas dos líneas
  // son lo que separa «la poda funciona» de «la maqueta no sabe contestar».
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === raiz; }
  // El enganche de un modal se llama con el nodo del modal como raíz, así que baja por los
  // descendientes: tras envolver, el input ya no es hijo directo de nadie conocido.
  querySelectorAll(sel) {
    if (sel.replace(/\s+/g, '') !== 'input[type=password]') throw new Error('selector inesperado: ' + sel);
    const out = [];
    (function bajar(n) { for (const h of n.children) {
      if (h.tagName === 'INPUT' && h.type === 'password') out.push(h); bajar(h);
    } })(this);
    return out;
  }
}

const entrada = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const raiz = new El('div');
const inputs = [], vecinos = {};
for (const c of entrada.campos) {
  // Los campos son los REALES: el id y el elemento de delante salen de parsear la página servida.
  if (c.prev) {
    const p = new El(c.prev.tag);
    if (c.prev.for) p.htmlFor = c.prev.for;
    raiz.appendChild(p);
    vecinos[c.id] = p;
  }
  const inp = new El('input');
  inp.id = c.id; inp.type = 'password'; inp.value = 'valor-' + c.id;
  raiz.appendChild(inp); inputs.push(inp);
}

const document = {
  createElement: t => new El(t),
  querySelectorAll: sel => {
    if (sel.replace(/\s+/g, '') !== 'input[type=password]') throw new Error('selector inesperado: ' + sel);
    return raiz.children.filter(e => e.tagName === 'INPUT' && e.type === 'password');
  },
};
// Las cinco funciones que el bloque envuelve para volver a tapar. doLogin es async y se traga su
// error, igual que la de verdad, y apunta lo que VE cuando la llaman: ahí está la diferencia entre
// tapar antes de llamarla y taparlo cuando la promesa se asienta — segundos de contraseña en
// pantalla que un tipo final 'password' no distingue. El valor también, para ver que enmascarar no
// le quita la credencial (la de verdad lee $('lp').value en su primera línea).
const llamadas = [];
let visto = null;
const window = {
  doLogin: async function () {
    llamadas.push('doLogin');
    visto = { tipos: inputs.map(i => i.type), valor: inputs[0].value };
    await null; return 'ok';
  },
  logout: function () { llamadas.push('logout'); },
  showTab: function () { llamadas.push('showTab'); },
  showSub: function () { llamadas.push('showSub'); },
  fpToggle: function () { llamadas.push('fpToggle'); },
};
new Function('window', 'document', entrada.bloque)(window, document);

const ojo = inp => inp.parentNode.children.filter(e => e.tagName === 'BUTTON')[0] || null;
const foto = () => inputs.map(i => {
  const b = ojo(i);
  return {
    id: i.id, valor: i.value, tipo: i.type,
    envuelto: i.parentNode !== raiz && i.parentNode.className === 'pw',
    boton: !b ? null : {
      tag: b.tagName, type: b.type, clase: b.className, aria: b.getAttribute('aria-label'),
      title: b.title, svg: /<svg/.test(b.innerHTML || ''), tachado: /M1 1l22 22/.test(b.innerHTML || ''),
    },
  };
});
const clic = () => inputs.forEach(i => {
  // Si a un campo le faltara el ojo, el arnés no revienta: sigue y deja que la foto llegue a
  // Python, donde la prueba que cuenta los diez dice CUÁL se quedó sin él.
  const b = ojo(i);
  if (b) b.onclick();
});

const out = { inicial: foto(), etiquetas: {}, tapa: {} };
for (const id in vecinos) {
  const inp = inputs.filter(i => i.id === id)[0];
  out.etiquetas[id] = { tag: vecinos[id].tagName, for: vecinos[id].htmlFor || null,
                        aun_hermano: inp.previousElementSibling === vecinos[id] };
}
clic(); out.tras_un_clic = foto();
clic(); out.tras_dos_clics = foto();
for (const salida of ['logout', 'showTab', 'showSub', 'fpToggle']) {
  clic();                          // los diez a la vista
  window[salida]('x');
  out.tapa[salida] = inputs.map(i => i.type);
}
// pwTapar: el mismo tapar, con nombre, para los bloques que se vuelven a mostrar sin cambiar de
// pantalla (#fp_step2, #tl_pwd_wrap, el <details> de la StringSession).
clic();                            // los diez a la vista
out.expuesta = typeof window.pwTapar === 'function';
if (out.expuesta) window.pwTapar();
out.tras_pwtapar = inputs.map(i => i.type);
// EL MODAL. #eu_pw nace cuando se abre «Editar usuario», o sea después de la carga: el
// recorrido inicial no lo ve. Aquí se engancha un nodo con un campo secreto dentro, se
// destapa, y se QUITA del documento — que es lo que hace `ov.remove()` al cerrar el modal.
out.modal = { engancha: typeof window.pwEnganchar === 'function' };
if (out.modal.engancha) {
  const ov = new El('div');
  const mp = new El('input'); mp.id = 'eu_pw'; mp.type = 'password'; mp.value = 'clave-nueva';
  ov.appendChild(mp); raiz.appendChild(ov);
  window.pwEnganchar(ov);
  const mb = ojo(mp);
  out.modal.tiene_ojo = !!mb;
  out.modal.aria = mb ? mb.getAttribute('aria-label') : null;
  out.modal.no_re_envuelve = inputs.every(i => i.parentNode.children
                                                  .filter(e => e.tagName === 'BUTTON').length === 1);
  if (mb) mb.onclick();
  out.modal.destapado = mp.type;
  clic();                          // y los diez de la página también a la vista
  raiz._quitar(ov); ov.parentNode = null;   // se cierra el modal
  window.pwTapar();
  // EL VECTOR: los diez siguen tapándose. Es exactamente lo que la poda rompió la primera
  // vez, y sin esta línea el arnes no habría notado nada.
  out.modal.los_diez = inputs.map(i => i.type);
  // Y el del modal ya no está en la lista: nadie toca un nodo que no está en el documento.
  out.modal.el_del_modal = mp.type;
}
clic(); window.pwTapar();          // se deja el DOM tapado para lo que sigue
const antes = llamadas.length;
ojo(inputs[0]).onclick();
out.clic_no_envia = llamadas.length === antes;
ojo(inputs[0]).onclick();          // y se queda tapado otra vez
clic();                            // los diez destapados justo antes de entrar
const r = window.doLogin();
out.login = { dentro: visto, al_lanzar: inputs.map(i => i.type) };
Promise.resolve(r).then(() => {
  out.login.al_terminar = inputs.map(i => i.type);
  out.llamadas = llamadas;
  process.stdout.write(JSON.stringify(out) + '\n');
});
"""


@unittest.skipUnless(shutil.which("node"), "sin node no se puede ejecutar el JS del panel")
class PasswordRevealDomTests(unittest.TestCase):
    """Ejecuta el bloque del ojo (el que sirve la Lambda, recortado sin tocarlo) contra un DOM
    mínimo en node y comprueba el COMPORTAMIENTO: el botón que aparece en cada campo, su type y
    el aria-label al alternar. Nada de eso se puede leer en el HTML: ahí solo está el JS.
    Los campos no son inventados: ids y vecinos salen de parsear la página servida.
    Si falta node la clase se salta entera; el suelo lo cubre PasswordRevealTests (Python puro)."""

    @classmethod
    def setUpClass(cls):
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"
        admin.config = FakeConfig()
        admin.subscribers = FakeSubs()
        pagina = _panel_html()
        campos = _campos_password(pagina)
        cls.ids = [c["id"] for c in campos]
        entrada = json.dumps({"campos": campos, "bloque": _bloque_del_ojo(pagina)})
        with tempfile.TemporaryDirectory() as tmp:
            # .cjs: el bloque es un script de navegador, no un módulo ES (con .js, un package.json
            # con "type":"module" en cualquier carpeta de arriba lo rompería).
            arnes = os.path.join(tmp, "arnes.cjs")
            datos = os.path.join(tmp, "entrada.json")
            with open(arnes, "w", encoding="utf-8") as fh:
                fh.write(_ARNES_OJO)
            with open(datos, "w", encoding="utf-8") as fh:
                fh.write(entrada)
            proc = subprocess.run(["node", arnes, datos], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        # Si el arnés no llega a ejecutar, la clase FALLA: saltarla sería dejar de medir sin avisar.
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AssertionError("el arnés no ejecutó el bloque: " + (proc.stderr or "sin salida")[:800])
        cls.dom = json.loads(proc.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        admin.config = admin.subscribers = None
        os.environ.pop("ADMIN_USER", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_los_diez_campos_acaban_con_su_ojo_dentro(self):
        campos = self.dom["inicial"]
        self.assertEqual([c["id"] for c in campos], self.ids)
        self.assertEqual(len(campos), 10)
        # Diez, contados: «al menos uno» dejaría pasar justo el fallo que importa (un campo suelto
        # que se quedó sin ojo).
        self.assertEqual(sum(1 for c in campos if c["boton"]), 10)
        for c in campos:
            self.assertTrue(c["envuelto"], c["id"])           # el input queda DENTRO del <span class="pw">
            self.assertEqual(c["boton"]["clase"], "pw-ojo", c["id"])
            self.assertTrue(c["boton"]["svg"], c["id"])        # icono, no un emoji que cambia de tamaño
            self.assertEqual(c["tipo"], "password", c["id"])   # y arranca tapado

    def test_cada_ojo_es_un_boton_type_button(self):
        # Un <button> sin type es un submit: el día que el acceso vaya dentro de un <form> (lo
        # normal para que los gestores de contraseñas lo reconozcan), el ojo haría de «Entrar».
        for c in self.dom["inicial"]:
            self.assertIsNotNone(c["boton"], c["id"])
            self.assertEqual(c["boton"]["tag"], "BUTTON", c["id"])
            self.assertEqual(c["boton"]["type"], "button", c["id"])

    def test_el_aria_label_y_el_icono_cambian_con_el_estado(self):
        # Un botón que no dice en qué estado está no sirve con lector de pantalla: se oiría
        # «mostrar la contraseña» con la contraseña ya a la vista.
        for antes, uno, dos in zip(self.dom["inicial"], self.dom["tras_un_clic"],
                                   self.dom["tras_dos_clics"]):
            self.assertTrue(antes["boton"]["aria"].startswith("Mostrar "), antes["id"])
            self.assertEqual(antes["boton"]["title"], antes["boton"]["aria"], antes["id"])
            self.assertFalse(antes["boton"]["tachado"], antes["id"])
            self.assertEqual(uno["tipo"], "text", uno["id"])
            self.assertTrue(uno["boton"]["aria"].startswith("Ocultar "), uno["id"])
            self.assertEqual(uno["boton"]["title"], uno["boton"]["aria"], uno["id"])
            self.assertTrue(uno["boton"]["tachado"], uno["id"])       # el icono cambia con la etiqueta
            self.assertEqual(dos["tipo"], "password", dos["id"])      # y vuelve a tapar
            self.assertEqual(dos["boton"]["aria"], antes["boton"]["aria"], dos["id"])
            self.assertEqual(dos["valor"], antes["valor"], dos["id"])   # alternar no pierde lo escrito

    def test_a_un_token_no_se_le_llama_contrasena(self):
        # Cuatro de los diez no guardan la contraseña de la persona, sino un secreto que se PEGA.
        # Decirle «la contraseña» a un token, al lector de pantalla, es mentirle.
        nombres = {"bot_token": "el token", "whatsapp_token": "el token",
                   "resend_api_key": "la clave", "telethon_session": "la sesión"}
        for c in self.dom["inicial"]:
            self.assertEqual(c["boton"]["aria"],
                             "Mostrar " + nombres.get(c["id"], "la contraseña"), c["id"])
        # tl_password sí es la contraseña de la persona (verificación en 2 pasos de Telegram).
        self.assertNotIn("tl_password", nombres)

    def test_ningun_cambio_de_pantalla_hereda_una_contrasena_a_la_vista(self):
        # Una contraseña destapada que sobrevive a una navegación se queda a la vista de quien pase
        # por detrás — y en el acceso, sin que nadie haya entrado siquiera.
        self.assertEqual(sorted(self.dom["tapa"]), ["fpToggle", "logout", "showSub", "showTab"])
        for salida, tipos in self.dom["tapa"].items():
            self.assertEqual(tipos, ["password"] * len(self.ids), salida)

    def test_el_acceso_tapa_ANTES_de_llamar_no_al_asentarse_la_promesa(self):
        # Con el ojo abierto y Enter pulsado, doLogin espera a /api/me: uno a tres segundos, más con
        # la Lambda fría y más aún si cae en el bloqueo por intentos. Taparlo en el .finally() dejaba
        # la contraseña EN CLARO toda esa espera; se mide DENTRO de doLogin, no al volver.
        self.assertEqual(self.dom["login"]["dentro"]["tipos"], ["password"] * len(self.ids))
        # Y enmascarar no le quita la credencial: sigue leyendo el valor (cambiar el type no borra).
        self.assertEqual(self.dom["login"]["dentro"]["valor"], "valor-lp")
        self.assertEqual(self.dom["login"]["al_lanzar"], ["password"] * len(self.ids))
        self.assertEqual(self.dom["login"]["al_terminar"], ["password"] * len(self.ids))
        self.assertIn("doLogin", self.dom["llamadas"])   # y la original se sigue llamando

    def test_el_mismo_tapar_se_puede_llamar_por_nombre_desde_fuera(self):
        # Los bloques de secretos que se ocultan y se vuelven a mostrar sin cambiar de pantalla no
        # los cubre ninguna de las cinco envueltas: necesitan llamarlo, no otra copia del recorrido.
        self.assertTrue(self.dom["expuesta"])
        self.assertEqual(self.dom["tras_pwtapar"], ["password"] * len(self.ids))

    def test_un_campo_que_nace_en_un_modal_tambien_lleva_ojo(self):
        # #eu_pw («Editar usuario») nace al abrir el modal, o sea después de la carga: el
        # recorrido inicial no lo ve. Sin el enganche, un admin veía el ojo en «Crear
        # usuario» y no lo veía al editar, en el mismo panel y a dos clics.
        m = self.dom["modal"]
        self.assertTrue(m["engancha"], "pwEnganchar no está expuesta")
        self.assertTrue(m["tiene_ojo"])
        # Es la contraseña de una persona, no un token: el nombre tiene que decir eso.
        self.assertEqual(m["aria"], "Mostrar la contraseña")
        self.assertEqual(m["destapado"], "text")
        # Y el vector: se engancha con la raíz ACOTADA al modal. Con `document` se
        # re-envolverían los diez de la página y quedarían dos ojos en cada campo.
        self.assertTrue(m["no_re_envuelve"], "hay un campo con dos ojos")

    def test_cerrar_un_modal_no_deja_tuerto_al_resto_del_panel(self):
        # Los campos de un modal se DESTRUYEN al cerrarlo, así que `tapar()` los poda por
        # `isConnected`. Si esa poda se pasa de larga se lleva la lista entera y el panel
        # deja de taparse del todo — pasó al escribirla, y esto es lo que lo cazó.
        m = self.dom["modal"]
        self.assertEqual(m["los_diez"], ["password"] * len(self.ids))
        # Y el del modal ya no está en la lista: nadie toca un nodo fuera del documento.
        self.assertEqual(m["el_del_modal"], "text")

    def test_pulsar_el_ojo_del_acceso_no_intenta_entrar(self):
        self.assertTrue(self.dom["clic_no_envia"])

    def test_la_etiqueta_no_se_queda_sin_su_campo(self):
        # a11yEnhance (M28) empareja <label> y campo por nextElementSibling: al envolver el input
        # en el <span> la etiqueta deja de ser su hermana, así que el for se pone ANTES de mover.
        etiquetas = self.dom["etiquetas"]
        for id_ in self.ids:
            if id_ == "telethon_session":
                continue
            self.assertEqual(etiquetas[id_]["tag"], "LABEL", id_)
            self.assertEqual(etiquetas[id_]["for"], id_, id_)
            self.assertFalse(etiquetas[id_]["aun_hermano"], id_)   # ya no lo es: de ahí el for
        # A este el vecino es el <summary> del <details>: no se le inventa una etiqueta.
        self.assertEqual(etiquetas["telethon_session"]["tag"], "SUMMARY")
        self.assertIsNone(etiquetas["telethon_session"]["for"])

if __name__ == "__main__":
    unittest.main()
