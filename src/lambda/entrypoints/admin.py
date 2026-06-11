"""Entrypoint de la interfaz web de administración (API Gateway + Basic Auth).

La página (shell) se sirve sin auth y trae su propia pantalla de login moderna; las
llamadas a la API (/admin/api/*) sí requieren Basic Auth (las envía el front tras el
login, sin el popup nativo del navegador). Permite: editar config (canal, markup,
footer, símbolos, exclusiones), subir la imagen que acompaña cada lista, ver la cola
y ver/activar/desactivar suscriptores.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
from typing import Any

import wiring
from adapters.config import admin_user

logger = logging.getLogger()
logger.setLevel(logging.INFO)

config = None
subscribers = None
queue_stats = None
image_store = None
broadcast_store = None
plan_store = None

_CAMPOS_EDITABLES = (
    "source_channel",
    "markup_percentage",
    "currency_symbols",
    "strip_patterns",
    "whatsapp_footer",
    "image_url",
    "excluded_ids",
    "send_mode",
    "bot_token",
    "telethon_api_id",
    "telethon_api_hash",
    "telethon_session",
    "whatsapp_enabled",
    "whatsapp_service_url",
    "whatsapp_token",
    "whatsapp_excluded",
    "telegram_lists",
    "telegram_target",
    "whatsapp_lists",
    "whatsapp_target",
    # Anti-baneo / colas / ventana de envío.
    "sending_enabled",
    "batch_size",
    "scheduling_enabled",
    "tg_delay_min",
    "tg_delay_max",
    "wa_delay_min",
    "wa_delay_max",
    "window_enabled",
    "window_start",
    "window_end",
    "window_tz",
)
_LISTAS = ("strip_patterns", "excluded_ids", "whatsapp_excluded")
_LISTAS_NOMBRADAS = ("telegram_lists", "whatsapp_lists")
_TARGETS = ("telegram_target", "whatsapp_target")
_FLOATS = ("tg_delay_min", "tg_delay_max")
_ENTEROS = ("batch_size", "wa_delay_min", "wa_delay_max", "window_tz")
_BOOLS = ("whatsapp_enabled", "scheduling_enabled", "window_enabled", "sending_enabled")
# Secretos que NO se sobreescriben con un valor vacío (para no borrarlos al guardar otros campos).
_NO_VACIAR = ("telethon_session", "telethon_api_hash", "whatsapp_token", "bot_token")


def _ensure() -> None:
    global config, subscribers, queue_stats, image_store, broadcast_store, plan_store
    if config is None:
        config = wiring.build_config_store()
    if subscribers is None:
        subscribers = wiring.build_subscribers()
    if queue_stats is None:
        queue_stats = wiring.build_queue_stats()
    if image_store is None:
        image_store = wiring.build_image_store()
    if broadcast_store is None:
        broadcast_store = wiring.build_broadcast_store()
    if plan_store is None:
        plan_store = wiring.build_plan_store()


def _planes_con_progreso() -> list[dict]:
    """Planes fraccionados con, por cada lote despachado, cuántos mensajes se han enviado.
    El progreso real se deriva de los contadores del job (Broadcasts) por canal."""
    salida = []
    for p in plan_store.listar():
        bid = p.get("broadcast_id")
        prog = broadcast_store.progreso(bid) if bid else {"tg": 0, "wa": 0}
        log = []
        for e in p.get("log", []):
            ch, n, target = e.get("ch"), int(e.get("n", 0)), int(e.get("target", 0))
            avance = int(prog.get(ch, 0))
            enviados = max(0, min(n, avance - (target - n)))  # progreso atribuible a este lote
            log.append({**e, "enviados": enviados})
        p["log"] = log
        p["progreso"] = prog
        salida.append(p)
    return salida


# --- auth -------------------------------------------------------------------

def _autorizado(event: dict[str, Any]) -> bool:
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return False  # fail-closed
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        usuario, _, clave = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return hmac.compare_digest(usuario, admin_user()) and hmac.compare_digest(clave, password)


# --- helpers ----------------------------------------------------------------

def _json(data: Any, status: int = 200) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(data, ensure_ascii=False)}


def _html_resp() -> dict[str, Any]:
    return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": _PAGE}


def _body(event: dict[str, Any]) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _sanea_config(cambios: dict) -> dict:
    out: dict = {}
    for k in _CAMPOS_EDITABLES:
        if k not in cambios:
            continue
        v = cambios[k]
        if k == "markup_percentage":
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
        elif k in _LISTAS:
            if isinstance(v, str):
                v = [ln.strip() for ln in v.splitlines() if ln.strip()]
            elif isinstance(v, list):
                v = [str(x).strip() for x in v if str(x).strip()]
            else:
                continue
        elif k in _LISTAS_NOMBRADAS:
            if not isinstance(v, list):
                continue
            limpio = []
            for item in v:
                if not isinstance(item, dict):
                    continue
                nombre = str(item.get("name", "")).strip()
                if not nombre:
                    continue
                ids = [str(x).strip() for x in (item.get("ids") or []) if str(x).strip()]
                limpio.append({"name": nombre, "ids": ids})
            v = limpio
        elif k in _TARGETS:
            if not isinstance(v, dict):
                continue
            mode = str(v.get("mode", "all")).strip().lower()
            if mode not in ("all", "only", "except"):
                mode = "all"
            listas = [str(x).strip() for x in (v.get("lists") or []) if str(x).strip()]
            v = {"mode": mode, "lists": listas}
        elif k == "send_mode":
            v = "userbot" if str(v).strip().lower() == "userbot" else "bot"
        elif k in _BOOLS:
            v = bool(v)
        elif k in _FLOATS:
            try:
                v = max(0.0, float(v))
            except (TypeError, ValueError):
                continue
        elif k == "batch_size":
            from domain.scheduling import cap_batch_size

            v = cap_batch_size(v)  # tope duro 150
        elif k in _ENTEROS:
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                continue
        else:
            v = str(v).strip()
        if k in _NO_VACIAR and not v:  # no borrar secretos con vacío
            continue
        out[k] = v
    return out


def _config_publico() -> dict:
    """Config para el panel, con los secretos enmascarados (no se exponen)."""
    cfg = config.get()
    cfg["telethon_session_set"] = bool(cfg.get("telethon_session"))
    cfg["telethon_session"] = ""
    cfg["whatsapp_token_set"] = bool(cfg.get("whatsapp_token"))
    cfg["whatsapp_token"] = ""
    cfg["bot_token_set"] = bool(cfg.get("bot_token"))
    cfg["bot_token"] = ""
    return cfg


def _whatsapp_proxy(path: str, timeout: float = 20.0, body: dict | None = None) -> dict:
    """Proxy al servicio de WhatsApp (status/contacts/pair) usando la URL+token de la config.

    GET por defecto; si se pasa `body`, hace POST con ese JSON.
    """
    import urllib.error
    import urllib.request

    cfg = config.get()
    url = (cfg.get("whatsapp_service_url") or "").rstrip("/")
    token = cfg.get("whatsapp_token") or ""
    if not url or not token:
        return _json({"error": "whatsapp_no_configurado"}, 409)
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{url}{path}", headers=headers, data=data, method=("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"statusCode": resp.status, "headers": {"Content-Type": "application/json"}, "body": resp.read().decode()}
    except urllib.error.HTTPError as error:
        return {"statusCode": error.code, "headers": {"Content-Type": "application/json"}, "body": error.read().decode()}
    except Exception as error:
        logger.exception("Proxy WhatsApp %s falló", path)
        return _json({"error": "whatsapp_inaccesible", "detalle": str(error)}, 502)


def _telegram_api(metodo: str, params: dict) -> dict:
    """Llama a la Bot API con el token de la config (o env). Para registrar/ver el webhook
    del bot conectado a la plataforma."""
    import urllib.error
    import urllib.parse
    import urllib.request

    token = (config.get().get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return _json({"error": "sin_token", "detalle": "Configura primero el token del bot."}, 400)
    url = f"https://api.telegram.org/bot{token}/{metodo}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return {"statusCode": resp.status, "headers": {"Content-Type": "application/json"}, "body": resp.read().decode()}
    except urllib.error.HTTPError as error:
        return {"statusCode": error.code, "headers": {"Content-Type": "application/json"}, "body": error.read().decode()}
    except Exception as error:
        return _json({"error": "telegram_inaccesible", "detalle": str(error)}, 502)


def _registrar_webhook() -> dict:
    """Registra el webhook del bot configurado (para que /start·/stop lleguen a la plataforma)."""
    wh = os.environ.get("WEBHOOK_URL", "").strip()
    secret = os.environ.get("WEBHOOK_SECRET_TOKEN", "").strip()
    if not wh:
        return _json({"error": "sin_webhook_url", "detalle": "WEBHOOK_URL no configurado en el stack."}, 500)
    params = {"url": wh}
    if secret:
        params["secret_token"] = secret
    return _telegram_api("setWebhook", params)


# --- dispatcher -------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    raw = event.get("rawPath") or http.get("path") or "/admin"
    idx = raw.find("/admin")
    sub = (raw[idx + len("/admin"):] if idx >= 0 else raw).rstrip("/")

    # La página (shell) es pública; el login y la auth viven en la API.
    if sub == "" and method == "GET":
        return _html_resp()

    if not _autorizado(event):
        return _json({"error": "unauthorized"}, 401)

    try:
        if sub == "/api/me" and method == "GET":
            return _json({"ok": True, "user": admin_user()})
        if sub == "/api/config" and method == "GET":
            return _json(_config_publico())
        if sub == "/api/config" and method == "POST":
            return _json(config.set(_sanea_config(_body(event))))
        if sub == "/api/image" and method == "POST":
            cuerpo = _body(event)
            datos = cuerpo.get("image", "")
            try:
                raw_img = base64.b64decode(datos)
            except (binascii.Error, ValueError):
                return _json({"error": "imagen inválida"}, 400)
            if not raw_img:
                return _json({"error": "imagen vacía"}, 400)
            key = image_store.guardar(raw_img, cuerpo.get("content_type", "image/jpeg"))
            config.set({"image_key": key})
            url = ""
            try:
                url = image_store.url_temporal(key)  # para adjuntarla en un envío manual
            except Exception:
                logger.exception("No se pudo firmar la imagen subida")
            return _json({"ok": True, "key": key, "url": url})
        if sub == "/api/subscribers" and method == "GET":
            return _json({"subscribers": subscribers.listar_todos()})
        if sub == "/api/subscribers" and method == "POST":
            cuerpo = _body(event)
            chat_id = str(cuerpo.get("chat_id", "")).strip()
            if not chat_id:
                return _json({"error": "chat_id requerido"}, 400)
            subscribers.registrar(chat_id, "active" if cuerpo.get("status") == "active" else "inactive")
            return _json({"ok": True})
        if sub == "/api/queue" and method == "GET":
            return _json(queue_stats.profundidades())
        if sub == "/api/broadcasts" and method == "GET":
            return _json({"broadcasts": broadcast_store.listar()})
        if sub == "/api/plans" and method == "GET":
            return _json({"plans": _planes_con_progreso()})
        if sub == "/api/plans/cancel" and method == "POST":
            return _json({"ok": True, "canceled": plan_store.cancelar_pendientes()})
        if sub == "/api/telegram/me" and method == "GET":
            return _telegram_api("getMe", {})  # verifica el token + muestra el bot
        if sub == "/api/telegram/webhook" and method == "GET":
            return _telegram_api("getWebhookInfo", {})
        if sub == "/api/telegram/webhook" and method == "POST":
            return _registrar_webhook()
        if sub == "/api/broadcast" and method == "POST":
            cuerpo = _body(event)
            texto = str(cuerpo.get("text", "")).strip()
            a_tg = bool(cuerpo.get("telegram"))
            a_wa = bool(cuerpo.get("whatsapp"))
            img = str(cuerpo.get("image_url", "")).strip()
            if not texto:
                return _json({"error": "texto requerido"}, 400)
            if len(texto) > 4096:  # límite de Telegram; evita que falle en cada destinatario
                return _json({"error": "el mensaje supera 4096 caracteres"}, 400)
            if not (a_tg or a_wa):
                return _json({"error": "elige al menos un canal"}, 400)
            if img and not img.startswith("https://"):
                return _json({"error": "la imagen debe ser una URL https:// (o súbela)"}, 400)
            def _ids(k):
                v = cuerpo.get(k)
                return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else None

            try:
                res = wiring.build_broadcast_list().enviar_manual(
                    texto,
                    image_url=img or None,
                    telegram=a_tg,
                    whatsapp=a_wa,
                    telegram_list=str(cuerpo.get("telegram_list", "")).strip() or None,
                    whatsapp_list=str(cuerpo.get("whatsapp_list", "")).strip() or None,
                    telegram_ids=_ids("telegram_ids"),
                    whatsapp_ids=_ids("whatsapp_ids"),
                )
            except ValueError as e:
                return _json({"error": str(e)}, 400)
            return _json({"ok": True, **res})
        if sub == "/api/broadcast/preview" and method == "POST":
            cuerpo = _body(event)

            def _ids(k):
                v = cuerpo.get(k)
                return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else None

            return _json(
                wiring.build_broadcast_list().previsualizar(
                    telegram=bool(cuerpo.get("telegram")),
                    whatsapp=bool(cuerpo.get("whatsapp")),
                    telegram_list=str(cuerpo.get("telegram_list", "")).strip() or None,
                    whatsapp_list=str(cuerpo.get("whatsapp_list", "")).strip() or None,
                    telegram_ids=_ids("telegram_ids"),
                    whatsapp_ids=_ids("whatsapp_ids"),
                )
            )
        if sub == "/api/whatsapp/status" and method == "GET":
            return _whatsapp_proxy("/status")
        if sub == "/api/whatsapp/contacts" and method == "GET":
            return _whatsapp_proxy("/contacts", timeout=25)
        if sub == "/api/whatsapp/pair" and method == "POST":
            return _whatsapp_proxy("/pair", timeout=25, body={"number": _body(event).get("number", "")})
    except Exception:
        logger.exception("Error en admin %s %s", method, sub)
        return _json({"error": "internal"}, 500)

    return _json({"error": "not found"}, 404)


_PAGE = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replica · Panel</title>
<style>
:root{
  --bg:#0a0e1a; --bg2:#0d1322;
  --card:#10172a; --card2:#0f1730; --elev:#0c1322;
  --bd:#1f2840; --bd2:#28324f;
  --tx:#e8ecf8; --tx2:#c3cbe4; --mut:#7c87a8; --mut2:#5c668a;
  --ac:#6366f1; --ac-h:#7c80f7; --ac2:#22d3ee;
  --ok:#34d399; --warn:#fbbf24; --bad:#fb7185; --info:#60a5fa;
  --r:14px; --r-sm:10px;
  --sh:0 1px 0 rgba(255,255,255,.02) inset, 0 18px 50px -20px rgba(0,0,0,.7);
  --ring:0 0 0 3px rgba(99,102,241,.22);
  --fs:13px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{
  font-family:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  margin:0; background:var(--bg); color:var(--tx);
  min-height:100vh; font-size:var(--fs); line-height:1.5;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  background-image:
    radial-gradient(900px 500px at 88% -8%, rgba(99,102,241,.12), transparent 60%),
    radial-gradient(700px 420px at 8% 0%, rgba(34,211,238,.06), transparent 55%);
  background-attachment:fixed;
}
::selection{background:rgba(99,102,241,.35);color:#fff}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#243056;border-radius:8px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:#33406b;background-clip:padding-box}
a{color:var(--ac2)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;
  background:var(--elev);border:1px solid var(--bd);padding:1px 6px;border-radius:6px;color:#b9c4ec}

/* ---------- marca ---------- */
.brand{display:flex;align-items:center;gap:11px}
.brand .wordmark{
  font-weight:800;font-size:20px;letter-spacing:-.5px;
  background:linear-gradient(95deg,#c7cdff,#7bdcf0);
  -webkit-background-clip:text;background-clip:text;color:transparent;
}
.brand svg{border-radius:11px;filter:drop-shadow(0 6px 18px rgba(99,102,241,.45))}
.brand-lg{justify-content:center;margin-bottom:6px}
.brand-lg .wordmark{font-size:30px}
.logo{font-size:32px;margin-bottom:10px}

/* ---------- login ---------- */
#login{display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
#login .box{
  position:relative;overflow:hidden;
  background:linear-gradient(180deg,var(--card),var(--card2));
  border:1px solid var(--bd);border-radius:20px;
  padding:38px 32px;width:372px;box-shadow:var(--sh);
}
#login .box::before{content:"";position:absolute;inset:-2px -2px auto -2px;height:3px;
  background:linear-gradient(90deg,var(--ac),var(--ac2));opacity:.9}
#login h1{font-size:20px;margin:0 0 4px}
#login p{color:var(--mut);margin:0 0 24px;font-size:13px}

/* ---------- app shell ---------- */
#app{display:none}
header{
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(10,14,26,.72);backdrop-filter:blur(14px) saturate(140%);
  padding:13px 22px;border-bottom:1px solid var(--bd);
  position:sticky;top:0;z-index:5;
}
header .t{font-weight:700;font-size:16px}
header .u{color:var(--mut);font-size:12.5px;display:inline-flex;align-items:center;gap:8px}
main{max-width:900px;margin:0 auto;padding:26px 22px 80px;display:grid;gap:18px}

/* ---------- nav (pestañas horizontales) ---------- */
.nav{
  position:sticky;top:50px;z-index:4;
  background:rgba(10,14,26,.8);backdrop-filter:blur(14px) saturate(140%);
  display:flex;gap:6px;justify-content:center;align-items:center;
  padding:11px 14px;border-bottom:1px solid var(--bd);flex-wrap:wrap;
}
.nav button{
  background:transparent;border:1px solid transparent;color:var(--mut);
  padding:8px 15px;border-radius:999px;font-weight:600;font-size:13px;
  cursor:pointer;transition:color .15s,background .15s,border-color .15s;
}
.nav button:hover{color:var(--tx2);background:rgba(255,255,255,.04)}
.nav button.on{
  background:rgba(99,102,241,.14);color:#cdd1ff;border-color:rgba(99,102,241,.4);
}

/* ---------- cards ---------- */
.card{
  background:linear-gradient(180deg,var(--card),var(--card2));
  border:1px solid var(--bd);border-radius:var(--r);padding:22px;box-shadow:var(--sh);
}
h2{
  margin:0 0 16px;font-size:12px;color:var(--ac2);
  letter-spacing:.9px;text-transform:uppercase;font-weight:700;
  display:flex;align-items:center;gap:8px;
}

/* ---------- form fields ---------- */
label{display:block;margin:13px 0 6px;font-size:12px;color:var(--mut);font-weight:600;letter-spacing:.2px}
input,textarea,select{
  width:100%;background:var(--elev);border:1px solid var(--bd);color:var(--tx);
  border-radius:var(--r-sm);padding:11px 12px;font-size:14px;font-family:inherit;
  transition:border-color .15s,box-shadow .15s,background .15s;
}
input::placeholder,textarea::placeholder{color:var(--mut2)}
input:hover,textarea:hover,select:hover{border-color:var(--bd2)}
input:focus,textarea:focus,select:focus{outline:0;border-color:var(--ac);box-shadow:var(--ring);background:#0d1426}
textarea{min-height:88px;resize:vertical;font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:13px;line-height:1.55}
select{appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%237c87a8' stroke-width='2.5'><path d='M6 9l6 6 6-6'/></svg>");
  background-repeat:no-repeat;background-position:right 12px center;padding-right:36px;cursor:pointer}
input[type=file]{padding:9px 12px;color:var(--mut);cursor:pointer}
input[type=file]::file-selector-button{
  background:#1b2440;border:1px solid var(--bd2);color:var(--tx2);
  border-radius:8px;padding:7px 13px;margin-right:12px;cursor:pointer;font:inherit;font-weight:600}
input[type=file]::file-selector-button:hover{background:#222c4d}
input[type=checkbox],input[type=radio]{accent-color:var(--ac);width:auto;cursor:pointer}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}

/* ---------- buttons ---------- */
button{
  background:var(--ac);color:#fff;border:1px solid transparent;border-radius:var(--r-sm);
  padding:10px 17px;font-size:13.5px;font-weight:600;font-family:inherit;cursor:pointer;
  transition:background .15s,transform .05s,box-shadow .15s,border-color .15s,opacity .15s;
}
button:hover{background:var(--ac-h)}
button:active{transform:translateY(1px)}
button:focus-visible{outline:0;box-shadow:0 0 0 3px rgba(99,102,241,.35)}
button:disabled{opacity:.5;cursor:not-allowed;transform:none}
button.sec{background:#1b2440;color:var(--tx2);border-color:var(--bd2)}
button.sec:hover{background:#222c4d}
button.ghost{background:transparent;border:1px solid var(--bd2);color:var(--mut)}
button.ghost:hover{background:rgba(255,255,255,.04);color:var(--tx2)}

/* ---------- markup widget ---------- */
.markup{display:flex;align-items:center;gap:18px;background:var(--elev);border:1px solid var(--bd);border-radius:var(--r);padding:18px}
.markup input{font-size:32px;font-weight:700;text-align:center;width:124px;padding:8px;color:var(--ac2)}

/* ---------- misc text ---------- */
.hint{color:var(--mut);font-size:12px;margin-top:6px;line-height:1.55}
.hint code{background:var(--elev);border:1px solid var(--bd);padding:1px 6px;border-radius:6px;font-size:11.5px}
.err{color:var(--bad);font-size:13px;min-height:18px;margin-top:8px}
img.preview{max-width:170px;border-radius:var(--r-sm);margin-top:12px;border:1px solid var(--bd)}

/* ---------- table ---------- */
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:10px 9px;border-bottom:1px solid var(--bd);vertical-align:middle}
tbody tr{transition:background .12s}
tbody tr:hover{background:rgba(255,255,255,.025)}
th{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px;font-weight:700}
td b{font-weight:600;color:var(--tx)}

/* ---------- pills / chips de estado ---------- */
.pill{padding:3px 11px;border-radius:999px;font-size:11.5px;font-weight:600;display:inline-block;border:1px solid transparent;white-space:nowrap}
.pill.active{background:rgba(52,211,153,.13);color:var(--ok);border-color:rgba(52,211,153,.28)}
.pill.inactive{background:rgba(251,191,36,.12);color:var(--warn);border-color:rgba(251,191,36,.26)}
/* estados de envíos (reutiliza .pill) */
.pill.queued{background:rgba(96,165,250,.13);color:var(--info);border-color:rgba(96,165,250,.3)}
.pill.sending{background:rgba(34,211,238,.12);color:var(--ac2);border-color:rgba(34,211,238,.28)}
.pill.done{background:rgba(52,211,153,.13);color:var(--ok);border-color:rgba(52,211,153,.28)}
.pill.partial{background:rgba(251,191,36,.12);color:var(--warn);border-color:rgba(251,191,36,.26)}
.pill.failed{background:rgba(251,113,133,.12);color:var(--bad);border-color:rgba(251,113,133,.3)}

/* ---------- stats ---------- */
.stats{display:flex;gap:14px;flex-wrap:wrap}
.stat{flex:1;min-width:130px;background:var(--elev);border:1px solid var(--bd);border-radius:var(--r);padding:18px;text-align:center;position:relative;overflow:hidden}
.stat::after{content:"";position:absolute;left:0;right:0;top:0;height:2px;background:linear-gradient(90deg,var(--ac),var(--ac2));opacity:.6}
.stat b{display:block;font-size:30px;font-weight:700;color:var(--ac2);line-height:1.1}
.stat span{color:var(--mut);font-size:12px}

/* ---------- toast ---------- */
.toast{
  position:fixed;bottom:24px;right:24px;z-index:50;
  background:#0f2a21;color:#9ff0d2;border:1px solid rgba(52,211,153,.35);
  padding:12px 18px;border-radius:var(--r-sm);font-weight:600;font-size:13px;
  box-shadow:0 18px 50px -16px rgba(0,0,0,.7);
  opacity:0;transform:translateY(12px);transition:opacity .25s,transform .25s;pointer-events:none;
}
.toast.show{opacity:1;transform:none}
.toast.err{background:#2a1015;color:#ffc0c8;border-color:rgba(251,113,133,.4)}

/* ---------- tab visibility ---------- */
main>.card{display:none;animation:fade .22s ease}
main>.card.show{display:block}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ===========================================================
   NUEVO — Componer y Envíos (data-tab="enviar")
   =========================================================== */
/* contador de caracteres */
.charcount{float:right;font-size:11px;color:var(--mut);font-weight:500;margin-top:-2px;font-variant-numeric:tabular-nums}

/* selector de canales (chips toggle) */
.chan-row{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 4px}
.pickbox{max-height:152px;overflow:auto;border:1px solid var(--bd);border-radius:var(--r-sm);background:var(--elev);margin-top:6px;padding:4px}
.pickitem{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;cursor:pointer;font-size:13px;color:var(--tx2)}
.pickitem:hover{background:rgba(255,255,255,.04)}
.pickitem input{width:auto;margin:0}
.pickbox .hint{padding:8px}
.chan{
  display:flex;align-items:center;gap:9px;cursor:pointer;user-select:none;
  background:var(--elev);border:1px solid var(--bd);border-radius:var(--r-sm);
  padding:11px 15px;font-size:13.5px;font-weight:600;color:var(--tx2);
  transition:border-color .15s,background .15s,color .15s;
}
.chan:hover{border-color:var(--bd2)}
.chan input{margin:0}
.chan.tg.on{border-color:rgba(99,102,241,.55);background:rgba(99,102,241,.1);color:#cdd1ff}
.chan.wa.on{border-color:rgba(52,211,153,.5);background:rgba(52,211,153,.1);color:#a9f0d4}
.chan .dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}
.chan.tg.on .dot{background:var(--ac)}
.chan.wa.on .dot{background:var(--ok)}

/* dropzone / preview del compositor */
.img-slot{display:flex;align-items:flex-start;gap:14px;margin-top:6px}
.img-slot .preview{margin-top:0}
.img-slot .meta{flex:1;min-width:0}

/* barra de acciones del compositor */
.compose-actions{display:flex;align-items:center;gap:12px;margin-top:18px;flex-wrap:wrap}
.compose-actions .grow{flex:1}

/* ---- tabla de envíos ---- */
.bc-empty{color:var(--mut);font-size:13px;padding:18px 4px;text-align:center}
.bc-msg{max-width:340px}
.bc-msg b{display:block;color:var(--tx);font-weight:500;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bc-meta{color:var(--mut);font-size:11.5px;margin-top:3px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.bc-src{text-transform:uppercase;letter-spacing:.5px;font-weight:700;font-size:10px;color:#8b96b8;
  background:var(--elev);border:1px solid var(--bd);border-radius:5px;padding:1px 6px}

/* progreso por canal */
.chprog{display:flex;flex-direction:column;gap:7px;min-width:150px}
.chprog .ch{display:flex;align-items:center;gap:8px;font-size:12px}
.chprog .ch .ic{width:7px;height:7px;border-radius:50%;flex:none}
.chprog .ch.tg .ic{background:var(--ac)}
.chprog .ch.wa .ic{background:var(--ok)}
.chprog .ch .num{color:var(--tx2);font-variant-numeric:tabular-nums;white-space:nowrap}
.chprog .ch .num .fail{color:var(--bad)}
.chprog .ch .muted{color:var(--mut)}
.bar{flex:1;height:5px;background:var(--elev);border:1px solid var(--bd);border-radius:999px;overflow:hidden;min-width:48px}
.bar>i{display:block;height:100%;width:0;border-radius:999px;transition:width .5s ease;background:linear-gradient(90deg,var(--ac),#818cf8)}
.bar.wa>i{background:linear-gradient(90deg,var(--ok),#5eead4)}
.bar.full>i{background:var(--ok)}
.bar.err>i{background:var(--bad)}

/* indicador de "vivo" (polling) */
.live{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;color:var(--mut);font-weight:600;text-transform:none;letter-spacing:0}
.live .ping{width:8px;height:8px;border-radius:50%;background:var(--mut)}
.live.on .ping{background:var(--ok);box-shadow:0 0 0 0 rgba(52,211,153,.6);animation:ping 1.8s infinite}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(52,211,153,.5)}70%{box-shadow:0 0 0 7px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}

/* ---------- responsive ---------- */
@media (max-width:620px){
  main{padding:18px 14px 70px}
  .card{padding:18px 16px}
  .row{grid-template-columns:1fr}
  .stats{flex-direction:column}
  .bc-msg{max-width:none}
  .nav{gap:4px;padding:9px 8px}
  .nav button{padding:7px 11px;font-size:12.5px}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

/* ===== Replica · refinamiento UI/UX (rebrand) ===== */
/* ============================================================
   REPLICA · capa de refinamiento UI/UX  (vanilla, aditiva)
   Compatible con tokens y clases existentes del panel (admin.py).
   Sin cambios de JS: reacciona a clases/estados ya presentes o
   que el front puede alternar con classList.
   Tokens verificados en :root del panel: --bg --card --elev --bd
   --bd2 --tx --tx2 --mut --mut2 --ac --ac2 --ok --warn --bad
   --info --r --r-sm --ring.
   ============================================================ */

:root{
  --ac-soft:rgba(99,102,241,.14);
  --ac2-soft:rgba(34,211,238,.12);
  --ok-soft:rgba(52,211,153,.12);
  --warn-soft:rgba(251,191,36,.12);
  --bad-soft:rgba(251,113,133,.12);
  --r-lg:18px;
  --tx3:#9aa4c6;            /* secundario un punto mas contrastado que --mut */
  --sh-sm:0 6px 18px -10px rgba(0,0,0,.6);
}

/* ------------------------------------------------------------
   1) FIX: filas de listas con separador #eee (roto en oscuro)
   El JS pinta inline border-bottom:1px solid #eee (admin.py L854);
   lo anulamos y convertimos cada fila en un item legible.
   ------------------------------------------------------------ */
#tg_lists>div, #wa_lists>div{
  border-bottom:1px solid var(--bd) !important;
  padding:11px 12px !important;
  margin:0 -12px;
  transition:background .12s;
  row-gap:8px !important;
}
#tg_lists>div:hover, #wa_lists>div:hover{ background:rgba(255,255,255,.025); }
#tg_lists>div:last-child, #wa_lists>div:last-child{ border-bottom:0 !important; }
#tg_lists>div b, #wa_lists>div b{ color:var(--tx); }
/* el "N miembros" como chip discreto */
#tg_lists>div .hint, #wa_lists>div .hint{
  background:var(--elev);border:1px solid var(--bd);
  padding:2px 9px;border-radius:999px;margin-top:0;font-weight:600;
  color:var(--tx3);
}
/* botones de fila mas compactos para que respiren */
#tg_lists>div button, #wa_lists>div button{ padding:7px 12px;font-size:12.5px; }

/* ------------------------------------------------------------
   2) CALLOUT / banner de advertencia reutilizable
   Uso sugerido: <div class="callout warn">...</div>
   Fallback automatico: realza el ultimo .hint de la tarjeta de
   WhatsApp (aviso de baneo) para que no se lea como texto plano.
   ------------------------------------------------------------ */
.callout{
  display:flex;gap:10px;align-items:flex-start;
  border:1px solid var(--bd2);border-left-width:3px;
  background:var(--elev);border-radius:var(--r-sm);
  padding:12px 14px;margin:12px 0;font-size:12.5px;line-height:1.55;color:var(--tx2);
}
.callout::before{content:"i";flex:none;width:18px;height:18px;border-radius:50%;
  display:grid;place-items:center;font-weight:800;font-size:11px;
  background:var(--info);color:#04121f;margin-top:1px}
.callout.warn{border-left-color:var(--warn);background:var(--warn-soft)}
.callout.warn::before{content:"!";background:var(--warn);color:#3a2c00}
.callout.danger{border-left-color:var(--bad);background:var(--bad-soft)}
.callout.danger::before{content:"!";background:var(--bad);color:#2a0a0f}
.callout.ok{border-left-color:var(--ok);background:var(--ok-soft)}
.callout.ok::before{content:"\2713";background:var(--ok);color:#04130d}
.callout b{color:var(--tx)}
/* fallback: ultimo .hint de la tarjeta de WhatsApp (aviso de baneo) resaltado */
.card[data-tab="whatsapp"] > .hint:last-of-type{
  border:1px solid rgba(251,191,36,.3);border-left:3px solid var(--warn);
  background:var(--warn-soft);border-radius:var(--r-sm);
  padding:11px 13px;color:#f3dca0;
}

/* ------------------------------------------------------------
   3) ESTADOS VACIOS + SKELETONS de carga
   ------------------------------------------------------------ */
.stat b{font-variant-numeric:tabular-nums}
.empty-state{
  text-align:center;padding:34px 18px;color:var(--mut);
  display:flex;flex-direction:column;align-items:center;gap:8px;
}
.empty-state .ico{
  width:56px;height:56px;border-radius:16px;display:grid;place-items:center;
  font-size:26px;background:var(--ac-soft);border:1px solid rgba(99,102,241,.28);
  color:#cdd1ff;margin-bottom:4px;
}
.empty-state h3{margin:0;font-size:15px;color:var(--tx)}
.empty-state p{margin:0;max-width:340px;font-size:12.5px;line-height:1.6}
.empty-state .cta{margin-top:6px}
/* refuerzo visual de los empties existentes */
.bc-empty, #subsempty{
  border:1px dashed var(--bd2);border-radius:var(--r);
  background:rgba(255,255,255,.012);
}
/* skeleton shimmer (el front puede inyectar .skeleton .sk-line al cargar) */
.skeleton{pointer-events:none}
.sk-line{
  height:12px;border-radius:6px;margin:9px 0;
  background:linear-gradient(90deg,var(--elev) 25%,#16203c 37%,var(--elev) 63%);
  background-size:400% 100%;animation:sk 1.3s ease infinite;
}
.sk-line.lg{height:26px;width:60%}
.sk-line.sm{width:40%}
@keyframes sk{0%{background-position:100% 0}100%{background-position:-100% 0}}

/* ------------------------------------------------------------
   4) FEEDBACK: botones cargando / ok + toasts mejorados
   ------------------------------------------------------------ */
.btn-loading{position:relative;color:transparent !important;pointer-events:none}
.btn-loading::after{
  content:"";position:absolute;inset:0;margin:auto;width:15px;height:15px;
  border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;
  animation:spin .6s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
button.ok{background:var(--ok);border-color:transparent;color:#04130d}
button.ok:hover{background:#46e0a9}
.err:empty{margin-top:0;min-height:0}
/* toasts: icono, variantes y barra de auto-cierre */
.toast{display:flex;align-items:center;gap:9px;padding-right:16px;position:relative;overflow:hidden}
.toast::before{content:"\2713";font-weight:800}
.toast.err::before{content:"!"}
.toast.info{background:#0c1d33;color:#bcd6ff;border-color:rgba(96,165,250,.35)}
.toast.info::before{content:"i"}
.toast.warn{background:#2a230d;color:#f3dca0;border-color:rgba(251,191,36,.4)}
.toast.warn::before{content:"!"}
.toast.show::after{
  content:"";position:absolute;left:0;bottom:0;height:2px;width:100%;
  background:currentColor;opacity:.45;transform-origin:left;
  animation:toastbar 2.2s linear forwards;
}
@keyframes toastbar{from{transform:scaleX(1)}to{transform:scaleX(0)}}
@media (max-width:620px){
  .toast{left:14px;right:14px;bottom:14px}
}

/* ------------------------------------------------------------
   5) NAV con indicador inferior + jerarquia de secciones + a11y
   ------------------------------------------------------------ */
.nav button{position:relative;min-height:36px}
.nav button.on::after{
  content:"";position:absolute;left:14px;right:14px;bottom:-12px;height:2px;
  border-radius:2px;background:linear-gradient(90deg,var(--ac),var(--ac2));
}
.nav button:hover{background:rgba(255,255,255,.05)}
/* etiqueta de seccion para encabezar grupos de tarjetas */
.section-label{
  grid-column:1/-1;display:flex;align-items:center;gap:10px;
  margin:6px 2px -4px;color:var(--tx3);font-size:11px;font-weight:700;
  letter-spacing:.8px;text-transform:uppercase;
}
.section-label::after{content:"";flex:1;height:1px;background:var(--bd)}
/* cabecera de tarjeta: acento superior opcional */
.card.accent{position:relative;overflow:hidden}
.card.accent::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;
  background:linear-gradient(90deg,var(--ac),var(--ac2));opacity:.7}
h2{align-items:center}
h2::before{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--ac2);box-shadow:0 0 0 3px var(--ac2-soft);margin-right:8px;flex:none}
/* ACCESIBILIDAD: foco visible y areas de toque */
.nav button:focus-visible,
.chan:focus-within,
input[type=checkbox]:focus-visible,
input[type=radio]:focus-visible,
select:focus-visible,
[onclick]:focus-visible{
  outline:0;box-shadow:var(--ring);border-radius:var(--r-sm);
}
.chan:focus-within{border-color:var(--ac)}
input[type=checkbox],input[type=radio]{min-width:17px;min-height:17px}
th input[type=checkbox],td input[type=checkbox]{transform:scale(1.05)}

/* ------------------------------------------------------------
   6) Chips de canal + pulido de superficie (tablas, badges, scroll)
   ------------------------------------------------------------ */
.chan{transition:transform .12s,border-color .15s,background .15s,color .15s}
.chan:active{transform:scale(.98)}
.chan.tg.on,.chan.wa.on{box-shadow:0 0 0 1px currentColor inset}
.chan .dot{transition:transform .15s,background .15s}
.chan.tg.on .dot,.chan.wa.on .dot{transform:scale(1.25)}
/* zebra muy tenue en tablas largas de contactos/destinatarios */
#subs tr:nth-child(even) td, #wa_subs tr:nth-child(even) td{
  background:rgba(255,255,255,.012);
}
tbody tr:hover td{background:rgba(255,255,255,.03)}
/* badge de origen del envio mas legible */
.bc-src{background:var(--ac-soft);border-color:rgba(99,102,241,.28);color:#b9c0ee}
/* pildora "sending" pulsa para indicar actividad */
.pill.sending{position:relative}
.pill.sending::after{
  content:"";display:inline-block;width:6px;height:6px;margin-left:6px;border-radius:50%;
  background:var(--ac2);vertical-align:middle;animation:pulseDot 1.2s ease-in-out infinite;
}
@keyframes pulseDot{0%,100%{opacity:.35}50%{opacity:1}}
/* sombra suave al hover en tarjetas para dar profundidad */
.card{transition:border-color .15s,box-shadow .2s}
.card:hover{border-color:var(--bd2)}
/* contador de caracteres avisa cerca del limite de Telegram (4096) */
.charcount[data-near="1"]{color:var(--warn)}
.charcount[data-over="1"]{color:var(--bad)}
/* indicador "en vivo" mas claro */
.live:not(.on) .ping{background:var(--mut2)}
.live.on{color:var(--ok)}
/* coherencia de radios/sombra en bloques anidados */
img.preview{box-shadow:var(--sh-sm)}
.stat{box-shadow:var(--sh-sm)}

@media(prefers-reduced-motion:reduce){
  .btn-loading::after,.pill.sending::after,.sk-line,.toast.show::after{animation:none!important}
}
</style></head><body>

<div id="login"><div class="box">
  <div class="brand brand-lg"><svg viewBox="0 0 48 48" width="46" height="46" aria-hidden="true"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#22d3ee"/></linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#lg)"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 24c5 0 5.5-9 11.5-9"/><path d="M21 24h11.5"/><path d="M21 24c5 0 5.5 9 11.5 9"/></g><circle cx="15" cy="24" r="4.2" fill="#fff"/><circle cx="33.5" cy="15" r="3" fill="#fff"/><circle cx="34.5" cy="24" r="3" fill="#fff"/><circle cx="33.5" cy="33" r="3" fill="#fff"/></svg><span class="wordmark">Replica</span></div>
  <p style="text-align:center">Tu lista de precios, replicada y enviada en segundos.</p>
  <label>Usuario</label><input id="lu" autocomplete="username" value="admin">
  <label>Contraseña</label><input id="lp" type="password" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
  <div class="err" id="lerr"></div>
  <button style="width:100%;margin-top:8px" onclick="doLogin()">Entrar</button>
</div></div>

<div id="app">
 <header><div class="brand"><svg viewBox="0 0 48 48" width="30" height="30" aria-hidden="true"><defs><linearGradient id="lg2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#22d3ee"/></linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#lg2)"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 24c5 0 5.5-9 11.5-9"/><path d="M21 24h11.5"/><path d="M21 24c5 0 5.5 9 11.5 9"/></g><circle cx="15" cy="24" r="4.2" fill="#fff"/><circle cx="33.5" cy="15" r="3" fill="#fff"/><circle cx="34.5" cy="24" r="3" fill="#fff"/><circle cx="33.5" cy="33" r="3" fill="#fff"/></svg><span class="wordmark">Replica</span></div><div><span id="hdr_badge" class="pill" style="display:none;margin-right:10px"></span><span class="u" id="who"></span>
   <button class="ghost" style="margin-left:12px;padding:7px 12px" onclick="logout()">Salir</button></div></header>
 <nav class="nav">
   <button data-tab="msg" onclick="showTab('msg')">📝 Mensaje</button>
   <button data-tab="telegram" onclick="showTab('telegram')">✈️ Telegram</button>
   <button data-tab="whatsapp" onclick="showTab('whatsapp')">🟢 WhatsApp</button>
   <button data-tab="prog" onclick="showTab('prog')">⏱️ Programación</button>
   <button data-tab="estado" onclick="showTab('estado')">📊 Estado</button>
   <button data-tab="enviar" onclick="showTab('enviar')">📨 Enviar</button>
 </nav>
 <main>
  <div class="card" data-tab="msg"><h2>Aumento (markup)</h2>
   <div class="markup"><input id="markup_percentage" type="number" step="0.1"><div>
     <div style="font-size:13px">% que se suma a cada precio</div>
     <div class="hint">Ej: $325.000 + 15% → $374.000 (redondeo al mil ↑)</div></div></div>
  </div>
  <div class="card accent" data-tab="telegram"><h2>Cuenta de Telegram</h2>
   <label>Modo de envío</label>
   <select id="send_mode"><option value="bot">Bot — a suscriptores que dan /start</option><option value="userbot">Userbot — desde mi cuenta a mis contactos</option></select>

   <div class="section-label">Bot (crea o usa otro bot)</div>
   <label>Token del bot <span id="bot_status" class="hint"></span></label>
   <input id="bot_token" type="password" placeholder="(pega el token de @BotFather)">
   <div class="hint">Crea un bot en <code>@BotFather</code> (<code>/newbot</code>), pega su token, pulsa <b>Guardar cuenta</b> y luego <b>Registrar webhook</b> para conectarlo a la plataforma.</div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
     <button class="sec" onclick="tgVerify()">Verificar bot</button>
     <button class="sec" onclick="tgWebhook()">Registrar webhook</button>
     <span id="tg_state" class="hint" style="margin-top:0"></span>
   </div>

   <div class="section-label">Userbot (unir otra cuenta de Telegram)</div>
   <div class="row">
     <div><label>API ID</label><input id="telethon_api_id"></div>
     <div><label>API Hash</label><input id="telethon_api_hash"></div>
   </div>
   <label>StringSession <span id="sess_status" class="hint"></span></label>
   <input id="telethon_session" type="password" placeholder="(pega para unir/cambiar la cuenta)">
   <div class="hint">Genérala con <code>scripts/generar_sesion.py</code> usando la cuenta que quieras unir. Da acceso total a esa cuenta: trátala como secreto.</div>
   <button onclick="saveAccount()">Guardar cuenta</button>
  </div>
  <div class="card" data-tab="whatsapp"><h2>WhatsApp (reenvío)</h2>
   <label style="display:flex;align-items:center;gap:8px;margin-top:0"><input type="checkbox" id="whatsapp_enabled" style="width:auto"> Reenviar también cada lista por WhatsApp</label>
   <label>URL del servicio WhatsApp</label><input id="whatsapp_service_url" placeholder="https://...onrender.com">
   <label>Token del servicio <span id="wa_tok_status" class="hint"></span></label>
   <input id="whatsapp_token" type="password" placeholder="(pegar solo si quieres cambiarlo)">
   <button onclick="saveWhatsapp()">Guardar WhatsApp</button>
   <div style="margin-top:14px">
     <button class="sec" onclick="waStatus()">Ver estado</button> <span id="wa_state" class="hint"></span>
   </div>
   <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--bd)">
     <div style="font-weight:700;font-size:13px;margin-bottom:6px">Vincular WhatsApp</div>
     <div class="hint" style="margin-bottom:10px">Si el QR da "inténtalo más tarde" (típico desde servidores), usa el código de 8 dígitos.</div>
     <div class="row">
       <div>
         <button class="sec" style="width:100%" onclick="waStatus(true)">Mostrar QR</button>
         <div><img id="wa_qr" class="preview" style="display:none"></div>
         <div class="hint" id="wa_qr_hint" style="display:none">WhatsApp → Dispositivos vinculados → Vincular un dispositivo.</div>
       </div>
       <div>
         <input id="wa_pair_num" placeholder="Número con código país, ej: 573001234567">
         <button class="sec" style="width:100%;margin-top:8px" onclick="waPair()">Vincular por código</button>
         <div class="hint" id="wa_pair_out" style="display:none"></div>
       </div>
     </div>
     <div class="hint" style="margin-top:10px">💡 Lo más fiable si Render bloquea el linking: vincula <b>localmente</b> (corre el servicio en tu PC con las mismas credenciales AWS) y Render reusará la sesión guardada en DynamoDB.</div>
   </div>
   <div class="callout warn">⚠️ Enviar masivamente por WhatsApp puede banear tu número. Empieza con listas pequeñas. Las <b>exclusiones</b> se gestionan por nombre abajo, en <b>Destinatarios WhatsApp</b>.</div>
  </div>
  <div class="card" data-tab="msg"><h2>Canal y mensaje</h2>
   <label>Canal fuente (username sin @)</label><input id="source_channel">
   <label>Símbolos de moneda</label><input id="currency_symbols">
   <label>Footer WhatsApp (se añade al final de cada lista)</label><textarea id="whatsapp_footer"></textarea>
   <label>Patrones a quitar (ubicación), uno por línea</label><textarea id="strip_patterns"></textarea>
   <button onclick="saveCfg()">Guardar cambios</button>
  </div>
  <div class="card" data-tab="msg"><h2>Imagen de la lista</h2>
   <div class="hint">Se envía como foto antes de cada lista. Sube un archivo o pega una URL.</div>
   <input type="file" id="imgfile" accept="image/*" style="margin-top:10px" onchange="uploadImg()">
   <img id="imgprev" class="preview" style="display:none">
   <label>…o URL externa</label><input id="image_url" placeholder="https://...">
   <button class="sec" onclick="saveCfg()">Guardar URL</button>
  </div>
  <div class="card" data-tab="estado"><h2>Cola de mensajes</h2>
   <div class="stats"><div class="stat"><b id="q_p">–</b><span>lotes programados pendientes</span></div>
     <div class="stat"><b id="q_b">–</b><span>en cola SQS (en vuelo)</span></div>
     <div class="stat"><b id="q_d">–</b><span>en DLQ (fallidos)</span></div></div>
   <div class="hint" style="margin-top:10px">Con el envío fraccionado, los lotes esperan en la <b>programación</b> y se liberan de a uno; por eso "en cola SQS" suele ser 0 o 1 (el lote en vuelo). Mira el detalle en <b>⏱️ Programación</b>.</div>
   <button class="sec" style="margin-top:14px" onclick="loadQueue()">Refrescar</button>
  </div>
  <div class="card" data-tab="telegram"><h2>Destinatarios <span id="subcount" class="hint"></span></h2>
   <div class="hint">Busca, navega y usa los botones para incluir/excluir en masa. Los excluidos NO reciben las listas.</div>
   <input id="subsearch" placeholder="🔎 Buscar por nombre o número..." oninput="onSearch()" style="margin-top:10px">
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
     <button class="sec" onclick="toggleAll(true)">Marcar visibles</button>
     <button class="sec" onclick="toggleAll(false)">Desmarcar</button>
     <button onclick="bulk('excluir')">Excluir marcados</button>
     <button onclick="bulk('incluir')">Incluir marcados</button>
     <button class="ghost" onclick="bulkFiltered('excluir')">Excluir filtrados</button>
     <button class="ghost" onclick="bulkFiltered('incluir')">Incluir filtrados</button>
   </div>
   <table><thead><tr><th><input type="checkbox" id="selall" onchange="toggleAll(this.checked)"></th><th>nombre</th><th>estado</th></tr></thead><tbody id="subs"></tbody></table>
   <div class="hint" id="subsempty" style="display:none;margin-top:12px">Sin destinatarios (modo bot: nadie dio /start; modo userbot: la cuenta no tiene contactos).</div>
   <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
     <button class="sec" onclick="prevPage()">◀</button>
     <span id="pageinfo" class="hint"></span>
     <button class="sec" onclick="nextPage()">▶</button>
   </div>
  </div>
  <div class="card" data-tab="telegram"><h2>Listas de distribución · Telegram</h2>
   <div class="hint">Agrupa contactos en listas con nombre y elige a quién enviar. "+ marcados" usa los contactos marcados arriba en <b>Destinatarios</b>.</div>
   <div id="tg_lists" style="margin-top:10px"></div>
   <div style="display:flex;gap:8px;margin-top:10px"><input id="tg_newlist" placeholder="Nombre de nueva lista"><button class="sec" onclick="addList('telegram')">Crear lista</button></div>
   <div style="margin-top:12px"><b>Modo de envío:</b><br>
     <label style="display:inline-flex;align-items:center;gap:6px;width:auto;margin-right:14px"><input type="radio" name="mode_telegram" value="all" style="width:auto"> Todos</label>
     <label style="display:inline-flex;align-items:center;gap:6px;width:auto;margin-right:14px"><input type="radio" name="mode_telegram" value="only" style="width:auto"> Solo listas activas</label>
     <label style="display:inline-flex;align-items:center;gap:6px;width:auto"><input type="radio" name="mode_telegram" value="except" style="width:auto"> Excluir listas activas</label>
   </div>
   <button onclick="saveLists('telegram')">Guardar listas Telegram</button>
  </div>
  <div class="card" data-tab="whatsapp"><h2>Destinatarios WhatsApp <span id="wa_c_count" class="hint"></span></h2>
   <div class="hint">Carga tus contactos (servicio conectado), busca por nombre, y marca para <b>excluir/incluir</b>. Los excluidos NO reciben las difusiones por WhatsApp.</div>
   <button class="sec" style="margin-top:10px" onclick="loadWaContacts()">Cargar contactos de WhatsApp</button>
   <input id="wa_search" placeholder="🔎 Buscar por nombre o número..." oninput="renderWa()" style="margin-top:10px">
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
     <button class="sec" onclick="waToggleAll(true)">Marcar visibles</button>
     <button class="sec" onclick="waToggleAll(false)">Desmarcar</button>
     <button onclick="waBulk('excluir')">Excluir marcados</button>
     <button onclick="waBulk('incluir')">Incluir marcados</button>
   </div>
   <table><thead><tr><th></th><th>nombre</th><th>estado</th></tr></thead><tbody id="wa_subs"></tbody></table>
   <div style="display:flex;gap:12px;align-items:center;margin-top:10px"><button class="sec" onclick="waPrev()">◀</button><span id="wa_pageinfo" class="hint"></span><button class="sec" onclick="waNext()">▶</button></div>
  </div>
  <div class="card" data-tab="whatsapp"><h2>Listas de distribución · WhatsApp</h2>
   <div class="hint">"+ marcados" usa los contactos marcados arriba en <b>Destinatarios WhatsApp</b>.</div>
   <div id="wa_lists" style="margin-top:10px"></div>
   <div style="display:flex;gap:8px;margin-top:10px"><input id="wa_newlist" placeholder="Nombre de nueva lista"><button class="sec" onclick="addList('whatsapp')">Crear lista</button></div>
   <div style="margin-top:12px"><b>Modo de envío:</b><br>
     <label style="display:inline-flex;align-items:center;gap:6px;width:auto;margin-right:14px"><input type="radio" name="mode_whatsapp" value="all" style="width:auto"> Todos</label>
     <label style="display:inline-flex;align-items:center;gap:6px;width:auto;margin-right:14px"><input type="radio" name="mode_whatsapp" value="only" style="width:auto"> Solo listas activas</label>
     <label style="display:inline-flex;align-items:center;gap:6px;width:auto"><input type="radio" name="mode_whatsapp" value="except" style="width:auto"> Excluir listas activas</label>
   </div>
   <button onclick="saveLists('whatsapp')">Guardar listas WhatsApp</button>
  </div>
  <div class="card" data-tab="enviar"><h2>✍️ Componer y enviar</h2>
   <div class="hint">Escribe un mensaje y envíalo de inmediato a los canales seleccionados. Respeta las listas y exclusiones configuradas en cada canal.</div>
   <label>Mensaje <span id="bc_count" class="charcount">0 caracteres</span></label>
   <textarea id="bc_text" style="min-height:120px" placeholder="Escribe aquí el mensaje a difundir..." oninput="bcCount()"></textarea>

   <label>Imagen (opcional)</label>
   <div class="img-slot">
     <div class="meta">
       <input type="file" id="bc_imgfile" accept="image/*" onchange="bcUploadImg()">
       <input id="bc_image_url" placeholder="…o pega una URL de imagen" style="margin-top:10px" oninput="bcPreview()">
     </div>
     <img id="bc_imgprev" class="preview" style="display:none">
   </div>

   <label style="margin-top:16px">Canales</label>
   <div class="chan-row">
     <label class="chan tg on" id="bc_chan_tg"><span class="dot"></span><input type="checkbox" id="bc_telegram" checked onchange="bcChan()" style="display:none">✈️ Telegram</label>
     <label class="chan wa" id="bc_chan_wa"><span class="dot"></span><input type="checkbox" id="bc_whatsapp" onchange="bcChan()" style="display:none">🟢 WhatsApp</label>
   </div>
   <div class="hint">⚠️ El envío masivo por WhatsApp puede banear tu número. El sistema lo hace con ritmo lento (anti-baneo); úsalo con listas pequeñas.</div>

   <label style="margin-top:16px">Enviar a</label>
   <div class="row">
     <div id="bc_tg_wrap"><div class="hint" style="margin-top:0">Telegram — busca y marca contactos</div>
       <input id="bc_tg_search" placeholder="🔎 Buscar por nombre o número..." oninput="bcRenderPick('tg')">
       <div id="bc_tg_pick" class="pickbox"></div>
       <div class="hint" style="margin-top:6px">o una lista: <select id="bc_tg_list" onchange="bcPrev()"><option value="">(según configuración)</option></select></div>
     </div>
     <div id="bc_wa_wrap" style="display:none"><div class="hint" style="margin-top:0">WhatsApp — busca y marca contactos</div>
       <input id="bc_wa_search" placeholder="🔎 Buscar por nombre o número..." oninput="bcRenderPick('wa')">
       <div id="bc_wa_pick" class="pickbox"></div>
       <div class="hint" style="margin-top:6px">o una lista: <select id="bc_wa_list" onchange="bcPrev()"><option value="">— elige una lista —</option></select></div>
     </div>
   </div>
   <div class="hint" id="bc_preview" style="margin-top:10px">—</div>

   <div class="compose-actions">
     <button id="bc_send" onclick="sendBroadcast()">Enviar ahora</button>
     <button class="ghost" onclick="bcClear()">Limpiar</button>
     <span class="grow"></span>
     <span id="bc_status" class="hint" style="margin-top:0"></span>
   </div>
  </div>
  <div class="card" data-tab="enviar"><h2>📡 Envíos <span class="live" id="bc_live" style="margin-left:auto"><span class="ping"></span><span id="bc_live_t">en vivo</span></span></h2>
   <div class="hint">Estado y progreso de cada difusión. Se actualiza automáticamente mientras hay envíos en curso.</div>
   <div style="overflow-x:auto;margin-top:12px">
     <table id="bc_table"><thead><tr><th>Mensaje</th><th>Estado</th><th>Progreso</th></tr></thead>
       <tbody id="bc_rows"></tbody></table>
   </div>
   <div class="bc-empty" id="bc_empty" style="display:none">Aún no hay envíos. Crea uno en <b>Componer y enviar</b>.</div>
   <div style="margin-top:14px"><button class="sec" onclick="loadBroadcasts()">Refrescar</button></div>
  </div>
  <div class="card accent" data-tab="prog"><h2>Interruptor de envíos</h2>
   <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
     <label style="display:flex;align-items:center;gap:10px;margin:0;font-size:15px;color:var(--tx)"><input type="checkbox" id="sending_enabled" style="width:auto;transform:scale(1.3)" onchange="toggleSending()"> <b>Envíos activos</b></label>
     <span id="sys_badge" class="pill">—</span>
   </div>
   <div class="hint" style="margin-top:10px">Apágalo para <b>PAUSAR al instante todos los envíos</b> (Telegram y WhatsApp). Las difusiones quedan en espera; nada sale hasta reactivar. Útil como freno de emergencia.</div>
   <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--bd)">
     <div class="hint" style="margin-top:0">¿Hay difusiones en cola que NO quieres enviar? Cancélalas (no se enviarán, ni al reactivar).</div>
     <button class="ghost" style="margin-top:8px" onclick="cancelPending()">🗑 Cancelar difusiones pendientes</button>
   </div>
  </div>
  <div class="card accent" data-tab="prog"><h2>Anti-baneo · lotes y ritmo</h2>
   <label style="display:flex;align-items:center;gap:8px;margin-top:0"><input type="checkbox" id="scheduling_enabled" style="width:auto"> Envío fraccionado y secuencial (procesa un lote a la vez)</label>
   <div class="row">
     <div><label>Tamaño de lote (máx 150)</label><input id="batch_size" type="number" min="1" max="150"></div>
     <div><label>Zona horaria (min vs UTC, ej -300)</label><input id="window_tz" type="number" step="1"></div>
   </div>
   <div class="row">
     <div><label>Delay Telegram mín (s)</label><input id="tg_delay_min" type="number" step="0.1" min="0"></div>
     <div><label>Delay Telegram máx (s)</label><input id="tg_delay_max" type="number" step="0.1" min="0"></div>
   </div>
   <div class="row">
     <div><label>Delay WhatsApp mín (ms)</label><input id="wa_delay_min" type="number" step="100" min="0"></div>
     <div><label>Delay WhatsApp máx (ms)</label><input id="wa_delay_max" type="number" step="100" min="0"></div>
   </div>
   <div class="callout">El delay entre mensajes es <b>aleatorio</b> dentro del rango (evita patrones predecibles). El dispatcher libera <b>un lote por minuto</b> y espera a que termine el anterior antes de soltar el siguiente.</div>
   <button onclick="saveSched()">Guardar anti-baneo</button>
  </div>
  <div class="card" data-tab="prog"><h2>Ventana de envío (horario permitido)</h2>
   <label style="display:flex;align-items:center;gap:8px;margin-top:0"><input type="checkbox" id="window_enabled" style="width:auto"> Enviar solo dentro del horario permitido</label>
   <div class="row">
     <div><label>Desde (HH:MM)</label><input id="window_start" placeholder="08:00"></div>
     <div><label>Hasta (HH:MM)</label><input id="window_end" placeholder="20:00"></div>
   </div>
   <div class="hint">Fuera del horario, los lotes quedan <b>encolados</b> y se procesan de forma diferida al reabrir la ventana. Soporta cruzar medianoche (p.ej. 22:00 → 06:00).</div>
   <button onclick="saveSched()">Guardar ventana</button>
  </div>
  <div class="card" data-tab="prog"><h2>📦 Envíos fraccionados <span class="live" id="pl_live" style="margin-left:auto"><span class="ping"></span><span id="pl_live_t">en vivo</span></span></h2>
   <div class="hint">De cada lote programado se muestra <b>cuántos mensajes se han enviado</b>. El sistema procesa un lote a la vez, en orden.</div>
   <div id="pl_list" style="margin-top:12px"></div>
   <div class="bc-empty" id="pl_empty" style="display:none">No hay envíos programados todavía. Crea uno en <b>Enviar</b> o espera al próximo del canal.</div>
   <div style="margin-top:14px"><button class="sec" onclick="loadPlans()">Refrescar</button></div>
  </div>
 </main>
</div>

<div class="toast" id="toast"></div>
<script>
const BASE = location.pathname.replace(/\/admin.*/, '/admin');
const $ = id => document.getElementById(id);
let CRED = sessionStorage.getItem('cred') || '';
function hdr(extra){ return Object.assign({Authorization:'Basic '+CRED}, extra||{}); }
async function api(p, opt){ opt=opt||{}; opt.headers=hdr(opt.headers); const r=await fetch(BASE+p,opt);
  if(r.status===401){ logout(); throw new Error('401'); } if(!r.ok) throw new Error(r.status); return r.json(); }
function toast(m,v){ const t=$('toast'); t.textContent=m;
  const cls = v===true ? 'err' : (typeof v==='string' && v ? v : '');  // true=err (compat); 'info'/'warn'/'err'
  t.className='toast show'+(cls?' '+cls:''); setTimeout(()=>t.className='toast',2200); }
async function doLogin(){ const u=$('lu').value, p=$('lp').value; CRED=btoa(u+':'+p);
  try{ await fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{if(!r.ok)throw 0;}); sessionStorage.setItem('cred',CRED);
    $('login').style.display='none'; $('app').style.display='block'; $('who').textContent=u; boot(); }
  catch(e){ $('lerr').textContent='Usuario o contraseña incorrectos'; } }
function logout(){ sessionStorage.removeItem('cred'); CRED=''; $('app').style.display='none'; $('login').style.display='flex'; }
async function loadCfg(){ const c=await api('/api/config');
  ['source_channel','markup_percentage','currency_symbols','whatsapp_footer','image_url','telethon_api_id','telethon_api_hash'].forEach(k=>$(k).value=c[k]??'');
  $('send_mode').value=c.send_mode||'bot';
  $('sess_status').textContent = c.telethon_session_set ? '· conectada ✓' : '· no configurada';
  $('bot_status').textContent = c.bot_token_set ? '· configurado ✓' : '· no configurado';
  $('strip_patterns').value=(c.strip_patterns||[]).join('\n');
  $('whatsapp_enabled').checked=!!c.whatsapp_enabled; $('whatsapp_service_url').value=c.whatsapp_service_url||'';
  $('wa_tok_status').textContent = c.whatsapp_token_set ? '· configurado ✓' : '· no configurado';
  WA_EXCLUDED=new Set((c.whatsapp_excluded||[]).map(String)); if($('wa_subs')) renderWa();
  LISTS.telegram=c.telegram_lists||[]; TGT.telegram=c.telegram_target||{mode:'all',lists:[]};
  LISTS.whatsapp=c.whatsapp_lists||[]; TGT.whatsapp=c.whatsapp_target||{mode:'all',lists:[]};
  renderLists('telegram'); renderLists('whatsapp');
  // --- anti-baneo / ventana ---
  ['batch_size','tg_delay_min','tg_delay_max','wa_delay_min','wa_delay_max','window_tz','window_start','window_end'].forEach(k=>{ if($(k)) $(k).value=c[k]??''; });
  if($('scheduling_enabled')) $('scheduling_enabled').checked = c.scheduling_enabled!==false;
  if($('window_enabled')) $('window_enabled').checked = !!c.window_enabled;
  renderSendingState(c.sending_enabled!==false); }
function renderSendingState(on){
  if($('sending_enabled')) $('sending_enabled').checked = on;
  const badge=$('sys_badge'); if(badge){ badge.className='pill '+(on?'active':'failed'); badge.textContent = on?'ACTIVOS':'PAUSADOS'; }
  const hb=$('hdr_badge'); if(hb){ if(on){ hb.style.display='none'; } else { hb.style.display='inline-block'; hb.className='pill failed'; hb.textContent='⏸ Envíos pausados'; } }
}
async function toggleSending(){
  const on=$('sending_enabled').checked;
  if(!on && !confirm('¿Pausar TODOS los envíos (Telegram y WhatsApp)? Nada saldrá hasta reactivar.')){ $('sending_enabled').checked=true; return; }
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sending_enabled:on})});
    renderSendingState(on); toast(on?'✓ Envíos ACTIVADOS':'⏸ Envíos PAUSADOS', on?'info':'warn'); }
  catch(e){ toast('Error al cambiar el estado',true); $('sending_enabled').checked=!on; }
}
async function cancelPending(){
  if(!confirm('¿Cancelar todas las difusiones pendientes/en curso? No se enviarán (ni al reactivar).')) return;
  try{ const r=await api('/api/plans/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    toast('✓ '+(r.canceled||0)+' difusión(es) cancelada(s)'); loadPlans(); loadQueue(); }
  catch(e){ toast('Error al cancelar',true); }
}
async function saveSched(){
  const b={ scheduling_enabled:$('scheduling_enabled').checked, window_enabled:$('window_enabled').checked,
    batch_size:parseInt($('batch_size').value||'150',10),
    tg_delay_min:parseFloat($('tg_delay_min').value||'1'), tg_delay_max:parseFloat($('tg_delay_max').value||'4'),
    wa_delay_min:parseInt($('wa_delay_min').value||'3000',10), wa_delay_max:parseInt($('wa_delay_max').value||'9000',10),
    window_start:($('window_start').value||'08:00').trim(), window_end:($('window_end').value||'20:00').trim(),
    window_tz:parseInt($('window_tz').value||'-300',10) };
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); toast('✓ Guardado'); loadCfg(); }
  catch(e){ toast('Error al guardar',true); } }
async function saveWhatsapp(){ const b={ whatsapp_enabled:$('whatsapp_enabled').checked, whatsapp_service_url:$('whatsapp_service_url').value };
   const tok=$('whatsapp_token').value; if(tok) b.whatsapp_token=tok;
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    toast('✓ WhatsApp guardado'); $('whatsapp_token').value=''; loadCfg(); } catch(e){ toast('Error',true); } }
async function waStatus(showQr){ $('wa_state').textContent='consultando...';
  try{ const s=await api('/api/whatsapp/status');
    let txt = s.connected ? ('conectado ✓ ('+(s.contacts||0)+' contactos)') : 'no conectado';
    if(!s.connected && s.lastClose) txt += ' · último cierre: '+s.lastClose;
    $('wa_state').textContent = txt;
    if(showQr && s.qr){ $('wa_qr').src=s.qr; $('wa_qr').style.display='block'; $('wa_qr_hint').style.display='block'; }
    else if(!showQr){ $('wa_qr').style.display='none'; $('wa_qr_hint').style.display='none'; }
  }catch(e){ $('wa_state').textContent='servicio inaccesible (¿URL/token? ¿desplegado?)'; } }
async function waPair(){ const num=$('wa_pair_num').value.replace(/[^0-9]/g,''); const out=$('wa_pair_out');
  if(num.length<8){ toast('Número inválido (incluye código de país, sin +)',true); return; }
  out.style.display='block'; out.textContent='generando código... (puede tardar unos segundos)';
  try{ const r=await api('/api/whatsapp/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({number:num})});
    if(r.pairingCode){ out.innerHTML='Código: <b style="font-size:19px;letter-spacing:3px;color:var(--ac2)">'+r.pairingCode+'</b><br>En el teléfono: WhatsApp → Dispositivos vinculados → <b>Vincular con número de teléfono</b> → ingresa el código.'; }
    else { out.textContent='No se pudo generar: '+(r.error||r.detalle||'desconocido'); } }
  catch(e){ out.textContent='Error: el servicio no respondió (¿ya conectado? ¿URL/token?)'; } }
async function saveAccount(){ const b={ send_mode:$('send_mode').value, telethon_api_id:$('telethon_api_id').value,
   telethon_api_hash:$('telethon_api_hash').value, telethon_session:$('telethon_session').value };
  const bt=$('bot_token').value.trim(); if(bt) b.bot_token=bt;
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    toast('✓ Cuenta guardada'); $('telethon_session').value=''; $('bot_token').value=''; loadCfg(); loadSubs(); } catch(e){ toast('Error',true); } }
async function tgVerify(){ $('tg_state').textContent='verificando...';
  try{ const r=await api('/api/telegram/me'); $('tg_state').textContent = (r.ok && r.result) ? ('bot ✓ @'+(r.result.username||r.result.id)) : ('error: '+(r.description||'token inválido')); }
  catch(e){ $('tg_state').textContent='error verificando (¿token guardado?)'; } }
async function tgWebhook(){ $('tg_state').textContent='registrando webhook...';
  try{ const r=await api('/api/telegram/webhook',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    $('tg_state').textContent = (r.ok || r.result===true) ? 'webhook registrado ✓ (el bot ya recibe /start)' : ('error: '+(r.description||r.detalle||r.error||'desconocido')); }
  catch(e){ $('tg_state').textContent='error registrando webhook'; } }
async function saveCfg(){ const b={ source_channel:$('source_channel').value, markup_percentage:parseFloat($('markup_percentage').value),
   currency_symbols:$('currency_symbols').value, whatsapp_footer:$('whatsapp_footer').value, image_url:$('image_url').value,
   strip_patterns:$('strip_patterns').value };
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); toast('✓ Guardado'); loadCfg(); }
  catch(e){ toast('Error al guardar',true); } }
async function uploadImg(){ const f=$('imgfile').files[0]; if(!f) return;
  const r=new FileReader(); r.onload=async()=>{ const b64=r.result.split(',')[1];
    try{ await api('/api/image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:b64,content_type:f.type})});
      $('imgprev').src=r.result; $('imgprev').style.display='block'; toast('✓ Imagen subida'); }
    catch(e){ toast('Error al subir',true); } }; r.readAsDataURL(f); }
async function loadQueue(){
  try{ const q=await api('/api/queue'); $('q_b').textContent=q.broadcast; $('q_d').textContent=q.dlq; }catch(e){}
  try{ const r=await api('/api/plans'); let pend=0;
    (r.plans||[]).forEach(p=>{ if(p.status==='pending'||p.status==='running'){
      pend += Math.max(0,(p.tg.batches|0)-(p.tg.next|0)) + Math.max(0,(p.wa.batches|0)-(p.wa.next|0)); }});
    if($('q_p')) $('q_p').textContent=pend;
  }catch(e){ if($('q_p')) $('q_p').textContent='–'; }
}
let Q_TIMER=null;
function qStartPolling(){
  if(Q_TIMER) return; loadQueue();
  Q_TIMER=setInterval(()=>{ if(!CRED||document.hidden) return;
    const vis=document.querySelector('main>.card[data-tab="estado"]')?.classList.contains('show');
    if(vis) loadQueue(); }, BC_POLL);
}
let EXCLUDED=new Set(), DEST=[], FILTER='', PAGE=0;
const PAGE_SIZE=50;
function filtered(){ if(!FILTER) return DEST; const q=FILTER.toLowerCase();
  return DEST.filter(s=> (s.name||'').toLowerCase().includes(q) || String(s.chatId||'').toLowerCase().includes(q) || String(s.phone||'').toLowerCase().includes(q)); }
function render(){ const f=filtered(); const pages=Math.max(1,Math.ceil(f.length/PAGE_SIZE));
  if(PAGE>=pages) PAGE=pages-1; if(PAGE<0) PAGE=0;
  const slice=f.slice(PAGE*PAGE_SIZE,(PAGE+1)*PAGE_SIZE);
  const t=$('subs'); t.innerHTML=''; $('selall').checked=false;
  $('subsempty').style.display=DEST.length?'none':'block';
  $('subcount').textContent = DEST.length ? `· ${f.length}${FILTER?' filtrados':''} de ${DEST.length} (${EXCLUDED.size} excluidos)` : '';
  $('pageinfo').textContent = f.length ? `página ${PAGE+1} de ${pages}` : 'sin resultados';
  slice.forEach(s=>{ const ex=EXCLUDED.has(String(s.chatId)); const label=s.name||'(sin nombre)'; const tr=document.createElement('tr');
    tr.innerHTML=`<td><input type="checkbox" class="selrow" data-id="${s.chatId}"></td>`+
      `<td><b>${bcEsc(label)}</b></td>`+
      `<td><span class="pill ${ex?'inactive':'active'}">${ex?'Excluido':'Incluido'}</span></td>`;
    t.appendChild(tr); }); }
async function loadSubs(){ const [d,c]=await Promise.all([api('/api/subscribers'),api('/api/config')]);
  DEST=d.subscribers||[]; EXCLUDED=new Set((c.excluded_ids||[]).map(String)); render(); }
function onSearch(){ FILTER=$('subsearch').value.trim(); PAGE=0; render(); }
function prevPage(){ PAGE--; render(); }
function nextPage(){ PAGE++; render(); }
function toggleAll(v){ document.querySelectorAll('.selrow').forEach(c=>c.checked=v); $('selall').checked=v; }
function selectedIds(){ return [...document.querySelectorAll('.selrow:checked')].map(c=>String(c.dataset.id)); }
async function persistExcluded(){ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({excluded_ids:[...EXCLUDED]})}); render(); loadCfg(); }
async function bulk(accion){ const ids=selectedIds(); if(!ids.length){ toast('Marca al menos un contacto (visible)',true); return; }
  ids.forEach(id=> accion==='excluir'?EXCLUDED.add(id):EXCLUDED.delete(id));
  await persistExcluded(); toast('✓ '+ids.length+' '+(accion==='excluir'?'excluidos':'incluidos')); }
async function bulkFiltered(accion){ const ids=filtered().map(s=>String(s.chatId)); if(!ids.length){ toast('Sin contactos que coincidan',true); return; }
  ids.forEach(id=> accion==='excluir'?EXCLUDED.add(id):EXCLUDED.delete(id));
  await persistExcluded(); toast('✓ '+ids.length+(FILTER?' filtrados ':' ')+(accion==='excluir'?'excluidos':'incluidos')); }
// --- listas de distribución (genérico para ambos canales) ---
let LISTS={telegram:[],whatsapp:[]}, TGT={telegram:{mode:'all',lists:[]},whatsapp:{mode:'all',lists:[]}};
function listsBox(ch){ return ch==='telegram'?'tg_lists':'wa_lists'; }
function selForChannel(ch){ return ch==='telegram'?selectedIds():waSelectedIds(); }
function renderLists(ch){ const cont=$(listsBox(ch)); cont.innerHTML='';
  const active=new Set((TGT[ch]||{}).lists||[]);
  if(!LISTS[ch].length){ cont.innerHTML='<div class="hint">Sin listas todavía.</div>'; }
  LISTS[ch].forEach((l,i)=>{ const row=document.createElement('div');
    row.style.cssText='display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid #eee';
    row.innerHTML=`<label style="display:inline-flex;align-items:center;gap:6px;width:auto;margin:0"><input type="checkbox" ${active.has(l.name)?'checked':''} style="width:auto" onchange="toggleListActive('${ch}',${i},this.checked)"> <b>${l.name}</b></label>`+
      `<span class="hint">${l.ids.length} miembros</span>`+
      `<button class="sec" onclick="addToList('${ch}',${i})">+ marcados</button>`+
      `<button class="ghost" onclick="removeFromList('${ch}',${i})">− marcados</button>`+
      `<button class="ghost" onclick="delList('${ch}',${i})">🗑</button>`;
    cont.appendChild(row); });
  document.querySelectorAll(`input[name=mode_${ch}]`).forEach(r=>r.checked=(r.value===((TGT[ch]||{}).mode||'all'))); }
function addList(ch){ const inp=$(ch==='telegram'?'tg_newlist':'wa_newlist'); const n=inp.value.trim(); if(!n)return;
  if(LISTS[ch].some(l=>l.name===n)){ toast('Ya existe una lista con ese nombre',true); return; }
  LISTS[ch].push({name:n,ids:[]}); inp.value=''; renderLists(ch); }
function delList(ch,i){ const n=LISTS[ch][i].name; if(!confirm('¿Borrar la lista "'+n+'"?'))return;
  LISTS[ch].splice(i,1); TGT[ch].lists=(TGT[ch].lists||[]).filter(x=>x!==n); renderLists(ch); }
function toggleListActive(ch,i,v){ const n=LISTS[ch][i].name; const s=new Set(TGT[ch].lists||[]); v?s.add(n):s.delete(n); TGT[ch].lists=[...s]; }
function addToList(ch,i){ const ids=selForChannel(ch); if(!ids.length){ toast('Marca contactos primero',true); return; }
  LISTS[ch][i].ids=[...new Set([...LISTS[ch][i].ids.map(String),...ids])]; renderLists(ch); toast('✓ '+ids.length+' añadidos a '+LISTS[ch][i].name); }
function removeFromList(ch,i){ const ids=new Set(selForChannel(ch)); if(!ids.size){ toast('Marca contactos primero',true); return; }
  LISTS[ch][i].ids=LISTS[ch][i].ids.filter(x=>!ids.has(String(x))); renderLists(ch); toast('✓ quitados de '+LISTS[ch][i].name); }
function curMode(ch){ const r=document.querySelector(`input[name=mode_${ch}]:checked`); return r?r.value:'all'; }
async function saveLists(ch){ TGT[ch].mode=curMode(ch);
  const body=ch==='telegram'?{telegram_lists:LISTS.telegram,telegram_target:TGT.telegram}:{whatsapp_lists:LISTS.whatsapp,whatsapp_target:TGT.whatsapp};
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); toast('✓ Listas guardadas'); loadCfg(); }
  catch(e){ toast('Error al guardar',true); } }
// --- contactos de WhatsApp (para armar listas de WhatsApp) ---
let WA_DEST=[], WA_PAGE=0, WA_EXCLUDED=new Set();
function waName(c){ return c.name || '(sin nombre)'; }
async function loadWaContacts(){ $('wa_c_count').textContent='· cargando...';
  try{ const r=await api('/api/whatsapp/contacts'); WA_DEST=r.contacts||[]; WA_PAGE=0; renderWa(); }
  catch(e){ $('wa_c_count').textContent='· servicio inaccesible (¿conectado?)'; } }
function waFiltered(){ const q=($('wa_search').value||'').trim().toLowerCase(); if(!q) return WA_DEST;
  return WA_DEST.filter(c=> waName(c).toLowerCase().includes(q) || String(c.id||'').toLowerCase().includes(q)); }
function renderWa(){ const f=waFiltered(); const pages=Math.max(1,Math.ceil(f.length/PAGE_SIZE));
  if(WA_PAGE>=pages)WA_PAGE=pages-1; if(WA_PAGE<0)WA_PAGE=0; const slice=f.slice(WA_PAGE*PAGE_SIZE,(WA_PAGE+1)*PAGE_SIZE);
  const t=$('wa_subs'); t.innerHTML='';
  $('wa_c_count').textContent = WA_DEST.length ? `· ${f.length} de ${WA_DEST.length} (${WA_EXCLUDED.size} excluidos)` : '';
  slice.forEach(c=>{ const id=String(c.id||''); const ex=WA_EXCLUDED.has(id); const tr=document.createElement('tr');
    tr.innerHTML=`<td><input type="checkbox" class="wsel" data-id="${id}"></td><td><b>${bcEsc(waName(c))}</b></td>`+
      `<td><span class="pill ${ex?'inactive':'active'}">${ex?'Excluido':'Incluido'}</span></td>`; t.appendChild(tr); });
  $('wa_pageinfo').textContent=f.length?`página ${WA_PAGE+1} de ${pages}`:'sin resultados'; }
function waSelectedIds(){ return [...document.querySelectorAll('.wsel:checked')].map(c=>String(c.dataset.id)); }
function waToggleAll(v){ document.querySelectorAll('.wsel').forEach(c=>c.checked=v); }
async function persistWaExcluded(){ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({whatsapp_excluded:[...WA_EXCLUDED]})}); renderWa(); }
async function waBulk(accion){ const ids=waSelectedIds(); if(!ids.length){ toast('Marca al menos un contacto',true); return; }
  ids.forEach(id=> accion==='excluir'?WA_EXCLUDED.add(id):WA_EXCLUDED.delete(id));
  await persistWaExcluded(); toast('✓ '+ids.length+' '+(accion==='excluir'?'excluidos':'incluidos')); }
function waPrev(){ WA_PAGE--; renderWa(); }
function waNext(){ WA_PAGE++; renderWa(); }
function showTab(t){
  document.querySelectorAll('main>.card').forEach(c=>c.classList.toggle('show', c.dataset.tab===t));
  document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('on', b.dataset.tab===t));
  try{ localStorage.setItem('tab',t); }catch(e){}
  window.scrollTo(0,0); }
function boot(){ showTab((()=>{try{return localStorage.getItem('tab')}catch(e){return null}})()||'msg'); loadCfg(); loadQueue(); loadSubs(); }
if(CRED){ fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{ if(r.ok){ $('login').style.display='none'; $('app').style.display='block'; boot(); } }).catch(()=>{}); }

// ===== Componer y enviar (POST /api/broadcast) =====
let BC_IMG_URL = '';           // URL devuelta tras subir un archivo a /api/image
function bcCount(){ const n=$('bc_text').value.length; $('bc_count').textContent=n+(n===1?' carácter':' caracteres'); }
// --- selección de contactos (picker) en el compositor ---
let BC_TG_SEL=new Set(), BC_WA_SEL=new Set();
function bcSel(ch){ return ch==='tg'?BC_TG_SEL:BC_WA_SEL; }
function bcNameOf(ch,c){ return ch==='tg'?(c.name||'(sin nombre)'):waName(c); }
function bcIdOf(ch,c){ return ch==='tg'?String(c.chatId):String(c.id); }
function bcRenderPick(ch){
  const box=$('bc_'+ch+'_pick'); if(!box) return;
  const data=(ch==='tg'?DEST:WA_DEST)||[], sel=bcSel(ch);
  const q=($('bc_'+ch+'_search').value||'').trim().toLowerCase();
  const f=data.filter(c=>{ if(!q) return true;
    const id=(ch==='tg'?String(c.chatId||''):String(c.id||'')).toLowerCase();
    return bcNameOf(ch,c).toLowerCase().includes(q) || id.includes(q); }).slice(0,40);
  box.innerHTML = f.length ? f.map(c=>{ const id=bcIdOf(ch,c);
    return `<label class="pickitem"><input type="checkbox" ${sel.has(id)?'checked':''} onchange="bcTogglePick('${ch}','${id}',this.checked)"> ${bcEsc(bcNameOf(ch,c))}</label>`; }).join('')
    : '<div class="hint">'+(data.length?'sin resultados':'sin contactos cargados aún')+'</div>';
}
function bcTogglePick(ch,id,on){ const s=bcSel(ch); on?s.add(id):s.delete(id); bcPrev(); }
function bcChan(){
  const tg=$('bc_telegram').checked, wa=$('bc_whatsapp').checked;
  $('bc_chan_tg').classList.toggle('on', tg);
  $('bc_chan_wa').classList.toggle('on', wa);
  $('bc_tg_wrap').style.display = tg ? 'block':'none';
  $('bc_wa_wrap').style.display = wa ? 'block':'none';
  if(wa && !WA_DEST.length){ loadWaContacts().then(()=>bcRenderPick('wa')); }
  bcRenderPick('tg'); bcRenderPick('wa'); bcPrev();
}
// Rellena los selectores de lista (conserva selección) y refresca los pickers.
function bcFillLists(){
  const fill=(sel,arr,first)=>{ const cur=sel.value;
    sel.innerHTML='<option value="">'+first+'</option>'+ (arr||[]).map(l=>`<option value="${bcEsc(l.name)}">${bcEsc(l.name)} (${(l.ids||[]).length})</option>`).join('');
    sel.value=cur; };
  fill($('bc_tg_list'), (LISTS&&LISTS.telegram)||[], '(según configuración)');
  fill($('bc_wa_list'), (LISTS&&LISTS.whatsapp)||[], '— elige una lista —');
  bcRenderPick('tg'); bcRenderPick('wa');
}
// Cuerpo común: contactos elegidos (ids) tienen prioridad; si no, la lista del select.
function bcBody(extra){
  const b=Object.assign({ telegram:$('bc_telegram').checked, whatsapp:$('bc_whatsapp').checked,
    telegram_list:$('bc_tg_list').value, whatsapp_list:$('bc_wa_list').value }, extra||{});
  if(BC_TG_SEL.size) b.telegram_ids=[...BC_TG_SEL];
  if(BC_WA_SEL.size) b.whatsapp_ids=[...BC_WA_SEL];
  return b;
}
let BC_PREV_T=null;
async function bcPrev(){
  clearTimeout(BC_PREV_T);
  const tg=$('bc_telegram').checked, wa=$('bc_whatsapp').checked, out=$('bc_preview');
  if(!tg && !wa){ out.textContent='—'; return; }
  out.textContent='calculando destinatarios…';
  BC_PREV_T=setTimeout(async()=>{
    try{ const r=await api('/api/broadcast/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(bcBody())});
      const parts=[];
      if(tg) parts.push('✈️ Telegram: <b>'+(r.telegram??0)+'</b>');
      if(wa) parts.push('🟢 WhatsApp: <b>'+(r.whatsapp??0)+'</b>');
      out.innerHTML='Se enviará a → '+parts.join(' · ');
    }catch(e){ out.textContent='no se pudo calcular la previsualización'; }
  }, 300);
}
function bcEffectiveUrl(){ return ($('bc_image_url').value || '').trim() || BC_IMG_URL || ''; }
function bcPreview(){
  const u=bcEffectiveUrl(), p=$('bc_imgprev');
  if(u){ p.src=u; p.style.display='block'; } else { p.style.display='none'; p.removeAttribute('src'); }
}
async function bcUploadImg(){
  const f=$('bc_imgfile').files[0]; if(!f) return;
  $('bc_status').textContent='subiendo imagen...';
  const r=new FileReader();
  r.onload=async()=>{ const b64=r.result.split(',')[1];
    try{
      const res=await api('/api/image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:b64,content_type:f.type})});
      BC_IMG_URL = res.url || res.image_url || '';   // si el backend devuelve URL pública la usamos
      $('bc_imgprev').src=r.result; $('bc_imgprev').style.display='block';
      $('bc_status').textContent=''; toast('✓ Imagen lista');
    }catch(e){ $('bc_status').textContent=''; toast('Error al subir imagen',true); }
  };
  r.readAsDataURL(f);
}
function bcClear(){
  $('bc_text').value=''; $('bc_image_url').value=''; BC_IMG_URL='';
  $('bc_imgfile').value=''; $('bc_imgprev').style.display='none'; $('bc_imgprev').removeAttribute('src');
  BC_TG_SEL.clear(); BC_WA_SEL.clear();
  $('bc_tg_search').value=''; $('bc_wa_search').value=''; $('bc_tg_list').value=''; $('bc_wa_list').value='';
  bcRenderPick('tg'); bcRenderPick('wa');
  $('bc_status').textContent=''; bcCount(); bcPrev();
}
async function sendBroadcast(){
  const text=$('bc_text').value.trim();
  const tg=$('bc_telegram').checked, wa=$('bc_whatsapp').checked;
  if(!text && !bcEffectiveUrl()){ toast('Escribe un mensaje o adjunta una imagen',true); return; }
  if(!tg && !wa){ toast('Elige al menos un canal',true); return; }
  let msg='¿Enviar este mensaje ahora?';
  if(wa) msg+='\n\n⚠️ El envío masivo por WhatsApp puede banear tu número.';
  if(!confirm(msg)) return;
  const body=bcBody({ text });
  const url=bcEffectiveUrl(); if(url) body.image_url=url;
  const btn=$('bc_send'); btn.disabled=true; $('bc_status').textContent='encolando...';
  try{
    await api('/api/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    // OJO: "programado" != "entregado". El envío se fracciona y la ENTREGA real se
    // confirma abajo en la tabla de Envíos (queda en cola → enviando → completado).
    toast('✓ Programado — la entrega se confirma abajo en Envíos','info'); $('bc_status').textContent='';
    bcClear();
    showTab('enviar');          // ambas tarjetas viven en la pestaña "enviar"
    loadBroadcasts();           // refresca la tabla de Envíos de inmediato
  }catch(e){ $('bc_status').textContent=''; toast('Error al encolar',true); }
  finally{ btn.disabled=false; }
}
// Al abrir la pestaña Enviar: rellenar listas + previsualizar (hook aditivo sobre showTab).
(function(){ const _s=window.showTab;
  if(typeof _s==='function'){ window.showTab=function(t){ _s(t); if(t==='enviar'){ try{ bcFillLists(); bcChan(); }catch(e){} } }; }
})();
// ===== Envíos: listado + polling (GET /api/broadcasts) =====
let BC_TIMER=null;
const BC_POLL=4000;
const BC_STATUS={ queued:'En cola', sending:'Enviando', done:'Completado', failed:'Fallido', partial:'Parcial' };
function bcEsc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function bcFmtTime(t){
  if(!t) return '';
  const d=new Date(typeof t==='number'? (t<1e12? t*1000 : t) : t);
  if(isNaN(d)) return bcEsc(t);
  return d.toLocaleString('es',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
}
function bcChanCell(isWa, data){
  const total=(data&&data.total)|0, sent=(data&&data.sent)|0, fail=(data&&data.failed)|0;
  if(!total){ return `<div class="ch ${isWa?'wa':'tg'}"><span class="ic"></span><span class="muted">${isWa?'WhatsApp':'Telegram'} —</span></div>`; }
  const done=sent+fail;
  const pct=total? Math.round(done/total*100):0;
  const full=done>=total;
  const failTxt=fail? ` <span class="fail">(${fail} fallidos)</span>`:'';
  return `<div class="ch ${isWa?'wa':'tg'}"><span class="ic"></span>`+
    `<span class="num">${sent}/${total}${failTxt}</span>`+
    `<span class="bar ${isWa?'wa':''} ${full&&!fail?'full':''} ${fail&&full?'err':''}"><i style="width:${pct}%"></i></span></div>`;
}
function bcRow(b){
  const st=String(b.status||'queued');
  const label=BC_STATUS[st]||st;
  const txt=(b.text||'').trim()||'(solo imagen)';
  const tr=document.createElement('tr');
  tr.innerHTML=
    `<td class="bc-msg"><b title="${bcEsc(txt)}">${bcEsc(txt)}</b>`+
      `<div class="bc-meta"><span class="bc-src">${bcEsc(b.source||'manual')}</span><span>${bcFmtTime(b.created_at)}</span></div></td>`+
    `<td><span class="pill ${st}">${bcEsc(label)}</span></td>`+
    `<td><div class="chprog">${bcChanCell(false,b.telegram)}${bcChanCell(true,b.whatsapp)}</div></td>`;
  return tr;
}
async function loadBroadcasts(){
  try{
    const r=await api('/api/broadcasts');
    const list=r.broadcasts||[];
    const rows=$('bc_rows'); rows.innerHTML='';
    $('bc_empty').style.display=list.length?'none':'block';
    let active=false;
    list.forEach(b=>{ rows.appendChild(bcRow(b)); if(b.status==='queued'||b.status==='sending') active=true; });
    const live=$('bc_live'); live.classList.toggle('on', active);
    $('bc_live_t').textContent = active ? 'en vivo' : 'al día';
  }catch(e){ /* silencioso: no romper el polling por un fallo puntual */ }
}
function bcStartPolling(){
  if(BC_TIMER) return;
  loadBroadcasts();
  BC_TIMER=setInterval(()=>{
    if(!CRED || document.hidden) return;            // sin sesión o pestaña del navegador oculta
    const visible=document.querySelector('main>.card[data-tab="enviar"]')?.classList.contains('show');
    if(visible) loadBroadcasts();
  }, BC_POLL);
}
// Arranque autónomo (no toca boot()/showTab()): hook aditivo sobre showTab + DOMContentLoaded.
(function(){
  const _showTab=window.showTab;
  if(typeof _showTab==='function'){
    window.showTab=function(t){ _showTab(t); if(t==='enviar') loadBroadcasts(); };
  }
  const start=()=>{ if(CRED) bcStartPolling(); };
  if(document.readyState!=='loading') start();
  else document.addEventListener('DOMContentLoaded', start);
})();
// ===== Programación: monitor de envíos fraccionados (GET /api/plans) =====
const PL_ST={pending:'Pendiente',running:'En curso',done:'Completado',canceled:'Cancelado'};
const PL_PILL={pending:'queued',running:'sending',done:'done',canceled:'failed'};
function plBatchLine(e){
  const chName=e.ch==='wa'?'🟢 WhatsApp':'✈️ Telegram';
  const n=e.n|0, env=e.enviados|0, pct=n?Math.round(env/n*100):0, full=env>=n;
  return `<div class="ch ${e.ch==='wa'?'wa':'tg'}" style="margin:6px 0">`+
    `<span class="ic"></span>`+
    `<span class="num">${chName} · lote ${ (e.idx|0)+1 } · programado <b>${bcFmtTime(e.at)}</b> → <b>${env}/${n}</b> enviados</span>`+
    `<span class="bar ${e.ch==='wa'?'wa':''} ${full?'full':''}" style="max-width:120px"><i style="width:${pct}%"></i></span></div>`;
}
function plCard(p){
  const txt=(p.text||'').trim()||'(solo imagen)';
  const st=String(p.status||'pending'); const lab=PL_ST[st]||st; const pill=PL_PILL[st]||'queued';
  const tgI=(p.tg&&p.tg.total)?`✈️ ${p.tg.next|0}/${p.tg.batches|0} lotes`:'';
  const waI=(p.wa&&p.wa.enabled)?`🟢 ${p.wa.next|0}/${p.wa.batches|0} lotes`+(!p.wa.resolved?' (resolviendo…)':''):'';
  const lines=(p.log||[]).map(plBatchLine).join('') || '<div class="hint" style="margin-top:6px">Aún sin lotes despachados (esperando ventana/turno).</div>';
  return `<div class="card" style="margin-bottom:12px;background:var(--elev);padding:16px">`+
    `<div style="display:flex;justify-content:space-between;gap:10px;align-items:center">`+
      `<b style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${bcEsc(txt)}</b>`+
      `<span class="pill ${pill}">${lab}</span></div>`+
    `<div class="bc-meta" style="margin:6px 0 4px">${bcFmtTime(p.created_at)} · lote ${p.batch_size|0} ${tgI?'· '+tgI:''} ${waI?'· '+waI:''}</div>`+
    lines+`</div>`;
}
async function loadPlans(){
  try{
    const r=await api('/api/plans'); const list=r.plans||[];
    $('pl_empty').style.display=list.length?'none':'block';
    $('pl_list').innerHTML=list.map(plCard).join('');
    const active=list.some(p=>p.status==='pending'||p.status==='running');
    $('pl_live').classList.toggle('on',active); $('pl_live_t').textContent=active?'en vivo':'al día';
  }catch(e){ /* silencioso */ }
}
let PL_TIMER=null;
function plStartPolling(){
  if(PL_TIMER) return; loadPlans();
  PL_TIMER=setInterval(()=>{ if(!CRED||document.hidden) return;
    const vis=document.querySelector('main>.card[data-tab="prog"]')?.classList.contains('show');
    if(vis) loadPlans(); }, BC_POLL);
}
(function(){ const _s=window.showTab;
  if(typeof _s==='function'){ window.showTab=function(t){ _s(t); if(t==='prog') loadPlans(); if(t==='estado') loadQueue(); }; }
  const start=()=>{ if(CRED){ plStartPolling(); qStartPolling(); } };
  if(document.readyState!=='loading') start();
  else document.addEventListener('DOMContentLoaded', start);
})();
</script></body></html>"""
