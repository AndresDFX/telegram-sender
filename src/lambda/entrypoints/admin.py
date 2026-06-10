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
)
_LISTAS = ("strip_patterns", "excluded_ids")


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
        else:
            v = str(v)
        out[k] = v
    return out


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
            return _json(config.get())
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
 input,textarea{width:100%;background:#0e1428;border:1px solid var(--bd);color:var(--tx);border-radius:10px;
   padding:11px;font-size:14px;transition:border .15s}
 input:focus,textarea:focus{outline:0;border-color:var(--ac)}
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
 <main>
  <div class="card"><h2>Aumento (markup)</h2>
   <div class="markup"><input id="markup_percentage" type="number" step="0.1"><div>
     <div style="font-size:13px">% que se suma a cada precio</div>
     <div class="hint">Ej: $325.000 + 15% → $374.000 (redondeo al mil ↑)</div></div></div>
  </div>
  <div class="card"><h2>Canal y mensaje</h2>
   <label>Canal fuente (username sin @)</label><input id="source_channel">
   <label>Símbolos de moneda</label><input id="currency_symbols">
   <label>Footer WhatsApp (se añade al final de cada lista)</label><textarea id="whatsapp_footer"></textarea>
   <label>Patrones a quitar (ubicación), uno por línea</label><textarea id="strip_patterns"></textarea>
   <button onclick="saveCfg()">Guardar cambios</button>
  </div>
  <div class="card"><h2>Imagen de la lista</h2>
   <div class="hint">Se envía como foto antes de cada lista. Sube un archivo o pega una URL.</div>
   <input type="file" id="imgfile" accept="image/*" style="margin-top:10px" onchange="uploadImg()">
   <img id="imgprev" class="preview" style="display:none">
   <label>…o URL externa</label><input id="image_url" placeholder="https://...">
   <button class="sec" onclick="saveCfg()">Guardar URL</button>
  </div>
  <div class="card"><h2>Excluir destinatarios</h2>
   <div class="hint">chat IDs que NO recibirán las listas, uno por línea.</div>
   <textarea id="excluded_ids" style="margin-top:8px"></textarea>
   <button onclick="saveCfg()">Guardar exclusiones</button>
  </div>
  <div class="card"><h2>Cola de mensajes</h2>
   <div class="stats"><div class="stat"><b id="q_b">–</b><span>en cola</span></div>
     <div class="stat"><b id="q_d">–</b><span>en DLQ (fallidos)</span></div></div>
   <button class="sec" style="margin-top:14px" onclick="loadQueue()">Refrescar</button>
  </div>
  <div class="card"><h2>Suscriptores</h2>
   <table><thead><tr><th>chatId</th><th>estado</th><th></th></tr></thead><tbody id="subs"></tbody></table>
   <div class="hint" id="subsempty" style="display:none;margin-top:12px">Aún no hay suscriptores: nadie le ha dado /start al bot.</div>
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
  ['source_channel','markup_percentage','currency_symbols','whatsapp_footer','image_url'].forEach(k=>$(k).value=c[k]??'');
  $('strip_patterns').value=(c.strip_patterns||[]).join('\n'); $('excluded_ids').value=(c.excluded_ids||[]).join('\n'); }
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
async function loadSubs(){ const d=await api('/api/subscribers'); const t=$('subs'); t.innerHTML='';
  const subs=d.subscribers||[]; $('subsempty').style.display=subs.length?'none':'block';
  subs.forEach(s=>{ const act=s.status==='active', next=act?'inactive':'active'; const tr=document.createElement('tr');
    tr.innerHTML=`<td>${s.chatId}</td><td><span class="pill ${act?'active':'inactive'}">${s.status||'—'}</span></td>`+
      `<td><button class="ghost" style="padding:6px 12px" onclick="toggle('${s.chatId}','${next}')">${act?'Desactivar':'Activar'}</button></td>`;
    t.appendChild(tr); }); }
async function toggle(id,status){ await api('/api/subscribers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:id,status})}); loadSubs(); }
function boot(){ loadCfg(); loadQueue(); loadSubs(); }
if(CRED){ fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{ if(r.ok){ $('login').style.display='none'; $('app').style.display='block'; boot(); } }).catch(()=>{}); }
</script></body></html>"""
