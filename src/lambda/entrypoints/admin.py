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

_CAMPOS_EDITABLES = (
    "source_channel",
    "markup_percentage",
    "currency_symbols",
    "strip_patterns",
    "whatsapp_footer",
    "image_url",
    "excluded_ids",
    "send_mode",
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
)
_LISTAS = ("strip_patterns", "excluded_ids", "whatsapp_excluded")
_LISTAS_NOMBRADAS = ("telegram_lists", "whatsapp_lists")
_TARGETS = ("telegram_target", "whatsapp_target")
# Secretos que NO se sobreescriben con un valor vacío (para no borrarlos al guardar otros campos).
_NO_VACIAR = ("telethon_session", "telethon_api_hash", "whatsapp_token")


def _ensure() -> None:
    global config, subscribers, queue_stats, image_store
    if config is None:
        config = wiring.build_config_store()
    if subscribers is None:
        subscribers = wiring.build_subscribers()
    if queue_stats is None:
        queue_stats = wiring.build_queue_stats()
    if image_store is None:
        image_store = wiring.build_image_store()


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
        elif k == "whatsapp_enabled":
            v = bool(v)
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
    return cfg


def _whatsapp_proxy(path: str, timeout: float = 20.0) -> dict:
    """GET al servicio de WhatsApp (status/contacts) usando la URL+token de la config."""
    import urllib.request

    cfg = config.get()
    url = (cfg.get("whatsapp_service_url") or "").rstrip("/")
    token = cfg.get("whatsapp_token") or ""
    if not url or not token:
        return _json({"error": "whatsapp_no_configurado"}, 409)
    req = urllib.request.Request(f"{url}{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"statusCode": resp.status, "headers": {"Content-Type": "application/json"}, "body": resp.read().decode()}
    except Exception as error:
        logger.exception("Proxy WhatsApp %s falló", path)
        return _json({"error": "whatsapp_inaccesible", "detalle": str(error)}, 502)


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
            return _json({"ok": True, "key": key})
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
        if sub == "/api/whatsapp/status" and method == "GET":
            return _whatsapp_proxy("/status")
        if sub == "/api/whatsapp/contacts" and method == "GET":
            return _whatsapp_proxy("/contacts", timeout=25)
    except Exception:
        logger.exception("Error en admin %s %s", method, sub)
        return _json({"error": "internal"}, 500)

    return _json({"error": "not found"}, 404)


_PAGE = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TelegramSender · Panel</title>
<style>
 :root{--bg:#0b1020;--card:#151c32;--card2:#1c2540;--bd:#2a3354;--tx:#e6ebff;--mut:#8b96b8;--ac:#6366f1;--ac2:#22d3ee;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185}
 *{box-sizing:border-box}body{font-family:system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:
   radial-gradient(1200px 600px at 80% -10%,#1e2a52 0%,transparent 60%),var(--bg);color:var(--tx);min-height:100vh}
 /* login */
 #login{display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}
 #login .box{background:var(--card);border:1px solid var(--bd);border-radius:18px;padding:34px;width:360px;
   box-shadow:0 24px 60px rgba(0,0,0,.45)}
 #login h1{font-size:20px;margin:0 0 4px}#login p{color:var(--mut);margin:0 0 22px;font-size:13px}
 .logo{font-size:34px;margin-bottom:10px}
 /* app */
 #app{display:none}
 header{display:flex;align-items:center;justify-content:space-between;background:rgba(21,28,50,.8);
   backdrop-filter:blur(8px);padding:14px 24px;border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5}
 header .t{font-weight:700;font-size:17px}header .u{color:var(--mut);font-size:13px}
 main{max-width:920px;margin:0 auto;padding:24px;display:grid;gap:20px}
 .card{background:linear-gradient(180deg,var(--card) 0%,var(--card2) 100%);border:1px solid var(--bd);
   border-radius:16px;padding:22px}
 h2{margin:0 0 16px;font-size:15px;color:var(--ac2);letter-spacing:.3px;text-transform:uppercase}
 label{display:block;margin:12px 0 5px;font-size:12px;color:var(--mut);font-weight:600}
 input,textarea,select{width:100%;background:#0e1428;border:1px solid var(--bd);color:var(--tx);border-radius:10px;
   padding:11px;font-size:14px;transition:border .15s}
 input:focus,textarea:focus,select:focus{outline:0;border-color:var(--ac)}
 textarea{min-height:84px;font-family:ui-monospace,monospace;font-size:13px}
 .row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 button{background:linear-gradient(90deg,var(--ac),#818cf8);color:#fff;border:0;border-radius:10px;
   padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;transition:filter .15s}
 button:hover{filter:brightness(1.1)}button.sec{background:#26304f}button.ghost{background:transparent;border:1px solid var(--bd)}
 .markup{display:flex;align-items:center;gap:14px;background:#0e1428;border:1px solid var(--bd);border-radius:12px;padding:16px}
 .markup input{font-size:30px;font-weight:700;text-align:center;width:120px;padding:6px}
 .hint{color:var(--mut);font-size:12px;margin-top:6px}
 table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--bd)}
 th{color:var(--mut);font-size:12px;text-transform:uppercase}
 .pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
 .pill.active{background:rgba(52,211,153,.15);color:var(--ok)}.pill.inactive{background:rgba(251,146,60,.15);color:var(--warn)}
 .stats{display:flex;gap:18px}.stat{flex:1;background:#0e1428;border:1px solid var(--bd);border-radius:12px;padding:16px;text-align:center}
 .stat b{display:block;font-size:30px;color:var(--ac2)}.stat span{color:var(--mut);font-size:12px}
 .toast{position:fixed;bottom:24px;right:24px;background:var(--ok);color:#04261a;padding:12px 18px;border-radius:10px;
   font-weight:600;opacity:0;transform:translateY(10px);transition:.25s;pointer-events:none}
 .toast.show{opacity:1;transform:none}.toast.err{background:var(--bad);color:#3b0712}
 .err{color:var(--bad);font-size:13px;min-height:18px;margin-top:8px}
 img.preview{max-width:160px;border-radius:10px;margin-top:10px;border:1px solid var(--bd)}
 /* navegación por módulos */
 .nav{position:sticky;top:51px;z-index:4;background:rgba(11,16,32,.85);backdrop-filter:blur(8px);
   display:flex;gap:8px;justify-content:center;padding:14px 16px;border-bottom:1px solid var(--bd);flex-wrap:wrap}
 .nav button{background:#1c2540;border:1px solid var(--bd);color:var(--mut);padding:9px 18px;border-radius:999px;font-weight:600}
 .nav button.on{background:linear-gradient(90deg,var(--ac),#818cf8);color:#fff;border-color:transparent}
 main>.card{display:none}
 main>.card.show{display:block}
</style></head><body>

<div id="login"><div class="box">
  <div class="logo">📦</div>
  <h1>TelegramSender</h1><p>Panel de administración</p>
  <label>Usuario</label><input id="lu" autocomplete="username" value="admin">
  <label>Contraseña</label><input id="lp" type="password" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
  <div class="err" id="lerr"></div>
  <button style="width:100%;margin-top:8px" onclick="doLogin()">Entrar</button>
</div></div>

<div id="app">
 <header><div class="t">📦 TelegramSender</div><div><span class="u" id="who"></span>
   <button class="ghost" style="margin-left:12px;padding:7px 12px" onclick="logout()">Salir</button></div></header>
 <nav class="nav">
   <button data-tab="msg" onclick="showTab('msg')">📝 Mensaje</button>
   <button data-tab="telegram" onclick="showTab('telegram')">✈️ Telegram</button>
   <button data-tab="whatsapp" onclick="showTab('whatsapp')">🟢 WhatsApp</button>
   <button data-tab="estado" onclick="showTab('estado')">📊 Estado</button>
 </nav>
 <main>
  <div class="card" data-tab="msg"><h2>Aumento (markup)</h2>
   <div class="markup"><input id="markup_percentage" type="number" step="0.1"><div>
     <div style="font-size:13px">% que se suma a cada precio</div>
     <div class="hint">Ej: $325.000 + 15% → $374.000 (redondeo al mil ↑)</div></div></div>
  </div>
  <div class="card" data-tab="telegram"><h2>Cuenta de Telegram</h2>
   <label>Modo de envío</label>
   <select id="send_mode"><option value="bot">Bot — a suscriptores que dan /start</option><option value="userbot">Userbot — desde mi cuenta a mis contactos</option></select>
   <div class="row">
     <div><label>API ID</label><input id="telethon_api_id"></div>
     <div><label>API Hash</label><input id="telethon_api_hash"></div>
   </div>
   <label>StringSession <span id="sess_status" class="hint"></span></label>
   <input id="telethon_session" type="password" placeholder="(pegar solo si quieres cambiar la cuenta)">
   <div class="hint">Genérala con <code>scripts/generar_sesion.py</code>. Da acceso total a tu cuenta: trátala como secreto.</div>
   <button onclick="saveAccount()">Guardar cuenta</button>
  </div>
  <div class="card" data-tab="whatsapp"><h2>WhatsApp (reenvío)</h2>
   <label style="display:flex;align-items:center;gap:8px;margin-top:0"><input type="checkbox" id="whatsapp_enabled" style="width:auto"> Reenviar también cada lista por WhatsApp</label>
   <label>URL del servicio WhatsApp</label><input id="whatsapp_service_url" placeholder="https://...onrender.com">
   <label>Token del servicio <span id="wa_tok_status" class="hint"></span></label>
   <input id="whatsapp_token" type="password" placeholder="(pegar solo si quieres cambiarlo)">
   <button onclick="saveWhatsapp()">Guardar WhatsApp</button>
   <div style="margin-top:14px">
     <button class="sec" onclick="waStatus()">Ver estado / QR</button> <span id="wa_state" class="hint"></span>
     <div><img id="wa_qr" class="preview" style="display:none"></div>
     <div class="hint" id="wa_qr_hint" style="display:none">Escanéalo: WhatsApp → Dispositivos vinculados → Vincular un dispositivo.</div>
   </div>
   <label>Excluir de WhatsApp (ids, uno por línea)</label><textarea id="whatsapp_excluded"></textarea>
   <button onclick="saveWhatsapp()">Guardar exclusiones WhatsApp</button>
   <div class="hint">⚠️ Enviar masivamente por WhatsApp puede banear tu número. Empieza excluyendo casi todos y prueba con pocos.</div>
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
  <div class="card" data-tab="telegram"><h2>Excluir destinatarios</h2>
   <div class="hint">chat IDs que NO recibirán las listas, uno por línea.</div>
   <textarea id="excluded_ids" style="margin-top:8px"></textarea>
   <button onclick="saveCfg()">Guardar exclusiones</button>
  </div>
  <div class="card" data-tab="estado"><h2>Cola de mensajes</h2>
   <div class="stats"><div class="stat"><b id="q_b">–</b><span>en cola</span></div>
     <div class="stat"><b id="q_d">–</b><span>en DLQ (fallidos)</span></div></div>
   <button class="sec" style="margin-top:14px" onclick="loadQueue()">Refrescar</button>
  </div>
  <div class="card" data-tab="telegram"><h2>Destinatarios <span id="subcount" class="hint"></span></h2>
   <div class="hint">Busca, navega y usa los botones para incluir/excluir en masa. Los excluidos NO reciben las listas.</div>
   <input id="subsearch" placeholder="🔎 Buscar por nombre o id..." oninput="onSearch()" style="margin-top:10px">
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
     <button class="sec" onclick="toggleAll(true)">Marcar visibles</button>
     <button class="sec" onclick="toggleAll(false)">Desmarcar</button>
     <button onclick="bulk('excluir')">Excluir marcados</button>
     <button onclick="bulk('incluir')">Incluir marcados</button>
     <button class="ghost" onclick="bulkFiltered('excluir')">Excluir filtrados</button>
     <button class="ghost" onclick="bulkFiltered('incluir')">Incluir filtrados</button>
   </div>
   <table><thead><tr><th><input type="checkbox" id="selall" onchange="toggleAll(this.checked)"></th><th>nombre / id</th><th>estado</th></tr></thead><tbody id="subs"></tbody></table>
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
  <div class="card" data-tab="whatsapp"><h2>Destinatarios WhatsApp</h2>
   <div class="hint">Carga los contactos de tu WhatsApp (requiere el servicio conectado) para armar listas.</div>
   <button class="sec" style="margin-top:10px" onclick="loadWaContacts()">Cargar contactos de WhatsApp</button> <span id="wa_c_count" class="hint"></span>
   <input id="wa_search" placeholder="🔎 Buscar por nombre o número..." oninput="renderWa()" style="margin-top:10px">
   <div style="display:flex;gap:8px;margin:10px 0"><button class="sec" onclick="waToggleAll(true)">Marcar visibles</button><button class="sec" onclick="waToggleAll(false)">Desmarcar</button></div>
   <table><thead><tr><th></th><th>nombre / número</th></tr></thead><tbody id="wa_subs"></tbody></table>
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
function toast(m,err){ const t=$('toast'); t.textContent=m; t.className='toast show'+(err?' err':''); setTimeout(()=>t.className='toast',2200); }
async function doLogin(){ const u=$('lu').value, p=$('lp').value; CRED=btoa(u+':'+p);
  try{ await fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{if(!r.ok)throw 0;}); sessionStorage.setItem('cred',CRED);
    $('login').style.display='none'; $('app').style.display='block'; $('who').textContent=u; boot(); }
  catch(e){ $('lerr').textContent='Usuario o contraseña incorrectos'; } }
function logout(){ sessionStorage.removeItem('cred'); CRED=''; $('app').style.display='none'; $('login').style.display='flex'; }
async function loadCfg(){ const c=await api('/api/config');
  ['source_channel','markup_percentage','currency_symbols','whatsapp_footer','image_url','telethon_api_id','telethon_api_hash'].forEach(k=>$(k).value=c[k]??'');
  $('send_mode').value=c.send_mode||'bot';
  $('sess_status').textContent = c.telethon_session_set ? '· conectada ✓' : '· no configurada';
  $('strip_patterns').value=(c.strip_patterns||[]).join('\n'); $('excluded_ids').value=(c.excluded_ids||[]).join('\n');
  $('whatsapp_enabled').checked=!!c.whatsapp_enabled; $('whatsapp_service_url').value=c.whatsapp_service_url||'';
  $('whatsapp_excluded').value=(c.whatsapp_excluded||[]).join('\n');
  $('wa_tok_status').textContent = c.whatsapp_token_set ? '· configurado ✓' : '· no configurado';
  LISTS.telegram=c.telegram_lists||[]; TGT.telegram=c.telegram_target||{mode:'all',lists:[]};
  LISTS.whatsapp=c.whatsapp_lists||[]; TGT.whatsapp=c.whatsapp_target||{mode:'all',lists:[]};
  renderLists('telegram'); renderLists('whatsapp'); }
async function saveWhatsapp(){ const b={ whatsapp_enabled:$('whatsapp_enabled').checked, whatsapp_service_url:$('whatsapp_service_url').value,
   whatsapp_excluded:$('whatsapp_excluded').value }; const tok=$('whatsapp_token').value; if(tok) b.whatsapp_token=tok;
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    toast('✓ WhatsApp guardado'); $('whatsapp_token').value=''; loadCfg(); } catch(e){ toast('Error',true); } }
async function waStatus(){ $('wa_state').textContent='consultando...';
  try{ const s=await api('/api/whatsapp/status');
    $('wa_state').textContent = s.connected ? ('conectado ✓ ('+(s.contacts||0)+' contactos)') : 'no conectado — escanea el QR';
    if(s.qr){ $('wa_qr').src=s.qr; $('wa_qr').style.display='block'; $('wa_qr_hint').style.display='block'; }
    else { $('wa_qr').style.display='none'; $('wa_qr_hint').style.display='none'; }
  }catch(e){ $('wa_state').textContent='servicio inaccesible (¿URL/token? ¿desplegado?)'; } }
async function saveAccount(){ const b={ send_mode:$('send_mode').value, telethon_api_id:$('telethon_api_id').value,
   telethon_api_hash:$('telethon_api_hash').value, telethon_session:$('telethon_session').value };
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    toast('✓ Cuenta guardada'); $('telethon_session').value=''; loadCfg(); loadSubs(); } catch(e){ toast('Error',true); } }
async function saveCfg(){ const b={ source_channel:$('source_channel').value, markup_percentage:parseFloat($('markup_percentage').value),
   currency_symbols:$('currency_symbols').value, whatsapp_footer:$('whatsapp_footer').value, image_url:$('image_url').value,
   strip_patterns:$('strip_patterns').value, excluded_ids:$('excluded_ids').value };
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); toast('✓ Guardado'); loadCfg(); }
  catch(e){ toast('Error al guardar',true); } }
async function uploadImg(){ const f=$('imgfile').files[0]; if(!f) return;
  const r=new FileReader(); r.onload=async()=>{ const b64=r.result.split(',')[1];
    try{ await api('/api/image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:b64,content_type:f.type})});
      $('imgprev').src=r.result; $('imgprev').style.display='block'; toast('✓ Imagen subida'); }
    catch(e){ toast('Error al subir',true); } }; r.readAsDataURL(f); }
async function loadQueue(){ const q=await api('/api/queue'); $('q_b').textContent=q.broadcast; $('q_d').textContent=q.dlq; }
let EXCLUDED=new Set(), DEST=[], FILTER='', PAGE=0;
const PAGE_SIZE=50;
function filtered(){ if(!FILTER) return DEST; const q=FILTER.toLowerCase();
  return DEST.filter(s=> (s.name||s.status||'').toLowerCase().includes(q) || String(s.chatId).includes(q)); }
function render(){ const f=filtered(); const pages=Math.max(1,Math.ceil(f.length/PAGE_SIZE));
  if(PAGE>=pages) PAGE=pages-1; if(PAGE<0) PAGE=0;
  const slice=f.slice(PAGE*PAGE_SIZE,(PAGE+1)*PAGE_SIZE);
  const t=$('subs'); t.innerHTML=''; $('selall').checked=false;
  $('subsempty').style.display=DEST.length?'none':'block';
  $('subcount').textContent = DEST.length ? `· ${f.length}${FILTER?' filtrados':''} de ${DEST.length} (${EXCLUDED.size} excluidos)` : '';
  $('pageinfo').textContent = f.length ? `página ${PAGE+1} de ${pages}` : 'sin resultados';
  slice.forEach(s=>{ const ex=EXCLUDED.has(String(s.chatId)); const label=s.name||s.status||'—'; const tr=document.createElement('tr');
    tr.innerHTML=`<td><input type="checkbox" class="selrow" data-id="${s.chatId}"></td>`+
      `<td><b>${label}</b><div class="hint">${s.chatId}</div></td>`+
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
let WA_DEST=[], WA_PAGE=0;
async function loadWaContacts(){ $('wa_c_count').textContent='cargando...';
  try{ const r=await api('/api/whatsapp/contacts'); WA_DEST=r.contacts||[]; WA_PAGE=0; renderWa(); $('wa_c_count').textContent='· '+WA_DEST.length+' contactos'; }
  catch(e){ $('wa_c_count').textContent='servicio inaccesible (¿conectado?)'; } }
function waFiltered(){ const q=($('wa_search').value||'').trim().toLowerCase(); if(!q) return WA_DEST;
  return WA_DEST.filter(c=> (c.name||'').toLowerCase().includes(q) || String(c.id||'').includes(q)); }
function renderWa(){ const f=waFiltered(); const pages=Math.max(1,Math.ceil(f.length/PAGE_SIZE));
  if(WA_PAGE>=pages)WA_PAGE=pages-1; if(WA_PAGE<0)WA_PAGE=0; const slice=f.slice(WA_PAGE*PAGE_SIZE,(WA_PAGE+1)*PAGE_SIZE);
  const t=$('wa_subs'); t.innerHTML='';
  slice.forEach(c=>{ const id=String(c.id||''); const tr=document.createElement('tr');
    tr.innerHTML=`<td><input type="checkbox" class="wsel" data-id="${id}"></td><td><b>${c.name||'—'}</b><div class="hint">${id}</div></td>`; t.appendChild(tr); });
  $('wa_pageinfo').textContent=f.length?`página ${WA_PAGE+1} de ${pages}`:'sin resultados'; }
function waSelectedIds(){ return [...document.querySelectorAll('.wsel:checked')].map(c=>String(c.dataset.id)); }
function waToggleAll(v){ document.querySelectorAll('.wsel').forEach(c=>c.checked=v); }
function waPrev(){ WA_PAGE--; renderWa(); }
function waNext(){ WA_PAGE++; renderWa(); }
function showTab(t){
  document.querySelectorAll('main>.card').forEach(c=>c.classList.toggle('show', c.dataset.tab===t));
  document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('on', b.dataset.tab===t));
  try{ localStorage.setItem('tab',t); }catch(e){}
  window.scrollTo(0,0); }
function boot(){ showTab((()=>{try{return localStorage.getItem('tab')}catch(e){return null}})()||'msg'); loadCfg(); loadQueue(); loadSubs(); }
if(CRED){ fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{ if(r.ok){ $('login').style.display='none'; $('app').style.display='block'; boot(); } }).catch(()=>{}); }
</script></body></html>"""
