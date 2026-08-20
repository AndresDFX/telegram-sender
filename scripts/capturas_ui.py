"""Smoke visual del panel: renderiza el shell y toma capturas (login/panel, oscuro/claro, móvil).

No es un test: se usa a mano para revisar la UI sin desplegar. Sirve el HTML del panel por http
local y responde a /api/* con datos falsos mínimos para que pinte todo.

    pip install playwright && playwright install chromium
    python scripts/capturas_ui.py     # deja los PNG en .build/ui/
"""

import http.server
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "lambda"))
from entrypoints import admin  # noqa: E402

HTML = admin._html_resp("/admin")["body"]
FAKE = {
    "/admin/api/me": {"ok": True, "user": "admin", "role": "admin"},
    "/admin/api/config": {"sending_enabled": False, "capture_enabled": True, "source_channel": "iproparts",
                          "markup_percentage": 15.0, "send_mode": "userbot", "batch_size": 150,
                          "currency_symbols": "$", "strip_patterns": [], "whatsapp_footer": "", "image_url": "",
                          "mode_telegram": "all", "mode_whatsapp": "all", "lists_telegram": [], "lists_whatsapp": [],
                          "timezone": "America/Bogota"},
    "/admin/api/subscribers": {"items": [{"chatId": "12345", "name": "Cliente Uno", "status": "active"},
                                         {"chatId": "67890", "name": "Cliente Dos", "status": "inactive"}]},
    "/admin/api/queue": {"broadcast": 0, "dlq": 0},
    "/admin/api/dlq": {"items": []},
    "/admin/api/metrics": {"sent": 1240, "failed": 18, "rate": 98.6, "daily": [{"d": "2026-08-18", "s": 120, "f": 2}]},
    "/admin/api/broadcasts": {"items": []},
    "/admin/api/plans": {"items": []},
    "/admin/api/schedules": {"items": []},
    "/admin/api/audit": {"items": []},
    "/admin/api/users": {"items": []},
    "/admin/api/whatsapp/status": {"connected": True},
    "/admin/api/telegram/account": {"mode": "userbot", "connected": True, "me": {"name": "Yo"}},
}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ruta = self.path.split("?")[0]
        if ruta in ("/admin", "/admin/"):
            cuerpo, ct = HTML.encode(), "text/html; charset=utf-8"
        elif ruta == "/admin/manifest.webmanifest":
            cuerpo, ct = admin._manifest_resp("/admin")["body"].encode(), "application/manifest+json"
        elif ruta in admin._PWA_ICONOS:
            import base64
            cuerpo, ct = base64.b64decode(admin._PWA_ICONOS[ruta]), "image/png"
        else:
            cuerpo = json.dumps(FAKE.get(ruta, {"items": [], "ok": True})).encode()
            ct = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    do_POST = do_GET

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8791), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.join(ROOT, ".build", "ui")
os.makedirs(OUT, exist_ok=True)
URL = "http://127.0.0.1:8791/admin"


def capturar_wa(page, tema, nombre):
    """Vinculación de WhatsApp «desde este teléfono»: hay que dispararla a mano (número → código).

    Se interceptan status/pair para simular «sin vincular» y un código recién emitido; así la
    captura muestra la caja del código y los pasos, que es lo que hay que revisar a ojo.
    """
    page.route("**/api/whatsapp/status", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"connected": False, "contacts": 0, "pairingCode": None})))
    page.route("**/api/whatsapp/pair", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"pairingCode": "48213765", "number": "573001234567"})))
    page.evaluate("goStep('ajustes','conexiones')")
    page.wait_for_timeout(400)
    page.fill("#wa_pair_num", "573001234567")
    page.click("#wa_pair_btn")
    page.wait_for_timeout(800)
    page.screenshot(path=f"{OUT}/wa-vincular-{tema}-{nombre}.png", full_page=True)


def entrar(page):
    """Entra al panel sin backend real: inyecta la credencial en memoria y arranca boot()."""
    page.evaluate("""() => {
      CRED = btoa('admin:x');
      try{ sessionStorage.setItem('sess_t', String(Date.now())); }catch(e){}
      document.getElementById('login').style.display='none';
      document.getElementById('app').style.display='block';
      boot();
    }""")
    page.wait_for_timeout(900)


with sync_playwright() as p:
    b = p.chromium.launch()
    for tema in ("dark", "light"):
        for nombre, vp in (("desktop", {"width": 1440, "height": 950}), ("movil", {"width": 390, "height": 844})):
            ctx = b.new_context(viewport=vp, device_scale_factor=2 if nombre == "movil" else 1)
            page = ctx.new_page()
            errores = []
            page.on("pageerror", lambda e: errores.append(str(e)))
            page.add_init_script(f"try{{localStorage.setItem('theme','{tema}')}}catch(e){{}}")
            page.goto(URL, wait_until="load")
            page.screenshot(path=f"{OUT}/login-{tema}-{nombre}.png", full_page=(nombre == "movil"))
            entrar(page)
            for tab, arch in (("inicio", "inicio"), ("enviar", "enviar"), ("fuentes", "contactos")):
                page.evaluate(f"showTab('{tab}')")
                page.wait_for_timeout(400)
                page.screenshot(path=f"{OUT}/{arch}-{tema}-{nombre}.png", full_page=True)
                # Vista de pantalla (sin full_page): es la única que muestra la barra inferior fija
                # en su sitio; con full_page se dibuja flotando a media altura.
                page.screenshot(path=f"{OUT}/vp-{arch}-{tema}-{nombre}.png")
            capturar_wa(page, tema, nombre)
            print(f"{tema}/{nombre}: errores JS = {errores[:3] if errores else 'ninguno'}")
            ctx.close()
    b.close()
srv.shutdown()
print("capturas en", OUT)
