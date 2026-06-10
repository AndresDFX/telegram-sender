"""Entrypoint de la interfaz web de administración (API Gateway + Basic Auth).

Sirve una página HTML protegida con usuario/contraseña (HTTP Basic) y una pequeña
API JSON para: editar la config (canal, markup, footer WhatsApp, imagen, etc.),
ver la profundidad de la cola/DLQ y listar/activar/desactivar suscriptores.
"""

from __future__ import annotations

import base64
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

_CAMPOS_EDITABLES = (
    "source_channel",
    "markup_percentage",
    "currency_symbols",
    "strip_patterns",
    "whatsapp_footer",
    "image_url",
)


def _ensure() -> None:
    global config, subscribers, queue_stats
    if config is None:
        config = wiring.build_config_store()
    if subscribers is None:
        subscribers = wiring.build_subscribers()
    if queue_stats is None:
        queue_stats = wiring.build_queue_stats()


# --- auth -------------------------------------------------------------------

def _autorizado(event: dict[str, Any]) -> bool:
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return False  # fail-closed: sin contraseña configurada, nadie entra
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        usuario, _, clave = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(usuario, admin_user()) and hmac.compare_digest(clave, password)


def _401() -> dict[str, Any]:
    return {
        "statusCode": 401,
        "headers": {"WWW-Authenticate": 'Basic realm="TelegramSender Admin"'},
        "body": "Autenticación requerida",
    }


# --- helpers ----------------------------------------------------------------

def _json(data: Any, status: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data, ensure_ascii=False),
    }


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
    out = {}
    for k in _CAMPOS_EDITABLES:
        if k not in cambios:
            continue
        v = cambios[k]
        if k == "markup_percentage":
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
        elif k == "strip_patterns":
            if isinstance(v, str):
                v = [ln.strip() for ln in v.splitlines() if ln.strip()]
            elif isinstance(v, list):
                v = [str(x) for x in v]
            else:
                continue
        else:
            v = str(v)
        out[k] = v
    return out


# --- dispatcher -------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    if not _autorizado(event):
        return _401()

    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    raw = event.get("rawPath") or http.get("path") or "/admin"
    idx = raw.find("/admin")
    sub = (raw[idx + len("/admin"):] if idx >= 0 else raw).rstrip("/")

    try:
        if sub == "":
            return _html_resp()
        if sub == "/api/config" and method == "GET":
            return _json(config.get())
        if sub == "/api/config" and method == "POST":
            return _json(config.set(_sanea_config(_body(event))))
        if sub == "/api/subscribers" and method == "GET":
            return _json({"subscribers": subscribers.listar_todos()})
        if sub == "/api/subscribers" and method == "POST":
            cuerpo = _body(event)
            chat_id = str(cuerpo.get("chat_id", "")).strip()
            status = cuerpo.get("status", "active")
            if not chat_id:
                return _json({"error": "chat_id requerido"}, 400)
            subscribers.registrar(chat_id, "active" if status == "active" else "inactive")
            return _json({"ok": True})
        if sub == "/api/queue" and method == "GET":
            return _json(queue_stats.profundidades())
    except Exception:
        logger.exception("Error en admin %s %s", method, sub)
        return _json({"error": "internal"}, 500)

    return _json({"error": "not found"}, 404)


_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TelegramSender · Admin</title>
<style>
 body{font-family:system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
 header{background:#1e293b;padding:16px 24px;font-size:20px;font-weight:600}
 main{max-width:880px;margin:0 auto;padding:24px}
 .card{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:20px}
 h2{margin:0 0 14px;font-size:16px;color:#93c5fd}
 label{display:block;margin:10px 0 4px;font-size:13px;color:#94a3b8}
 input,textarea{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:8px;padding:9px;font-size:14px}
 textarea{min-height:90px;font-family:ui-monospace,monospace}
 button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer;margin-top:12px}
 button.sec{background:#334155}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{text-align:left;padding:8px;border-bottom:1px solid #334155}
 .pill{padding:2px 8px;border-radius:999px;font-size:12px}
 .active{background:#064e3b;color:#6ee7b7}.inactive{background:#451a03;color:#fdba74}
 .stat{display:inline-block;margin-right:24px}.stat b{font-size:24px;display:block;color:#93c5fd}
 .ok{color:#6ee7b7;font-size:13px;margin-left:10px}
</style></head><body>
<header>📦 TelegramSender · Panel</header>
<main>
 <div class="card"><h2>Configuración</h2>
  <label>Canal fuente (username sin @)</label><input id="source_channel">
  <label>Markup %</label><input id="markup_percentage" type="number" step="0.1">
  <label>Símbolos de moneda</label><input id="currency_symbols">
  <label>Footer WhatsApp (se añade a cada lista)</label><textarea id="whatsapp_footer"></textarea>
  <label>URL de imagen propia (se envía antes de la lista)</label><input id="image_url">
  <label>Patrones a quitar (ubicación), uno por línea</label><textarea id="strip_patterns"></textarea>
  <button onclick="saveCfg()">Guardar</button><span id="cfgok" class="ok"></span>
 </div>
 <div class="card"><h2>Cola de mensajes</h2>
  <div class="stat">Broadcast<b id="q_broadcast">–</b></div>
  <div class="stat">DLQ<b id="q_dlq">–</b></div>
  <div><button class="sec" onclick="loadQueue()">Refrescar</button></div>
 </div>
 <div class="card"><h2>Suscriptores</h2>
  <table><thead><tr><th>chatId</th><th>estado</th><th></th></tr></thead><tbody id="subs"></tbody></table>
 </div>
</main>
<script>
const BASE = location.pathname.replace(/\\/admin.*/, '/admin');
const $ = id => document.getElementById(id);
async function api(p, opt){ const r = await fetch(BASE+p, opt); if(!r.ok) throw new Error(r.status); return r.json(); }
async function loadCfg(){ const c = await api('/api/config');
  $('source_channel').value=c.source_channel||''; $('markup_percentage').value=c.markup_percentage;
  $('currency_symbols').value=c.currency_symbols||''; $('whatsapp_footer').value=c.whatsapp_footer||'';
  $('image_url').value=c.image_url||''; $('strip_patterns').value=(c.strip_patterns||[]).join('\\n'); }
async function saveCfg(){ const body={ source_channel:$('source_channel').value, markup_percentage:parseFloat($('markup_percentage').value),
  currency_symbols:$('currency_symbols').value, whatsapp_footer:$('whatsapp_footer').value, image_url:$('image_url').value,
  strip_patterns:$('strip_patterns').value };
  await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  $('cfgok').textContent='✓ Guardado'; setTimeout(()=>$('cfgok').textContent='',2000); loadCfg(); }
async function loadQueue(){ const q=await api('/api/queue'); $('q_broadcast').textContent=q.broadcast; $('q_dlq').textContent=q.dlq; }
async function loadSubs(){ const d=await api('/api/subscribers'); const t=$('subs'); t.innerHTML='';
  (d.subscribers||[]).forEach(s=>{ const tr=document.createElement('tr');
    const act=s.status==='active'; const next=act?'inactive':'active';
    tr.innerHTML=`<td>${s.chatId}</td><td><span class="pill ${act?'active':'inactive'}">${s.status||'—'}</span></td>`+
      `<td><button class="sec" onclick="toggle('${s.chatId}','${next}')">${act?'Desactivar':'Activar'}</button></td>`;
    t.appendChild(tr); }); }
async function toggle(id,status){ await api('/api/subscribers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:id,status})}); loadSubs(); }
loadCfg(); loadQueue(); loadSubs();
</script></body></html>"""
