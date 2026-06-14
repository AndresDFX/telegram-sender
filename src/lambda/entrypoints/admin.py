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

import time

import wiring
from adapters.config import admin_user
from domain import auth as auth_dom
from domain.message import componer_mensaje
from domain.schedules import hhmm, proximo_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Anti-fuerza-bruta del Basic Auth (en memoria, por contenedor): tras N fallos consecutivos se
# bloquea por una ventana corta y cada fallo añade un pequeño retardo. Mitigación; el endurecimiento
# real (WAF/dominio/multiusuario) es Fase 1. Best-effort entre contenedores (admin = baja concurrencia).
_AUTH = {"fails": 0, "locked_until": 0.0}
_AUTH_MAX_FAILS = 5
_AUTH_LOCK_SECS = 300

config = None
subscribers = None
queue_stats = None
image_store = None
broadcast_store = None
plan_store = None
audit_store = None
schedule_store = None

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
    # Correo transaccional (recuperación de contraseña vía Resend).
    "resend_api_key",
    "mail_from",
)
_LISTAS = ("strip_patterns", "excluded_ids", "whatsapp_excluded")
_LISTAS_NOMBRADAS = ("telegram_lists", "whatsapp_lists")
_TARGETS = ("telegram_target", "whatsapp_target")
_FLOATS = ("tg_delay_min", "tg_delay_max")
_ENTEROS = ("batch_size", "wa_delay_min", "wa_delay_max", "window_tz")
_BOOLS = ("whatsapp_enabled", "scheduling_enabled", "window_enabled", "sending_enabled")
# Secretos que NO se sobreescriben con un valor vacío (para no borrarlos al guardar otros campos).
_NO_VACIAR = ("telethon_session", "telethon_api_hash", "whatsapp_token", "bot_token", "resend_api_key")


def _ensure() -> None:
    global config, subscribers, queue_stats, image_store, broadcast_store, plan_store, audit_store, schedule_store
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
    if audit_store is None:
        audit_store = wiring.build_audit_store()
    if schedule_store is None:
        schedule_store = wiring.build_schedule_store()


def _audit(action: str, detail: str = "") -> None:
    """Registra una acción del panel (best-effort; nunca rompe la operación)."""
    try:
        audit_store.registrar(action, detail, admin_user())
    except Exception:
        logger.exception("No se pudo auditar %s", action)


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

def _auth_bloqueado() -> bool:
    return _AUTH["locked_until"] > time.time()


def _auth_fallo() -> None:
    _AUTH["fails"] += 1
    if _AUTH["fails"] >= _AUTH_MAX_FAILS:
        _AUTH["locked_until"] = time.time() + _AUTH_LOCK_SECS
    try:
        time.sleep(min(_AUTH["fails"] * 0.5, 3))  # retardo creciente: frena la fuerza bruta
    except Exception:
        pass


def _autorizado(event: dict[str, Any]) -> bool:
    if _auth_bloqueado():
        return False  # en cooldown tras demasiados intentos fallidos
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        usuario, _, clave = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        _auth_fallo()
        return False
    ok = _verificar(usuario, clave)
    if ok:
        _AUTH["fails"] = 0
        _AUTH["locked_until"] = 0.0
    else:
        _auth_fallo()
    return ok


def _verificar(usuario: str, clave: str) -> bool:
    """Valida contra los usuarios del panel (hash PBKDF2) y, como bootstrap, contra el usuario
    de entorno (ADMIN_USER/ADMIN_PASSWORD): así nunca se queda uno bloqueado del panel."""
    try:
        u = (config.get_users() or {}).get(usuario)
    except Exception:
        u = None
    if u and auth_dom.verify_password(clave, str(u.get("hash", ""))):
        return True
    envpw = os.environ.get("ADMIN_PASSWORD")
    return bool(envpw and hmac.compare_digest(usuario, admin_user()) and hmac.compare_digest(clave, envpw))


def _usuario_actual(event: dict[str, Any]) -> str:
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return ""
    try:
        return base64.b64decode(auth[6:]).decode("utf-8").partition(":")[0]
    except Exception:
        return ""


def _reset_email_html(usuario: str, code: str) -> str:
    """Cuerpo HTML de marca para el correo de recuperación de contraseña."""
    return (
        '<div style="background:#161514;padding:32px 0;font-family:Inter,Segoe UI,Arial,sans-serif">'
        '<div style="max-width:440px;margin:0 auto;background:#211F1E;border:1px solid #332F2D;'
        'border-radius:16px;overflow:hidden">'
        '<div style="height:4px;background:linear-gradient(90deg,#FD531E,#FF8A5C)"></div>'
        '<div style="padding:28px 30px;color:#E7E7E5">'
        '<div style="font-size:20px;font-weight:800;color:#FF8A5C;margin-bottom:4px">Replica</div>'
        '<p style="color:#9C9892;margin:0 0 22px;font-size:14px">Recuperación de contraseña</p>'
        f'<p style="margin:0 0 8px;font-size:15px">Hola <b>{usuario}</b>, este es tu código para '
        'restablecer la contraseña:</p>'
        f'<div style="margin:18px 0;text-align:center;font-size:30px;font-weight:800;letter-spacing:8px;'
        f'color:#FBFAF9;background:#161514;border:1px dashed #4A4644;border-radius:12px;padding:16px">{code}</div>'
        '<p style="color:#9C9892;font-size:13px;margin:0">Válido por 15 minutos. Si no lo solicitaste, '
        'puedes ignorar este mensaje.</p>'
        '</div></div></div>'
    )


def _enviar_reset_email(email: str, usuario: str, code: str) -> None:
    """Envía el código de reseteo. Prefiere Resend (correo real, gratis) si está
    configurado; si no, cae al tópico SNS de alertas (suscripción por confirmar)."""
    asunto = "Replica — código para restablecer tu contraseña"
    texto = (
        f"Código para restablecer la contraseña del usuario '{usuario}': {code}\n"
        f"Válido por 15 minutos. Si no lo solicitaste, ignora este mensaje.\n"
        f"(Cuenta: {email})"
    )
    # 1) Resend (gratis) — preferido: llega a la bandeja sin confirmar suscripción.
    try:
        cfg = config.get()
    except Exception:
        cfg = {}
    api_key = (cfg.get("resend_api_key") or os.environ.get("RESEND_API_KEY") or "").strip()
    mail_from = (cfg.get("mail_from") or os.environ.get("MAIL_FROM") or "Replica <onboarding@resend.dev>").strip()
    if api_key:
        try:
            from adapters.email_sender import enviar_resend

            if enviar_resend(api_key, mail_from, email, asunto, texto, _reset_email_html(usuario, code)):
                return
            logger.warning("Resend no aceptó el correo; intento SNS")
        except Exception:
            logger.exception("Resend falló al enviar el código; intento SNS")
    # 2) Fallback: SNS (al endpoint suscrito al tópico de alertas).
    arn = os.environ.get("ALERTS_TOPIC_ARN", "")
    if not arn:
        logger.warning("Sin Resend ni ALERTS_TOPIC_ARN; no se envió el código de reseteo")
        return
    try:
        import boto3

        boto3.client("sns").publish(
            TopicArn=arn,
            Subject="Replica - codigo para restablecer contrasena",
            Message=texto,
        )
    except Exception:
        logger.exception("No se pudo publicar el codigo de reseteo por SNS")


def _auth_forgot(body: dict) -> dict[str, Any]:
    """Genera y envía un código de reseteo (respuesta genérica para no revelar usuarios)."""
    usuario = str(body.get("username", "")).strip()
    try:
        u = (config.get_users() or {}).get(usuario)
        if u and u.get("email"):
            code = auth_dom.gen_code(6)
            resets = config.get_resets() or {}
            resets[usuario] = {"code_hash": auth_dom.hash_password(code), "exp": int(time.time()) + 900, "attempts": 0}
            config.set_resets(resets)
            _enviar_reset_email(str(u.get("email")), usuario, code)
    except Exception:
        logger.exception("Fallo en /api/auth/forgot")
    return _json({"ok": True})


