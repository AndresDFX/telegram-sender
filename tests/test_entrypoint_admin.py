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
                    "strip_patterns": ["ubicad"], "whatsapp_footer": "", "image_url": ""}

    def get(self):
        return dict(self.cfg)

    def set(self, cambios):
        self.saved = cambios
        self.cfg.update(cambios)
        return dict(self.cfg)


class FakeSubs:
    def __init__(self):
        self.registros = []

    def listar_todos(self):
        return [{"chatId": "1", "status": "active"}, {"chatId": "2", "status": "inactive"}]

    def registrar(self, chat_id, status):
        self.registros.append((chat_id, status))


class FakeQueueStats:
    def profundidades(self):
        return {"broadcast": 3, "dlq": 1}


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
        os.environ["ADMIN_USER"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "secret123"

    def tearDown(self):
        admin.config = admin.subscribers = admin.queue_stats = admin.image_store = None
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
        self.assertIn("TelegramSender", resp["body"])

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


if __name__ == "__main__":
    unittest.main()