def _auth_reset(body: dict) -> dict[str, Any]:
    usuario = str(body.get("username", "")).strip()
    code = str(body.get("code", "")).strip()
    nueva = body.get("new") or ""
    if not auth_dom.password_valida(nueva):
        return _json({"error": "La nueva contraseña debe tener al menos 8 caracteres."}, 400)
    resets = config.get_resets() or {}
    r = resets.get(usuario)
    if not r:
        return _json({"error": "Solicita un código primero."}, 400)
    if int(r.get("exp", 0)) < int(time.time()):
        resets.pop(usuario, None); config.set_resets(resets)
        return _json({"error": "El código expiró; solicita uno nuevo."}, 400)
    if int(r.get("attempts", 0)) >= 5:
        resets.pop(usuario, None); config.set_resets(resets)
        return _json({"error": "Demasiados intentos; solicita un código nuevo."}, 400)
    if not auth_dom.verify_password(code, str(r.get("code_hash", ""))):
        r["attempts"] = int(r.get("attempts", 0)) + 1; resets[usuario] = r; config.set_resets(resets)
        return _json({"error": "Código incorrecto."}, 400)
    users = config.get_users() or {}
    u = users.get(usuario) or {"email": "", "created_at": int(time.time())}
    u["hash"] = auth_dom.hash_password(nueva)
    users[usuario] = u; config.set_users(users)
    resets.pop(usuario, None); config.set_resets(resets)
    _audit("user:reset", usuario)
    return _json({"ok": True})


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
    cfg["resend_api_key_set"] = bool(cfg.get("resend_api_key"))
    cfg["resend_api_key"] = ""
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

    # Recuperación de contraseña: rutas PÚBLICAS (sin sesión). El reseteo tiene tope de
    # intentos + caducidad; forgot responde genérico (no revela si el usuario existe).
    if sub == "/api/auth/forgot" and method == "POST":
        return _auth_forgot(_body(event))
    if sub == "/api/auth/reset" and method == "POST":
        return _auth_reset(_body(event))

    if not _autorizado(event):
        return _json({"error": "unauthorized"}, 401)

    try:
        if sub == "/api/me" and method == "GET":
            return _json({"ok": True, "user": _usuario_actual(event) or admin_user()})
        if sub == "/api/users" and method == "GET":
            users = config.get_users() or {}
            return _json({"me": _usuario_actual(event), "users": [
                {"username": k, "email": v.get("email", ""), "created_at": int(v.get("created_at", 0) or 0)}
                for k, v in users.items()]})
        if sub == "/api/users" and method == "POST":
            c = _body(event)
            username = str(c.get("username", "")).strip()
            pw = c.get("password") or ""
            if not username:
                return _json({"error": "El usuario es obligatorio."}, 400)
            if not auth_dom.password_valida(pw):
                return _json({"error": "La contraseña debe tener al menos 8 caracteres."}, 400)
            users = config.get_users() or {}
            if username in users:
                return _json({"error": "Ya existe un usuario con ese nombre."}, 400)
            users[username] = {"email": str(c.get("email", "")).strip(), "hash": auth_dom.hash_password(pw), "created_at": int(time.time())}
            config.set_users(users)
            _audit("user:crear", username)
            return _json({"ok": True})
        if sub == "/api/users/delete" and method == "POST":
            username = str(_body(event).get("username", "")).strip()
            users = config.get_users() or {}
            if username not in users:
                return _json({"error": "Usuario no encontrado."}, 400)
            if len(users) <= 1:
                return _json({"error": "No puedes borrar el último usuario."}, 400)
            if username == _usuario_actual(event):
                return _json({"error": "No puedes borrar el usuario con el que iniciaste sesión."}, 400)
            del users[username]
            config.set_users(users)
            _audit("user:borrar", username)
            return _json({"ok": True})
        if sub == "/api/auth/change-password" and method == "POST":
            c = _body(event)
            me = _usuario_actual(event)
            nueva = c.get("new") or ""
            if not auth_dom.password_valida(nueva):
                return _json({"error": "La nueva contraseña debe tener al menos 8 caracteres."}, 400)
            if not _verificar(me, c.get("current") or ""):
                return _json({"error": "La contraseña actual es incorrecta."}, 400)
            users = config.get_users() or {}
            u = users.get(me) or {"email": "", "created_at": int(time.time())}
            u["hash"] = auth_dom.hash_password(nueva)
            users[me] = u
            config.set_users(users)
            _audit("user:cambiar-clave", me)
            return _json({"ok": True})
        if sub == "/api/config" and method == "GET":
            return _json(_config_publico())
        if sub == "/api/config" and method == "POST":
            cambios = _sanea_config(_body(event))
            _audit("config", "campos: " + (", ".join(sorted(cambios.keys())) or "(ninguno)"))
            return _json(config.set(cambios))
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
        if sub == "/api/preview/process" and method == "POST":
            cfg = config.get()
            texto = str(_body(event).get("text", ""))
            procesado = componer_mensaje(
                texto,
                markup_percentage=cfg["markup_percentage"],
                currency_symbols=cfg["currency_symbols"],
                strip_patterns=cfg.get("strip_patterns", []),
                footer=cfg.get("whatsapp_footer", ""),
            )
            return _json({"processed": procesado})
        if sub == "/api/dlq" and method == "GET":
            return _json({"sample": queue_stats.dlq_muestra(5), "depth": queue_stats.profundidades().get("dlq", 0)})
        if sub == "/api/dlq/redrive" and method == "POST":
            _audit("dlq_redrive")
            return _json(queue_stats.dlq_redrive())
        if sub == "/api/dlq/purge" and method == "POST":
            _audit("dlq_purge")
            return _json(queue_stats.dlq_purgar())
        if sub == "/api/broadcasts" and method == "GET":
            return _json({"broadcasts": broadcast_store.listar()})
        if sub == "/api/metrics" and method == "GET":
            return _json(broadcast_store.metricas(30))
        if sub == "/api/audit" and method == "GET":
            return _json({"audit": audit_store.listar(50)})
        if sub == "/api/plans" and method == "GET":
            return _json({"plans": _planes_con_progreso()})
        if sub == "/api/plans/cancel" and method == "POST":
            pid = str(_body(event).get("pid", "")).strip()
            if pid:  # cancelar un envío puntual (en tiempo real)
                plan_store.cancelar(pid)
                _audit("cancelar_envio", pid)
                return _json({"ok": True, "canceled": 1, "pid": pid})
            n = plan_store.cancelar_pendientes()
            _audit("cancelar_pendientes", f"{n} difusiones")
            return _json({"ok": True, "canceled": n})
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
                sched = int(cuerpo.get("scheduled_at") or 0)  # epoch s; 0/ausente = enviar ya
            except (TypeError, ValueError):
                sched = 0
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
                    scheduled_at=sched or None,
                )
            except ValueError as e:
                return _json({"error": str(e)}, 400)
            canales = "+".join([c for c, on in (("tg", a_tg), ("wa", a_wa)) if on])
            _audit("broadcast" + (" (programado)" if sched else ""), f"[{canales}] {texto[:60]}")
            return _json({"ok": True, **res})
        if sub == "/api/schedules" and method == "GET":
            return _json({"schedules": schedule_store.listar()})
        if sub == "/api/schedules" and method == "POST":
            c = _body(event)
            texto = str(c.get("text", "")).strip()
            if not texto:
                return _json({"error": "El mensaje no puede estar vacío"}, 400)
            if len(texto) > 4096:
                return _json({"error": "El mensaje supera 4096 caracteres"}, 400)
            a_tg, a_wa = bool(c.get("telegram")), bool(c.get("whatsapp"))
            if not (a_tg or a_wa):
                return _json({"error": "Elige al menos un canal"}, 400)
            wa_list = str(c.get("whatsapp_list", "")).strip()
            if a_wa and not wa_list:
                return _json({"error": "WhatsApp exige elegir una lista (evita mandar a todos)"}, 400)
            img = str(c.get("image_url", "")).strip()
            if img and not img.startswith("https://"):
                return _json({"error": "La imagen debe ser una URL https://"}, 400)
            tipo = str(c.get("type", "once")).lower()
            if tipo not in ("once", "daily", "weekly"):
                return _json({"error": "Tipo de programación inválido"}, 400)
            tz = int(config.get().get("window_tz", -300))
            ahora = int(time.time())
            at, dias = "", []
            if tipo == "once":
                try:
                    next_run = int(c.get("run_at") or 0)
                except (TypeError, ValueError):
                    next_run = 0
                if next_run <= ahora:
                    return _json({"error": "La fecha y hora deben ser futuras"}, 400)
            else:
                at = str(c.get("at", "")).strip()
                if not hhmm(at):
                    return _json({"error": "Hora inválida (usa HH:MM)"}, 400)
                if tipo == "weekly":
                    dias = sorted({int(d) for d in (c.get("days") or []) if str(d).strip().isdigit() and 0 <= int(d) <= 6})
                    if not dias:
                        return _json({"error": "Elige al menos un día de la semana"}, 400)
                next_run = proximo_run(tipo, at, dias, tz, ahora)
                if not next_run:
                    return _json({"error": "No se pudo calcular el próximo envío"}, 400)
            sid = schedule_store.crear(
                name=str(c.get("name", "")).strip(), text=texto, image_url=img,
                telegram=a_tg, telegram_list=str(c.get("telegram_list", "")).strip(),
                whatsapp=a_wa, whatsapp_list=wa_list, type=tipo, at=at, days=dias,
                next_run=next_run, enabled=True,
            )
            canales = "+".join(x for x, on in (("tg", a_tg), ("wa", a_wa)) if on)
            _audit("schedule:crear", f"{tipo} [{canales}] {texto[:40]}")
            return _json({"ok": True, "sid": sid, "next_run": next_run})
        if sub == "/api/schedules/toggle" and method == "POST":
            c = _body(event)
            sid = str(c.get("sid", "")).strip()
            if not sid:
                return _json({"error": "sid requerido"}, 400)
            enabled = bool(c.get("enabled"))
            campos = {"enabled": enabled}
            if enabled:  # al reactivar un recurrente, recalcula su próximo disparo desde ahora
                s = next((x for x in schedule_store.listar() if x["sid"] == sid), None)
                if s and s["type"] != "once":
                    nr = proximo_run(s["type"], s["at"], s["days"], int(config.get().get("window_tz", -300)), int(time.time()))
                    if nr:
                        campos["next_run"] = nr
            schedule_store.actualizar(sid, **campos)
            _audit("schedule:toggle", f"{sid}={'on' if enabled else 'off'}")
            return _json({"ok": True})
        if sub == "/api/schedules/delete" and method == "POST":
            sid = str(_body(event).get("sid", "")).strip()
            if not sid:
                return _json({"error": "sid requerido"}, 400)
            schedule_store.borrar(sid)
            _audit("schedule:borrar", sid)
            return _json({"ok": True})
        if sub == "/api/telethon/send-code" and method == "POST":
            from adapters import telethon_login
            cfg = config.get()
            api_id, api_hash = cfg.get("telethon_api_id"), cfg.get("telethon_api_hash")
            if not (api_id and api_hash):
                return _json({"error": "Configura primero el API ID y API HASH de Telegram (my.telegram.org)."}, 400)
            phone = str(_body(event).get("phone", "")).strip()
            try:
                r = telethon_login.enviar_codigo(api_id, api_hash, phone)
            except telethon_login.TelethonLoginError as e:
                return _json({"error": str(e)}, 400)
            config.set_login_temp(r["session"], r["phone_code_hash"], phone)
            _audit("telethon:send-code", phone[-4:] if phone else "")
            return _json({"ok": True, "sent": True})
        if sub == "/api/telethon/sign-in" and method == "POST":
            from adapters import telethon_login
            cfg = config.get()
            api_id, api_hash = cfg.get("telethon_api_id"), cfg.get("telethon_api_hash")
            tmp = config.get_login_temp()
            if not tmp.get("session"):
                return _json({"error": "Primero envía el código (paso anterior)."}, 400)
            cuerpo = _body(event)
            code = str(cuerpo.get("code", "")).strip()
            password = cuerpo.get("password") or None
            if not code and not password:
                return _json({"error": "Ingresa el código que te llegó por Telegram."}, 400)
            try:
                res = telethon_login.confirmar(
                    api_id, api_hash, tmp["session"], tmp["phone"], tmp["phone_code_hash"],
                    code=code or None, password=password,
                )
            except telethon_login.TelethonLoginError as e:
                return _json({"error": str(e)}, 400)
            if res.get("status") == "needs_password":
                config.update_login_session(res["session"])  # sesión tras aceptar el código (para el 2FA)
                return _json({"ok": True, "needs_password": True})
            config.set({"telethon_session": res["session"], "send_mode": "userbot"})  # sesión definitiva + userbot
            config.clear_login_temp()
            _audit("telethon:sign-in", "conectado @" + str((res.get("me") or {}).get("username") or ""))
            return _json({"ok": True, "connected": True, "me": res.get("me")})
        if sub == "/api/telethon/logout" and method == "POST":
            config.set({"telethon_session": ""})  # vacía la sesión (telethon_session SÍ se borra aquí)
            config.clear_login_temp()
            _audit("telethon:logout", "limpiar sesión userbot")
            return _json({"ok": True})
        if sub == "/api/broadcasts/delete" and method == "POST":
            cuerpo = _body(event)
            if cuerpo.get("finished"):
                n = broadcast_store.borrar_terminados()
                _audit("broadcasts:borrar", f"terminados {n}")
                return _json({"ok": True, "deleted": n})
            ids = cuerpo.get("ids")
            if isinstance(ids, list) and ids:
                n = 0
                for x in ids:
                    try:
                        broadcast_store.borrar(str(x)); n += 1
                    except Exception:
                        pass
                _audit("broadcasts:borrar", f"masivo {n}")
                return _json({"ok": True, "deleted": n})
            bid = str(cuerpo.get("id", "")).strip()
            if not bid:
                return _json({"error": "id requerido"}, 400)
            broadcast_store.borrar(bid)
            _audit("broadcasts:borrar", bid)
            return _json({"ok": True})
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
        if sub == "/api/whatsapp/blocked" and method == "GET":
            return _whatsapp_proxy("/blocked")
        if sub == "/api/whatsapp/blocked/clear" and method == "POST":
            _audit("whatsapp_blocked_clear")
            return _whatsapp_proxy("/blocked/clear", body={})
        if sub == "/api/whatsapp/pair" and method == "POST":
            return _whatsapp_proxy("/pair", timeout=25, body={"number": _body(event).get("number", "")})
        if sub == "/api/whatsapp/reset" and method == "POST":
            _audit("whatsapp:reset", "limpiar sesión de WhatsApp")
            return _whatsapp_proxy("/reset", timeout=25, body={})
    except Exception:
        logger.exception("Error en admin %s %s", method, sub)
        return _json({"error": "internal"}, 500)

    return _json({"error": "not found"}, 404)


_PAGE = r"""<!doctype html><html lang="es" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replica · Panel</title>
<link rel="icon" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'><defs><linearGradient id='f' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%23FD531E'/><stop offset='1' stop-color='%23FD9E76'/></linearGradient></defs><rect width='48' height='48' rx='12' fill='url(%23f)'/><g fill='none' stroke='%23fff' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'><path d='M21 24c5 0 5.5-9 11.5-9'/><path d='M21 24h11.5'/><path d='M21 24c5 0 5.5 9 11.5 9'/></g><circle cx='15' cy='24' r='4.2' fill='%23fff'/><circle cx='33.5' cy='15' r='3' fill='%23fff'/><circle cx='34.5' cy='24' r='3' fill='%23fff'/><circle cx='33.5' cy='33' r='3' fill='%23fff'/></svg>">
<link rel="apple-touch-icon" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'><defs><linearGradient id='f' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%23FD531E'/><stop offset='1' stop-color='%23FD9E76'/></linearGradient></defs><rect width='48' height='48' rx='12' fill='url(%23f)'/><g fill='none' stroke='%23fff' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'><path d='M21 24c5 0 5.5-9 11.5-9'/><path d='M21 24h11.5'/><path d='M21 24c5 0 5.5 9 11.5 9'/></g><circle cx='15' cy='24' r='4.2' fill='%23fff'/><circle cx='33.5' cy='15' r='3' fill='%23fff'/><circle cx='34.5' cy='24' r='3' fill='%23fff'/><circle cx='33.5' cy='33' r='3' fill='%23fff'/></svg>">
<meta name="theme-color" content="#FD531E">
<style>
/* === DESIGN SYSTEM TOKENS (Replica) — fuente: docs/design-system/tokens.css === */
/* Replica Design System — Tokens de diseño
   Primario: #FD531E · Neutro base: #4A4A49
   Generado automaticamente. Editar aqui es la fuente de verdad de los valores. */

:root {
  /* ============================================================
     PALETA PRIMARIA / MARCA — Naranja (#FD531E = --color-primary-500)
     Tints (50..400) mezclados hacia blanco; shades (600..900) hacia
     negro calido, manteniendo el matiz ~13deg para coherencia.
     ============================================================ */
  --color-primary-50:  #FFF2ED;
  --color-primary-100: #FFE0D3;
  --color-primary-200: #FEC2A8;
  --color-primary-300: #FD9E76;
  --color-primary-400: #FD7848;
  --color-primary-500: #FD531E; /* base de marca */
  --color-primary-600: #E84410;
  --color-primary-700: #BD350B;
  --color-primary-800: #8F280A;
  --color-primary-900: #5E1B08;

  /* Alias semanticos de marca */
  --color-primary:       var(--color-primary-500);
  --color-primary-hover: var(--color-primary-600);
  --color-primary-active:var(--color-primary-700);
  --color-on-primary:    #FFFFFF; /* texto sobre primario */

  /* ============================================================
     ESCALA DE GRISES — armonizada con #4A4A49 (gris calido neutro).
     Matiz ligeramente calido (sin azul) para casar con el naranja.
     #4A4A49 se ubica en --color-gray-700.
     ============================================================ */
  --color-gray-50:  #FAFAF9;
  --color-gray-100: #F4F4F3;
  --color-gray-200: #E7E7E5;
  --color-gray-300: #D2D2CF;
  --color-gray-400: #A8A8A5;
  --color-gray-500: #7C7C7A;
  --color-gray-600: #5E5E5C;
  --color-gray-700: #4A4A49; /* base neutra */
  --color-gray-800: #333332;
  --color-gray-900: #1C1C1B;

  /* Utilitarios base */
  --color-white: #FFFFFF;
  --color-black: #0F0F0E;

  /* ============================================================
     COLORES SEMANTICOS (cada uno con texto fuerte + fondo suave)
     ============================================================ */
  --color-success:        #1E9E5A;
  --color-success-bg:     #E5F6EC;
  --color-success-border: #A7E0BF;

  --color-warning:        #E0900B;
  --color-warning-bg:     #FDF3DF;
  --color-warning-border: #F4D58A;

  --color-danger:         #DC362E;
  --color-danger-bg:      #FCEAE9;
  --color-danger-border:  #F3B5B2;

  --color-info:           #1E6FE0;
  --color-info-bg:        #E8F0FD;
  --color-info-border:    #AECBF6;

  /* ============================================================
     SUPERFICIE / TEXTO — Tema CLARO (por defecto)
     ============================================================ */
  --color-bg:         var(--color-gray-50);   /* fondo de pagina */
  --color-surface:    var(--color-white);     /* tarjetas, paneles */
  --color-surface-2:  var(--color-gray-100);  /* superficie elevada/zebra */
  --color-text:       var(--color-gray-900);  /* texto principal */
  --color-text-muted: var(--color-gray-500);  /* texto secundario */
  --color-border:     var(--color-gray-200);  /* bordes sutiles */
  --color-border-strong: var(--color-gray-300);
  --color-ring:       var(--color-primary-500);/* color del focus ring */
  --color-overlay:    rgba(28, 28, 27, 0.55); /* scrim de modales */

  /* ============================================================
     TIPOGRAFIA
     ============================================================ */
  --font-family-base: system-ui, -apple-system, "Segoe UI", Roboto,
                      "Helvetica Neue", Arial, "Noto Sans", sans-serif,
                      "Apple Color Emoji", "Segoe UI Emoji";
  --font-family-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo,
                      Consolas, "Liberation Mono", monospace;

  /* Escala de tamaños (base 16px, ratio ~1.2 Minor Third) */
  --text-xs:   0.75rem;   /* 12px */
  --text-sm:   0.875rem;  /* 14px */
  --text-base: 1rem;      /* 16px */
  --text-md:   1.125rem;  /* 18px */
  --text-lg:   1.25rem;   /* 20px */
  --text-xl:   1.5rem;    /* 24px */
  --text-2xl:  1.875rem;  /* 30px */
  --text-3xl:  2.25rem;   /* 36px */
  --text-4xl:  3rem;      /* 48px */

  /* Pesos */
  --font-weight-regular:  400;
  --font-weight-medium:   500;
  --font-weight-semibold: 600;
  --font-weight-bold:     700;

  /* Line-heights */
  --leading-none:    1;
  --leading-tight:   1.25;
  --leading-snug:    1.375;
  --leading-normal:  1.5;
  --leading-relaxed: 1.625;

  /* Letter-spacing */
  --tracking-tighter: -0.05em;
  --tracking-tight:   -0.025em;
  --tracking-normal:  0em;
  --tracking-wide:    0.025em;
  --tracking-wider:   0.05em;

  /* ============================================================
     ESPACIADO — escala base 4px
     ============================================================ */
  --space-0:  0;
  --space-1:  0.25rem;  /* 4px  */
  --space-2:  0.5rem;   /* 8px  */
  --space-3:  0.75rem;  /* 12px */
  --space-4:  1rem;     /* 16px */
  --space-5:  1.25rem;  /* 20px */
  --space-6:  1.5rem;   /* 24px */
  --space-7:  2rem;     /* 32px */
  --space-8:  2.5rem;   /* 40px */
  --space-9:  3rem;     /* 48px */
  --space-10: 4rem;     /* 64px */
  --space-11: 5rem;     /* 80px */
  --space-12: 6rem;     /* 96px */

  /* ============================================================
     RADIOS
     ============================================================ */
  --radius-sm:   0.25rem;  /* 4px  */
  --radius-md:   0.5rem;   /* 8px  */
  --radius-lg:   0.75rem;  /* 12px */
  --radius-xl:   1rem;     /* 16px */
  --radius-full: 9999px;

  /* ============================================================
     SOMBRAS (tono calido derivado del gris de marca)
     ============================================================ */
  --shadow-sm: 0 1px 2px 0 rgba(28, 28, 27, 0.06);
  --shadow-md: 0 2px 4px -1px rgba(28, 28, 27, 0.08),
              0 4px 8px -2px rgba(28, 28, 27, 0.08);
  --shadow-lg: 0 4px 8px -2px rgba(28, 28, 27, 0.10),
              0 12px 20px -4px rgba(28, 28, 27, 0.10);
  --shadow-xl: 0 8px 16px -4px rgba(28, 28, 27, 0.12),
              0 24px 40px -8px rgba(28, 28, 27, 0.14);

  /* ============================================================
     Z-INDEX
     ============================================================ */
  --z-base:     0;
  --z-dropdown: 1000;
  --z-sticky:   1100;
  --z-modal:    1300;
  --z-toast:    1400;

  /* ============================================================
     TRANSICIONES
     ============================================================ */
  --transition-fast: 120ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-base: 200ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-slow: 320ms cubic-bezier(0.4, 0, 0.2, 1);

  /* ============================================================
     FOCUS RING
     ============================================================ */
  --focus-ring-width:  2px;
  --focus-ring-offset: 2px;
  --focus-ring-color:  var(--color-ring);
  --focus-ring: 0 0 0 var(--focus-ring-offset) var(--color-surface),
               0 0 0 calc(var(--focus-ring-offset) + var(--focus-ring-width)) var(--color-ring);
}

/* ============================================================
   TEMA OSCURO — via atributo explicito [data-theme="dark"]
   ============================================================ */
[data-theme="dark"] {
  --color-bg:         var(--color-gray-900);
  --color-surface:    var(--color-gray-800);
  --color-surface-2:  #2A2A29;            /* entre 800 y 700 */
  --color-text:       var(--color-gray-50);
  --color-text-muted: var(--color-gray-400);
  --color-border:     #3A3A39;            /* entre 800 y 700 */
  --color-border-strong: var(--color-gray-600);
  --color-ring:       var(--color-primary-400);
  --color-overlay:    rgba(0, 0, 0, 0.65);

  /* Primario: aclarado para legibilidad sobre fondos oscuros */
  --color-primary:       var(--color-primary-400);
  --color-primary-hover: var(--color-primary-300);
  --color-primary-active:var(--color-primary-500);
  --color-on-primary:    #1C1C1B;

  /* Semanticos: fondos suaves re-derivados a baja luminancia */
  --color-success-bg: #11331F;
  --color-warning-bg: #33270A;
  --color-danger-bg:  #38120F;
  --color-info-bg:    #0F2440;

  --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.40);
  --shadow-md: 0 2px 4px -1px rgba(0, 0, 0, 0.45),
              0 4px 8px -2px rgba(0, 0, 0, 0.45);
  --shadow-lg: 0 4px 8px -2px rgba(0, 0, 0, 0.50),
              0 12px 20px -4px rgba(0, 0, 0, 0.50);
  --shadow-xl: 0 8px 16px -4px rgba(0, 0, 0, 0.55),
              0 24px 40px -8px rgba(0, 0, 0, 0.60);
}

/* Fallback automatico por preferencia del SO (si no hay data-theme) */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --color-bg:         var(--color-gray-900);
    --color-surface:    var(--color-gray-800);
    --color-surface-2:  #2A2A29;
    --color-text:       var(--color-gray-50);
    --color-text-muted: var(--color-gray-400);
    --color-border:     #3A3A39;
    --color-border-strong: var(--color-gray-600);
    --color-ring:       var(--color-primary-400);
    --color-overlay:    rgba(0, 0, 0, 0.65);
    --color-primary:       var(--color-primary-400);
    --color-primary-hover: var(--color-primary-300);
    --color-primary-active:var(--color-primary-500);
    --color-on-primary:    #1C1C1B;
    --color-success-bg: #11331F;
    --color-warning-bg: #33270A;
    --color-danger-bg:  #38120F;
    --color-info-bg:    #0F2440;
  }
}

:root{
  --bg:#1A1917; --bg2:#111010;
  --card:#2B2A27; --card2:#232220; --elev:#302E2B;
  --bd:#3A3733; --bd2:#565049;
  --tx:#FBFAF9; --tx2:#E4E1DB; --mut:#A39D93; --mut2:#787269;
  --ac:#FD531E; --ac-h:#FF6A3C; --ac2:#FF9166;
  --ok:#34d399; --warn:#fbbf24; --bad:#fb7185; --info:#60a5fa;
  --r:14px; --r-sm:10px;
  --sh:0 1px 0 rgba(255,255,255,.04) inset, 0 1px 2px rgba(0,0,0,.28), 0 24px 56px -28px rgba(0,0,0,.82);
  --glow:0 6px 22px -8px rgba(253,83,30,.55);
  --ring:0 0 0 3px rgba(253,83,30,.28);
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
    radial-gradient(1000px 560px at 86% -10%, rgba(253,83,30,.15), transparent 60%),
    radial-gradient(760px 460px at 4% 2%, rgba(255,120,72,.07), transparent 55%),
    radial-gradient(600px 600px at 100% 100%, rgba(253,83,30,.05), transparent 60%);
  background-attachment:fixed;
}
::selection{background:rgba(253,83,30,.38);color:#fff}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#3A3733;border-radius:8px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:#565049;background-clip:padding-box}
a{color:var(--ac2)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;
  background:var(--elev);border:1px solid var(--bd);padding:1px 6px;border-radius:6px;color:#E7E7E5}

/* ---------- marca ---------- */
.brand{display:flex;align-items:center;gap:11px}
.brand .wordmark{
  font-weight:800;font-size:20px;letter-spacing:-.5px;
  background:linear-gradient(95deg,#FFD3C2,#FD7848);
  -webkit-background-clip:text;background-clip:text;color:transparent;
}
.brand svg{border-radius:11px;filter:drop-shadow(0 6px 18px rgba(253,83,30,.45))}
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
  background:rgba(26,25,23,.74);backdrop-filter:blur(16px) saturate(150%);
  padding:13px 22px;border-bottom:1px solid var(--bd);
  position:sticky;top:0;z-index:5;
}
header .t{font-weight:700;font-size:16px}
header .u{color:var(--mut);font-size:12.5px;display:inline-flex;align-items:center;gap:8px}
main{max-width:900px;margin:0 auto;padding:26px 22px 80px;display:grid;gap:18px}

/* ---------- nav (pestañas horizontales) ---------- */
.nav{
  position:sticky;top:50px;z-index:4;
  background:rgba(26,25,23,.82);backdrop-filter:blur(16px) saturate(150%);
  display:flex;gap:6px;justify-content:center;align-items:center;
  padding:11px 14px;border-bottom:1px solid var(--bd);flex-wrap:wrap;
}
.nav button{
  background:transparent;border:1px solid transparent;color:var(--mut);
  padding:8px 15px;border-radius:999px;font-weight:600;font-size:13px;
  cursor:pointer;transition:color .15s,background .15s,border-color .15s;
}
.nav button:hover{color:var(--tx2);background:rgba(255,255,255,.05);filter:none;box-shadow:none}
.nav button.on{
  background:rgba(253,83,30,.14);color:#FFE0D3;border-color:rgba(253,83,30,.4);
}

/* ---------- barra global de estado de envíos (acción siempre visible) ---------- */
#send_banner{margin:0 0 2px;border-radius:var(--r);padding:14px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
#send_banner[hidden]{display:none}
#send_banner .sb-txt{flex:1;min-width:210px;font-size:13.5px;line-height:1.45}
#send_banner .sb-dot{width:9px;height:9px;border-radius:50%;flex:none}
#send_banner.paused{background:linear-gradient(180deg,rgba(251,113,133,.18),rgba(251,113,133,.07));border:1px solid rgba(251,113,133,.5);color:#FFE3E0;box-shadow:0 12px 32px -16px rgba(251,113,133,.55)}
#send_banner.paused .sb-dot{background:var(--bad);box-shadow:0 0 0 4px rgba(251,113,133,.18);animation:pulseDot 1.4s ease-in-out infinite}
#send_banner.active{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.26);color:var(--tx2);padding:10px 16px}
#send_banner.active .sb-dot{background:var(--ok);box-shadow:0 0 0 4px rgba(52,211,153,.16)}
#send_banner button{flex:none}
#send_banner .sb-go{background:linear-gradient(180deg,#34d399,#10b981);border-color:rgba(255,255,255,.12);font-size:14px;padding:11px 22px;font-weight:700}
#send_banner .sb-go:hover{filter:brightness(1.06);box-shadow:0 8px 24px -8px rgba(16,185,129,.6)}
#send_banner .sb-pause{background:transparent;border:1px solid rgba(255,255,255,.18);color:var(--mut);font-size:12.5px;padding:6px 13px}
#send_banner .sb-pause:hover{background:rgba(255,255,255,.06);color:var(--tx2);filter:none;box-shadow:none}

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
input::placeholder,textarea::placeholder{color:var(--mut)}
input:hover,textarea:hover,select:hover{border-color:var(--bd2)}
input:focus,textarea:focus,select:focus{outline:0;border-color:var(--ac);box-shadow:var(--ring);background:var(--elev)}
textarea{min-height:88px;resize:vertical;font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:13px;line-height:1.55}
select{appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%23A8A8A5' stroke-width='2.5'><path d='M6 9l6 6 6-6'/></svg>");
  background-repeat:no-repeat;background-position:right 12px center;padding-right:36px;cursor:pointer}
input[type=file]{padding:9px 12px;color:var(--mut);cursor:pointer}
input[type=file]::file-selector-button{
  background:#34322F;border:1px solid var(--bd2);color:var(--tx2);
  border-radius:8px;padding:7px 13px;margin-right:12px;cursor:pointer;font:inherit;font-weight:600}
input[type=file]::file-selector-button:hover{background:#3F3D39}
input[type=checkbox],input[type=radio]{accent-color:var(--ac);width:auto;cursor:pointer}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}

/* ---------- buttons ---------- */
button{
  background:linear-gradient(180deg,var(--ac-h),var(--ac));color:#fff;border:1px solid rgba(255,255,255,.07);border-radius:var(--r-sm);
  padding:10px 17px;font-size:13.5px;font-weight:600;font-family:inherit;cursor:pointer;
  transition:background .15s,filter .15s,transform .05s,box-shadow .15s,border-color .15s,opacity .15s;
}
button:hover{filter:brightness(1.07);box-shadow:var(--glow)}
button:active{transform:translateY(1px);filter:brightness(.97)}
button:focus-visible{outline:0;box-shadow:0 0 0 3px rgba(253,83,30,.4)}
button:disabled{opacity:.5;cursor:not-allowed;transform:none;filter:none;box-shadow:none}
button.sec{background:#34322F;color:var(--tx2);border-color:var(--bd2)}
button.sec:hover{background:#3F3D39;filter:none;box-shadow:none}
button.ghost{background:transparent;border:1px solid var(--bd2);color:var(--mut)}
button.ghost:hover{background:rgba(255,255,255,.05);color:var(--tx2);filter:none;box-shadow:none}
button.danger{background:var(--color-danger);color:#fff;border-color:transparent}
button.danger:hover{background:#C02B24;filter:none;box-shadow:0 6px 22px -8px rgba(192,43,36,.5)}

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
.pill.sending{background:rgba(253,120,72,.12);color:var(--ac2);border-color:rgba(253,120,72,.28)}
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
  background:#11331F;color:#9ff0d2;border:1px solid rgba(52,211,153,.35);
  padding:12px 18px;border-radius:var(--r-sm);font-weight:600;font-size:13px;
  box-shadow:0 18px 50px -16px rgba(0,0,0,.7);
  opacity:0;transform:translateY(12px);transition:opacity .25s,transform .25s;pointer-events:none;
}
.toast.show{opacity:1;transform:none}
.toast.err{background:#38120F;color:#ffc0c8;border-color:rgba(251,113,133,.4)}

/* ---------- tab visibility ---------- */
main>.card{display:none;animation:fade .22s ease}
main>.card.show{display:block}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ===========================================================
   NUEVO — Componer y Envíos (data-tab="envios")
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
.chan.tg.on{border-color:rgba(253,83,30,.55);background:rgba(253,83,30,.1);color:#FFE0D3}
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
.bc-src{text-transform:uppercase;letter-spacing:.5px;font-weight:700;font-size:10px;color:var(--mut);
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
.bar>i{display:block;height:100%;width:0;border-radius:999px;transition:width .5s ease;background:linear-gradient(90deg,var(--ac),#FD9E76)}
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
  table{font-size:12.5px}
  th,td{padding:8px 7px}
  .tbl-toolbar button,.compose-actions button{flex:1 1 auto}
  .ds-modal-actions button{flex:1}
  .markup{flex-wrap:wrap}
  .markup input{width:100%}
  #fuentes_subnav{width:100%}
  #fuentes_subnav button{flex:1 1 auto}
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
  --ac-soft:rgba(253,83,30,.14);
  --ac2-soft:rgba(253,120,72,.12);
  --ok-soft:rgba(52,211,153,.12);
  --warn-soft:rgba(251,191,36,.12);
  --bad-soft:rgba(251,113,133,.12);
  --r-lg:18px;
  --tx3:#A8A8A5;            /* secundario un punto mas contrastado que --mut */
  --sh-sm:0 6px 18px -10px rgba(0,0,0,.6);
}

/* ------------------------------------------------------------
   1) FIX: filas de listas con separador #3A3A39 (roto en oscuro)
   El JS pinta inline border-bottom:1px solid #3A3A39 (admin.py L854);
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
  background:var(--info);color:#0F2440;margin-top:1px}
.callout.warn{border-left-color:var(--warn);background:var(--warn-soft)}
.callout.warn::before{content:"!";background:var(--warn);color:#33270A}
.callout.danger{border-left-color:var(--bad);background:var(--bad-soft)}
.callout.danger::before{content:"!";background:var(--bad);color:#38120F}
.callout.ok{border-left-color:var(--ok);background:var(--ok-soft)}
.callout.ok::before{content:"\2713";background:var(--ok);color:#11331F}
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
  font-size:26px;background:var(--ac-soft);border:1px solid rgba(253,83,30,.28);
  color:#FFE0D3;margin-bottom:4px;
}
.empty-state h3{margin:0;font-size:15px;color:var(--tx)}
.empty-state p{margin:0;max-width:340px;font-size:12.5px;line-height:1.6}
.empty-state .cta{margin-top:6px}
/* refuerzo visual de los empties existentes */
.bc-empty, #subsempty{
  border:1px dashed var(--bd2);border-radius:var(--r);
  background:rgba(255,255,255,.04);
}
/* skeleton shimmer (el front puede inyectar .skeleton .sk-line al cargar) */
.skeleton{pointer-events:none}
.sk-line{
  height:12px;border-radius:6px;margin:9px 0;
  background:linear-gradient(90deg,var(--elev) 25%,#0F2440 37%,var(--elev) 63%);
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
button.ok{background:var(--ok);border-color:transparent;color:#11331F}
button.ok:hover{background:#46e0a9}
.err:empty{margin-top:0;min-height:0}
/* toasts: icono, variantes y barra de auto-cierre */
.toast{display:flex;align-items:center;gap:9px;padding-right:16px;overflow:hidden}
.toast::before{content:"\2713";font-weight:800}
.toast.err::before{content:"!"}
.toast.info{background:#0F2440;color:#bcd6ff;border-color:rgba(96,165,250,.35)}
.toast.info::before{content:"i"}
.toast.warn{background:#33270A;color:#f3dca0;border-color:rgba(251,191,36,.4)}
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
  background:rgba(255,255,255,.04);
}
tbody tr:hover td{background:rgba(255,255,255,.07)}
/* badge de origen del envio mas legible */
.bc-src{background:var(--ac-soft);border-color:rgba(253,83,30,.28);color:#E7E7E5}
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
/* sub-navegación de Fuentes y listas (divide la vista por Fuente / Telegram / WhatsApp) */
#fuentes_subnav{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
#fuentes_subnav button{background:transparent;border:1px solid var(--bd2);color:var(--mut);padding:8px 16px;border-radius:999px;font-weight:600;font-size:13px;cursor:pointer;transition:color .15s,background .15s,border-color .15s}
#fuentes_subnav button:hover{color:var(--tx2);filter:none;box-shadow:none}
#fuentes_subnav button.on{background:rgba(253,83,30,.14);color:#FFE0D3;border-color:rgba(253,83,30,.4)}
.card.subhide{display:none !important}
/* Tablas con selección masiva (patrón reutilizable: checkbox + barra de acciones) */
tbody tr.sel-row td{background:rgba(253,83,30,.12)}
.tbl-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:14px}
.tbl-toolbar .grow{flex:1}
.bc-err{color:var(--bad);font-size:11px;margin-top:4px;line-height:1.35;max-width:240px;cursor:pointer;border-bottom:1px dotted rgba(255,107,90,.5);display:inline-block}
.bc-err:hover{color:#ff8f7d}
.bc-err:focus-visible{outline:2px solid var(--ac2);outline-offset:2px;border-radius:3px}
/* Sistema de modales reutilizable (reemplaza confirm()/prompt() nativos por diálogos de marca) */
.ds-overlay{position:fixed;inset:0;background:rgba(0,0,0,.66);display:flex;align-items:center;justify-content:center;z-index:1300;padding:20px;animation:dsFade .14s ease}
@keyframes dsFade{from{opacity:0}to{opacity:1}}
.ds-modal{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--bd2);border-radius:16px;box-shadow:0 24px 60px -16px rgba(0,0,0,.7);padding:22px;max-width:440px;width:100%;animation:dsPop .16s ease}
@keyframes dsPop{from{transform:translateY(10px) scale(.98);opacity:0}to{transform:none;opacity:1}}
.ds-modal h3{margin:0 0 10px;font-size:17px}
.ds-modal-body{color:var(--tx2);font-size:14px;line-height:1.55;white-space:pre-line}
.ds-modal input{width:100%;margin-top:14px}
.ds-modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:20px;flex-wrap:wrap}
/* Accesibilidad: foco visible consistente por teclado en elementos interactivos */
a:focus-visible,input[type=checkbox]:focus-visible,input[type=radio]:focus-visible,#fuentes_subnav button:focus-visible,.nav button:focus-visible,.chan:focus-within{outline:2px solid var(--ac);outline-offset:2px;border-radius:8px}
@keyframes kpipulse{0%,100%{opacity:.35}50%{opacity:.7}}
.stat b.kpi-load{animation:kpipulse 1s ease-in-out infinite}
/* === Revisión móvil: ajustes UI/UX integrales para pantallas pequeñas === */
@media (max-width:620px){
  header{position:static;flex-wrap:wrap;gap:8px;padding:10px 14px}
  header>div{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  header .brand .wordmark{font-size:17px}
  header .brand svg{width:26px;height:26px}
  .nav{top:0}
  main{padding:16px 12px 84px}
  main table{display:block;overflow-x:auto;max-width:100%;-webkit-overflow-scrolling:touch}
  .tbl-toolbar,.compose-actions{flex-direction:column;align-items:stretch}
  .tbl-toolbar .grow,.compose-actions .grow{display:none}
  input[type=datetime-local],input[type=time]{width:100%;max-width:100%}
  .pickbox{max-height:200px}
  .ds-modal{padding:18px}
  .ds-modal-actions{flex-direction:column-reverse}
  .ds-modal-actions button{width:100%}
  .stat b{font-size:26px}
  .markup input{font-size:26px}
  h1{font-size:18px}
  html,body{overflow-x:hidden;max-width:100%}
  main,.card{max-width:100%}
  .nav{flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;justify-content:flex-start;-webkit-overflow-scrolling:touch}
  .nav button{flex:0 0 auto}
  .nav button.on::after{display:none}
}
th.selcol,td.selcol{width:34px;text-align:center}
</style></head><body>

<div id="login"><div class="box">
  <div class="brand brand-lg"><svg viewBox="0 0 48 48" width="46" height="46" aria-hidden="true"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#FD531E"/><stop offset="1" stop-color="#FD9E76"/></linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#lg)"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 24c5 0 5.5-9 11.5-9"/><path d="M21 24h11.5"/><path d="M21 24c5 0 5.5 9 11.5 9"/></g><circle cx="15" cy="24" r="4.2" fill="#fff"/><circle cx="33.5" cy="15" r="3" fill="#fff"/><circle cx="34.5" cy="24" r="3" fill="#fff"/><circle cx="33.5" cy="33" r="3" fill="#fff"/></svg><span class="wordmark">Replica</span></div>
  <p style="text-align:center">Captura listas de precios y envíalas a tus contactos — Telegram y WhatsApp, al instante o programado.</p>
  <label>Usuario</label><input id="lu" autocomplete="username" placeholder="usuario o correo">
  <label>Contraseña</label><input id="lp" type="password" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
  <div class="err" id="lerr"></div>
  <button style="width:100%;margin-top:8px" onclick="doLogin()">Entrar</button>
  <div style="text-align:center;margin-top:12px"><a href="#" onclick="fpToggle();return false" style="color:var(--ac);font-size:13px;text-decoration:none">¿Olvidaste tu contraseña?</a></div>
  <div id="fp_box" style="display:none;margin-top:14px;border-top:1px solid var(--bd);padding-top:14px">
    <label>Usuario o correo</label><input id="fp_user" placeholder="tu usuario">
    <button class="sec" style="width:100%;margin-top:8px" onclick="fpSend()">Enviar código</button>
    <div id="fp_step2" style="display:none;margin-top:10px">
      <label>Código (te llega por correo)</label><input id="fp_code" inputmode="numeric" placeholder="123456">
      <label>Nueva contraseña (mínimo 8)</label><input id="fp_new" type="password">
      <button style="width:100%;margin-top:8px" onclick="fpReset()">Restablecer contraseña</button>
    </div>
    <div class="hint" id="fp_status" style="margin-top:8px"></div>
  </div>
</div></div>

<div id="app">
 <header><div class="brand"><svg viewBox="0 0 48 48" width="30" height="30" aria-hidden="true"><defs><linearGradient id="lg2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#FD531E"/><stop offset="1" stop-color="#FD9E76"/></linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#lg2)"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 24c5 0 5.5-9 11.5-9"/><path d="M21 24h11.5"/><path d="M21 24c5 0 5.5 9 11.5 9"/></g><circle cx="15" cy="24" r="4.2" fill="#fff"/><circle cx="33.5" cy="15" r="3" fill="#fff"/><circle cx="34.5" cy="24" r="3" fill="#fff"/><circle cx="33.5" cy="33" r="3" fill="#fff"/></svg><span class="wordmark">Replica</span></div><div><span id="conn_tg" class="pill" title="Estado del bot de Telegram" style="margin-right:6px"></span><span id="conn_wa" class="pill" title="Estado del servicio WhatsApp" style="margin-right:6px"></span><span id="hdr_badge" class="pill" style="display:none;margin-right:10px"></span><span class="u" id="who"></span>
   <button class="ghost" style="margin-left:12px;padding:7px 12px" onclick="logout()">Salir</button></div></header>
 <nav class="nav">
   <button data-tab="inicio" onclick="showTab('inicio')">🏠 Inicio</button>
   <button data-tab="fuentes" onclick="showTab('fuentes')">📋 Fuentes y listas</button>
   <button data-tab="envios" onclick="showTab('envios')">📨 Envíos</button>
   <button data-tab="ajustes" onclick="showTab('ajustes')">⚙️ Ajustes y estado</button>
 </nav>
 <main>
  <div id="send_banner" hidden></div>
  <div class="card accent" data-tab="inicio"><h2>Resumen</h2>
   <div class="hint" style="margin:-4px 0 12px">Replica captura la lista de un canal fuente (con markup) <b>y</b> envía tus propios mensajes a listas de contactos por Telegram y WhatsApp — al instante o programados. Gestiona fuentes y listas en <b>📋 Fuentes y listas</b> y los envíos en <b>📨 Envíos</b>.</div>
   <div id="dash_estado" class="callout">cargando…</div>
   <div class="stats" style="margin-top:14px">
     <div class="stat"><b id="k_sent">–</b><span>enviados (30 días)</span></div>
     <div class="stat"><b id="k_rate">–</b><span>tasa de éxito</span></div>
     <div class="stat"><b id="k_pend">–</b><span>lotes pendientes</span></div>
     <div class="stat"><b id="k_dlq">–</b><span>en DLQ</span></div>
   </div>
   <div id="dash_serie" style="margin-top:16px"></div>
   <div id="dash_last" class="hint" style="margin-top:14px"></div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">
     <button onclick="showTab('envios')">📨 Componer y enviar</button>
     <button class="sec" onclick="showTab('ajustes')">⚙️ Ajustes y estado</button>
     <button class="sec" onclick="loadDashboard()">Refrescar</button>
   </div>
  </div>
  <div class="card" data-tab="inicio" id="dash_steps_card"><h2>Primeros pasos <span id="steps_n" class="hint"></span></h2>
   <div class="hint">Configuración guiada. Cada paso te lleva a su pestaña.</div>
   <div id="dash_steps" style="margin-top:10px">cargando…</div>
  </div>
  <div class="card" data-tab="fuentes" style="padding:14px 18px"><div id="fuentes_subnav"><span class="hint" style="margin:0 8px 0 0">Ver:</span><button data-sub="fuente" onclick="showSub('fuente')">📡 Fuente del canal</button><button data-sub="tg" onclick="showSub('tg')">✈️ Telegram</button><button data-sub="wa" onclick="showSub('wa')">🟢 WhatsApp</button></div></div>
  <div class="card" data-tab="fuentes" data-sub="fuente"><h2>Aumento (markup)</h2>
   <div class="markup"><input id="markup_percentage" type="number" step="0.1"><div>
     <div style="font-size:13px">% que se suma a cada precio</div>
     <div class="hint">Ej: $325.000 + 15% → $374.000 (redondeo al mil ↑)</div></div></div>
  </div>
  <div class="card accent" data-tab="ajustes"><h2>Cuenta de Telegram</h2>
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
   <label>Sesión de la cuenta <span id="sess_status" class="hint"></span></label>
   <div class="hint">Conéctate <b>sin scripts</b>: guarda primero el API ID y API Hash (botón de abajo), luego ingresa tu número, recibe el código por Telegram y confírmalo aquí mismo. La plataforma genera y guarda la sesión sola.</div>
   <div class="row" style="margin-top:8px">
     <div><label>Teléfono (formato internacional)</label><input id="tl_phone" placeholder="+57 300 123 4567"></div>
     <div style="display:flex;align-items:flex-end"><button class="sec" id="tl_send" onclick="tlSendCode()" style="width:100%">Enviar código</button></div>
   </div>
   <div id="tl_step2" style="display:none;margin-top:10px">
     <div class="row">
       <div><label>Código recibido</label><input id="tl_code" inputmode="numeric" placeholder="12345"></div>
       <div id="tl_pwd_wrap" style="display:none"><label>Contraseña (verificación en 2 pasos)</label><input id="tl_password" type="password" placeholder="contraseña 2FA"></div>
     </div>
     <button id="tl_confirm" onclick="tlSignIn()" style="margin-top:8px">Confirmar y conectar</button>
     <span id="tl_status" class="hint" style="margin-left:10px"></span>
   </div>
   <div class="section-label" style="margin-top:14px">Avanzado: pegar StringSession</div>
   <input id="telethon_session" type="password" placeholder="(pega para unir/cambiar la cuenta)">
   <div class="hint">Alternativa manual: genérala con <code>scripts/generar_sesion.py</code>. Da acceso total a esa cuenta: trátala como secreto.</div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:4px">
     <button onclick="saveAccount()">Guardar cuenta</button>
     <button class="danger" onclick="tlLogout()">🗑 Limpiar sesión userbot</button>
     <span id="tl_logout_out" class="hint" style="margin-top:0"></span>
   </div>
  </div>
  <div class="card" data-tab="ajustes"><h2>WhatsApp (reenvío)</h2>
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
     <div style="margin-top:10px"><button class="danger" onclick="waReset()">🗑 Limpiar sesión de WhatsApp</button> <span class="hint" id="wa_reset_out" style="margin-left:6px"></span></div>
     <div class="hint" style="margin-top:10px">💡 Lo más fiable si Render bloquea el linking: vincula <b>localmente</b> (corre el servicio en tu PC con las mismas credenciales AWS) y Render reusará la sesión guardada en DynamoDB.</div>
   </div>
   <div class="callout warn">⚠️ Enviar masivamente por WhatsApp puede banear tu número. Empieza con listas pequeñas. Las <b>exclusiones</b> se gestionan por nombre abajo, en <b>Destinatarios WhatsApp</b>.</div>
  </div>
  <div class="card" data-tab="fuentes" data-sub="fuente"><h2>Canal y mensaje</h2>
   <div class="hint" style="margin-top:-4px">La <b>captura del canal está siempre activa</b> (se guarda cada lista publicada). El <b>envío</b> a tus contactos se controla con el <b>interruptor</b> en «Ajustes y estado»: si está apagado, lo capturado queda en espera.</div>
   <label>Canal fuente (username sin @)</label><input id="source_channel">
   <label>Símbolos de moneda</label><input id="currency_symbols">
   <label>Footer WhatsApp (se añade al final de cada lista)</label><textarea id="whatsapp_footer"></textarea>
   <label>Patrones a quitar (ubicación), uno por línea</label><textarea id="strip_patterns"></textarea>
   <button onclick="saveCfg()">Guardar cambios</button>
  </div>
  <div class="card" data-tab="fuentes" data-sub="fuente"><h2>Probar procesamiento del mensaje</h2>
   <div class="hint">Pega un mensaje tal como lo publica el canal y mira cómo quedará <b>ya procesado</b> (markup aplicado, sin ubicación/marca/teléfonos, con footer). Así verificas las reglas antes de enviar.</div>
   <textarea id="pp_in" style="min-height:90px;margin-top:10px" placeholder="Pega aquí el texto original del canal..."></textarea>
   <button class="sec" style="margin-top:8px" onclick="probarProcesado()">Procesar</button>
   <div class="hint" style="margin-top:10px">Resultado (lo que se enviaría):</div>
   <div id="pp_out" style="white-space:pre-wrap;background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:12px;margin-top:4px;min-height:40px;font-size:13px;color:var(--tx2)">—</div>
  </div>
  <div class="card" data-tab="fuentes" data-sub="fuente"><h2>Imagen de la lista</h2>
   <div class="hint">Se envía como foto antes de cada lista. Sube un archivo o pega una URL.</div>
   <input type="file" id="imgfile" accept="image/*" style="margin-top:10px" onchange="uploadImg()">
   <img id="imgprev" class="preview" style="display:none">
   <label>…o URL externa</label><input id="image_url" placeholder="https://...">
   <button class="sec" onclick="saveCfg()">Guardar URL</button>
  </div>
  <div class="card" data-tab="ajustes"><h2>Cola de mensajes</h2>
   <div class="stats"><div class="stat"><b id="q_p">–</b><span>lotes programados pendientes</span></div>
     <div class="stat"><b id="q_b">–</b><span>en cola SQS (en vuelo)</span></div>
     <div class="stat"><b id="q_d">–</b><span>en DLQ (fallidos)</span></div></div>
   <div class="hint" style="margin-top:10px">Con el envío fraccionado, los lotes esperan en la <b>programación</b> y se liberan de a uno; por eso "en cola SQS" suele ser 0 o 1 (el lote en vuelo). Mira el detalle en <b>⏱️ Programación</b>.</div>
   <button class="sec" style="margin-top:14px" onclick="loadQueue()">Refrescar</button>
  </div>
  <div class="card" data-tab="ajustes"><h2>Cola de fallidos (DLQ) <span id="dlq_n" class="hint"></span></h2>
   <div class="hint">Lotes que agotaron reintentos. Puedes <b>reintentarlos</b> (vuelven a la cola) o <b>descartarlos</b>.</div>
   <div id="dlq_list" style="margin-top:10px"></div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
     <button class="sec" onclick="loadDlq()">Ver / refrescar</button>
     <button class="sec" onclick="dlqRedrive()">↩ Reintentar todo</button>
     <button class="danger" onclick="dlqPurge()">🗑 Descartar todo</button>
   </div>
  </div>
  <div class="card" data-tab="ajustes"><h2>Auditoría <span id="audit_n" class="hint"></span></h2>
   <div class="hint">Últimas acciones realizadas en el panel (config, envíos, cancelaciones, DLQ).</div>
   <div style="overflow-x:auto;margin-top:10px"><table><thead><tr><th>cuándo</th><th>usuario</th><th>acción</th><th>detalle</th></tr></thead><tbody id="audit_rows"></tbody></table></div>
   <button class="sec" style="margin-top:12px" onclick="loadAudit()">Refrescar</button>
  </div>
  <div class="card" data-tab="fuentes" data-sub="tg"><h2>Destinatarios <span id="subcount" class="hint"></span></h2>
   <div class="hint">Busca, navega y usa los botones para incluir/excluir en masa. Los excluidos NO reciben las listas.</div>
   <input id="subsearch" placeholder="🔎 Buscar por nombre o número..." oninput="onSearch()" style="margin-top:10px">
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
     <button class="sec" onclick="toggleAll(true)">Marcar visibles</button>
     <button class="sec" onclick="toggleAll(false)">Desmarcar</button>
     <button onclick="bulk('excluir')">Excluir marcados</button>
     <button onclick="bulk('incluir')">Incluir marcados</button>
     <button class="sec" onclick="createListFromGrid('telegram')">➕ Crear lista con marcados</button>
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
  <div class="card" data-tab="fuentes" data-sub="tg"><h2>Listas de distribución · Telegram</h2>
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
  <div class="card" data-tab="fuentes" data-sub="wa"><h2>Destinatarios WhatsApp <span id="wa_c_count" class="hint"></span></h2>
   <div class="hint">Carga tus contactos (servicio conectado), busca por nombre, y marca para <b>excluir/incluir</b>. Los excluidos NO reciben las difusiones por WhatsApp.</div>
   <button class="sec" style="margin-top:10px" onclick="loadWaContacts()">Cargar contactos de WhatsApp</button>
   <input id="wa_search" placeholder="🔎 Buscar por nombre o número..." oninput="renderWa()" style="margin-top:10px">
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
     <button class="sec" onclick="waToggleAll(true)">Marcar visibles</button>
     <button class="sec" onclick="waToggleAll(false)">Desmarcar</button>
     <button onclick="waBulk('excluir')">Excluir marcados</button>
     <button onclick="waBulk('incluir')">Incluir marcados</button>
     <button class="sec" onclick="createListFromGrid('whatsapp')">➕ Crear lista con marcados</button>
   </div>
   <table><thead><tr><th></th><th>nombre</th><th>estado</th></tr></thead><tbody id="wa_subs"></tbody></table>
   <div style="display:flex;gap:12px;align-items:center;margin-top:10px"><button class="sec" onclick="waPrev()">◀</button><span id="wa_pageinfo" class="hint"></span><button class="sec" onclick="waNext()">▶</button></div>
  </div>
  <div class="card" data-tab="fuentes" data-sub="wa"><h2>Auto-excluidos por fallos <span id="wa_blk_n" class="hint"></span></h2>
   <div class="hint">Los contactos que fallan al enviar de forma repetida (≥ umbral) se excluyen <b>solos</b> de los próximos envíos para proteger tu número. Limpia el conteo para reincluirlos.</div>
   <div id="wa_blk_list" class="hint" style="margin-top:10px">—</div>
   <div style="display:flex;gap:8px;margin-top:12px">
     <button class="sec" onclick="loadBlocked()">Ver / refrescar</button>
     <button class="ghost" onclick="clearBlocked()">Reincluir a todos</button>
   </div>
  </div>
  <div class="card" data-tab="fuentes" data-sub="wa"><h2>Listas de distribución · WhatsApp</h2>
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
  <div class="card" data-tab="envios"><h2>✍️ Componer y enviar</h2>
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
   <div class="hint" id="bc_wa_warn" style="display:none">⚠️ El envío masivo por WhatsApp puede banear tu número. El sistema lo hace con ritmo lento (anti-baneo); úsalo con listas pequeñas.</div>

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

   <label style="margin-top:16px">Programar (opcional)</label>
   <input type="datetime-local" id="bc_sched" style="max-width:260px">
   <div class="hint">Vacío = enviar ya (fraccionado). Con fecha/hora = se difiere hasta entonces y luego se gotea por lotes.</div>

   <div class="compose-actions">
     <button id="bc_send" onclick="sendBroadcast()">Enviar</button>
     <button class="ghost" onclick="bcClear()">Limpiar</button>
     <span class="grow"></span>
     <span id="bc_status" class="hint" style="margin-top:0"></span>
   </div>
  </div>
  <div class="card" data-tab="envios"><h2>📡 Envíos <span class="live" id="bc_live" style="margin-left:auto"><span class="ping"></span><span id="bc_live_t">en vivo</span></span></h2>
   <div class="hint">Estado y progreso de cada difusión. Se actualiza automáticamente mientras hay envíos en curso.</div>
   <div style="overflow-x:auto;margin-top:12px">
     <table id="bc_table"><thead><tr><th class="selcol"><input type="checkbox" id="bc_selall" onchange="bcSelAll(this.checked)"></th><th>Mensaje</th><th>Estado</th><th>Progreso</th><th></th></tr></thead>
       <tbody id="bc_rows"></tbody></table>
   </div>
   <div class="bc-empty" id="bc_empty" style="display:none">Aún no hay envíos. Crea uno en <b>Componer y enviar</b>.</div>
   <div class="tbl-toolbar">
     <button class="danger" id="bc_delsel" onclick="bcDeleteSelected()" disabled>🗑 Borrar seleccionados</button>
     <button class="danger" onclick="bcClearFinished()">🗑 Limpiar terminados</button>
     <span class="grow"></span>
     <button class="sec" onclick="loadBroadcasts()">Refrescar</button>
   </div>
  </div>
  <div class="card accent" data-tab="envios"><h2>⏰ Programar un mensaje</h2>
   <div class="hint">Crea mensajes que se envían solos a la hora indicada, por las conexiones existentes de Telegram y WhatsApp. Una vez, a diario o semanal. Respetan el ritmo anti-baneo, la ventana horaria y el interruptor maestro.</div>
   <label>Nombre (opcional)</label>
   <input id="sg_name" placeholder="p. ej. Lista de la mañana" maxlength="80">
   <label>Mensaje <span id="sg_count" class="charcount">0 caracteres</span></label>
   <textarea id="sg_text" style="min-height:110px" placeholder="Escribe el mensaje a programar..." oninput="sgCount()"></textarea>
   <label>Imagen (opcional)</label>
   <input id="sg_image_url" placeholder="…pega una URL https:// de imagen">
   <label style="margin-top:16px">Canales</label>
   <div class="chan-row">
     <label class="chan tg on" id="sg_chan_tg"><span class="dot"></span><input type="checkbox" id="sg_telegram" checked onchange="sgChan()" style="display:none">✈️ Telegram</label>
     <label class="chan wa" id="sg_chan_wa"><span class="dot"></span><input type="checkbox" id="sg_whatsapp" onchange="sgChan()" style="display:none">🟢 WhatsApp</label>
   </div>
   <div class="row">
     <div id="sg_tg_wrap"><div class="hint" style="margin-top:0">Lista de Telegram</div>
       <select id="sg_tg_list"><option value="">(según configuración)</option></select></div>
     <div id="sg_wa_wrap" style="display:none"><div class="hint" style="margin-top:0">Lista de WhatsApp (obligatoria)</div>
       <select id="sg_wa_list"><option value="">— elige una lista —</option></select></div>
   </div>
   <div class="hint">⚠️ WhatsApp exige una lista (evita mandar a toda la agenda por error).</div>
   <label style="margin-top:16px">Frecuencia</label>
   <div class="chan-row">
     <label class="chan on" id="sg_freq_once"><input type="radio" name="sg_type" value="once" checked onchange="sgType()" style="display:none">Una vez</label>
     <label class="chan" id="sg_freq_daily"><input type="radio" name="sg_type" value="daily" onchange="sgType()" style="display:none">Diario</label>
     <label class="chan" id="sg_freq_weekly"><input type="radio" name="sg_type" value="weekly" onchange="sgType()" style="display:none">Semanal</label>
   </div>
   <div id="sg_once_box" style="margin-top:12px">
     <label>Fecha y hora</label>
     <input type="datetime-local" id="sg_run_at" style="max-width:260px">
   </div>
   <div id="sg_time_box" style="margin-top:12px;display:none">
     <label>Hora</label>
     <input type="time" id="sg_at" value="09:00" style="max-width:160px">
   </div>
   <div id="sg_days_box" style="margin-top:12px;display:none">
     <label>Días</label>
     <div class="chan-row" id="sg_days"></div>
   </div>
   <div class="hint" style="margin-top:8px">La hora usa la zona horaria configurada en la pestaña <b>Programación</b> (ventana de envío).</div>
   <div class="compose-actions">
     <button id="sg_create" onclick="sgCreate()">Programar</button>
     <button class="ghost" onclick="sgClear()">Limpiar</button>
     <span class="grow"></span>
     <span id="sg_status" class="hint" style="margin-top:0"></span>
   </div>
  </div>
  <div class="card" data-tab="envios"><h2>📅 Mensajes programados <span id="sg_n" class="hint"></span></h2>
   <div class="hint">Próximos envíos automáticos. Puedes pausarlos/activarlos o eliminarlos.</div>
   <div style="overflow-x:auto;margin-top:12px">
     <table id="sg_table"><thead><tr><th>Mensaje</th><th>Canales</th><th>Cuándo</th><th>Próximo</th><th></th></tr></thead>
       <tbody id="sg_rows"></tbody></table>
   </div>
   <div class="empty-state" id="sg_empty" style="display:none"><div class="ico">⏰</div><h3>Sin mensajes programados</h3><p>Crea uno arriba para enviarlo automáticamente.</p></div>
   <div style="margin-top:14px"><button class="sec" onclick="loadSchedules()">Refrescar</button></div>
  </div>
  <div class="card accent" data-tab="ajustes"><h2>👥 Usuarios del panel <span id="usr_n" class="hint"></span></h2>
   <div class="hint">Cada usuario entra con sus propias credenciales (independientes). El correo se usa para recuperar la contraseña.</div>
   <div style="overflow-x:auto;margin-top:12px"><table id="usr_table"><thead><tr><th>Usuario</th><th>Correo</th><th></th></tr></thead><tbody id="usr_rows"></tbody></table></div>
   <div class="section-label" style="margin-top:14px">Crear usuario</div>
   <div class="row">
     <div><label>Usuario o correo</label><input id="usr_new_name" placeholder="nuevo@correo.com"></div>
     <div><label>Correo (para recuperación)</label><input id="usr_new_email" placeholder="correo@dominio.com"></div>
   </div>
   <label>Contraseña (mínimo 8)</label><input id="usr_new_pw" type="password">
   <button style="margin-top:10px" onclick="createUser()">Crear usuario</button>
  </div>
  <div class="card" data-tab="ajustes"><h2>✉️ Correo de recuperación <span id="mail_status" class="hint"></span></h2>
   <div class="hint">Servicio gratis para entregar el código cuando alguien usa «¿Olvidaste tu contraseña?». Crea una cuenta en <b>resend.com</b> (100 correos/día gratis), genera una API key y pégala aquí. Sin esto, el código se intenta enviar por el correo de alertas de AWS (SNS).</div>
   <label>Remitente (From)</label>
   <input id="mail_from" placeholder="Replica &lt;onboarding@resend.dev&gt;">
   <div class="hint">Para enviar a cualquier destinatario, verifica tu dominio en Resend. <code>onboarding@resend.dev</code> solo entrega al correo con el que te registraste.</div>
   <label>API key de Resend</label>
   <input id="resend_api_key" type="password" placeholder="(pegar solo si quieres cambiarla)">
   <div style="margin-top:10px"><button onclick="saveEmail()">Guardar correo</button> <span id="mail_save_status" class="hint" style="margin-left:10px"></span></div>
  </div>
  <div class="card" data-tab="ajustes"><h2>🔑 Cambiar mi contraseña</h2>
   <label>Contraseña actual</label><input id="cp_cur" type="password">
   <label>Nueva contraseña (mínimo 8)</label><input id="cp_new" type="password">
   <div style="margin-top:10px"><button onclick="changePassword()">Cambiar contraseña</button> <span id="cp_status" class="hint" style="margin-left:10px"></span></div>
  </div>
  <div class="card accent" data-tab="ajustes"><h2>Interruptor de envíos</h2>
   <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
     <label style="display:flex;align-items:center;gap:10px;margin:0;font-size:15px;color:var(--tx)"><input type="checkbox" id="sending_enabled" style="width:auto;transform:scale(1.3)" onchange="toggleSending()"> <b>Envíos activos</b></label>
     <span id="sys_badge" class="pill">—</span>
   </div>
   <div class="hint" style="margin-top:10px">Controla <b>solo el ENVÍO</b>. La <b>captura de @iproparts siempre está activa</b> (la información nunca se pierde): mientras esto esté apagado, lo capturado queda <b>EN ESPERA</b> y se envía en cuanto lo reactivas. Apágalo como freno: pausa al instante Telegram y WhatsApp.</div>
   <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--bd)">
     <div class="hint" style="margin-top:0">¿Hay difusiones en cola que NO quieres enviar? Cancélalas (no se enviarán, ni al reactivar).</div>
     <button class="danger" style="margin-top:8px" onclick="cancelPending()">🗑 Cancelar difusiones pendientes</button>
   </div>
  </div>
  <div class="card accent" data-tab="ajustes"><h2>Anti-baneo · lotes y ritmo</h2>
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
  <div class="card" data-tab="ajustes"><h2>Ventana de envío (horario permitido)</h2>
   <label style="display:flex;align-items:center;gap:8px;margin-top:0"><input type="checkbox" id="window_enabled" style="width:auto"> Enviar solo dentro del horario permitido</label>
   <div class="row">
     <div><label>Desde (HH:MM)</label><input id="window_start" placeholder="08:00"></div>
     <div><label>Hasta (HH:MM)</label><input id="window_end" placeholder="20:00"></div>
   </div>
   <div class="hint">Fuera del horario, los lotes quedan <b>encolados</b> y se procesan de forma diferida al reabrir la ventana. Soporta cruzar medianoche (p.ej. 22:00 → 06:00).</div>
   <button onclick="saveSched()">Guardar ventana</button>
  </div>
  <div class="card" data-tab="envios"><h2>📦 Envíos fraccionados <span class="live" id="pl_live" style="margin-left:auto"><span class="ping"></span><span id="pl_live_t">en vivo</span></span></h2>
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
  if(r.status===401){ logout(); throw new Error('401'); }
  if(!r.ok){ let _m=''; try{ _m=(await r.json()).error||''; }catch(e){} throw new Error(_m||('error '+r.status)); }
  return r.json(); }
function toast(m,v){ const t=$('toast'); t.textContent=m;
  const cls = v===true ? 'err' : (typeof v==='string' && v ? v : '');  // true=err (compat); 'info'/'warn'/'err'
  t.className='toast show'+(cls?' '+cls:''); setTimeout(()=>t.className='toast',2200); }
// Modales (promesas): reemplazan confirm()/prompt() nativos por diálogos de marca, accesibles (Esc/Enter, foco).
function dsModal(o){ o=o||{}; return new Promise(resolve=>{
  const ov=document.createElement('div'); ov.className='ds-overlay';
  const d=document.createElement('div'); d.className='ds-modal'; d.setAttribute('role','dialog'); d.setAttribute('aria-modal','true');
  d.innerHTML='<h3>'+bcEsc(o.title||'Confirmar')+'</h3>'+
    (o.message?'<div class="ds-modal-body">'+bcEsc(o.message)+'</div>':'')+
    (o.input?'<input id="ds_modal_input" placeholder="'+bcEsc(o.placeholder||'')+'">':'')+
    '<div class="ds-modal-actions">'+(o.noCancel?'':'<button class="ghost" data-a="c">'+bcEsc(o.cancelText||'Cancelar')+'</button>')+
    '<button class="'+(o.danger?'danger':'')+'" data-a="k">'+bcEsc(o.okText||'Aceptar')+'</button></div>';
  ov.appendChild(d); document.body.appendChild(ov);
  const inp=d.querySelector('#ds_modal_input'); if(inp) setTimeout(()=>{inp.focus();},40);
  const no=o.input?null:(o.noCancel?true:false);
  function close(v){ document.removeEventListener('keydown',onKey); ov.remove(); resolve(v); }
  function onKey(e){ if(e.key==='Escape') close(no); else if(e.key==='Enter') close(o.input?(inp?inp.value:''):true); }
  ov.addEventListener('mousedown',e=>{ if(e.target===ov) close(no); });
  const _cb=d.querySelector('[data-a=c]'); if(_cb) _cb.onclick=()=>close(no);
  d.querySelector('[data-a=k]').onclick=()=>close(o.input?(inp?inp.value:''):true);
  document.addEventListener('keydown',onKey);
}); }
function confirmModal(message,opts){ opts=opts||{}; return dsModal({title:opts.title||'¿Confirmas?',message:message,okText:opts.okText||'Aceptar',cancelText:opts.cancelText,danger:!!opts.danger}); }
function promptModal(message,opts){ opts=opts||{}; return dsModal({title:opts.title||'Escribe un valor',message:message,input:true,placeholder:opts.placeholder,okText:opts.okText||'Aceptar'}); }
function alertModal(message,opts){ opts=opts||{}; return dsModal({title:opts.title||'Aviso',message:message,okText:opts.okText||'Entendido',noCancel:true,danger:opts.danger}); }
// Skeleton de carga reutilizable: filas placeholder con shimmer mientras llegan los datos (solo 1ª carga).
function skelTable(id,cols,rows){ const t=$(id); if(!t) return; const r=rows||4, c=cols||3;
  t.innerHTML=Array.from({length:r},()=>'<tr class="skeleton">'+Array.from({length:c},()=>'<td><div class="sk-line"></div></td>').join('')+'</tr>').join(''); }
async function doLogin(){ const u=$('lu').value, p=$('lp').value; CRED=btoa(u+':'+p);
  try{ await fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{if(!r.ok)throw 0;}); sessionStorage.setItem('cred',CRED); sessionStorage.setItem('cred_ts',String(Date.now()));
    $('login').style.display='none'; $('app').style.display='block'; $('who').textContent=u; boot(); }
  catch(e){ $('lerr').textContent='Usuario o contraseña incorrectos (tras varios intentos se bloquea unos minutos)'; } }
function logout(){ sessionStorage.removeItem('cred'); CRED=''; $('app').style.display='none'; $('login').style.display='flex'; }
// --- recuperación de contraseña (público, sin sesión) ---
function fpToggle(){ const b=$('fp_box'); b.style.display = b.style.display==='none'?'block':'none'; }
async function fpSend(){
  const u=$('fp_user').value.trim(); if(!u){ $('fp_status').textContent='Indica tu usuario o correo.'; return; }
  $('fp_status').textContent='Enviando…';
  try{ await fetch(BASE+'/api/auth/forgot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u})}); }catch(e){}
  $('fp_step2').style.display='block';
  $('fp_status').textContent='Si el usuario existe, enviamos un código al correo registrado. Revisa tu bandeja.';
}
async function fpReset(){
  const u=$('fp_user').value.trim(), code=$('fp_code').value.trim(), nw=$('fp_new').value;
  if(!code||!nw){ $('fp_status').textContent='Completa el código y la nueva contraseña.'; return; }
  try{
    const r=await fetch(BASE+'/api/auth/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,code:code,new:nw})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status));
    $('fp_status').textContent='✅ Contraseña actualizada. Ya puedes iniciar sesión.'; $('fp_step2').style.display='none';
  }catch(e){ $('fp_status').textContent=e.message||'No se pudo restablecer.'; }
}
// --- usuarios del panel ---
let USR_ME='';
async function loadUsers(){
  { const _u=$('usr_rows'); if(_u && !_u.children.length) skelTable('usr_rows',3,3); }
  try{ const r=await api('/api/users'); USR_ME=r.me||''; const list=r.users||[];
    $('usr_n').textContent='· '+list.length;
    $('usr_rows').innerHTML=list.map(u=>{ const me=u.username===USR_ME;
      return `<tr><td><b>${bcEsc(u.username)}</b>${me?' <span class="hint">(tú)</span>':''}</td><td>${bcEsc(u.email||'—')}</td>`+
        `<td style="text-align:right">${me?'':`<button class="danger" style="padding:4px 9px" onclick="deleteUser('${bcEsc(u.username)}')">🗑</button>`}</td></tr>`; }).join('');
  }catch(e){}
}
async function createUser(){
  const username=$('usr_new_name').value.trim(), email=$('usr_new_email').value.trim(), pw=$('usr_new_pw').value;
  if(!username||!pw){ toast('Usuario y contraseña requeridos',true); return; }
  try{ const r=await fetch(BASE+'/api/users',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({username:username,email:email,password:pw})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status));
    toast('✓ Usuario creado'); $('usr_new_name').value=''; $('usr_new_email').value=''; $('usr_new_pw').value=''; loadUsers();
  }catch(e){ toast(e.message||'Error al crear',true); }
}
async function deleteUser(u){ if(!await confirmModal('¿Borrar el usuario "'+u+'"?',{danger:true,okText:'Borrar'})) return;
  try{ const r=await fetch(BASE+'/api/users/delete',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({username:u})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status)); toast('✓ Usuario borrado'); loadUsers();
  }catch(e){ toast(e.message||'Error',true); } }
async function changePassword(){
  const cur=$('cp_cur').value, nw=$('cp_new').value; if(!cur||!nw){ toast('Completa ambos campos',true); return; }
  $('cp_status').textContent='Cambiando…';
  try{ const r=await fetch(BASE+'/api/auth/change-password',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({current:cur,new:nw})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status));
    $('cp_status').textContent='✅ Contraseña cambiada'; $('cp_cur').value=''; $('cp_new').value=''; toast('✓ Contraseña cambiada');
  }catch(e){ $('cp_status').textContent=e.message||''; toast(e.message||'Error',true); }
}
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
  if($('mail_from')) $('mail_from').value=c.mail_from||'';
  if($('mail_status')) $('mail_status').textContent = c.resend_api_key_set ? '· API key configurada ✓' : '· sin API key (usa SNS)';
  renderSendingState(c.sending_enabled!==false); }
async function saveEmail(){ const b={ mail_from:($('mail_from').value||'').trim() };
  const k=$('resend_api_key').value; if(k) b.resend_api_key=k.trim();
  $('mail_save_status').textContent='guardando...';
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    $('resend_api_key').value=''; $('mail_save_status').textContent='✅ guardado'; toast('✓ Correo guardado'); loadCfg(); }
  catch(e){ $('mail_save_status').textContent=''; toast('Error al guardar',true); } }
function renderSendingState(on){
  if($('sending_enabled')) $('sending_enabled').checked = on;
  const badge=$('sys_badge'); if(badge){ badge.className='pill '+(on?'active':'failed'); badge.textContent = on?'ACTIVOS':'PAUSADOS'; }
  const hb=$('hdr_badge'); if(hb){
    if(on){ hb.style.display='none'; hb.onclick=null; }
    else { hb.style.display='inline-block'; hb.className='pill failed'; hb.style.cursor='pointer';
      hb.title='Los envíos están en pausa — clic para activarlos'; hb.textContent='⏸ Pausado · activar'; hb.onclick=enableSending; }
  }
  const sb=$('send_banner'); if(sb){ sb.hidden=false;
    if(on){ sb.className='active'; sb.innerHTML='<span class="sb-dot"></span><span class="sb-txt"><b>Envíos activos.</b> Tus mensajes se entregan con normalidad.</span><button class="sb-pause" onclick="setSending(false)">Pausar envíos</button>'; }
    else { sb.className='paused'; sb.innerHTML='<span class="sb-dot"></span><span class="sb-txt"><b>⏸ Los envíos están EN PAUSA.</b> Ningún mensaje (Telegram ni WhatsApp) saldrá hasta que los actives. Lo que se captura queda en espera.</span><button class="sb-go" onclick="enableSending()">▶ Activar envíos</button>'; }
  }
}
async function pendingSummary(){
  try{ const r=await api('/api/plans'); let planes=0,envios=0;
    (r.plans||[]).forEach(p=>{ if(p.status==='pending'||p.status==='running'){ planes++; envios+=((p.tg&&p.tg.total)|0)+((p.wa&&p.wa.total)|0); } });
    return {planes,envios};
  }catch(e){ return {planes:0,envios:0}; }
}
async function setSending(on){
  if(!on){ if(!(await confirmModal('¿Pausar TODOS los envíos (Telegram y WhatsApp)? Nada saldrá hasta que los reactives.',{okText:'Pausar envíos',danger:true}))){ renderSendingState(true); return; } }
  else {
    const ps=await pendingSummary();
    let msg='¿Activar los envíos? A partir de ahora los mensajes se entregarán a tus contactos, de forma gradual (anti-baneo).';
    if(ps.planes>0){
      const ne = ps.envios>0 ? (' (~'+ps.envios.toLocaleString('es')+' envíos en total)') : '';
      msg='⚠️ Hay '+ps.planes+' difusión(es) en cola'+ne+'. Al activar empezarán a salir de forma gradual a tus contactos. Si no quieres enviarlas, pausa y cancélalas primero (Ajustes → Interruptor de envíos). ¿Activar los envíos?';
    }
    if(!(await confirmModal(msg,{okText:'Activar envíos',danger:ps.planes>0}))){ renderSendingState(false); return; }
  }
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sending_enabled:on})});
    renderSendingState(on); toast(on?'✓ Envíos ACTIVADOS — los mensajes ya se entregan':'⏸ Envíos PAUSADOS', on?'info':'warn');
    if($('k_sent')) loadDashboard(); }
  catch(e){ toast('Error al cambiar el estado',true); renderSendingState(!on); }
}
function enableSending(){ return setSending(true); }
function toggleSending(){ return setSending($('sending_enabled').checked); }
async function cancelPending(){
  if(!await confirmModal('¿Cancelar todas las difusiones pendientes/en curso? No se enviarán (ni al reactivar).',{danger:true,okText:'Cancelar difusiones'})) return;
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
async function tlSendCode(){
  const phone=$('tl_phone').value.trim(); if(!phone){ toast('Ingresa el número de teléfono',true); return; }
  $('tl_send').disabled=true; toast('Enviando código…','info');
  try{
    const r=await fetch(BASE+'/api/telethon/send-code',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({phone})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status));
    $('tl_step2').style.display='block'; $('tl_status').textContent='Código enviado a tu app de Telegram.'; toast('✓ Código enviado');
  }catch(e){ toast(e.message||'No se pudo enviar el código',true); }
  finally{ $('tl_send').disabled=false; }
}
async function tlSignIn(){
  const code=$('tl_code').value.trim(); const pwd=$('tl_password').value;
  const pwdVisible=$('tl_pwd_wrap').style.display!=='none';
  if(!code && !(pwdVisible&&pwd)){ toast('Ingresa el código recibido',true); return; }
  const body={}; if(code) body.code=code; if(pwdVisible&&pwd) body.password=pwd;
  $('tl_confirm').disabled=true; $('tl_status').textContent='Conectando…';
  try{
    const r=await fetch(BASE+'/api/telethon/sign-in',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status));
    if(j.needs_password){ $('tl_pwd_wrap').style.display='block'; $('tl_status').textContent='Esta cuenta tiene verificación en dos pasos: ingresa tu contraseña y confirma de nuevo.'; toast('Ingresa tu contraseña 2FA','warn'); return; }
    if(j.connected){ $('tl_status').textContent='✅ Conectado'+(j.me&&j.me.username?(' como @'+j.me.username):''); toast('✓ Cuenta conectada'); $('tl_code').value=''; $('tl_password').value=''; $('tl_step2').style.display='none'; loadCfg(); loadSubs(); }
  }catch(e){ toast(e.message||'No se pudo iniciar sesión',true); $('tl_status').textContent=e.message||''; }
  finally{ $('tl_confirm').disabled=false; }
}
async function tlLogout(){
  if(!await confirmModal('¿Limpiar la sesión de Telegram (userbot)? La cuenta dejará de poder enviar hasta que la reconectes aquí.',{danger:true,okText:'Limpiar sesión'})) return;
  try{ await api('/api/telethon/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    $('tl_logout_out').textContent='✓ Sesión limpiada'; toast('✓ Sesión de Telegram limpiada'); loadCfg(); }
  catch(e){ toast('Error al limpiar',true); }
}
async function waReset(){
  if(!await confirmModal('¿Limpiar la sesión de WhatsApp? Se borran las credenciales y tendrás que volver a vincular (QR o código).',{danger:true,okText:'Limpiar sesión'})) return;
  $('wa_reset_out').textContent='Limpiando…';
  try{ await api('/api/whatsapp/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    $('wa_reset_out').textContent='✓ Sesión limpiada. Vincula de nuevo (QR o código).'; toast('✓ Sesión de WhatsApp limpiada'); setTimeout(()=>{ try{waStatus(false)}catch(e){} },1500); }
  catch(e){ $('wa_reset_out').textContent=''; toast('Error al limpiar',true); }
}
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
    const vis=document.querySelector('main>.card[data-tab="ajustes"]')?.classList.contains('show');
    if(vis) loadQueue(); }, BC_POLL);
}
// --- Probar procesamiento del mensaje (preview ya procesado) ---
async function probarProcesado(){
  const t=$('pp_in').value; $('pp_out').textContent='procesando...';
  try{ const r=await api('/api/preview/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
    $('pp_out').textContent=r.processed||'(vacío)'; }
  catch(e){ $('pp_out').textContent='error procesando'; }
}
// --- DLQ (cola de fallidos) ---
async function loadDlq(){
  try{ const r=await api('/api/dlq'); const n=r.depth||0; $('dlq_n').textContent='· '+n+' fallido(s)';
    const L=$('dlq_list'); const s=r.sample||[];
    L.innerHTML = n? (s.map(m=>`<div class="bc-meta" style="padding:6px 0;border-bottom:1px solid var(--bd)">lote ${m.batch_index??'?'} · ${m.chat_ids||0} dest · ${bcEsc((m.text||'').slice(0,50))}</div>`).join('') + (n>s.length?`<div class="hint">…y ${n-s.length} más</div>`:'')) : '<div class="hint">Sin mensajes fallidos. 🎉</div>';
  }catch(e){ $('dlq_n').textContent='· error'; }
}
async function dlqRedrive(){
  if(!await confirmModal('¿Reintentar todos los fallidos? Volverán a la cola para procesarse.',{okText:'Reintentar'})) return;
  try{ await api('/api/dlq/redrive',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); toast('✓ Reintento iniciado'); setTimeout(loadDlq,1500); }
  catch(e){ toast('Error al reintentar',true); }
}
async function dlqPurge(){
  if(!await confirmModal('¿Descartar TODOS los mensajes fallidos? No se podrán recuperar.',{danger:true,okText:'Descartar'})) return;
  try{ await api('/api/dlq/purge',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); toast('✓ DLQ descartada'); setTimeout(loadDlq,1500); }
  catch(e){ toast('Error al descartar',true); }
}
// --- Opt-out WhatsApp: contactos auto-excluidos por fallos ---
async function loadBlocked(){
  try{ const r=await api('/api/whatsapp/blocked'); $('wa_blk_n').textContent='· '+(r.total||0)+' (umbral '+(r.umbral||3)+')';
    const b=r.blocked||[]; if($('wa_blk_list')) $('wa_blk_list').innerHTML = b.length? b.map(x=>bcEsc(x.name)+' — '+x.fallos+' fallos').join('<br>') : 'Ninguno por ahora.';
  }catch(e){ if($('wa_blk_n')) $('wa_blk_n').textContent='· servicio inaccesible'; if($('wa_blk_list')) $('wa_blk_list').textContent='—'; }
}
async function clearBlocked(){
  if(!await confirmModal('¿Reincluir a TODOS los auto-excluidos? Volverán a recibir envíos.',{okText:'Reincluir'})) return;
  try{ await api('/api/whatsapp/blocked/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); toast('✓ Reincluidos'); loadBlocked(); }
  catch(e){ toast('Error',true); }
}
// --- Auditoría (acciones del panel) ---
async function loadAudit(){
  try{ const r=await api('/api/audit'); const a=r.audit||[];
    if($('audit_n')) $('audit_n').textContent='· '+a.length;
    const t=$('audit_rows'); if(!t) return;
    t.innerHTML = a.length ? a.map(x=>`<tr><td>${bcFmtTime(x.ts)}</td><td>${bcEsc(x.user)}</td><td><b>${bcEsc(x.action)}</b></td><td style="color:var(--mut)">${bcEsc(x.detail)}</td></tr>`).join('') : '<tr><td colspan="4" class="hint">Sin registros aún.</td></tr>';
  }catch(e){}
}
// --- Estado de conexiones (Telegram bot + WhatsApp) en el header ---
async function refreshConn(){
  const tg=$('conn_tg'), wa=$('conn_wa');
  try{ const r=await api('/api/telegram/me'); const ok=r&&(r.ok&&r.result);
    if(tg){ tg.className='pill '+(ok?'active':'failed'); tg.textContent=ok?('✈️ @'+(r.result.username||'bot')):'✈️ bot ✕'; } }
  catch(e){ if(tg){ tg.className='pill failed'; tg.textContent='✈️ ✕'; } }
  try{ const s=await api('/api/whatsapp/status'); const ok=s&&s.connected;
    if(wa){ wa.className='pill '+(ok?'active':'failed'); wa.textContent=ok?'🟢 WhatsApp':'🟢 WA ✕'; wa.title=ok?('conectado'+(s.contacts?(' · '+s.contacts+' contactos'):'')):('desconectado'+(s.lastCloseMsg?(' · '+s.lastCloseMsg):'')); } }
  catch(e){ if(wa){ wa.className='pill inactive'; wa.textContent='🟢 WA ?'; wa.title='servicio no configurado o inaccesible'; } }
}
let CONN_TIMER=null;
function connStartPolling(){ if(CONN_TIMER) return; refreshConn();
  CONN_TIMER=setInterval(()=>{ if(CRED && !document.hidden) refreshConn(); }, 60000); }
// --- Expiración de sesión (cliente): re-login tras 8h o inactividad larga ---
const SESSION_MAX_MS=8*3600*1000;
function sessionFresca(){ try{ const t=parseInt(sessionStorage.getItem('cred_ts')||'0',10); return t && (Date.now()-t)<SESSION_MAX_MS; }catch(e){ return true; } }
// --- Onboarding: checklist de primeros pasos (desde la config) ---
function renderSteps(c){
  const steps=[
    {ok: !!(c.bot_token_set||c.telethon_session_set), t:'Conectar cuenta o bot de Telegram', tab:'ajustes'},
    {ok: !!(c.source_channel&&String(c.source_channel).trim()), t:'Definir el canal fuente', tab:'fuentes'},
    {ok: ((c.telegram_lists||[]).length>0 || (c.whatsapp_lists||[]).length>0), t:'Crear listas o elegir destinatarios', tab:'fuentes'},
    {ok: !!c.whatsapp_enabled, t:'Conectar WhatsApp', tab:'ajustes', opt:true},
    {ok: c.sending_enabled!==false, t:'Activar los envíos', tab:'ajustes'},
  ];
  const done=steps.filter(s=>s.ok).length;
  if($('steps_n')) $('steps_n').textContent='· '+done+'/'+steps.length+(done===steps.length?' ✓':'');
  if($('dash_steps')) $('dash_steps').innerHTML=steps.map(s=>
    `<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--bd)">`+
    `<span style="font-size:15px">${s.ok?'✅':(s.opt?'⚪':'⬜')}</span>`+
    `<span style="flex:1;color:${s.ok?'var(--mut)':'var(--tx)'}">${s.t}${s.opt?' <span class="hint">(opcional)</span>':''}</span>`+
    `<button class="ghost" style="padding:5px 11px" onclick="showTab('${s.tab}')">Ir</button></div>`).join('');
}
// --- Dashboard / Inicio (KPIs + estado de un vistazo) ---
async function loadDashboard(){
  ['k_sent','k_rate','k_pend','k_dlq'].forEach(id=>{const e=$(id); if(e) e.classList.add('kpi-load');});
  try{
    const [m,c,pl,q]=await Promise.all([api('/api/metrics'),api('/api/config'),api('/api/plans'),api('/api/queue')]);
    if($('k_sent')) $('k_sent').textContent=m.enviados;
    if($('k_rate')) $('k_rate').textContent=(m.tasa_exito!=null?m.tasa_exito:100)+'%';
    let pend=0; (pl.plans||[]).forEach(p=>{ if(p.status==='pending'||p.status==='running') pend+=Math.max(0,(p.tg.batches|0)-(p.tg.next|0))+Math.max(0,((p.wa&&p.wa.batches)|0)-((p.wa&&p.wa.next)|0)); });
    if($('k_pend')) $('k_pend').textContent=pend;
    if($('k_dlq')) $('k_dlq').textContent=q.dlq;
    ['k_sent','k_rate','k_pend','k_dlq'].forEach(id=>{const e=$(id); if(e) e.classList.remove('kpi-load');});
    renderSteps(c);
    const on=c.sending_enabled!==false; const de=$('dash_estado');
    renderSendingState(on);
    if(de){ de.className='callout '+(on?'ok':'warn');
      de.innerHTML='Envíos: <b>'+(on?'ACTIVOS':'PAUSADOS')+'</b> · '+(c.window_enabled?('ventana '+c.window_start+'–'+c.window_end):'24 h')+' · WhatsApp '+(c.whatsapp_enabled?'activo':'desactivado')+' · lote '+(c.batch_size|0); }
    const s=(m.serie||[]).slice(-14); const max=Math.max(1,...s.map(d=>(d.sent|0)+(d.failed|0)));
    if($('dash_serie')) $('dash_serie').innerHTML='<div class="hint" style="margin-top:0">Actividad (últimos '+s.length+' días con envíos)</div>'+
      '<div style="display:flex;gap:3px;align-items:flex-end;height:56px;margin-top:6px">'+
      (s.length? s.map(d=>`<div title="${d.dia}: ${d.sent} enviados, ${d.failed} fallidos" style="flex:1;background:linear-gradient(180deg,var(--ac),var(--ac2));height:${Math.round(((d.sent|0)+(d.failed|0))/max*100)}%;min-height:2px;border-radius:3px 3px 0 0"></div>`).join('') : '<div class="hint">sin actividad aún</div>')+'</div>';
    try{ const last=((await api('/api/broadcasts')).broadcasts||[])[0];
      if($('dash_last')) $('dash_last').innerHTML = last? ('Último envío: <b>'+bcEsc((last.text||'(imagen)').slice(0,48))+'</b> — '+(BC_STATUS[last.status]||last.status)+' · '+bcFmtTime(last.created_at)) : 'Aún no hay envíos.'; }catch(e){}
  }catch(e){ if($('dash_estado')) $('dash_estado').textContent='no se pudo cargar el resumen';
    ['k_sent','k_rate','k_pend','k_dlq'].forEach(id=>{const e2=$(id); if(e2) e2.classList.remove('kpi-load');}); }
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
      `<td><b>${bcEsc(label)}</b><div class="hint" style="margin-top:2px;font-size:11px">${bcEsc(s.phone||s.chatId||'')}</div></td>`+
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
    row.style.cssText='display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid #3A3A39';
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
async function delList(ch,i){ const n=LISTS[ch][i].name; if(!await confirmModal('¿Borrar la lista "'+n+'"?',{danger:true,okText:'Borrar'}))return;
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
async function createListFromGrid(ch){
  const ids=selForChannel(ch);
  if(!ids.length){ toast('Marca primero los contactos en el grid de arriba',true); return; }
  const n=(await promptModal('Nombre de la nueva lista (con los '+ids.length+' contactos marcados):',{title:'Crear lista',placeholder:'Nombre de la lista',okText:'Crear'})||'').trim();
  if(!n) return;
  if(LISTS[ch].some(l=>l.name===n)){ toast('Ya existe una lista con ese nombre',true); return; }
  LISTS[ch].push({name:n, ids:[...new Set(ids.map(String))]});
  renderLists(ch);
  await saveLists(ch);
  toast('✓ Lista "'+n+'" creada con '+ids.length+' contactos');
}
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
    tr.innerHTML=`<td><input type="checkbox" class="wsel" data-id="${id}"></td><td><b>${bcEsc(waName(c))}</b><div class="hint" style="margin-top:2px;font-size:11px">${bcEsc(id)}</div></td>`+
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
function showSub(s){
  document.querySelectorAll('main>.card[data-tab="fuentes"][data-sub]').forEach(c=>c.classList.toggle('subhide', c.dataset.sub!==s));
  document.querySelectorAll('#fuentes_subnav button').forEach(b=>b.classList.toggle('on', b.dataset.sub===s));
  try{ localStorage.setItem('fsub',s); }catch(e){}
}
function showTab(t){
  document.querySelectorAll('main>.card').forEach(c=>c.classList.toggle('show', c.dataset.tab===t));
  document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('on', b.dataset.tab===t));
  try{ localStorage.setItem('tab',t); }catch(e){}
  if(t==='envios'){ sgFillLists(); sgChan(); sgType(); loadSchedules(); }
  if(t==='fuentes'){ showSub((function(){try{return localStorage.getItem('fsub')}catch(e){return null}})()||'tg'); }
  window.scrollTo(0,0); }
function boot(){ showTab((()=>{try{const s=localStorage.getItem('tab');return ['inicio','fuentes','envios','ajustes'].includes(s)?s:'inicio'}catch(e){return 'inicio'}})()); loadCfg(); loadQueue(); loadSubs(); loadDlq(); loadDashboard(); connStartPolling(); }
if(CRED && !sessionFresca()){ logout(); }
else if(CRED){ fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{ if(r.ok){ $('login').style.display='none'; $('app').style.display='block'; boot(); } else { logout(); } }).catch(()=>{}); }

// ===== Componer y enviar (POST /api/broadcast) =====
let BC_IMG_URL = '';           // URL devuelta tras subir un archivo a /api/image
function bcCount(){ const n=$('bc_text').value.length, el=$('bc_count');
  el.textContent = n>4096 ? (n+' / 4096 · supera el límite de Telegram') : (n+(n===1?' carácter':' caracteres'));
  el.dataset.near = (n>3600 && n<=4096) ? '1':'0'; el.dataset.over = n>4096 ? '1':'0'; }
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
  { const w=$('bc_wa_warn'); if(w) w.style.display = wa ? 'block':'none'; }
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
  if($('bc_sched')) $('bc_sched').value='';
  bcRenderPick('tg'); bcRenderPick('wa');
  $('bc_status').textContent=''; bcCount(); bcPrev();
}
async function sendBroadcast(){
  const text=$('bc_text').value.trim();
  const tg=$('bc_telegram').checked, wa=$('bc_whatsapp').checked;
  if(!text && !bcEffectiveUrl()){ toast('Escribe un mensaje o adjunta una imagen',true); return; }
  if(!tg && !wa){ toast('Elige al menos un canal',true); return; }
  if(text.length>4096){ toast('El mensaje supera el límite de Telegram (4096 caracteres)',true); $('bc_text').focus(); return; }
  const body=bcBody({ text });
  const url=bcEffectiveUrl(); if(url) body.image_url=url;
  // Programación opcional: datetime-local -> epoch (s). Vacío = enviar ya.
  let ep=0; const sv=$('bc_sched')?$('bc_sched').value:'';
  if(sv){ ep=Math.floor(new Date(sv).getTime()/1000); if(ep>Math.floor(Date.now()/1000)) body.scheduled_at=ep; else { toast('La fecha programada debe ser futura',true); return; } }
  let msg = body.scheduled_at ? ('¿Programar este envío para '+new Date(sv).toLocaleString('es')+'?') : '¿Enviar este mensaje ahora?';
  if(wa) msg+='\n\n⚠️ El envío masivo por WhatsApp puede banear tu número.';
  if(!await confirmModal(msg,{okText:'Enviar'})) return;
  const btn=$('bc_send'); btn.disabled=true; btn.classList.add('btn-loading'); $('bc_status').textContent='guardando...';
  try{
    await api('/api/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    // "programado" != "entregado": el envío se fracciona y la ENTREGA real se confirma abajo en Envíos.
    toast(body.scheduled_at?'✓ Programado para más tarde':'✓ Programado — la entrega se confirma abajo en Envíos','info'); $('bc_status').textContent='';
    bcClear();
    showTab('envios');
    loadBroadcasts();
  }catch(e){ const _m=e.message||'Error al programar'; $('bc_status').textContent=_m; toast(_m,true); }
  finally{ btn.disabled=false; btn.classList.remove('btn-loading'); }
}
// Al abrir la pestaña Enviar: rellenar listas + previsualizar (hook aditivo sobre showTab).
(function(){ const _s=window.showTab;
  if(typeof _s==='function'){ window.showTab=function(t){ _s(t); if(t==='envios'){ try{ bcFillLists(); bcChan(); }catch(e){} } }; }
})();
// ===== Envíos: listado + polling (GET /api/broadcasts) =====
let BC_TIMER=null;
const BC_POLL=4000;
const BC_STATUS={ queued:'En cola', sending:'Enviando', done:'Completado', failed:'Fallido', partial:'Parcial' };
function bcEsc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// ===== Mensajes programados (módulo /api/schedules) =====
const SG_DAYNAMES=['L','M','X','J','V','S','D'];  // 0=lunes .. 6=domingo
let SG_DAYS=new Set();
function sgCount(){ const n=$('sg_text').value.length, el=$('sg_count');
  el.textContent = n>4096 ? (n+' / 4096 · supera el límite de Telegram') : (n+(n===1?' carácter':' caracteres'));
  el.dataset.near = (n>3600 && n<=4096) ? '1':'0'; el.dataset.over = n>4096 ? '1':'0'; }
function sgFillLists(){
  const fill=(sel,arr,first)=>{ if(!sel) return; const cur=sel.value;
    sel.innerHTML='<option value="">'+first+'</option>'+(arr||[]).map(l=>`<option value="${bcEsc(l.name)}">${bcEsc(l.name)} (${(l.ids||[]).length})</option>`).join('');
    sel.value=cur; };
  fill($('sg_tg_list'),(LISTS&&LISTS.telegram)||[],'(según configuración)');
  fill($('sg_wa_list'),(LISTS&&LISTS.whatsapp)||[],'— elige una lista —');
}
function sgRenderDays(){ const box=$('sg_days'); if(!box||box.children.length) return;
  box.innerHTML=SG_DAYNAMES.map((d,i)=>`<label class="chan${SG_DAYS.has(i)?' on':''}" data-d="${i}" onclick="sgToggleDay(${i})">${d}</label>`).join(''); }
function sgToggleDay(i){ SG_DAYS.has(i)?SG_DAYS.delete(i):SG_DAYS.add(i);
  const el=document.querySelector('#sg_days [data-d="'+i+'"]'); if(el) el.classList.toggle('on',SG_DAYS.has(i)); }
function sgChan(){ const tg=$('sg_telegram').checked, wa=$('sg_whatsapp').checked;
  $('sg_chan_tg').classList.toggle('on',tg); $('sg_chan_wa').classList.toggle('on',wa);
  $('sg_tg_wrap').style.display=tg?'block':'none'; $('sg_wa_wrap').style.display=wa?'block':'none'; }
function sgCurType(){ const r=document.querySelector('input[name="sg_type"]:checked'); return r?r.value:'once'; }
function sgType(){ const t=sgCurType();
  $('sg_freq_once').classList.toggle('on',t==='once'); $('sg_freq_daily').classList.toggle('on',t==='daily'); $('sg_freq_weekly').classList.toggle('on',t==='weekly');
  $('sg_once_box').style.display=t==='once'?'block':'none';
  $('sg_time_box').style.display=t==='once'?'none':'block';
  $('sg_days_box').style.display=t==='weekly'?'block':'none';
  sgRenderDays(); }
function sgClear(){ $('sg_name').value=''; $('sg_text').value=''; $('sg_image_url').value=''; $('sg_run_at').value='';
  SG_DAYS.clear(); document.querySelectorAll('#sg_days .chan').forEach(e=>e.classList.remove('on')); sgCount(); $('sg_status').textContent=''; }
async function sgCreate(){
  const t=sgCurType();
  const body={ name:$('sg_name').value.trim(), text:$('sg_text').value, image_url:$('sg_image_url').value.trim(),
    telegram:$('sg_telegram').checked, whatsapp:$('sg_whatsapp').checked,
    telegram_list:$('sg_tg_list').value, whatsapp_list:$('sg_wa_list').value, type:t };
  if(!body.text.trim()){ toast('El mensaje no puede estar vacío',true); return; }
  if(t==='once'){ const v=$('sg_run_at').value; if(!v){ toast('Elige fecha y hora',true); return; }
    body.run_at=Math.floor(new Date(v).getTime()/1000); }
  else { body.at=$('sg_at').value; if(t==='weekly') body.days=[...SG_DAYS]; }
  $('sg_create').disabled=true; $('sg_create').classList.add('btn-loading'); $('sg_status').textContent='Guardando…';
  try{
    const r=await fetch(BASE+'/api/schedules',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error||('error '+r.status));
    toast('✓ Mensaje programado'); $('sg_status').textContent=''; sgClear(); loadSchedules();
  }catch(e){ toast(e.message||'Error al programar',true); $('sg_status').textContent=e.message||''; }
  finally{ $('sg_create').disabled=false; $('sg_create').classList.remove('btn-loading'); }
}
function sgDesc(s){
  if(s.type==='daily') return 'Diario · '+s.at;
  if(s.type==='weekly') return 'Semanal · '+(s.days||[]).slice().sort((a,b)=>a-b).map(d=>SG_DAYNAMES[d]).join(',')+' · '+s.at;
  return 'Una vez';
}
function sgWhen(ep){ if(!ep) return '—'; try{ return new Date(ep*1000).toLocaleString('es',{dateStyle:'medium',timeStyle:'short'}); }catch(e){ return '—'; } }
function sgChans(s){ return [s.telegram?'✈️':'', s.whatsapp?'🟢':''].filter(Boolean).join(' ')||'—'; }
async function loadSchedules(){
  { const _s=$('sg_rows'); if(_s && !_s.children.length) skelTable('sg_rows',5,3); }
  let data;
  try{ data=(await api('/api/schedules')).schedules||[]; }catch(e){ return; }
  $('sg_n').textContent=data.length?('· '+data.length):'';
  $('sg_empty').style.display=data.length?'none':'block';
  $('sg_rows').innerHTML=data.map(s=>{
    const msg=bcEsc((s.name||s.text||'').slice(0,48))||'(sin texto)';
    const tag=s.enabled?'<span class="pill done">activo</span>':'<span class="pill inactive">pausado</span>';
    return `<tr>
      <td><b>${msg}</b><div class="hint" style="margin-top:2px">${tag}</div></td>
      <td>${sgChans(s)}</td>
      <td>${bcEsc(sgDesc(s))}</td>
      <td>${s.enabled?sgWhen(s.next_run):'—'}</td>
      <td style="white-space:nowrap;text-align:right">
        <button class="sec" style="padding:5px 10px" onclick="sgToggle('${s.sid}',${s.enabled?'false':'true'})">${s.enabled?'Pausar':'Activar'}</button>
        <button class="danger" style="padding:5px 10px;margin-left:6px" onclick="sgDelete('${s.sid}')">Borrar</button>
      </td></tr>`;
  }).join('');
}
async function sgToggle(sid,en){ try{ await api('/api/schedules/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid,enabled:en})}); loadSchedules(); }catch(e){ toast('Error',true); } }
async function sgDelete(sid){ if(!await confirmModal('¿Borrar este mensaje programado?',{danger:true,okText:'Borrar'})) return;
  try{ await api('/api/schedules/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid})}); toast('✓ Borrado'); loadSchedules(); }catch(e){ toast('Error',true); } }
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
    `<td class="selcol"><input type="checkbox" class="bcsel" data-id="${b.id}" onchange="bcSelChanged()"></td>`+
    `<td class="bc-msg"><b title="${bcEsc(txt)}">${bcEsc(txt)}</b>`+
      `<div class="bc-meta"><span class="bc-src">${bcEsc(b.source||'manual')}</span><span>${bcFmtTime(b.created_at)}</span></div></td>`+
    `<td><span class="pill ${st}">${bcEsc(label)}</span>${b.last_error?`<div class="bc-err" role="button" tabindex="0" onclick="bcErrDetail(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();bcErrDetail(this)}" data-err="${bcEsc((b.error_reasons||[]).join('\n')||b.last_error)}" title="Ver detalle del error">⚠ ${bcEsc(String(b.last_error).slice(0,72))} ›</div>`:''}</td>`+
    `<td><div class="chprog">${bcChanCell(false,b.telegram)}${bcChanCell(true,b.whatsapp)}</div></td>`+
    `<td style="text-align:right;white-space:nowrap"><button class="danger" style="padding:4px 9px" title="Borrar definitivamente" onclick="bcDelete('${b.id}')">🗑</button></td>`;
  return tr;
}
function bcErrDetail(el){
  const raw=(el.getAttribute('data-err')||'').trim();
  const lines=raw.split('\n').filter(Boolean);
  const body = lines.length>1 ? lines.map((l,i)=>(i+1)+'. '+l).join('\n') : (raw||'Sin detalle disponible.');
  alertModal(body,{title:'Detalle del error',danger:true});
}
async function loadBroadcasts(){
  { const _b=$('bc_rows'); if(_b && !_b.children.length) skelTable('bc_rows',5,4); }
  try{
    const r=await api('/api/broadcasts');
    const list=r.broadcasts||[];
    const rows=$('bc_rows'); rows.innerHTML='';
    $('bc_empty').style.display=list.length?'none':'block';
    let active=false;
    list.forEach(b=>{ rows.appendChild(bcRow(b)); if(b.status==='queued'||b.status==='sending') active=true; });
    if($('bc_selall')) $('bc_selall').checked=false; bcSelChanged();
    const live=$('bc_live'); live.classList.toggle('on', active);
    $('bc_live_t').textContent = active ? 'en vivo' : 'al día';
  }catch(e){ /* silencioso: no romper el polling por un fallo puntual */ }
}
async function bcDelete(id){
  if(!await confirmModal('¿Borrar este envío DEFINITIVAMENTE de la tabla? No se puede deshacer (no afecta lo ya entregado).',{danger:true,okText:'Borrar'})) return;
  try{ await api('/api/broadcasts/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}); toast('✓ Envío borrado'); loadBroadcasts(); }
  catch(e){ toast('Error al borrar',true); }
}
async function bcClearFinished(){
  if(!await confirmModal('¿Borrar DEFINITIVAMENTE todos los envíos terminados? Se conservan los que están en cola o enviándose.',{danger:true,okText:'Borrar terminados'})) return;
  try{ const r=await api('/api/broadcasts/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({finished:true})}); toast('✓ '+(r.deleted||0)+' envíos borrados'); loadBroadcasts(); }
  catch(e){ toast('Error al borrar',true); }
}
// Selección masiva (patrón de tabla reutilizable: checkbox por fila + barra de acciones).
function bcSelAll(v){ document.querySelectorAll('.bcsel').forEach(c=>c.checked=v); bcSelChanged(); }
function bcSelectedIds(){ return [...document.querySelectorAll('.bcsel:checked')].map(c=>c.dataset.id); }
function bcSelChanged(){
  document.querySelectorAll('.bcsel').forEach(c=>{ const tr=c.closest('tr'); if(tr) tr.classList.toggle('sel-row', c.checked); });
  const n=bcSelectedIds().length, b=$('bc_delsel');
  if(b){ b.disabled=n===0; b.textContent='🗑 Borrar seleccionados'+(n?' ('+n+')':''); }
}
async function bcDeleteSelected(){
  const ids=bcSelectedIds(); if(!ids.length) return;
  if(!await confirmModal('¿Borrar DEFINITIVAMENTE '+ids.length+' envío(s) seleccionados? No se puede deshacer.',{danger:true,okText:'Borrar'})) return;
  try{ const r=await api('/api/broadcasts/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:ids})}); toast('✓ '+(r.deleted||0)+' borrados'); loadBroadcasts(); }
  catch(e){ toast('Error al borrar',true); }
}
function bcStartPolling(){
  if(BC_TIMER) return;
  loadBroadcasts();
  BC_TIMER=setInterval(()=>{
    if(!CRED || document.hidden) return;            // sin sesión o pestaña del navegador oculta
    const visible=document.querySelector('main>.card[data-tab="envios"]')?.classList.contains('show');
    if(visible) loadBroadcasts();
  }, BC_POLL);
}
// Arranque autónomo (no toca boot()/showTab()): hook aditivo sobre showTab + DOMContentLoaded.
(function(){
  const _showTab=window.showTab;
  if(typeof _showTab==='function'){
    window.showTab=function(t){ _showTab(t); if(t==='envios') loadBroadcasts(); };
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
  const activo=(st==='pending'||st==='running');
  const cancelBtn=activo?`<button class="danger" style="padding:6px 12px" onclick="cancelPlan('${p.pid}')">🛑 Cancelar este envío</button>`:'';
  // Mensaje EXACTO que se envía (ya procesado: markup, sin IPRO PARTS, footer). Scrollable para revisar.
  return `<div class="card" style="margin-bottom:12px;background:var(--elev);padding:16px">`+
    `<div style="display:flex;justify-content:space-between;gap:10px;align-items:center">`+
      `<span class="pill ${pill}">${lab}</span>${cancelBtn}</div>`+
    `<div class="bc-meta" style="margin:6px 0">${bcFmtTime(p.created_at)} · lote ${p.batch_size|0} ${tgI?'· '+tgI:''} ${waI?'· '+waI:''}</div>`+
    `<div style="margin:6px 0;padding:10px;background:var(--bg);border:1px solid var(--bd);border-radius:8px;max-height:170px;overflow:auto;white-space:pre-wrap;font-size:12px;color:var(--tx2);line-height:1.5">${bcEsc(txt)}</div>`+
    `<div class="hint" style="margin:8px 0 2px">Progreso por lote:</div>`+
    lines+`</div>`;
}
async function cancelPlan(pid){
  if(!await confirmModal('¿Cancelar ESTE envío? Los lotes que falten NO se enviarán (los ya enviados no se pueden retirar).',{danger:true,okText:'Cancelar envío'})) return;
  try{ await api('/api/plans/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pid:pid})});
    toast('✓ Envío cancelado'); loadPlans(); loadQueue(); }
  catch(e){ toast('Error al cancelar',true); }
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
    const vis=document.querySelector('main>.card[data-tab="envios"]')?.classList.contains('show');
    if(vis) loadPlans(); }, BC_POLL);
}
(function(){ const _s=window.showTab;
  if(typeof _s==='function'){ window.showTab=function(t){ _s(t); if(t==='inicio') loadDashboard(); if(t==='envios') loadPlans(); if(t==='fuentes') loadBlocked(); if(t==='ajustes'){ loadQueue(); loadDlq(); loadAudit(); loadUsers(); } }; }
  const start=()=>{ if(CRED){ plStartPolling(); qStartPolling(); } };
  if(document.readyState!=='loading') start();
  else document.addEventListener('DOMContentLoaded', start);
})();
</script></body></html>"""
