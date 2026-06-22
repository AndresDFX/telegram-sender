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
    "send_mode",
    "bot_token",
    "telethon_api_id",
    "telethon_api_hash",
    "telethon_session",
    "whatsapp_enabled",
    "whatsapp_service_url",
    "whatsapp_token",
    "telegram_lists",
    "telegram_target",
    "whatsapp_lists",
    "whatsapp_target",
    # Lista existente que usa el ENVÍO AUTOMÁTICO del canal (por canal).
    "auto_telegram_list",
    "auto_whatsapp_list",
    # Anti-baneo / colas / ventana de envío.
    "capture_enabled",
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
    # Ventana horaria POR CANAL (independiente Telegram/WhatsApp).
    "tg_window_enabled",
    "tg_window_start",
    "tg_window_end",
    "wa_window_enabled",
    "wa_window_start",
    "wa_window_end",
    # Correo transaccional (recuperación de contraseña vía Resend).
    "resend_api_key",
    "mail_from",
    # NOTA: la info de DESTINATARIOS (exclusiones, excepciones y patrones, ambos canales) NO va aquí:
    # es POR USUARIO (se guarda en el registro del usuario vía /api/patterns; el efectivo para envíos
    # es la unión de todos los usuarios). Quitarla de /api/config evita que un guardado la borre.
)
_LISTAS = ("strip_patterns",)
# Info de destinatarios POR USUARIO (se guarda en __users__, no en la config global; el envío usa la unión).
_PER_USER = (
    "telegram_exclude_patterns", "whatsapp_exclude_patterns",
    "excluded_ids", "whatsapp_excluded",
    "telegram_pattern_exceptions", "whatsapp_pattern_exceptions",
)
_LISTAS_NOMBRADAS = ("telegram_lists", "whatsapp_lists")
_TARGETS = ("telegram_target", "whatsapp_target")
_FLOATS = ("tg_delay_min", "tg_delay_max")
_ENTEROS = ("batch_size", "wa_delay_min", "wa_delay_max", "window_tz")
_BOOLS = ("whatsapp_enabled", "scheduling_enabled", "window_enabled", "capture_enabled", "sending_enabled",
          "tg_window_enabled", "wa_window_enabled")
# Secretos que NO se sobreescriben con un valor vacío (para no borrarlos al guardar otros campos).
_NO_VACIAR = ("telethon_session", "telethon_api_id", "telethon_api_hash", "whatsapp_token", "bot_token", "resend_api_key")


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
    # M16: NO dormir el handler (bloquea el worker Lambda y se factura). El freno real es el
    # lock 'locked_until' (devuelve 429 sin procesar) + el throttling de API Gateway.
    _AUTH["fails"] += 1
    if _AUTH["fails"] >= _AUTH_MAX_FAILS:
        _AUTH["locked_until"] = time.time() + _AUTH_LOCK_SECS


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


def _rol_de(usuario: str) -> str:
    """Rol del usuario: 'admin' (gestiona usuarios + todo) o 'user' (todo MENOS gestión de
    usuarios). El usuario de entorno (bootstrap ADMIN_USER) es admin SIEMPRE. Los usuarios
    guardados SIN campo 'role' se tratan como 'admin' por compatibilidad (no dejar sin acceso a
    quien ya existía al introducir los roles); los usuarios nuevos se crean con rol explícito."""
    if usuario and usuario == admin_user():
        return "admin"
    try:
        u = (config.get_users() or {}).get(usuario) or {}
    except Exception:
        u = {}
    if not u:
        return "user"  # usuario desconocido → mínimo privilegio
    return "admin" if str(u.get("role", "admin")) == "admin" else "user"


def _rol_actual(event: dict[str, Any]) -> str:
    return _rol_de(_usuario_actual(event))


def _es_admin(event: dict[str, Any]) -> bool:
    return _rol_actual(event) == "admin"


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
            resets = config.get_resets() or {}
            prev = resets.get(usuario) or {}
            now = int(time.time())
            # A5: rate-limit anti-abuso/fuerza-bruta. La regeneración ilimitada de códigos era el hueco
            # (cada código da 5 intentos sobre 10^6). Tope: 1 código/60s y máx 3 por ventana de 15 min.
            if prev and now - int(prev.get("sent_at", 0)) < 60:
                return _json({"ok": True})  # genérico: ni revela ni reenvía (anti-spam)
            if prev and now < int(prev.get("exp", 0)) and int(prev.get("sends", 0)) >= 3:
                return _json({"ok": True})  # tope de códigos en la ventana vigente
            sends = int(prev.get("sends", 0)) + 1 if (prev and now < int(prev.get("exp", 0))) else 1
            code = auth_dom.gen_code(6)
            resets[usuario] = {
                "code_hash": auth_dom.hash_password(code), "exp": now + 900,
                "attempts": 0, "sends": sends, "sent_at": now,
            }
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

    # B12: si ya está bloqueado por intentos PREVIOS, 429 sin procesar (el front avisa "espera N min").
    # Se evalúa ANTES de _autorizado: el intento que DISPARA el lock aún devuelve 401 (credencial mala);
    # solo los intentos POSTERIORES, ya bloqueados, devuelven 429.
    if _auth_bloqueado():
        secs = max(1, int(_AUTH["locked_until"] - time.time()))
        return _json({"error": f"Demasiados intentos. Espera ~{max(1, (secs + 59) // 60)} min."}, 429)
    if not _autorizado(event):
        return _json({"error": "unauthorized"}, 401)

    try:
        if sub == "/api/me" and method == "GET":
            return _json({
                "ok": True,
                "user": _usuario_actual(event) or admin_user(),
                "role": _rol_actual(event),
                "is_admin": _es_admin(event),
            })
        if sub == "/api/users" and method == "GET":
            # Solo un administrador puede ver/gestionar la lista de usuarios del panel.
            if not _es_admin(event):
                return _json({"error": "Solo un administrador puede gestionar usuarios."}, 403)
            users = config.get_users() or {}
            return _json({"me": _usuario_actual(event), "is_admin": True, "users": [
                {"username": k, "email": v.get("email", ""), "role": str(v.get("role", "admin")),
                 "created_at": int(v.get("created_at", 0) or 0)}
                for k, v in users.items()]})
        if sub == "/api/users" and method == "POST":
            if not _es_admin(event):
                return _json({"error": "Solo un administrador puede crear usuarios."}, 403)
            c = _body(event)
            username = str(c.get("username", "")).strip()
            pw = c.get("password") or ""
            rol = "admin" if str(c.get("role", "user")) == "admin" else "user"
            if not username:
                return _json({"error": "El usuario es obligatorio."}, 400)
            if not auth_dom.password_valida(pw):
                return _json({"error": "La contraseña debe tener al menos 8 caracteres."}, 400)
            users = config.get_users() or {}
            if username in users:
                return _json({"error": "Ya existe un usuario con ese nombre."}, 400)
            users[username] = {"email": str(c.get("email", "")).strip(), "hash": auth_dom.hash_password(pw),
                               "role": rol, "created_at": int(time.time())}
            config.set_users(users)
            _audit("user:crear", f"{username} ({rol})")
            return _json({"ok": True})
        if sub == "/api/users/role" and method == "POST":
            # Cambiar el rol (admin ⇄ user). Solo admins; nunca dejar el panel sin administradores.
            if not _es_admin(event):
                return _json({"error": "Solo un administrador puede cambiar roles."}, 403)
            c = _body(event)
            username = str(c.get("username", "")).strip()
            nuevo = "admin" if str(c.get("role", "")) == "admin" else "user"
            users = config.get_users() or {}
            if username not in users:
                return _json({"error": "Usuario no encontrado."}, 400)
            if username == admin_user():
                # El admin principal (el del bootstrap ADMIN_USER) es el piso garantizado:
                # nunca se degrada, así que el panel siempre tiene al menos un administrador.
                return _json({"error": "El administrador principal no puede cambiar de rol."}, 400)
            users[username]["role"] = nuevo
            config.set_users(users)
            _audit("user:rol", f"{username} → {nuevo}")
            return _json({"ok": True})
        if sub == "/api/users/delete" and method == "POST":
            if not _es_admin(event):
                return _json({"error": "Solo un administrador puede borrar usuarios."}, 403)
            username = str(_body(event).get("username", "")).strip()
            users = config.get_users() or {}
            if username not in users:
                return _json({"error": "Usuario no encontrado."}, 400)
            if len(users) <= 1:
                return _json({"error": "No puedes borrar el último usuario."}, 400)
            if username == admin_user():
                return _json({"error": "No puedes borrar al administrador principal."}, 400)
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
        if sub == "/api/patterns" and method == "GET":
            # Info de destinatarios del USUARIO actual (se guarda en su registro, no en la config global).
            u = (config.get_users() or {}).get(_usuario_actual(event)) or {}
            return _json({k: [str(x) for x in (u.get(k) or [])] for k in _PER_USER})
        if sub == "/api/patterns" and method == "POST":
            cuerpo = _body(event)
            usuario = _usuario_actual(event)
            users = config.get_users() or {}
            rec = dict(users.get(usuario) or {})
            cambiados = []
            for k in _PER_USER:
                if k in cuerpo:
                    v = cuerpo.get(k)
                    if isinstance(v, str):
                        v = [ln.strip() for ln in v.splitlines() if ln.strip()]
                    elif isinstance(v, list):
                        v = [str(x).strip() for x in v if str(x).strip()]
                    else:
                        continue
                    rec[k] = v
                    cambiados.append(k)
            if usuario:  # solo persiste si hay un usuario identificado (no el fallback de entorno sin registro)
                users[usuario] = rec
                config.set_users(users)
            _audit("destinatarios", f"{usuario}: " + (", ".join(cambiados) or "(ninguno)"))
            return _json({k: [str(x) for x in (rec.get(k) or [])] for k in _PER_USER})
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
            # La imagen del COMPOSITOR (scope="compose") NO debe pisar la imagen por defecto de
            # Configuración: solo esa última actualiza config.image_key.
            if str(cuerpo.get("scope", "")) != "compose":
                config.set({"image_key": key})
            url = ""
            try:
                url = image_store.url_temporal(key)  # para previsualizarla / adjuntarla en un envío manual
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
        if sub == "/api/plans/delete" and method == "POST":
            cuerpo = _body(event)
            if cuerpo.get("finished"):
                n = plan_store.borrar_terminados()
                _audit("plans:borrar", f"terminados {n}")
                return _json({"ok": True, "deleted": n})
            pids = cuerpo.get("pids")
            if isinstance(pids, list) and pids:
                n = 0
                for x in pids:
                    try:
                        plan_store.borrar(str(x)); n += 1
                    except Exception:
                        pass
                _audit("plans:borrar", f"masivo {n}")
                return _json({"ok": True, "deleted": n})
            pid = str(cuerpo.get("pid", "")).strip()
            if not pid:
                return _json({"error": "pid requerido"}, 400)
            plan_store.borrar(pid)
            _audit("plans:borrar", pid)
            return _json({"ok": True})
        if sub == "/api/telegram/me" and method == "GET":
            return _telegram_api("getMe", {})  # verifica el token + muestra el bot
        if sub == "/api/telegram/account" and method == "GET":
            # Estado de la IDENTIDAD que ENVÍA por Telegram, para el header:
            #  - userbot: verifica la sesión de la cuenta (¿válida o hay que renovar?) + teléfono/nombre.
            #  - bot: identidad del bot (getMe) + id numérico.
            cfg = config.get()
            if str(cfg.get("send_mode", "bot")).lower() == "userbot":
                if not cfg.get("telethon_session"):
                    return _json({"mode": "userbot", "configured": False, "connected": False, "needs_renew": True})
                try:
                    cuenta = wiring.build_telethon_account()
                    est = cuenta.estado() if cuenta else {"authorized": False, "me": None}
                    return _json({
                        "mode": "userbot", "configured": True,
                        "connected": bool(est.get("authorized")),
                        "needs_renew": not bool(est.get("authorized")),
                        "me": est.get("me"),
                    })
                except Exception:
                    logger.exception("No se pudo verificar la sesión userbot de Telegram")
                    # No afirmamos "renovar" ante un fallo transitorio: estado desconocido.
                    return _json({"mode": "userbot", "configured": True, "connected": None, "needs_renew": False, "error": "no verificable"})
            r = _telegram_api("getMe", {})
            try:
                res = json.loads(r["body"]).get("result") or {}
            except Exception:
                res = {}
            ok = bool(res)
            return _json({"mode": "bot", "configured": bool(cfg.get("bot_token") or os.environ.get("BOT_TOKEN")),
                          "connected": ok, "needs_renew": not ok,
                          "me": {"id": str(res.get("id", "")), "username": res.get("username", ""),
                                 "name": res.get("first_name", "")} if ok else None})
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
            # image_key (clave S3 de una imagen SUBIDA): se propaga al plan para RE-FIRMAR la URL al
            # despachar (las prefirmadas caducan en 1h; un envío diferido las dejaría muertas).
            img_key = str(cuerpo.get("image_key", "")).strip()
            if img_key and not img_key.startswith("images/"):
                img_key = ""  # solo aceptamos claves de nuestro bucket de imágenes
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
                    image_key=img_key or None,
                )
            except ValueError as e:
                # Auditamos el RECHAZO para que se vea EN LA APP por qué un envío "no salió"
                # (p. ej. sin destinatarios por patrones de exclusión), no solo en el error del momento.
                canales = "+".join([c for c, on in (("tg", a_tg), ("wa", a_wa)) if on])
                _audit("broadcast:rechazado", f"[{canales}] {str(e)[:160]}")
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
            cuerpo = _body(event)
            if cuerpo.get("all"):  # borrar TODOS los programados
                n = 0
                try:
                    for s in schedule_store.listar():
                        try:
                            schedule_store.borrar(str(s.get("sid"))); n += 1
                        except Exception:
                            pass
                except Exception:
                    logger.exception("No se pudieron listar los schedules para borrado masivo")
                _audit("schedule:borrar", f"todos {n}")
                return _json({"ok": True, "deleted": n})
            sids = cuerpo.get("sids")
            if isinstance(sids, list) and sids:  # borrar SELECCIONADOS
                n = 0
                for x in sids:
                    try:
                        schedule_store.borrar(str(x)); n += 1
                    except Exception:
                        pass
                _audit("schedule:borrar", f"masivo {n}")
                return _json({"ok": True, "deleted": n})
            sid = str(cuerpo.get("sid", "")).strip()
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
        if sub == "/api/telethon/refresh-contacts" and method == "POST":
            # Relee los contactos del userbot EN VIVO (GetContacts) y actualiza la caché, saltando
            # el TTL del poller. Útil tras cambiar un nombre en la agenda (Google Contacts → Telegram).
            fuente = wiring.build_contacts_source()
            if fuente is None:
                return _json({"error": "Solo en modo userbot (en modo bot los destinatarios son los suscriptores)."}, 400)
            try:
                contactos = fuente.listar()
            except Exception as exc:  # FloodWait u otros: no romper el panel
                logger.exception("refresh-contacts falló")
                return _json({"error": "No se pudieron leer los contactos ahora (¿FloodWait de Telegram? espera unos minutos): " + str(exc)[:140]}, 502)
            config.set_contacts(contactos)
            _audit("telethon:refresh-contacts", f"{len(contactos)} contactos")
            return _json({"ok": True, "count": len(contactos)})
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
        if sub == "/api/whatsapp/sync" and method == "POST":
            # Re-sincroniza el estado de la cuenta (resyncAppState): trae cambios de nombre
            # de la agenda al servicio (que escucha contacts.update). Útil tras editar contactos.
            _audit("whatsapp:sync", "re-sincronizar contactos")
            return _whatsapp_proxy("/sync", timeout=25, body={})
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
/* === DESIGN SYSTEM TOKENS (Replica) ===
   Tokens ACTIVOS en el :root de RUNTIME (abajo): dark-only, acento naranja Replica, neutros slate.
   Para cambiar el aspecto edita ese :root. (Se eliminó el set --color-* de referencia que no se
   usaba; la marca documental vive en docs/brand/integratech/tokens.css.) */

:root{
  /* M26: neutros SLATE fríos (antes cálidos marrón); el acento sigue siendo el naranja Replica.
     Los grises de la UI derivan de estos tokens (literales sueltos mapeados a var(--bd)/--elev...). */
  --bg:#0F1217; --bg2:#0A0C10;
  --card:#161B22; --card2:#11151B; --elev:#1B212A;
  --bd:#2A323D; --bd2:#3B4654;
  --tx:#F3F6FA; --tx2:#CBD5E1; --mut:#93A1B2; --mut2:#647284;
  --ac:#FD531E; --ac-h:#FF6A3C; --ac2:#FF9166;
  --ok:#34d399; --warn:#fbbf24; --bad:#fb7185; --info:#60a5fa;
  --danger:#DC362E; --danger-h:#C02B24;   /* B18: rojo destructivo tokenizado en el runtime (mismos hex) */
  --r:12px; --r-sm:8px;   /* B17: escala de radios consistente (lg 12 / md 8) */
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
::-webkit-scrollbar-thumb{background:var(--bd);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--bd2);background-clip:padding-box}
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
  background:var(--elev);border:1px solid var(--bd2);color:var(--tx2);
  border-radius:8px;padding:7px 13px;margin-right:12px;cursor:pointer;font:inherit;font-weight:600}
input[type=file]::file-selector-button:hover{background:var(--bd2)}
/* Reset: los checkbox/radio NO deben heredar el padding/borde/fondo del input de texto (arriba),
   que les pintaba una caja fea alrededor. Render nativo limpio con el acento de marca. */
input[type=checkbox],input[type=radio]{
  accent-color:var(--ac);width:auto;cursor:pointer;
  padding:0;border:0;background:none;border-radius:0;box-shadow:none;margin:0;vertical-align:middle;flex:none;
}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.row>*{min-width:0}

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
button.sec{background:var(--elev);color:var(--tx2);border-color:var(--bd2)}
button.sec:hover{background:var(--bd2);filter:none;box-shadow:none}
button.ghost{background:transparent;border:1px solid var(--bd2);color:var(--mut)}
button.ghost:hover{background:rgba(255,255,255,.05);color:var(--tx2);filter:none;box-shadow:none}
button.danger{background:var(--danger);color:#fff;border-color:transparent}
button.danger:hover{background:var(--danger-h);filter:none;box-shadow:0 6px 22px -8px rgba(192,43,36,.5)}

/* ---------- markup widget ---------- */
.markup{display:flex;align-items:center;gap:18px;background:var(--elev);border:1px solid var(--bd);border-radius:var(--r);padding:18px}
.markup input{font-size:32px;font-weight:700;text-align:center;width:124px;padding:8px;color:var(--ac2)}

/* ---------- misc text ---------- */
.hint{color:var(--mut);font-size:12px;margin-top:6px;line-height:1.55}
.hint code{background:var(--elev);border:1px solid var(--bd);padding:1px 6px;border-radius:6px;font-size:11.5px}
/* ---------- help hint (ⓘ con tooltip; reutilizable en toda la app) ---------- */
.help{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:50%;
  border:1px solid var(--bd2);color:var(--mut);font-size:10px;font-weight:700;line-height:1;cursor:help;
  margin-left:6px;position:relative;vertical-align:middle;user-select:none;flex:none;transition:color .15s,border-color .15s}
.help:hover,.help:focus{color:var(--tx2);border-color:var(--ac);outline:none}
/* tooltip ABAJO del ícono (evita recorte en encabezados cerca del tope) */
.help::after{content:attr(data-tip);position:absolute;top:calc(100% + 9px);left:50%;
  transform:translateX(-50%) translateY(-4px);background:var(--elev);color:var(--tx2);border:1px solid var(--bd2);
  border-radius:9px;padding:9px 11px;width:250px;max-width:62vw;font-size:12px;font-weight:400;line-height:1.5;
  letter-spacing:normal;text-transform:none;text-align:left;white-space:normal;box-shadow:var(--sh);
  opacity:0;visibility:hidden;transition:opacity .15s,transform .15s;z-index:40;pointer-events:none}
.help::before{content:"";position:absolute;top:calc(100% + 4px);left:50%;transform:translateX(-50%);
  border:5px solid transparent;border-bottom-color:var(--bd2);opacity:0;visibility:hidden;transition:opacity .15s;z-index:40}
.help:hover::after,.help:focus::after{opacity:1;visibility:visible;transform:translateX(-50%) translateY(0)}
.help:hover::before,.help:focus::before{opacity:1;visibility:visible}
/* en headings (uppercase) el tooltip mantiene tipografía normal (ya forzado arriba) */
h2 .help{width:16px;height:16px}
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
.ico{width:1.05em;height:1.05em;vertical-align:-0.18em;display:inline-block;flex:none}
.steps-bar{height:6px;background:var(--bd);border-radius:999px;overflow:hidden;margin:2px 0 6px}
.steps-bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--ac),var(--ac2));border-radius:999px;transition:width .45s ease}
.step{display:flex;align-items:center;gap:11px;padding:9px 0;border-bottom:1px solid var(--bd)}
.step:last-child{border-bottom:0}
.step-ic{font-size:15px;flex:none;width:18px;text-align:center}
.step-tx{flex:1;display:flex;flex-direction:column;gap:1px;min-width:0}
.step-t{color:var(--tx)}
.step.done .step-t{color:var(--mut)}
.pill .ico{vertical-align:-0.15em;margin-right:3px}
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
  .subnav{width:100%}
  .subnav button{flex:1 1 auto}
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
/* refuerzo visual de los empties existentes */
.bc-empty, #subsempty{
  border:1px dashed var(--bd2);border-radius:var(--r);
  background:rgba(255,255,255,.04);
}
/* skeleton shimmer (el front puede inyectar .skeleton .sk-line al cargar) */
.skeleton{pointer-events:none}
.sk-line{
  height:12px;border-radius:6px;margin:9px 0;
  background:linear-gradient(90deg,var(--elev) 25%,var(--bd2) 37%,var(--elev) 63%);
  background-size:400% 100%;animation:sk 1.3s ease infinite;
}
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
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
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
.subnav{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.subnav button{background:transparent;border:1px solid var(--bd2);color:var(--mut);padding:8px 16px;border-radius:999px;font-weight:600;font-size:13px;cursor:pointer;transition:color .15s,background .15s,border-color .15s}
.subnav button:hover{color:var(--tx2);filter:none;box-shadow:none}
.subnav button.on{background:rgba(253,83,30,.14);color:#FFE0D3;border-color:rgba(253,83,30,.4)}
.card.subhide{display:none !important}
/* contenedor scrolleable para listas largas (muchos contactos) — la página no crece */
.tbl-scroll{max-height:340px;overflow:auto;margin-top:8px;border:1px solid var(--bd);border-radius:10px}
.tbl-scroll table{margin:0}
.tbl-scroll thead th{position:sticky;top:0;background:var(--card2);z-index:1}
.tbl-scroll td,.tbl-scroll th{padding-left:11px;padding-right:11px}
/* Tablas con selección masiva (patrón reutilizable: checkbox + barra de acciones) */
tbody tr.sel-row td{background:rgba(253,83,30,.12)}
.tbl-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:14px}
.tbl-toolbar .grow{flex:1}
.tbl-toolbar .sel-all{display:inline-flex;align-items:center;gap:8px;margin:0;font-size:13px;color:var(--mut);font-weight:600;cursor:pointer}
.tbl-toolbar .sel-all input{width:auto;margin:0}
.pl-card{transition:border-color .15s,box-shadow .15s}
.pl-card.sel-card{border-color:rgba(253,83,30,.55);box-shadow:0 0 0 1px rgba(253,83,30,.35)}
/* filtro segmentado (Todos / Incluidos / Excluidos) en Destinatarios */
.segf{display:inline-flex;border:1px solid var(--bd2);border-radius:999px;overflow:hidden;background:var(--elev);vertical-align:middle}
.segf button{background:transparent;border:none;border-radius:0;color:var(--mut);padding:7px 15px;font-weight:600;font-size:12.5px;cursor:pointer;transition:color .15s,background .15s}
.segf button+button{border-left:1px solid var(--bd2)}
.segf button:hover{color:var(--tx2);background:rgba(255,255,255,.05);filter:none;box-shadow:none}
.segf button.on{background:rgba(253,83,30,.16);color:#FFE0D3}
.segf button.on.exc{background:rgba(251,191,36,.16);color:#FCE7B0}
.excl-pat{margin:12px 0;padding:12px 14px;background:var(--bg);border:1px solid var(--bd);border-radius:10px}
.pill.pat{background:rgba(251,146,60,.14);color:#FFC79A;border-color:rgba(251,146,60,.32)}
.pill.exc-ok{background:rgba(52,211,153,.13);color:var(--ok);border-color:rgba(52,211,153,.45);border-style:dashed}
.bc-err{color:var(--bad);font-size:11px;margin-top:4px;line-height:1.35;max-width:240px;cursor:pointer;border-bottom:1px dotted rgba(255,107,90,.5);display:inline-block}
.bc-err:hover{color:#ff8f7d}
.bc-err:focus-visible{outline:2px solid var(--ac2);outline-offset:2px;border-radius:3px}
/* Sistema de modales reutilizable (reemplaza confirm()/prompt() nativos por diálogos de marca) */
.ds-overlay{position:fixed;inset:0;background:rgba(0,0,0,.66);display:flex;align-items:center;justify-content:center;z-index:1300;padding:20px;animation:dsFade .14s ease}
@keyframes dsFade{from{opacity:0}to{opacity:1}}
.ds-modal{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--bd2);border-radius:16px;box-shadow:0 24px 60px -16px rgba(0,0,0,.7);padding:22px;max-width:440px;width:100%;animation:dsPop .16s ease}
@keyframes dsPop{from{transform:translateY(10px) scale(.98);opacity:0}to{transform:none;opacity:1}}
.ds-modal h3{margin:0 0 10px;font-size:17px}
.ds-modal-body{color:var(--tx2);font-size:14px;line-height:1.55;white-space:pre-line;max-height:48vh;overflow:auto;overflow-wrap:anywhere;word-break:break-word}
.ds-modal input{width:100%;margin-top:14px}
.ds-modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:20px;flex-wrap:wrap}
/* Accesibilidad: foco visible consistente por teclado en elementos interactivos */
a:focus-visible,input[type=checkbox]:focus-visible,input[type=radio]:focus-visible,.subnav button:focus-visible,.nav button:focus-visible,.chan:focus-within{outline:2px solid var(--ac);outline-offset:2px;border-radius:8px}
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
  /* B24: el mensaje vuelve a truncar con ellipsis (antes max-width:none lo desbordaba) */
  .bc-msg{max-width:60vw}
  /* B23: el banner global de estado se apila y los botones ocupan el ancho */
  #send_banner{flex-direction:column;align-items:stretch}
  #send_banner .sb-txt{min-width:0}
  #send_banner .sb-go,#send_banner .sb-pause{width:100%}
  /* M33: el header no desborda — se trunca el correo y se ocultan badges no esenciales */
  header .u{max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  header #conn_tg_src{display:none}
  /* M32: el scroll interno de contactos no se 'pega' y deja respirar la página */
  .tbl-scroll{max-height:60vh;overscroll-behavior:contain}
}
th.selcol,td.selcol{width:34px;text-align:center}
</style></head><body>
<!-- Iconos de marca reutilizables (Telegram / WhatsApp) para mostrar junto a la info de cada canal. -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-tg" viewBox="0 0 240 240"><circle cx="120" cy="120" r="120" fill="#2AABEE"/><path fill="#fff" d="M53 118.5l116-44.7c5.4-2 10.1 1.3 8.3 9.5l-19.8 93.2c-1.5 6.6-5.4 8.2-10.9 5.1l-30.2-22.3-14.6 14c-1.6 1.6-3 3-6.1 3l2.1-30.9 56.3-50.9c2.4-2.2-.5-3.4-3.8-1.2l-69.6 43.8-30-9.4c-6.5-2-6.6-6.5 1.4-9.6z"/></symbol>
<symbol id="i-wa" viewBox="0 0 240 240"><circle cx="120" cy="120" r="120" fill="#25D366"/><path fill="#fff" d="M120 54c-36.4 0-66 29.6-66 66 0 11.6 3 22.5 8.3 32L54 186l34.7-9.1c9.1 5 19.6 7.8 30.8 7.8h.5c36.4 0 66-29.6 66-66s-29.6-66-66-66zm38.6 93.2c-1.6 4.5-9.4 8.7-13 9.2-3.3.5-7.5.7-12.1-.8-2.8-.9-6.4-2.1-11-4.1-19.4-8.4-32-27.9-33-29.2-1-1.3-7.9-10.5-7.9-20s5-14.2 6.8-16.2c1.8-2 3.9-2.5 5.2-2.5h3.7c1.2 0 2.8-.2 4.4 3.4 1.6 3.7 5.5 12.9 6 13.8.5.9.8 2 .1 3.3-.7 1.3-1 2.1-2 3.2-1 1.1-2.1 2.5-3 3.3-1 1-2 2.1-.9 4s5 8.2 10.7 13.3c7.4 6.6 13.6 8.6 15.5 9.6 1.9 1 3 .8 4.1-.5 1.1-1.3 4.7-5.5 6-7.4 1.3-1.9 2.6-1.6 4.4-1 1.8.7 11.4 5.4 13.3 6.3 1.9 1 3.2 1.4 3.7 2.2.5.9.5 4.6-1.1 9.1z"/></symbol>
</defs></svg>

<div id="login"><div class="box">
  <div class="brand brand-lg"><svg viewBox="0 0 48 48" width="46" height="46" aria-hidden="true"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#FD531E"/><stop offset="1" stop-color="#FD9E76"/></linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#lg)"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 24c5 0 5.5-9 11.5-9"/><path d="M21 24h11.5"/><path d="M21 24c5 0 5.5 9 11.5 9"/></g><circle cx="15" cy="24" r="4.2" fill="#fff"/><circle cx="33.5" cy="15" r="3" fill="#fff"/><circle cx="34.5" cy="24" r="3" fill="#fff"/><circle cx="33.5" cy="33" r="3" fill="#fff"/></svg><span class="wordmark">Replica</span></div>
  <p style="text-align:center">Captura listas de precios y envíalas a tus contactos — Telegram y WhatsApp, al instante o programado.</p>
  <label>Usuario</label><input id="lu" autocomplete="username" placeholder="usuario o correo">
  <label>Contraseña</label><input id="lp" type="password" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
  <div class="err" id="lerr" role="alert" aria-live="assertive"></div>
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
 <header><div class="brand"><svg viewBox="0 0 48 48" width="30" height="30" aria-hidden="true"><defs><linearGradient id="lg2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#FD531E"/><stop offset="1" stop-color="#FD9E76"/></linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#lg2)"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 24c5 0 5.5-9 11.5-9"/><path d="M21 24h11.5"/><path d="M21 24c5 0 5.5 9 11.5 9"/></g><circle cx="15" cy="24" r="4.2" fill="#fff"/><circle cx="33.5" cy="15" r="3" fill="#fff"/><circle cx="34.5" cy="24" r="3" fill="#fff"/><circle cx="33.5" cy="33" r="3" fill="#fff"/></svg><span class="wordmark">Replica</span></div><div><span id="conn_tg" class="pill" title="Estado del bot de Telegram" style="margin-right:6px"></span><span id="conn_tg_src" class="pill" title="Canal fuente del que Telegram lee las listas" style="margin-right:6px;display:none"></span><span id="conn_wa" class="pill" title="Estado del servicio WhatsApp" style="margin-right:6px"></span><span id="hdr_badge" class="pill" style="display:none;margin-right:10px"></span><span class="u" id="who"></span><span id="who_role" class="pill" style="display:none;margin-left:7px;padding:2px 8px;font-size:11px"></span>
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
     <div class="stat"><b id="k_rate">–</b><span>tasa de éxito <span class="help" tabindex="0" data-tip="Histórica de los últimos 30 días, sobre los envíos contabilizados. No es el estado actual.">ⓘ</span></span></div>
     <div class="stat"><b id="k_pend">–</b><span>lotes pendientes</span></div>
     <div class="stat"><b id="k_dlq">–</b><span>en DLQ <span class="help" tabindex="0" data-tip="Lotes atascados ahora mismo (cola de fallidos SQS). Es un conteo APROXIMADO: puede tardar unos segundos en actualizarse tras reintentar/descartar, y convivir con una tasa de éxito alta (la tasa es histórica).">ⓘ</span></span></div>
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
   <div class="hint">Configuración guiada: el botón de cada paso te lleva directo a la sección exacta para completarlo.</div>
   <div id="dash_steps" style="margin-top:10px">cargando…</div>
  </div>
  <div class="card" data-tab="fuentes" style="padding:14px 18px"><div class="subnav" data-subnav="fuentes"><span class="hint" style="margin:0 8px 0 0">Ver:</span><button data-sub="fuente" onclick="showSub('fuentes','fuente')">📡 Fuente del canal</button><button data-sub="tg" onclick="showSub('fuentes','tg')"><svg class="ico"><use href="#i-tg"></use></svg> Telegram</button><button data-sub="wa" onclick="showSub('fuentes','wa')"><svg class="ico"><use href="#i-wa"></use></svg> WhatsApp</button></div></div>
  <div class="card" data-tab="fuentes" data-sub="fuente"><h2>Aumento (markup)<span class="help" tabindex="0" data-tip="Sube un % los precios detectados antes de difundir. Solo afecta números con símbolo de moneda ($, 💸, COP); no toca modelos ni especificaciones. Redondea al mil hacia arriba.">ⓘ</span></h2>
   <div class="markup"><input id="markup_percentage" type="number" step="0.1"><div>
     <div style="font-size:13px">% que se suma a cada precio</div>
     <div class="hint">Ej: $325.000 + 15% → $374.000 (redondeo al mil ↑)</div></div></div>
  </div>
  <div class="card" data-tab="ajustes" style="padding:14px 18px"><div class="subnav" data-subnav="ajustes"><span class="hint" style="margin:0 8px 0 0">Ver:</span><button data-sub="telegram" onclick="showSub('ajustes','telegram')"><svg class="ico"><use href="#i-tg"></use></svg> Telegram</button><button data-sub="whatsapp" onclick="showSub('ajustes','whatsapp')"><svg class="ico"><use href="#i-wa"></use></svg> WhatsApp</button><button data-sub="acceso" onclick="showSub('ajustes','acceso')">👤 Acceso</button><button data-sub="envio" onclick="showSub('ajustes','envio')">📤 Envío</button><button data-sub="sistema" onclick="showSub('ajustes','sistema')">🛠️ Sistema</button></div></div>
  <div class="card accent" data-tab="ajustes" data-sub="telegram"><h2><svg class="ico"><use href="#i-tg"></use></svg> Cuenta de Telegram<span class="help" tabindex="0" data-tip="Dos modos: Bot (envía a quienes te dan /start) o Userbot (envía desde TU cuenta a tus contactos, vía Telethon). El userbot llega a más gente pero tiene más riesgo de baneo.">ⓘ</span></h2>
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
  <div class="card" data-tab="ajustes" data-sub="whatsapp"><h2><svg class="ico"><use href="#i-wa"></use></svg> WhatsApp (reenvío)<span class="help" tabindex="0" data-tip="Conecta el servicio de WhatsApp (URL + token, QR o código) y decide si cada lista capturada también se reenvía por WhatsApp. Vincula desde tu IP residencial.">ⓘ</span></h2>
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
  <div class="card" data-tab="fuentes" data-sub="fuente"><h2>Canal y mensaje<span class="help" tabindex="0" data-tip="Canal público de Telegram que se sondea (t.me/s/canal) y la limpieza del mensaje: líneas a quitar (ubicación/teléfono), símbolos de moneda y footer de WhatsApp.">ⓘ</span></h2>
   <div class="hint" style="margin-top:-4px">La <b>recopilación</b> del canal y el <b>envío</b> a tus contactos son interruptores <b>separados</b> en «Ajustes y estado → Envío». Con la recopilación activa se guarda y se ve cada lista publicada (en «Envíos»); el envío automático se controla aparte.</div>
   <label>Canal fuente (username sin @)</label><input id="source_channel">
   <label>Símbolos de moneda</label><input id="currency_symbols">
   <label>Footer WhatsApp (se añade al final de cada lista)</label><textarea id="whatsapp_footer"></textarea>
   <label>Patrones a quitar (ubicación), uno por línea</label><textarea id="strip_patterns"></textarea>
   <button onclick="saveCfg()">Guardar cambios</button>
  </div>
  <div class="card" data-tab="fuentes" data-sub="fuente"><h2>Probar procesamiento del mensaje<span class="help" tabindex="0" data-tip="Pega un texto de ejemplo del canal y mira cómo quedaría YA procesado (markup, limpieza y footer) sin enviar nada.">ⓘ</span></h2>
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
  <div class="card" data-tab="ajustes" data-sub="sistema"><h2>Cola de mensajes</h2>
   <div class="stats"><div class="stat"><b id="q_p">–</b><span>lotes programados pendientes</span></div>
     <div class="stat"><b id="q_b">–</b><span>en cola SQS (en vuelo)</span></div>
     <div class="stat"><b id="q_d">–</b><span>en DLQ (fallidos)</span></div></div>
   <div class="hint" style="margin-top:10px">Con el envío fraccionado, los lotes esperan en la <b>programación</b> y se liberan de a uno; por eso "en cola SQS" suele ser 0 o 1 (el lote en vuelo). Mira el detalle en <b>Envíos → Envíos fraccionados</b>.</div>
   <button class="sec" style="margin-top:14px" onclick="loadQueue()">Refrescar</button>
  </div>
  <div class="card" data-tab="ajustes" data-sub="sistema"><h2>Cola de fallidos (DLQ)<span class="help" tabindex="0" data-tip="Mensajes que fallaron tras varios reintentos. Puedes reintentarlos (redrive) o descartarlos (purgar). Útil para diagnosticar problemas de envío.">ⓘ</span> <span id="dlq_n" class="hint"></span></h2>
   <div class="hint">Lotes que agotaron reintentos. Puedes <b>reintentarlos</b> (vuelven a la cola) o <b>descartarlos</b>.</div>
   <div id="dlq_list" style="margin-top:10px"></div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
     <button class="sec" onclick="loadDlq()">Ver / refrescar</button>
     <button class="sec" onclick="dlqRedrive()">↩ Reintentar todo</button>
     <button class="danger" onclick="dlqPurge()">🗑 Descartar todo</button>
   </div>
  </div>
  <div class="card" data-tab="ajustes" data-sub="sistema"><h2>Auditoría <span id="audit_n" class="hint"></span></h2>
   <div class="hint">Últimas acciones realizadas en el panel (config, envíos, cancelaciones, DLQ).</div>
   <div style="overflow-x:auto;margin-top:10px"><table><thead><tr><th>cuándo</th><th>usuario</th><th>acción</th><th>detalle</th></tr></thead><tbody id="audit_rows"></tbody></table></div>
   <button class="sec" style="margin-top:12px" onclick="loadAudit()">Refrescar</button>
  </div>
  <div class="card" data-tab="fuentes" data-sub="tg"><h2>Destinatarios<span class="help" tabindex="0" data-tip="Contactos a los que envías por Telegram. Filtra por estado (Todos/Incluidos/Excluidos). Marca y usa Excluir/Incluir; incluir un contacto que coincide con un patrón crea una EXCEPCIÓN (se envía pese al patrón).">ⓘ</span> <span id="subcount" class="hint"></span></h2>
   <div class="hint">Busca, navega y usa los botones para incluir/excluir en masa. Los excluidos NO reciben las listas.</div>
   <div style="margin-top:8px"><button class="sec" onclick="tgRefreshContacts()">🔄 Actualizar nombres</button><span class="help" tabindex="0" data-tip="Relee tus contactos de Telegram EN VIVO y actualiza los nombres aquí. Úsalo si cambiaste un nombre en tu agenda (p. ej. Google Contacts) y quieres que se refleje. (Sin esto, se refrescan solos cada ~30 min.)">ⓘ</span></div>
   <input id="subsearch" placeholder="🔎 Buscar por nombre o número..." oninput="onSearch()" style="margin-top:10px">
   <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px">
     <span class="hint" style="margin:0">Mostrar:</span>
     <div class="segf" id="seg_tg" role="group" aria-label="Filtrar destinatarios de Telegram">
       <button class="on" data-v="" aria-pressed="true" onclick="setStateFilter('')">Todos</button>
       <button data-v="inc" aria-pressed="false" onclick="setStateFilter('inc')">✅ Incluidos</button>
       <button data-v="exc" aria-pressed="false" onclick="setStateFilter('exc')">⛔ Excluidos</button>
     </div>
   </div>
   <div class="excl-pat">
     <label style="margin:0 0 6px">⛔ Auto-excluir por patrón de nombre</label>
     <div class="hint" style="margin:0 0 8px">Un patrón por línea. Cualquier contacto cuyo nombre <b>contenga</b> un patrón (sin distinguir mayúsculas) queda excluido solo de los envíos. Ej.: <code>FAM</code> (familia), <code>#</code>. Son <b>tus</b> patrones (por usuario); los envíos excluyen con la <b>unión</b> de los de todos los usuarios.</div>
     <textarea id="tg_excl_pat" style="min-height:62px" placeholder="FAM&#10;#"></textarea>
     <button style="margin-top:8px" onclick="saveExclPatterns('telegram')">Guardar patrones</button>
   </div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
     <button class="sec" onclick="toggleAll(true)">Marcar página</button>
     <button class="sec" onclick="toggleAll(false)">Desmarcar</button>
     <button onclick="bulk('excluir')">Excluir marcados</button>
     <button onclick="bulk('incluir')">Incluir marcados</button>
     <button class="sec" onclick="createListFromGrid('telegram')">➕ Lista con marcados</button>
     <button class="sec" onclick="createListFromIncluded('telegram')">➕ Lista con incluidos</button>
     <button class="ghost" onclick="bulkFiltered('excluir')">Excluir filtrados</button>
     <button class="ghost" onclick="bulkFiltered('incluir')">Incluir filtrados</button>
   </div>
   <div class="tbl-scroll"><table><thead><tr><th><input type="checkbox" id="selall" onchange="toggleAll(this.checked)"></th><th>nombre</th><th>estado</th></tr></thead><tbody id="subs"></tbody></table></div>
   <div class="hint" id="subsempty" style="display:none;margin-top:12px">Sin destinatarios (modo bot: nadie dio /start; modo userbot: la cuenta no tiene contactos).</div>
   <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
     <button class="sec" onclick="prevPage()">◀</button>
     <span id="pageinfo" class="hint"></span>
     <button class="sec" onclick="nextPage()">▶</button>
   </div>
  </div>
  <div class="card" data-tab="fuentes" data-sub="tg"><h2>Listas de distribución · Telegram<span class="help" tabindex="0" data-tip="Agrupa contactos en listas con nombre y elige el modo: Todos, Solo estas listas (whitelist) o Excepto estas listas (blacklist).">ⓘ</span></h2>
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
  <div class="card" data-tab="fuentes" data-sub="wa"><h2>Destinatarios WhatsApp<span class="help" tabindex="0" data-tip="Contactos de WhatsApp (cárgalos del servicio conectado). Filtra por estado y marca para Excluir/Incluir; incluir un contacto que coincide con un patrón crea una EXCEPCIÓN.">ⓘ</span> <span id="wa_c_count" class="hint"></span></h2>
   <div class="hint">Carga tus contactos (servicio conectado), busca por nombre, y marca para <b>excluir/incluir</b>. Los excluidos NO reciben las difusiones por WhatsApp.</div>
   <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
     <button class="sec" onclick="loadWaContacts()">Cargar contactos de WhatsApp</button>
     <button class="sec" onclick="waSyncContacts()">🔄 Sincronizar nombres</button>
     <span class="help" tabindex="0" data-tip="Re-sincroniza tu cuenta de WhatsApp para traer cambios de nombre de la agenda (p. ej. Google Contacts). Tras sincronizar, recarga los contactos para ver los nombres nuevos.">ⓘ</span>
   </div>
   <input id="wa_search" placeholder="🔎 Buscar por nombre o número..." oninput="renderWa()" style="margin-top:10px">
   <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px">
     <span class="hint" style="margin:0">Mostrar:</span>
     <div class="segf" id="seg_wa" role="group" aria-label="Filtrar destinatarios de WhatsApp">
       <button class="on" data-v="" aria-pressed="true" onclick="setWaStateFilter('')">Todos</button>
       <button data-v="inc" aria-pressed="false" onclick="setWaStateFilter('inc')">✅ Incluidos</button>
       <button data-v="exc" aria-pressed="false" onclick="setWaStateFilter('exc')">⛔ Excluidos</button>
     </div>
   </div>
   <div class="excl-pat">
     <label style="margin:0 0 6px">⛔ Auto-excluir por patrón de nombre</label>
     <div class="hint" style="margin:0 0 8px">Un patrón por línea. Cualquier contacto cuyo nombre <b>contenga</b> un patrón (sin distinguir mayúsculas) queda excluido solo de los envíos. Ej.: <code>FAM</code> (familia), <code>#</code>. Son <b>tus</b> patrones (por usuario); los envíos excluyen con la <b>unión</b> de los de todos los usuarios.</div>
     <textarea id="wa_excl_pat" style="min-height:62px" placeholder="FAM&#10;#"></textarea>
     <button style="margin-top:8px" onclick="saveExclPatterns('whatsapp')">Guardar patrones</button>
   </div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
     <button class="sec" onclick="waToggleAll(true)">Marcar página</button>
     <button class="sec" onclick="waToggleAll(false)">Desmarcar</button>
     <button onclick="waBulk('excluir')">Excluir marcados</button>
     <button onclick="waBulk('incluir')">Incluir marcados</button>
     <button class="ghost" onclick="waBulkFiltered('excluir')">Excluir filtrados</button>
     <button class="ghost" onclick="waBulkFiltered('incluir')">Incluir filtrados</button>
     <button class="sec" onclick="createListFromGrid('whatsapp')">➕ Lista con marcados</button>
     <button class="sec" onclick="createListFromIncluded('whatsapp')">➕ Lista con incluidos</button>
   </div>
   <div class="tbl-scroll"><table><thead><tr><th></th><th>nombre</th><th>estado</th></tr></thead><tbody id="wa_subs"></tbody></table></div>
   <div style="display:flex;gap:12px;align-items:center;margin-top:10px"><button class="sec" onclick="waPrev()">◀</button><span id="wa_pageinfo" class="hint"></span><button class="sec" onclick="waNext()">▶</button></div>
  </div>
  <div class="card" data-tab="fuentes" data-sub="wa"><h2>Auto-excluidos por fallos<span class="help" tabindex="0" data-tip="El servicio excluye solo a quien falla al recibir varias veces seguidas (protege tu número de baneos). Limpia el conteo para reincluirlos.">ⓘ</span> <span id="wa_blk_n" class="hint"></span></h2>
   <div class="hint">Los contactos que fallan al enviar de forma repetida (≥ umbral) se excluyen <b>solos</b> de los próximos envíos para proteger tu número. Limpia el conteo para reincluirlos.</div>
   <div id="wa_blk_list" class="hint" style="margin-top:10px">—</div>
   <div style="display:flex;gap:8px;margin-top:12px">
     <button class="sec" onclick="loadBlocked()">Ver / refrescar</button>
     <button class="ghost" onclick="clearBlocked()">Reincluir a todos</button>
   </div>
  </div>
  <div class="card" data-tab="fuentes" data-sub="wa"><h2>Listas de distribución · WhatsApp<span class="help" tabindex="0" data-tip="Agrupa contactos de WhatsApp en listas y elige el modo: Todos, Solo estas listas (whitelist) o Excepto estas listas (blacklist).">ⓘ</span></h2>
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
  <div class="card" data-tab="envios" style="padding:14px 18px"><div class="subnav" data-subnav="envios"><span class="hint" style="margin:0 8px 0 0">Ver:</span><button data-sub="componer" onclick="showSub('envios','componer')">✍️ Componer</button><button data-sub="programados" onclick="showSub('envios','programados')">⏰ Programados</button><button data-sub="historial" onclick="showSub('envios','historial')">📡 Historial</button></div></div>
  <div class="card" data-tab="envios" data-sub="componer"><h2>✍️ Componer y enviar<span class="help" tabindex="0" data-tip="Redacta un mensaje propio y envíalo ya (o prográmalo). Respeta listas y exclusiones de cada canal. WhatsApp exige elegir una lista; el texto de Telegram no puede pasar de 4096 caracteres.">ⓘ</span></h2>
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
     <label class="chan tg" id="bc_chan_tg"><span class="dot"></span><input type="checkbox" id="bc_telegram" onchange="bcChan()" style="display:none"><svg class="ico"><use href="#i-tg"></use></svg> Telegram</label>
     <label class="chan wa" id="bc_chan_wa"><span class="dot"></span><input type="checkbox" id="bc_whatsapp" onchange="bcChan()" style="display:none"><svg class="ico"><use href="#i-wa"></use></svg> WhatsApp</label>
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
   <div class="hint">Vacío = enviar ya (fraccionado). Con fecha/hora = se difiere hasta entonces y luego se gotea por lotes. La hora se interpreta en la <b>zona horaria configurada</b> (Ajustes → Envío → Anti-baneo), no en la de tu navegador.</div>

   <div class="compose-actions">
     <button id="bc_send" onclick="sendBroadcast()">Enviar</button>
     <button class="ghost" onclick="bcClear()">Limpiar</button>
     <span class="grow"></span>
     <span id="bc_status" class="hint" style="margin-top:0"></span>
   </div>
  </div>
  <div class="card" data-tab="envios" data-sub="historial"><h2>📡 Envíos <span class="live" id="bc_live" style="margin-left:auto"><span class="ping"></span><span id="bc_live_t">en vivo</span></span></h2>
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
  <div class="card accent" data-tab="envios" data-sub="programados"><h2>⏰ Programar un mensaje<span class="help" tabindex="0" data-tip="Crea un envío que sale a una fecha/hora exacta (una vez) o de forma recurrente (diario/semanal). Requiere que los envíos estén activos a esa hora.">ⓘ</span></h2>
   <div class="hint">Crea mensajes que se envían solos a la hora indicada, por las conexiones existentes de Telegram y WhatsApp. Una vez, a diario o semanal. Respetan el ritmo anti-baneo, la ventana horaria y el interruptor maestro.</div>
   <label>Nombre (opcional)</label>
   <input id="sg_name" placeholder="p. ej. Lista de la mañana" maxlength="80">
   <label>Mensaje <span id="sg_count" class="charcount">0 caracteres</span></label>
   <textarea id="sg_text" style="min-height:110px" placeholder="Escribe el mensaje a programar..." oninput="sgCount()"></textarea>
   <label>Imagen (opcional)</label>
   <input id="sg_image_url" placeholder="…pega una URL https:// de imagen">
   <label style="margin-top:16px">Canales</label>
   <div class="chan-row">
     <label class="chan tg on" id="sg_chan_tg"><span class="dot"></span><input type="checkbox" id="sg_telegram" checked onchange="sgChan()" style="display:none"><svg class="ico"><use href="#i-tg"></use></svg> Telegram</label>
     <label class="chan wa" id="sg_chan_wa"><span class="dot"></span><input type="checkbox" id="sg_whatsapp" onchange="sgChan()" style="display:none"><svg class="ico"><use href="#i-wa"></use></svg> WhatsApp</label>
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
   <div class="hint" style="margin-top:8px">La hora usa la zona horaria configurada en <b>Ajustes y estado → Envío → Anti-baneo</b> (ventana de envío).</div>
   <div class="compose-actions">
     <button id="sg_create" onclick="sgCreate()">Programar</button>
     <button class="ghost" onclick="sgClear()">Limpiar</button>
     <span class="grow"></span>
     <span id="sg_status" class="hint" style="margin-top:0"></span>
   </div>
  </div>
  <div class="card" data-tab="envios" data-sub="programados"><h2>📅 Mensajes programados <span id="sg_n" class="hint"></span></h2>
   <div class="hint">Próximos envíos automáticos. Puedes pausarlos/activarlos o eliminarlos.</div>
   <div style="overflow-x:auto;margin-top:12px">
     <table id="sg_table"><thead><tr><th class="selcol"><input type="checkbox" id="sg_selall" onchange="sgSelAll(this.checked)"></th><th>Mensaje</th><th>Canales</th><th>Cuándo</th><th>Próximo</th><th></th></tr></thead>
       <tbody id="sg_rows"></tbody></table>
   </div>
   <div class="empty-state" id="sg_empty" style="display:none"><div class="ico">⏰</div><h3>Sin mensajes programados</h3><p>Crea uno arriba para enviarlo automáticamente.</p></div>
   <div class="tbl-toolbar">
     <button class="danger" id="sg_delsel" onclick="sgDeleteSelected()" disabled>🗑 Borrar seleccionados</button>
     <button class="danger" onclick="sgDeleteAll()">🗑 Borrar todos</button>
     <span class="grow"></span>
     <button class="sec" onclick="loadSchedules()">Refrescar</button>
   </div>
  </div>
  <div class="card accent" id="usr_card" data-tab="ajustes" data-sub="acceso" style="display:none"><h2>👥 Usuarios del panel <span id="usr_n" class="hint"></span></h2>
   <div class="hint">Cada usuario entra con sus propias credenciales (independientes). El correo se usa para recuperar la contraseña. Solo un <b>administrador</b> ve esta sección y gestiona usuarios; los usuarios normales pueden hacer todo lo demás.</div>
   <div style="overflow-x:auto;margin-top:12px"><table id="usr_table"><thead><tr><th>Usuario</th><th>Correo</th><th>Rol</th><th></th></tr></thead><tbody id="usr_rows"></tbody></table></div>
   <div class="section-label" style="margin-top:14px">Crear usuario</div>
   <div class="row">
     <div><label>Usuario o correo</label><input id="usr_new_name" placeholder="nuevo@correo.com"></div>
     <div><label>Correo (para recuperación)</label><input id="usr_new_email" placeholder="correo@dominio.com"></div>
   </div>
   <div class="row">
     <div><label>Contraseña (mínimo 8)</label><input id="usr_new_pw" type="password"></div>
     <div><label>Rol<span class="help" tabindex="0" data-tip="Administrador: gestiona usuarios y todo lo demás. Usuario: hace todo MENOS gestionar usuarios.">ⓘ</span></label>
       <select id="usr_new_role"><option value="user" selected>Usuario (sin gestión de usuarios)</option><option value="admin">Administrador</option></select></div>
   </div>
   <button style="margin-top:10px" onclick="createUser()">Crear usuario</button>
  </div>
  <div class="card" data-tab="ajustes" data-sub="acceso"><h2>✉️ Correo de recuperación<span class="help" tabindex="0" data-tip="Servicio gratis (Resend, 100/día) para enviar el código de «¿Olvidaste tu contraseña?». Sin API key, el código se intenta enviar por el correo de alertas de AWS (SNS).">ⓘ</span> <span id="mail_status" class="hint"></span></h2>
   <div class="hint">Servicio gratis para entregar el código cuando alguien usa «¿Olvidaste tu contraseña?». Crea una cuenta en <b>resend.com</b> (100 correos/día gratis), genera una API key y pégala aquí. Sin esto, el código se intenta enviar por el correo de alertas de AWS (SNS).</div>
   <label>Remitente (From)</label>
   <input id="mail_from" placeholder="Replica &lt;onboarding@resend.dev&gt;">
   <div class="hint">Para enviar a cualquier destinatario, verifica tu dominio en Resend. <code>onboarding@resend.dev</code> solo entrega al correo con el que te registraste.</div>
   <label>API key de Resend</label>
   <input id="resend_api_key" type="password" placeholder="(pegar solo si quieres cambiarla)">
   <div style="margin-top:10px"><button onclick="saveEmail()">Guardar correo</button> <span id="mail_save_status" class="hint" style="margin-left:10px"></span></div>
  </div>
  <div class="card" data-tab="ajustes" data-sub="acceso"><h2>🔑 Cambiar mi contraseña</h2>
   <label>Contraseña actual</label><input id="cp_cur" type="password">
   <label>Nueva contraseña (mínimo 8)</label><input id="cp_new" type="password">
   <div style="margin-top:10px"><button onclick="changePassword()">Cambiar contraseña</button> <span id="cp_status" class="hint" style="margin-left:10px"></span></div>
  </div>
  <div class="card accent" data-tab="ajustes" data-sub="envio"><h2>Recopilación automática<span class="help" tabindex="0" data-tip="Lee el canal fuente y guarda cada lista nueva (con markup y footer) para que la VEAS, sin enviarla a nadie. Es independiente del ENVÍO: puedes recopilar con el envío apagado.">ⓘ</span></h2>
   <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
     <label style="display:flex;align-items:center;gap:10px;margin:0;font-size:15px;color:var(--tx)"><input type="checkbox" id="capture_enabled" style="width:auto;transform:scale(1.3)" onchange="toggleCapture()"> <b>Recopilar listas del canal</b></label>
     <span id="cap_badge" class="pill">—</span>
   </div>
   <div class="hint" style="margin-top:10px">Lee el <b>canal fuente configurado</b> y registra cada lista nueva para que la <b>veas</b>, <b>sin enviarla a nadie</b>. Mientras el <b>envío automático</b> esté apagado, cada lista capturada aparece en <b>Envíos</b> como <b>📥 Capturada</b> y se te manda a tus <b>Mensajes Guardados</b> de Telegram. Es independiente del envío.</div>
  </div>
  <div class="card accent" data-tab="ajustes" data-sub="envio"><h2>Envío automático<span class="help" tabindex="0" data-tip="Pausa o activa el ENVÍO automático de las listas capturadas. El envío MANUAL (Componer → Enviar) SIEMPRE sale, aun en pausa. La RECOPILACIÓN es aparte (arriba).">ⓘ</span></h2>
   <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
     <label style="display:flex;align-items:center;gap:10px;margin:0;font-size:15px;color:var(--tx)"><input type="checkbox" id="sending_enabled" style="width:auto;transform:scale(1.3)" onchange="toggleSending()"> <b>Envíos automáticos activos</b></label>
     <span id="sys_badge" class="pill">—</span>
   </div>
   <div class="hint" style="margin-top:10px">Controla <b>solo el ENVÍO</b> de lo recopilado. <b>Apagado:</b> cada lista capturada solo se <b>ve</b> (en Envíos y en tus Mensajes Guardados), NO se envía. <b>Activado:</b> cada lista nueva se envía <b>a la lista que elijas abajo</b> por canal. Activar <b>no</b> reenvía lo ya capturado. El envío MANUAL siempre funciona.</div>
   <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--bd)">
     <div class="hint" style="margin-top:0">¿Hay difusiones en cola que NO quieres enviar? Cancélalas (no se enviarán, ni al reactivar).</div>
     <button class="danger" style="margin-top:8px" onclick="cancelPending()">🗑 Cancelar difusiones pendientes</button>
   </div>
  </div>
  <div class="card" data-tab="ajustes" data-sub="envio"><h2>Lista del envío automático<span class="help" tabindex="0" data-tip="Cuando el envío automático está activo, cada lista capturada se envía SOLO a la lista que elijas aquí, por canal. Elige una para no enviar a 'todos' por error.">ⓘ</span></h2>
   <div class="hint">Al activar el envío automático, cada lista del canal se enviará <b>solo a la lista elegida</b> por canal. Crea listas en <b>Fuentes y listas</b>.</div>
   <div class="row" style="margin-top:10px">
     <div><label>✈️ Telegram</label><select id="auto_telegram_list" onchange="saveAutoList()"></select></div>
     <div><label>🟢 WhatsApp</label><select id="auto_whatsapp_list" onchange="saveAutoList()"></select></div>
   </div>
   <div class="hint" style="margin-top:8px">Si dejas un canal sin lista, al intentar activar el envío te pediremos elegir una (para no difundir a todos).</div>
  </div>
  <div class="card accent" data-tab="ajustes" data-sub="envio"><h2>Anti-baneo · lotes y ritmo<span class="help" tabindex="0" data-tip="Tamaño de lote (máx 150) y delays ALEATORIOS entre mensajes para no parecer spam y reducir el riesgo de baneo. Envío fraccionado = un lote a la vez.">ⓘ</span></h2>
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
  <div class="card" data-tab="ajustes" data-sub="envio"><h2>Horario de envío por canal<span class="help" tabindex="0" data-tip="Cada canal tiene su PROPIO horario, independiente. Fuera de su ventana, los lotes de ESE canal se difieren; el otro canal sigue enviando con normalidad. Apagado = 24 h. Soporta cruzar medianoche (22:00 → 06:00). La zona horaria es la de «Anti-baneo».">ⓘ</span></h2>
   <div class="hint">Telegram y WhatsApp son <b>independientes</b>: la ventana horaria de un canal no afecta al otro.</div>
   <div class="section-label" style="margin-top:12px"><svg class="ico"><use href="#i-tg"></use></svg> Telegram</div>
   <label style="display:flex;align-items:center;gap:8px;margin-top:0"><input type="checkbox" id="tg_window_enabled" style="width:auto"> Enviar Telegram solo dentro del horario</label>
   <div class="row">
     <div><label>Desde (HH:MM)</label><input id="tg_window_start" placeholder="08:00"></div>
     <div><label>Hasta (HH:MM)</label><input id="tg_window_end" placeholder="20:00"></div>
   </div>
   <div class="section-label" style="margin-top:14px"><svg class="ico"><use href="#i-wa"></use></svg> WhatsApp</div>
   <label style="display:flex;align-items:center;gap:8px;margin-top:0"><input type="checkbox" id="wa_window_enabled" style="width:auto"> Enviar WhatsApp solo dentro del horario</label>
   <div class="row">
     <div><label>Desde (HH:MM)</label><input id="wa_window_start" placeholder="08:00"></div>
     <div><label>Hasta (HH:MM)</label><input id="wa_window_end" placeholder="20:00"></div>
   </div>
   <div class="hint" style="margin-top:10px">Fuera del horario, los lotes de ese canal quedan <b>encolados</b> y salen al reabrir su ventana.</div>
   <button onclick="saveSched()">Guardar horarios</button>
  </div>
  <div class="card" data-tab="envios" data-sub="historial"><h2>📦 Envíos fraccionados<span class="help" tabindex="0" data-tip="Las difusiones grandes se dividen en lotes que salen de a uno, con pausas (anti-baneo). Aquí ves el progreso por canal; puedes cancelar o borrar planes.">ⓘ</span> <span class="live" id="pl_live" style="margin-left:auto"><span class="ping"></span><span id="pl_live_t">en vivo</span></span></h2>
   <div class="hint">De cada lote programado se muestra <b>cuántos mensajes se han enviado</b>. El sistema procesa un lote a la vez, en orden.</div>
   <div id="pl_list" style="margin-top:12px"></div>
   <div class="bc-empty" id="pl_empty" style="display:none">No hay envíos programados todavía. Crea uno en <b>Enviar</b> o espera al próximo del canal.</div>
   <div class="tbl-toolbar">
     <label class="sel-all"><input type="checkbox" id="pl_selall" onchange="plSelAll(this.checked)"> Seleccionar todos</label>
     <button class="danger" id="pl_delsel" onclick="plDeleteSelected()" disabled>🗑 Borrar seleccionados</button>
     <button class="ghost" onclick="plClearFinished()">🧹 Borrar terminados</button>
     <span class="grow"></span>
     <button class="sec" onclick="loadPlans()">Refrescar</button>
   </div>
  </div>
 </main>
</div>

<div class="toast" id="toast" role="status" aria-live="polite" aria-atomic="true"></div>
<script>
const BASE = location.pathname.replace(/\/admin.*/, '/admin');
const $ = id => document.getElementById(id);
// Iconos de marca (referencian los <symbol> del SVG). Úsalos SOLO en contextos HTML (innerHTML);
// en diálogos de texto plano (confirmModal/alert/toast) se mantienen los emojis ✈️/🟢.
const ICO_TG = '<svg class="ico"><use href="#i-tg"></use></svg>';
const ICO_WA = '<svg class="ico"><use href="#i-wa"></use></svg>';
let CRED = '';  // M17: la credencial vive SOLO en memoria (no se persiste). Recargar => re-login.
function hdr(extra){ return Object.assign({Authorization:'Basic '+CRED}, extra||{}); }
async function api(p, opt){ opt=opt||{}; opt.headers=hdr(opt.headers); const r=await fetch(BASE+p,opt);
  if(r.status===401){ logout(); throw new Error('401'); }
  if(!r.ok){ let _m=''; try{ _m=(await r.json()).error||''; }catch(e){} throw new Error(_m||('error '+r.status)); }
  return r.json(); }
function toast(m,v){ const t=$('toast'); t.textContent=m;
  const cls = v===true ? 'err' : (typeof v==='string' && v ? v : '');  // true=err (compat); 'info'/'warn'/'err'
  // A8: los errores se anuncian de inmediato (assertive); el resto, cortés (polite).
  t.setAttribute('aria-live', (v===true||cls==='err'||cls==='warn') ? 'assertive' : 'polite');
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
  try{ const r=await fetch(BASE+'/api/me',{headers:hdr()});
    if(!r.ok){ const j=await r.json().catch(()=>({})); const er=new Error(j.error||''); er.status=r.status; throw er; }
    $('login').style.display='none'; $('app').style.display='block'; $('who').textContent=u; boot(); }
  catch(e){ CRED='';
    $('lerr').textContent = (e&&e.status===429&&e.message) ? e.message : 'Usuario o contraseña incorrectos (tras varios intentos se bloquea unos minutos)'; } }
function logout(){ sessionStorage.removeItem('cred'); CRED='';
  // M39: detener TODOS los polls (si no, CONN_TIMER y otros siguen vivos tras salir).
  try{ [BC_TIMER,Q_TIMER,PL_TIMER,CONN_TIMER].forEach(t=>{ if(t) clearInterval(t); }); BC_TIMER=Q_TIMER=PL_TIMER=CONN_TIMER=null; }catch(e){}
  $('app').style.display='none'; $('login').style.display='flex'; }
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
// --- usuarios del panel (solo administradores) ---
let USR_ME='', IS_ADMIN=false;
// Carga quién soy + mi rol; muestra/oculta la gestión de usuarios según sea admin.
async function loadMe(){
  try{ const m=await api('/api/me'); USR_ME=m.user||USR_ME; IS_ADMIN=!!m.is_admin;
    const w=$('who'); if(w){ w.title=(IS_ADMIN?'Administrador':'Usuario')+' · '+(m.user||''); if(m.user) w.textContent=m.user; }  // B11: nombre visible también al recargar
    const badge=$('who_role'); if(badge){ badge.textContent=IS_ADMIN?'admin':'usuario'; badge.className='pill '+(IS_ADMIN?'active':'inactive'); badge.style.display='inline-block'; }
    const card=$('usr_card'); if(card) card.style.display=IS_ADMIN?'':'none';
    if(IS_ADMIN) loadUsers();
  }catch(e){}
}
function roleBadge(rol){ return rol==='admin'
  ? '<span class="pill active" style="padding:2px 8px">Administrador</span>'
  : '<span class="pill inactive" style="padding:2px 8px">Usuario</span>'; }
async function loadUsers(){
  if(!IS_ADMIN) return;
  { const _u=$('usr_rows'); if(_u && !_u.children.length) skelTable('usr_rows',4,3); }
  try{ const r=await api('/api/users'); USR_ME=r.me||''; const list=r.users||[];
    $('usr_n').textContent='· '+list.length;
    $('usr_rows').innerHTML=list.map(u=>{ const me=u.username===USR_ME; const rol=u.role||'admin';
      const toggle = (rol==='admin')
        ? `<button class="ghost" style="padding:4px 9px" onclick="setUserRole('${bcEsc(u.username)}','user')" title="Quitar permisos de administrador">↓ a usuario</button>`
        : `<button class="ghost" style="padding:4px 9px" onclick="setUserRole('${bcEsc(u.username)}','admin')" title="Dar permisos de administrador">↑ a admin</button>`;
      return `<tr><td><b>${bcEsc(u.username)}</b>${me?' <span class="hint">(tú)</span>':''}</td><td>${bcEsc(u.email||'—')}</td>`+
        `<td>${roleBadge(rol)}</td>`+
        `<td style="text-align:right;white-space:nowrap">${(u.username==='admin')?'<span class="hint">principal</span>':(toggle+' '+(me?'':`<button class="danger" style="padding:4px 9px" onclick="deleteUser('${bcEsc(u.username)}')">🗑</button>`))}</td></tr>`; }).join('');
  }catch(e){}
}
async function createUser(){
  const username=$('usr_new_name').value.trim(), email=$('usr_new_email').value.trim(), pw=$('usr_new_pw').value;
  const role=($('usr_new_role')&&$('usr_new_role').value)||'user';
  if(!username||!pw){ toast('Usuario y contraseña requeridos',true); return; }
  try{ const r=await fetch(BASE+'/api/users',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({username:username,email:email,password:pw,role:role})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status));
    toast('✓ Usuario creado'); $('usr_new_name').value=''; $('usr_new_email').value=''; $('usr_new_pw').value=''; if($('usr_new_role'))$('usr_new_role').value='user'; loadUsers();
  }catch(e){ toast(e.message||'Error al crear',true); }
}
async function setUserRole(u,role){
  const txt = role==='admin' ? ('¿Dar permisos de ADMINISTRADOR a "'+u+'"? Podrá gestionar usuarios.')
                             : ('¿Quitar a "'+u+'" los permisos de administrador? Pasará a usuario normal (sin gestión de usuarios).');
  if(!await confirmModal(txt,{okText:'Cambiar rol',danger:role!=='admin'})) return;
  try{ const r=await fetch(BASE+'/api/users/role',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({username:u,role:role})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status)); toast('✓ Rol actualizado'); loadUsers();
  }catch(e){ toast(e.message||'Error',true); } }
async function deleteUser(u){ if(!await confirmModal('¿Borrar el usuario "'+u+'"?',{danger:true,okText:'Borrar'})) return;
  try{ const r=await fetch(BASE+'/api/users/delete',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({username:u})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status)); toast('✓ Usuario borrado'); loadUsers();
  }catch(e){ toast(e.message||'Error',true); } }
async function changePassword(){
  const cur=$('cp_cur').value, nw=$('cp_new').value; if(!cur||!nw){ toast('Completa ambos campos',true); return; }
  if(nw.length<8){ toast('La nueva contraseña debe tener al menos 8 caracteres',true); return; }
  $('cp_status').textContent='Cambiando…';
  try{ const r=await fetch(BASE+'/api/auth/change-password',{method:'POST',headers:hdr({'Content-Type':'application/json'}),body:JSON.stringify({current:cur,new:nw})});
    const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||('error '+r.status));
    $('cp_status').textContent='✅ Contraseña cambiada'; $('cp_cur').value=''; $('cp_new').value=''; toast('✓ Contraseña cambiada');
  }catch(e){ $('cp_status').textContent=e.message||''; toast(e.message||'Error',true); }
}
let SRC_CHANNEL='';
// Badge del header con el canal fuente al que Telegram está integrado (de dónde lee las listas).
function renderTgSource(){ const e=$('conn_tg_src'); if(!e) return;
  const ch=(SRC_CHANNEL||'').replace(/^@/,'').trim();
  if(ch){ e.style.display='inline-block'; e.className='pill'; e.innerHTML=ICO_TG+' @'+bcEsc(ch); e.title='Canal fuente del que Telegram lee las listas: @'+ch; }
  else{ e.style.display='none'; } }
async function loadCfg(){ const c=await api('/api/config');
  ['source_channel','markup_percentage','currency_symbols','whatsapp_footer','image_url','telethon_api_id','telethon_api_hash'].forEach(k=>$(k).value=c[k]??'');
  SRC_CHANNEL=(c.source_channel||'').trim(); renderTgSource();
  $('send_mode').value=c.send_mode||'bot';
  $('sess_status').textContent = c.telethon_session_set ? '· conectada ✓' : '· no configurada';
  $('bot_status').textContent = c.bot_token_set ? '· configurado ✓' : '· no configurado';
  $('strip_patterns').value=(c.strip_patterns||[]).join('\n');
  $('whatsapp_enabled').checked=!!c.whatsapp_enabled; $('whatsapp_service_url').value=c.whatsapp_service_url||'';
  $('wa_tok_status').textContent = c.whatsapp_token_set ? '· configurado ✓' : '· no configurado';
  if($('wa_subs')) renderWa();
  LISTS.telegram=c.telegram_lists||[]; TGT.telegram=c.telegram_target||{mode:'all',lists:[]};
  LISTS.whatsapp=c.whatsapp_lists||[]; TGT.whatsapp=c.whatsapp_target||{mode:'all',lists:[]};
  renderLists('telegram'); renderLists('whatsapp');
  // --- anti-baneo / horario por canal ---
  ['batch_size','tg_delay_min','tg_delay_max','wa_delay_min','wa_delay_max','window_tz',
   'tg_window_start','tg_window_end','wa_window_start','wa_window_end'].forEach(k=>{ if($(k)) $(k).value=c[k]??''; });
  if($('scheduling_enabled')) $('scheduling_enabled').checked = c.scheduling_enabled!==false;
  if($('tg_window_enabled')) $('tg_window_enabled').checked = !!c.tg_window_enabled;
  if($('wa_window_enabled')) $('wa_window_enabled').checked = !!c.wa_window_enabled;
  if($('mail_from')) $('mail_from').value=c.mail_from||'';
  if($('mail_status')) $('mail_status').textContent = c.resend_api_key_set ? '· API key configurada ✓' : '· sin API key (usa SNS)';
  loadPatterns();
  autoFillLists(c);
  renderCaptureState(c.capture_enabled!==false);
  renderSendingState(c.sending_enabled!==false); }
// Selectores de LISTA para el envío automático (por canal). Se rellenan desde las listas existentes.
function autoFillLists(c){
  const fill=(sel,arr,cur)=>{ if(!sel) return;
    sel.innerHTML='<option value="">— sin lista (no auto-envía) —</option>'+(arr||[]).map(l=>`<option value="${bcEsc(l.name)}">${bcEsc(l.name)} (${(l.ids||[]).length})</option>`).join('');
    sel.value=(cur||''); };
  fill($('auto_telegram_list'),(LISTS&&LISTS.telegram)||[],c&&c.auto_telegram_list);
  fill($('auto_whatsapp_list'),(LISTS&&LISTS.whatsapp)||[],c&&c.auto_whatsapp_list);
}
async function saveAutoList(){
  const b={ auto_telegram_list:($('auto_telegram_list')?$('auto_telegram_list').value:'')||'',
            auto_whatsapp_list:($('auto_whatsapp_list')?$('auto_whatsapp_list').value:'')||'' };
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); toast('✓ Lista del envío automático guardada'); }
  catch(e){ toast('Error al guardar',true); } }
function renderCaptureState(on){
  if($('capture_enabled')) $('capture_enabled').checked = on;
  const b=$('cap_badge'); if(b){ b.className='pill '+(on?'active':'failed'); b.textContent = on?'ACTIVA':'PAUSADA'; } }
async function setCapture(on){
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capture_enabled:on})});
    renderCaptureState(on); toast(on?'✓ Recopilación ACTIVADA':'⏸ Recopilación PAUSADA — no se leerán listas nuevas', on?'info':'warn'); }
  catch(e){ toast('Error al cambiar la recopilación',true); renderCaptureState(!on); } }
function toggleCapture(){ return setCapture($('capture_enabled').checked); }
// Info de DESTINATARIOS por usuario (patrones, excepciones y exclusiones manuales, ambos canales):
// se carga/guarda en /api/patterns (registro del usuario), no en la config global. El envío real
// usa la UNIÓN de todos los usuarios; el panel muestra LO TUYO.
async function loadPatterns(){
  try{ const p=await api('/api/patterns');
    EXCL_PAT_TG=p.telegram_exclude_patterns||[]; WA_EXCL_PAT=p.whatsapp_exclude_patterns||[];
    EXCLUDED=new Set((p.excluded_ids||[]).map(String)); WA_EXCLUDED=new Set((p.whatsapp_excluded||[]).map(String));
    EXCEPT_TG=new Set((p.telegram_pattern_exceptions||[]).map(String)); WA_EXCEPT=new Set((p.whatsapp_pattern_exceptions||[]).map(String));
    if($('tg_excl_pat')) $('tg_excl_pat').value=EXCL_PAT_TG.join('\n');
    if($('wa_excl_pat')) $('wa_excl_pat').value=WA_EXCL_PAT.join('\n');
    try{render();}catch(e){} try{renderWa();}catch(e){}
  }catch(e){}
}
async function saveExclPatterns(canal){
  const id = canal==='telegram' ? 'tg_excl_pat' : 'wa_excl_pat';
  const lines = ($(id).value||'').split('\n').map(s=>s.trim()).filter(Boolean);
  try{ await api('/api/patterns',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({[canal+'_exclude_patterns']:lines})});
    toast('✓ Tus patrones se guardaron');
    if(canal==='telegram'){ EXCL_PAT_TG=lines; try{render();}catch(e){} } else { WA_EXCL_PAT=lines; try{renderWa();}catch(e){} } }
  catch(e){ toast('Error al guardar',true); } }
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
      hb.title='Envíos AUTOMÁTICOS en pausa (puedes seguir enviando manualmente) — clic para activarlos'; hb.textContent='⏸ Auto en pausa · activar'; hb.onclick=enableSending; }
  }
  const sb=$('send_banner'); if(sb){ sb.hidden=false;
    if(on){ sb.className='active'; sb.innerHTML='<span class="sb-dot"></span><span class="sb-txt"><b>Envíos automáticos activos.</b> Cada lista nueva del canal se envía a la lista elegida por canal.</span><button class="sb-pause" onclick="setSending(false)">Pausar automáticos</button>'; }
    else { sb.className='paused'; sb.innerHTML='<span class="sb-dot"></span><span class="sb-txt"><b>⏸ Envíos AUTOMÁTICOS en pausa.</b> Las listas capturadas solo se VEN (en Envíos y en tus Mensajes Guardados), no se envían. <b>Tú sí puedes enviar manualmente</b> desde «Componer → Enviar en el momento».</span><button class="sb-go" onclick="enableSending()">▶ Activar automáticos</button>'; }
  }
}
async function pendingSummary(){
  try{ const r=await api('/api/plans'); let planes=0,envios=0; const items=[];
    (r.plans||[]).forEach(p=>{ if(p.status==='pending'||p.status==='running'){
      const tg=(p.tg&&p.tg.total)|0, wa=(p.wa&&p.wa.total)|0, n=tg+wa; planes++; envios+=n;
      const ch=((tg?'✈️':'')+(wa?'🟢':''))||'·';
      items.push({ch,n,text:((p.text||'').trim()||'(solo imagen)').replace(/\s+/g,' ').slice(0,60)});
    }});
    return {planes,envios,items};
  }catch(e){ return {planes:0,envios:0,items:[]}; }
}
async function setSending(on){
  if(!on){ if(!(await confirmModal('¿Pausar el ENVÍO automático? Las listas que se capturen quedarán solo VISIBLES (en Envíos y en tus Mensajes Guardados), sin enviarse a nadie. Podrás seguir enviando manualmente desde «Componer → Enviar en el momento».',{okText:'Pausar automáticos',danger:true}))){ renderSendingState(true); return; } }
  else {
    toast('Calculando a quién se enviará…','info');
    const c=await api('/api/config');
    // OBLIGATORIO elegir lista para el envío automático (evita difundir a TODOS por error).
    const faltan=[];
    if(!c.auto_telegram_list) faltan.push('✈️ Telegram');
    if(c.whatsapp_enabled && !c.auto_whatsapp_list) faltan.push('🟢 WhatsApp');
    if(faltan.length){
      await confirmModal('Antes de activar el ENVÍO automático, elige una LISTA para: '+faltan.join(' y ')+'.\n\nVe a Ajustes → «Lista del envío automático» y elige una lista por canal (así no se envía a TODOS por error).',{okText:'Entendido',noCancel:true});
      renderSendingState(false); return;
    }
    const ps=await pendingSummary();
    const {waOk}=await ensureContactsLoaded();
    const autoT=c.auto_telegram_list?{mode:'only',lists:[c.auto_telegram_list]}:(TGT.telegram||{mode:'all',lists:[]});
    const autoW=c.auto_whatsapp_list?{mode:'only',lists:[c.auto_whatsapp_list]}:(TGT.whatsapp||{mode:'all',lists:[]});
    const tg=audienceFor('telegram',autoT), wa=audienceFor('whatsapp',autoW);
    const fmt=(emoji,canal,a,ok)=>{ if(ok===false) return emoji+' '+canal+': no se pudo calcular (abre Fuentes → '+canal+' para ver el detalle).';
      const MAX=12, lista=a.names.slice(0,MAX).map(x=>'   • '+x).join('\n'), mas=a.names.length>MAX?('\n   …y '+(a.names.length-MAX)+' más'):'';
      return emoji+' '+canal+' ('+a.mode+') — '+a.total+' contacto(s)'+(a.total?':\n'+lista+mas:' (nadie)'); };
    let msg='Al ACTIVAR, cada lista NUEVA del canal se enviará SOLO a la lista elegida por canal:\n\n'
      + fmt('✈️','Telegram',tg,true) + '\n\n' + fmt('🟢','WhatsApp',wa,waOk);
    if(ps.planes>0){
      const ne = ps.envios>0 ? (' (~'+ps.envios.toLocaleString('es')+' envíos)') : '';
      msg += '\n\n⚠️ Además hay '+ps.planes+' difusión(es) EN COLA'+ne+' que saldrán al activar (gestiónalas en «Envíos fraccionados»).';
    }
    msg += '\n\n¿Activar los envíos automáticos?';
    if(!(await confirmModal(msg,{okText:'Activar envíos',danger:ps.planes>0}))){ renderSendingState(false); return; }
  }
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sending_enabled:on})});
    renderSendingState(on); toast(on?'✓ Envíos automáticos ACTIVADOS':'⏸ Automáticos en pausa — el envío manual sigue activo', on?'info':'warn');
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
  const HHMM=/^([01]?\d|2[0-3]):[0-5]\d$/;
  const tw=$('tg_window_enabled').checked, ww=$('wa_window_enabled').checked;
  const tgs=($('tg_window_start').value||'').trim(), tge=($('tg_window_end').value||'').trim();
  const was=($('wa_window_start').value||'').trim(), wae=($('wa_window_end').value||'').trim();
  // A4: una hora mal escrita ya no se acepta silenciosamente (antes caía a ventana 24h).
  if(tw && (!HHMM.test(tgs)||!HHMM.test(tge))){ toast('Horario de Telegram inválido — usa HH:MM (p. ej. 08:00)',true); return; }
  if(ww && (!HHMM.test(was)||!HHMM.test(wae))){ toast('Horario de WhatsApp inválido — usa HH:MM (p. ej. 08:00)',true); return; }
  // M11: el lote vacío ya no se coerce silenciosamente a 150 (el valor más agresivo).
  const bs=parseInt($('batch_size').value,10);
  if(!Number.isFinite(bs)||bs<1||bs>150){ toast('Tamaño de lote inválido (1–150)',true); return; }
  const tdmin=parseFloat($('tg_delay_min').value), tdmax=parseFloat($('tg_delay_max').value);
  const wdmin=parseInt($('wa_delay_min').value,10), wdmax=parseInt($('wa_delay_max').value,10);
  if([tdmin,tdmax].some(x=>!Number.isFinite(x)||x<0)){ toast('Delays de Telegram inválidos',true); return; }
  if([wdmin,wdmax].some(x=>!Number.isFinite(x)||x<0)){ toast('Delays de WhatsApp inválidos',true); return; }
  // M12: el delay mínimo no puede ser mayor que el máximo.
  if(tdmin>tdmax || wdmin>wdmax){ toast('El delay mínimo no puede ser mayor que el máximo',true); return; }
  const b={ scheduling_enabled:$('scheduling_enabled').checked,
    batch_size:bs, tg_delay_min:tdmin, tg_delay_max:tdmax, wa_delay_min:wdmin, wa_delay_max:wdmax,
    window_tz:parseInt($('window_tz').value||'-300',10),
    // Ventana horaria INDEPENDIENTE por canal.
    tg_window_enabled:tw, tg_window_start:tgs||'08:00', tg_window_end:tge||'20:00',
    wa_window_enabled:ww, wa_window_start:was||'08:00', wa_window_end:wae||'20:00' };
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); toast('✓ Guardado'); loadCfg(); }
  catch(e){ toast('Error al guardar',true); } }
async function saveWhatsapp(){ const b={ whatsapp_enabled:$('whatsapp_enabled').checked, whatsapp_service_url:($('whatsapp_service_url').value||'').trim() };
   const tok=$('whatsapp_token').value; if(tok) b.whatsapp_token=tok;
   // B9: no activar el reenvío sin URL del servicio (quedaría "activado" pero inoperativo).
   if(b.whatsapp_enabled && !b.whatsapp_service_url){ toast('Pon la URL del servicio de WhatsApp antes de activar el reenvío',true); return; }
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    toast('✓ WhatsApp guardado'); $('whatsapp_token').value=''; loadCfg(); } catch(e){ toast(e.message||'No se pudo guardar WhatsApp',true); } }
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
    if(r.pairingCode){ out.innerHTML='Código: <b style="font-size:19px;letter-spacing:3px;color:var(--ac2)">'+bcEsc(r.pairingCode)+'</b><br>En el teléfono: WhatsApp → Dispositivos vinculados → <b>Vincular con número de teléfono</b> → ingresa el código.'; }
    else { out.textContent='No se pudo generar: '+(r.error||r.detalle||'desconocido'); } }
  catch(e){ out.textContent='Error: el servicio no respondió (¿ya conectado? ¿URL/token?)'; } }
async function saveAccount(){ const b={ send_mode:$('send_mode').value, telethon_api_id:$('telethon_api_id').value,
   telethon_api_hash:$('telethon_api_hash').value, telethon_session:$('telethon_session').value };
  const bt=$('bot_token').value.trim(); if(bt) b.bot_token=bt;
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    toast('✓ Cuenta guardada'); $('telethon_session').value=''; $('bot_token').value=''; loadCfg(); loadSubs(); } catch(e){ toast(e.message||'No se pudo guardar la cuenta de Telegram',true); } }
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
async function saveCfg(){
   const mp=parseFloat($('markup_percentage').value);   // B31: no enviar NaN si el campo está vacío/mal
   if(!Number.isFinite(mp)||mp<0){ toast('Markup inválido (usa un número, p. ej. 15)',true); return; }
   const b={ source_channel:$('source_channel').value, markup_percentage:mp,
   currency_symbols:$('currency_symbols').value, whatsapp_footer:$('whatsapp_footer').value, image_url:$('image_url').value,
   // M41: strip_patterns debe viajar como ARRAY (el backend lo lee como lista), no como string crudo.
   strip_patterns:($('strip_patterns').value||'').split('\n').map(s=>s.trim()).filter(Boolean) };
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
      pend += Math.max(0,((p.tg&&p.tg.batches)|0)-((p.tg&&p.tg.next)|0)) + Math.max(0,((p.wa&&p.wa.batches)|0)-((p.wa&&p.wa.next)|0)); }});
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
  try{ const r=await api('/api/dlq/redrive',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); toast(r&&r.detalle? ('ℹ '+r.detalle) : '✓ Reintento iniciado'); setTimeout(()=>{ loadDlq(); loadQueue(); if($('k_dlq')) loadDashboard(); },1500); }  // M21+M24
  catch(e){ toast(e.message||'No se pudo reintentar (¿ya hay un reintento en curso?)',true); }
}
async function dlqPurge(){
  if(!await confirmModal('¿Descartar TODOS los mensajes fallidos? No se podrán recuperar.',{danger:true,okText:'Descartar'})) return;
  try{ await api('/api/dlq/purge',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); toast('✓ DLQ descartada'); setTimeout(()=>{ loadDlq(); loadQueue(); if($('k_dlq')) loadDashboard(); },1500); }  // M21
  catch(e){ toast('Error al descartar',true); }
}
// --- Opt-out WhatsApp: contactos auto-excluidos por fallos ---
async function loadBlocked(){
  try{ const r=await api('/api/whatsapp/blocked'); $('wa_blk_n').textContent='· '+(r.total||0)+' (umbral '+(r.umbral||3)+')';
    const b=r.blocked||[]; if($('wa_blk_list')) $('wa_blk_list').innerHTML = b.length? b.map(x=>bcEsc(x.name)+' 📞 '+bcEsc(waNum(x))+' — '+x.fallos+' fallos').join('<br>') : 'Ninguno por ahora.';
  }catch(e){ if($('wa_blk_n')) $('wa_blk_n').textContent='· servicio inaccesible'; if($('wa_blk_list')) $('wa_blk_list').textContent='—'; }
}
async function clearBlocked(){
  if(!await confirmModal('¿Reincluir a TODOS los auto-excluidos? Volverán a recibir envíos.',{okText:'Reincluir'})) return;
  try{ await api('/api/whatsapp/blocked/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); toast('✓ Reincluidos'); loadBlocked(); }
  catch(e){ toast('No se pudieron reincluir los contactos',true); }
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
// Estado de SINCRONIZACIÓN de cada cuenta. Si la cuenta NO está sincronizada/conectada, las
// pantallas NO muestran contactos cacheados viejos (solo las LISTAS persisten). null = sin verificar.
let TG_STALE=false, WA_STALE=false;
function syncReRender(){ // refresca las vistas de contactos según el estado de sincronización
  try{ if(typeof render==='function' && $('subs')) render(); }catch(e){}
  try{ if(typeof renderWa==='function' && $('wa_subs')) renderWa(); }catch(e){}
  try{ if(typeof bcRenderPick==='function'){ bcRenderPick('tg'); bcRenderPick('wa'); } }catch(e){}
}
async function refreshConn(){
  const tg=$('conn_tg'), wa=$('conn_wa');
  try{ const a=await api('/api/telegram/account');
    // userbot con sesión caída (connected===false) => contactos cacheados VIEJOS (no sincronizado).
    // En modo bot los suscriptores no son caché de sesión (vienen de /start) => nunca "viejos".
    TG_STALE = (a.mode==='userbot' && a.connected===false);
    if(tg){ tg.onclick=null; tg.style.cursor='';
      const irCuenta=()=>{ showTab('ajustes'); try{ showSub('ajustes','telegram'); }catch(e){} };
      if(a.mode==='userbot'){
        const me=a.me||{}; const phone=me.phone?('+'+String(me.phone).replace(/^\+/,'')):''; const who=phone||me.name||'cuenta';
        if(a.connected===true){ tg.className='pill active'; tg.innerHTML=ICO_TG+' '+bcEsc(who)+' ✓';
          tg.title='Cuenta de Telegram '+(me.name||'')+(phone?(' ('+phone+')'):'')+' · sesión válida ✓'; }
        else if(a.connected===false){ tg.className='pill failed'; tg.style.cursor='pointer'; tg.innerHTML=ICO_TG+' Telegram: renovar ✕';
          tg.title='La sesión de Telegram caducó o se revocó — clic para volver a conectar la cuenta (Ajustes → Cuenta de Telegram)';
          tg.onclick=irCuenta; }
        else { tg.className='pill inactive'; tg.innerHTML=ICO_TG+' Telegram ?'; tg.title='No se pudo verificar la sesión de Telegram ahora mismo'; }
      } else {
        const me=a.me||{}; const uname=me.username?('@'+me.username):'bot'; const id=me.id?(' · '+me.id):'';
        if(a.connected){ tg.className='pill active'; tg.innerHTML=ICO_TG+' '+bcEsc(uname+id);
          tg.title='Bot de Telegram: '+(me.name||'')+' '+uname+(me.id?(' · ID '+me.id):''); }
        else { tg.className='pill failed'; tg.style.cursor='pointer'; tg.innerHTML=ICO_TG+' bot ✕';
          tg.title='El bot de Telegram no responde (¿token?) — clic para revisar'; tg.onclick=irCuenta; }
      }
    } }
  catch(e){ if(tg){ tg.className='pill failed'; tg.innerHTML=ICO_TG+' ✕'; tg.onclick=null; } }
  try{ const s=await api('/api/whatsapp/status'); const ok=s&&s.connected;
    WA_STALE = !ok;  // WhatsApp no conectado => sus contactos cacheados están viejos (no sincronizado)
    if(wa){ const num=(ok&&s.me&&s.me.id)?('+'+String(s.me.id).split('@')[0].split(':')[0]):'';
      wa.className='pill '+(ok?'active':'failed'); wa.innerHTML=ok?(ICO_WA+' '+bcEsc(num||'WhatsApp')):(ICO_WA+' WA ✕');
      wa.title=ok?((num?('WhatsApp '+num):'WhatsApp conectado')+(s.contacts?(' · '+s.contacts+' contactos'):'')):('desconectado'+(s.lastCloseMsg?(' · '+s.lastCloseMsg):'')); } }
  catch(e){ WA_STALE=true; if(wa){ wa.className='pill inactive'; wa.innerHTML=ICO_WA+' WA ?'; wa.title='servicio no configurado o inaccesible'; } }
  syncReRender();  // refleja el estado de sincronización en las pantallas de contactos
}
let CONN_TIMER=null;
function connStartPolling(){ if(CONN_TIMER) return; refreshConn();
  CONN_TIMER=setInterval(()=>{ if(CRED && !document.hidden) refreshConn(); }, 60000); }
// --- Expiración de sesión (cliente): re-login tras 8h o inactividad larga ---
const SESSION_MAX_MS=8*3600*1000;
function sessionFresca(){ try{ const t=parseInt(sessionStorage.getItem('cred_ts')||'0',10); return t && (Date.now()-t)<SESSION_MAX_MS; }catch(e){ return true; } }
// --- Onboarding: checklist de primeros pasos (desde la config) ---
function goStep(tab,sub){ showTab(tab); if(sub){ try{ showSub(tab,sub); }catch(e){} } }
function renderSteps(c){
  const steps=[
    {ok: !!(c.bot_token_set||c.telethon_session_set), ic:ICO_TG, t:'Conectar Telegram',
     d:'Bot (a suscriptores) o tu cuenta (userbot, a tus contactos) para poder enviar.', tab:'ajustes', sub:'telegram'},
    {ok: !!(c.source_channel&&String(c.source_channel).trim()), ic:ICO_TG, t:'Definir el canal fuente',
     d:'El canal de Telegram del que se leen las listas a reenviar.', tab:'fuentes', sub:'fuente'},
    {ok: ((c.telegram_lists||[]).length>0 || (c.whatsapp_lists||[]).length>0), t:'Crear listas o elegir destinatarios',
     d:'Agrupa contactos en listas y define a quién se envía en cada canal.', tab:'fuentes', sub:'tg'},
    {ok: !!c.whatsapp_enabled, ic:ICO_WA, t:'Conectar WhatsApp',
     d:'Opcional: reenvía también por WhatsApp (vincula desde tu IP residencial).', tab:'ajustes', sub:'whatsapp', opt:true},
    {ok: c.sending_enabled!==false, t:'Activar los envíos',
     d:'Enciende el interruptor para que salgan los envíos automáticos.', tab:'ajustes', sub:'envio'},
  ];
  const done=steps.filter(s=>s.ok).length, total=steps.length, pct=Math.round(done/total*100);
  const reqDone=steps.filter(s=>!s.opt).every(s=>s.ok);
  if($('steps_n')) $('steps_n').textContent='· '+done+'/'+total+(done===total?' ✓':'');
  if($('dash_steps')) $('dash_steps').innerHTML=
    `<div class="steps-bar"><i style="width:${pct}%"></i></div>`+
    (reqDone?'<div class="callout ok" style="margin:8px 0 4px">🎉 ¡Listo para enviar! Los pasos opcionales amplían el alcance.</div>':'')+
    steps.map(s=>
      `<div class="step${s.ok?' done':''}">`+
      `<span class="step-ic">${s.ok?'✅':(s.opt?'⚪':'⬜')}</span>`+
      `<span class="step-tx"><span class="step-t">${s.ic?(s.ic+' '):''}${s.t}${s.opt?' <span class="hint">(opcional)</span>':''}</span>`+
      `<span class="hint">${s.d}</span></span>`+
      `<button class="${s.ok?'ghost':'sec'}" style="padding:5px 11px;flex:none" onclick="goStep('${s.tab}','${s.sub||''}')">${s.ok?'Revisar':'Ir →'}</button>`+
      `</div>`).join('');
}
// --- Dashboard / Inicio (KPIs + estado de un vistazo) ---
async function loadDashboard(){
  ['k_sent','k_rate','k_pend','k_dlq'].forEach(id=>{const e=$(id); if(e) e.classList.add('kpi-load');});
  try{
    const [m,c,pl,q]=await Promise.all([api('/api/metrics'),api('/api/config'),api('/api/plans'),api('/api/queue')]);
    if($('k_sent')) $('k_sent').textContent=(m.enviados!=null?m.enviados:'–');
    if($('k_rate')) $('k_rate').textContent=(m.tasa_exito!=null?m.tasa_exito:100)+'%';
    let pend=0; (pl.plans||[]).forEach(p=>{ if(p.status==='pending'||p.status==='running') pend+=Math.max(0,((p.tg&&p.tg.batches)|0)-((p.tg&&p.tg.next)|0))+Math.max(0,((p.wa&&p.wa.batches)|0)-((p.wa&&p.wa.next)|0)); });
    if($('k_pend')) $('k_pend').textContent=pend;
    if($('k_dlq')) $('k_dlq').textContent=q.dlq;
    ['k_sent','k_rate','k_pend','k_dlq'].forEach(id=>{const e=$(id); if(e) e.classList.remove('kpi-load');});
    renderSteps(c);
    const on=c.sending_enabled!==false; const de=$('dash_estado');
    renderSendingState(on);
    if(de){ de.className='callout '+(on?'ok':'warn'); const win=(en,a,b)=>en?(a+'–'+b):'24h';
      de.innerHTML='Envíos: <b>'+(on?'ACTIVOS':'PAUSADOS')+'</b> · '+ICO_TG+' '+win(c.tg_window_enabled,c.tg_window_start,c.tg_window_end)+' · '+ICO_WA+' '+win(c.wa_window_enabled,c.wa_window_start,c.wa_window_end)+' · WhatsApp '+(c.whatsapp_enabled?'activo':'desactivado')+' · lote '+(c.batch_size|0); }
    const s=(m.serie||[]).slice(-14); const max=Math.max(1,...s.map(d=>(d.sent|0)+(d.failed|0)));
    if($('dash_serie')) $('dash_serie').innerHTML='<div class="hint" style="margin-top:0">Días con actividad (de los últimos 30; cada barra es un día con envíos)</div>'+
      '<div style="display:flex;gap:3px;align-items:flex-end;height:56px;margin-top:6px">'+
      (s.length? s.map(d=>{ const se=(d.sent|0), fa=(d.failed|0), tot=se+fa, h=Math.round(tot/max*100), fp=tot?Math.round(fa/tot*100):0;
        return `<div title="${d.dia}: ${se} enviados, ${fa} fallidos" style="flex:1;height:${h}%;min-height:2px;display:flex;flex-direction:column;justify-content:flex-end;border-radius:3px 3px 0 0;overflow:hidden">`+
          (fa?`<div style="height:${fp}%;background:var(--bad)"></div>`:'')+
          `<div style="flex:1;background:linear-gradient(180deg,var(--ac),var(--ac2))"></div></div>`; }).join('') : '<div class="hint">sin actividad aún</div>')+'</div>'+
      '<div class="hint" style="margin-top:4px;font-size:11px"><span style="color:var(--ac)">■</span> enviados · <span style="color:var(--bad)">■</span> fallidos</div>';
    try{ const last=((await api('/api/broadcasts')).broadcasts||[])[0];
      if($('dash_last')) $('dash_last').innerHTML = last? ('Último envío: <b>'+bcEsc((last.text||'(imagen)').slice(0,48))+'</b> — '+(BC_STATUS[last.status]||last.status)+' · '+bcFmtTime(last.created_at)) : 'Aún no hay envíos.'; }catch(e){}
  }catch(e){ if($('dash_estado')) $('dash_estado').textContent='no se pudo cargar el resumen';
    ['k_sent','k_rate','k_pend','k_dlq'].forEach(id=>{const e2=$(id); if(e2) e2.classList.remove('kpi-load');}); }
}
let EXCLUDED=new Set(), DEST=[], FILTER='', PAGE=0, STATEF='', EXCL_PAT_TG=[], EXCEPT_TG=new Set();
const PAGE_SIZE=50;
function nameMatchesPatterns(name, pats){ const n=String(name||'').toLowerCase();
  return (pats||[]).some(p=>{ p=String(p).trim().toLowerCase(); return p && n.includes(p); }); }
// Excluido = manual (excluded_ids) O (coincide patrón Y NO tiene excepción manual).
function isExcludedTg(s){ const id=String(s.chatId);
  return EXCLUDED.has(id) || (nameMatchesPatterns(s.name, EXCL_PAT_TG) && !EXCEPT_TG.has(id)); }
function filtered(){ let arr=DEST;
  if(FILTER){ const q=FILTER.toLowerCase();
    arr=arr.filter(s=> (s.name||'').toLowerCase().includes(q) || String(s.chatId||'').toLowerCase().includes(q) || String(s.phone||'').toLowerCase().includes(q)); }
  if(STATEF==='inc') arr=arr.filter(s=>!isExcludedTg(s));
  else if(STATEF==='exc') arr=arr.filter(s=>isExcludedTg(s));
  return arr; }
function setStateFilter(v){ STATEF=v; PAGE=0;
  document.querySelectorAll('#seg_tg button').forEach(b=>{ const on=b.dataset.v===v; b.classList.toggle('on',on); b.classList.toggle('exc',on&&v==='exc'); b.setAttribute('aria-pressed',on?'true':'false'); });
  render(); }
function render(){
  // Si la cuenta de Telegram NO está sincronizada (userbot con sesión caída), NO mostramos
  // contactos cacheados viejos. Las LISTAS sí persisten (más abajo, no dependen de esto).
  if(TG_STALE){
    const t=$('subs'); if(t) t.innerHTML=''; if($('selall')) $('selall').checked=false;
    if($('subcount')) $('subcount').textContent='· cuenta no sincronizada';
    if($('pageinfo')) $('pageinfo').textContent='';
    const e=$('subsempty'); if(e){ e.style.display='block';
      e.innerHTML='⚠️ La cuenta de Telegram no está conectada/sincronizada. Conéctala (Ajustes → Cuenta de Telegram) para ver tus contactos. <b>Tus listas se conservan</b> y se reusarán al reconectar.'; }
    return;
  }
  { const e=$('subsempty'); if(e) e.innerHTML='Sin destinatarios (modo bot: nadie dio /start; modo userbot: la cuenta no tiene contactos).'; }
  const f=filtered(); const pages=Math.max(1,Math.ceil(f.length/PAGE_SIZE));
  if(PAGE>=pages) PAGE=pages-1; if(PAGE<0) PAGE=0;
  const slice=f.slice(PAGE*PAGE_SIZE,(PAGE+1)*PAGE_SIZE);
  const t=$('subs'); t.innerHTML=''; $('selall').checked=false;
  $('subsempty').style.display=DEST.length?'none':'block';
  const exCount=DEST.filter(isExcludedTg).length; const inc=DEST.length-exCount;
  $('subcount').textContent = DEST.length ? `· ${f.length} en vista · ${inc} incluidos · ${exCount} excluidos` : '';
  $('pageinfo').textContent = f.length ? `página ${PAGE+1} de ${pages}` : 'sin resultados';
  slice.forEach(s=>{ const id=String(s.chatId); const exM=EXCLUDED.has(id); const matchP=nameMatchesPatterns(s.name, EXCL_PAT_TG);
    const exP=!exM && matchP && !EXCEPT_TG.has(id); const exc=!exM && matchP && EXCEPT_TG.has(id);
    const label=s.name||'(sin nombre)'; const tr=document.createElement('tr');
    const pill = exM?'<span class="pill inactive">Excluido</span>':(exP?'<span class="pill pat">Excluido · patrón</span>':(exc?'<span class="pill exc-ok">Incluido · excepción</span>':'<span class="pill active">Incluido</span>'));
    tr.innerHTML=`<td><input type="checkbox" class="selrow" data-id="${s.chatId}"></td>`+
      `<td><b>${bcEsc(label)}</b><div class="hint" style="margin-top:2px;font-size:11px">${bcEsc(s.phone||s.chatId||'')}</div></td>`+
      `<td>${pill}</td>`;
    t.appendChild(tr); }); }
async function loadSubs(){ const d=await api('/api/subscribers');
  DEST=d.subscribers||[];
  loadPatterns(); render(); }  // EXCLUDED/EXCEPT/patrones son por-usuario (loadPatterns vía /api/patterns)
async function tgRefreshContacts(){
  toast('Actualizando contactos de Telegram (en vivo)…','info');
  try{ const r=await api('/api/telethon/refresh-contacts',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    toast('✓ '+(r.count||0)+' contactos actualizados'); loadSubs(); }
  catch(e){ toast(e.message||'No se pudo actualizar',true); } }
function onSearch(){ FILTER=$('subsearch').value.trim(); PAGE=0; render(); }
function prevPage(){ PAGE--; render(); }
function nextPage(){ PAGE++; render(); }
function toggleAll(v){ document.querySelectorAll('.selrow').forEach(c=>c.checked=v); $('selall').checked=v; }
function selectedIds(){ return [...document.querySelectorAll('.selrow:checked')].map(c=>String(c.dataset.id)); }
async function persistExcluded(){ // B6: si falla, re-sincroniza desde el servidor (no dejar estado local divergente) y devuelve false
  try{ await api('/api/patterns',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({excluded_ids:[...EXCLUDED],telegram_pattern_exceptions:[...EXCEPT_TG]})}); render(); return true; }
  catch(e){ toast('No se pudo guardar — recargado desde el servidor',true); await loadPatterns(); return false; } }
// Incluir un id que coincide con un patrón crea una EXCEPCIÓN (se envía pese al patrón).
function tgInclExcl(id,accion){
  if(accion==='excluir'){ EXCLUDED.add(id); EXCEPT_TG.delete(id); }
  else { EXCLUDED.delete(id);
    const s=DEST.find(x=>String(x.chatId)===id);
    if(s && nameMatchesPatterns(s.name, EXCL_PAT_TG)) EXCEPT_TG.add(id); else EXCEPT_TG.delete(id); }
}
async function bulk(accion){ const ids=selectedIds(); if(!ids.length){ toast('Marca al menos un contacto (visible)',true); return; }
  ids.forEach(id=>tgInclExcl(id,accion));
  if(await persistExcluded()) toast('✓ '+ids.length+' '+(accion==='excluir'?'excluidos':'incluidos')); }
async function bulkFiltered(accion){ const ids=filtered().map(s=>String(s.chatId)); if(!ids.length){ toast('Sin contactos que coincidan',true); return; }
  ids.forEach(id=>tgInclExcl(id,accion));
  if(await persistExcluded()) toast('✓ '+ids.length+((FILTER||STATEF)?' filtrados ':' ')+(accion==='excluir'?'excluidos':'incluidos')); }
// --- listas de distribución (genérico para ambos canales) ---
let LISTS={telegram:[],whatsapp:[]}, TGT={telegram:{mode:'all',lists:[]},whatsapp:{mode:'all',lists:[]}};
function listsBox(ch){ return ch==='telegram'?'tg_lists':'wa_lists'; }
function selForChannel(ch){ return ch==='telegram'?selectedIds():waSelectedIds(); }
function renderLists(ch){ const cont=$(listsBox(ch)); cont.innerHTML='';
  const active=new Set((TGT[ch]||{}).lists||[]);
  if(!LISTS[ch].length){ cont.innerHTML='<div class="hint">Sin listas todavía.</div>'; }
  LISTS[ch].forEach((l,i)=>{ const row=document.createElement('div');
    row.style.cssText='display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid var(--bd)';
    row.innerHTML=`<label style="display:inline-flex;align-items:center;gap:6px;width:auto;margin:0"><input type="checkbox" ${active.has(l.name)?'checked':''} style="width:auto" onchange="toggleListActive('${ch}',${i},this.checked)"> <b>${bcEsc(l.name)}</b></label>`+
      `<button class="ghost" style="padding:3px 9px" onclick="listMembers('${ch}',${i})" title="Ver/editar miembros">${l.ids.length} miembros ›</button>`+
      `<button class="sec" onclick="addToList('${ch}',${i})">+ marcados</button>`+
      `<button class="ghost" onclick="removeFromList('${ch}',${i})">− marcados</button>`+
      `<button class="ghost" onclick="delList('${ch}',${i})">🗑</button>`;
    cont.appendChild(row); });
  document.querySelectorAll(`input[name=mode_${ch}]`).forEach(r=>r.checked=(r.value===((TGT[ch]||{}).mode||'all'))); }
// Persiste listas/targets SIN recargar (loadCfg reemplazaría los objetos LISTS y rompería referencias en modales abiertos).
async function persistListsQuiet(ch){ const body=ch==='telegram'?{telegram_lists:LISTS.telegram,telegram_target:TGT.telegram}:{whatsapp_lists:LISTS.whatsapp,whatsapp_target:TGT.whatsapp};
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); return true; }catch(e){ toast('No se pudo guardar',true); return false; } }
// B5: ver/editar los miembros de una lista (resuelve nombres contra los contactos; marca y limpia huérfanos).
function listMembers(ch,i){
  const l=(LISTS[ch]||[])[i]; if(!l) return;
  const dest=(ch==='telegram'?DEST:WA_DEST)||[];
  const stale=(ch==='telegram'?TG_STALE:WA_STALE);
  const idOf=ch==='telegram'?(x=>String(x.chatId)):(x=>String(x.id||''));
  const nameOf=ch==='telegram'?(x=>x.name||'(sin nombre)'):(x=>waName(x));
  const numOf=ch==='telegram'?(x=>String(x.phone||x.chatId||'')):(x=>waNum(x));
  const byId={}; dest.forEach(x=>{ byId[idOf(x)]=x; });
  const ov=document.createElement('div'); ov.className='ds-overlay';
  const d=document.createElement('div'); d.className='ds-modal'; d.setAttribute('role','dialog'); d.setAttribute('aria-modal','true');
  const head=()=>'Miembros de "'+bcEsc(l.name)+'" ('+(l.ids||[]).length+')';
  const rows=()=> (l.ids||[]).length ? (l.ids||[]).map(id=>{ const sid=String(id); const c=byId[sid];
      const nm = c ? bcEsc(nameOf(c)) : (bcEsc(sid)+(stale?'':' <span style="color:var(--bad)">(ya no en contactos)</span>'));
      const num = c ? bcEsc(numOf(c)) : '';
      return '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--bd)"><span style="flex:1;min-width:0">'+nm+(num?(' <span class="hint">📞 '+num+'</span>'):'')+'</span><button class="ghost" style="padding:3px 9px;flex:none" data-rm="'+bcEsc(sid)+'">quitar</button></div>'; }).join('')
    : '<div class="hint">Lista vacía. Marca contactos arriba y pulsa «+ marcados».</div>';
  d.innerHTML='<h3 id="lm_h">'+head()+'</h3>'+
    (stale?'<div class="hint" style="margin-bottom:6px">Cuenta no sincronizada: se muestran los ids guardados (sin nombres).</div>':'')+
    '<div class="ds-modal-body" id="lm_body">'+rows()+'</div>'+
    '<div class="ds-modal-actions"><button class="ghost" data-a="orphan">Limpiar huérfanos</button><button data-a="close">Cerrar</button></div>';
  ov.appendChild(d); document.body.appendChild(ov);
  const refresh=()=>{ const b=$('lm_body'); if(b) b.innerHTML=rows(); const h=$('lm_h'); if(h) h.textContent=head(); renderLists(ch); };
  const close=()=>{ document.removeEventListener('keydown',onKey); ov.remove(); };
  function onKey(e){ if(e.key==='Escape') close(); }
  document.addEventListener('keydown',onKey);
  ov.addEventListener('mousedown',e=>{ if(e.target===ov) close(); });
  d.addEventListener('click', async e=>{ const t=e.target;
    const rm=t.getAttribute&&t.getAttribute('data-rm');
    if(rm){ l.ids=(l.ids||[]).filter(x=>String(x)!==rm); refresh(); await persistListsQuiet(ch); return; }
    const a=t.getAttribute&&t.getAttribute('data-a');
    if(a==='close'){ close(); return; }
    if(a==='orphan'){ if(stale){ toast('Conecta la cuenta para limpiar huérfanos',true); return; }
      const before=(l.ids||[]).length; l.ids=(l.ids||[]).filter(id=>byId[String(id)]); const removed=before-l.ids.length;
      if(removed){ refresh(); await persistListsQuiet(ch); toast('✓ '+removed+' huérfano(s) quitado(s)'); } else toast('No hay huérfanos'); }
  });
}
function addList(ch){ const inp=$(ch==='telegram'?'tg_newlist':'wa_newlist'); const n=inp.value.trim(); if(!n)return;
  if(LISTS[ch].some(l=>l.name===n)){ toast('Ya existe una lista con ese nombre',true); return; }
  LISTS[ch].push({name:n,ids:[]}); inp.value=''; renderLists(ch); }
async function delList(ch,i){ const n=LISTS[ch][i].name; if(!await confirmModal('¿Borrar la lista "'+n+'"?',{danger:true,okText:'Borrar'}))return;
  LISTS[ch].splice(i,1); TGT[ch].lists=(TGT[ch].lists||[]).filter(x=>x!==n); renderLists(ch);
  // M6: si la lista borrada era la del envío automático, limpiar ese campo (no dejarlo apuntando a nada).
  const asel=$('auto_'+ch+'_list'); if(asel && asel.value===n){ asel.value=''; try{ await saveAutoList(); }catch(e){} }
  await saveLists(ch); }  // A2: persistir de inmediato
function toggleListActive(ch,i,v){ const n=LISTS[ch][i].name; const s=new Set(TGT[ch].lists||[]); v?s.add(n):s.delete(n); TGT[ch].lists=[...s]; saveLists(ch); }  // A2
function addToList(ch,i){ const ids=selForChannel(ch); if(!ids.length){ toast('Marca contactos primero',true); return; }
  LISTS[ch][i].ids=[...new Set([...LISTS[ch][i].ids.map(String),...ids])]; renderLists(ch); saveLists(ch); }  // A2: persiste (saveLists avisa)
function removeFromList(ch,i){ const ids=new Set(selForChannel(ch)); if(!ids.size){ toast('Marca contactos primero',true); return; }
  LISTS[ch][i].ids=LISTS[ch][i].ids.filter(x=>!ids.has(String(x))); renderLists(ch); saveLists(ch); }  // A2
function curMode(ch){ const r=document.querySelector(`input[name=mode_${ch}]:checked`); return r?r.value:'all'; }
async function saveLists(ch){ TGT[ch].mode=curMode(ch);
  const body=ch==='telegram'?{telegram_lists:LISTS.telegram,telegram_target:TGT.telegram}:{whatsapp_lists:LISTS.whatsapp,whatsapp_target:TGT.whatsapp};
  try{ await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); toast('✓ Listas guardadas'); loadCfg(); }
  catch(e){ toast('Error al guardar',true); } }
// IDs INCLUIDOS (los que SÍ reciben: no excluidos manualmente ni por patrón) por canal.
function includedIds(ch){
  return ch==='telegram'
    ? DEST.filter(s=>!isExcludedTg(s)).map(s=>String(s.chatId))
    : WA_DEST.filter(c=>!isExcludedWa(c)).map(c=>String(c.id||'')).filter(Boolean);
}
async function crearListaCon(ch, ids, etiqueta){
  if(!ids.length){ toast('No hay contactos '+etiqueta,true); return; }
  const n=(await promptModal('Nombre de la nueva lista (con los '+ids.length+' contactos '+etiqueta+'):',{title:'Crear lista',placeholder:'Nombre de la lista',okText:'Crear'})||'').trim();
  if(!n) return;
  if(LISTS[ch].some(l=>l.name===n)){ toast('Ya existe una lista con ese nombre',true); return; }
  LISTS[ch].push({name:n, ids:[...new Set(ids.map(String))]});
  renderLists(ch);
  await saveLists(ch);
  toast('✓ Lista "'+n+'" creada con '+ids.length+' contactos');
}
async function createListFromGrid(ch){
  const ids=selForChannel(ch);
  if(!ids.length){ toast('Marca primero los contactos en el grid de arriba',true); return; }
  await crearListaCon(ch, ids, 'marcados');
}
async function createListFromIncluded(ch){
  // M5: bajo cuenta no sincronizada el grid está vacío pero DEST conserva contactos viejos;
  // no crear una lista a partir de ese caché para no incluir datos obsoletos.
  if(ch==='telegram'?TG_STALE:WA_STALE){ toast('Conecta la cuenta para crear listas con «incluidos»',true); return; }
  await crearListaCon(ch, includedIds(ch), 'incluidos'); }
// Audiencia EFECTIVA por canal (a quién se enviaría con la config actual): aplica modo
// (todos/solo/excepto) + listas activas + exclusiones (manuales y por patrón). Devuelve nombres.
function audienceFor(ch, overrideTgt){
  const dest = ch==='telegram'? DEST : WA_DEST;
  const idOf = ch==='telegram'? (x=>String(x.chatId)) : (x=>String(x.id||''));
  const nameOf = ch==='telegram'
    ? (x=>{ const n=x.name||'(sin nombre)'; const num=String(x.phone||x.chatId||''); return num?(n+' · '+num):n; })
    : (x=>{ const num=waNum(x); return num?(waName(x)+' · '+num):waName(x); });
  const isExc = ch==='telegram'? isExcludedTg : isExcludedWa;
  const tgt = overrideTgt || TGT[ch]||{mode:'all',lists:[]}; const mode=tgt.mode||'all';
  const sel=new Set(); (LISTS[ch]||[]).forEach(l=>{ if((tgt.lists||[]).includes(l.name)) (l.ids||[]).forEach(x=>sel.add(String(x))); });
  const inc=(dest||[]).filter(x=>{ const id=idOf(x);
    if(isExc(x)) return false;
    if(mode==='only') return sel.has(id);
    if(mode==='except') return !sel.has(id);
    return true; });
  const lab = mode==='only'?('solo listas: '+((tgt.lists||[]).join(', ')||'ninguna')) : mode==='except'?('excepto listas: '+((tgt.lists||[]).join(', ')||'ninguna')) : 'todos';
  return {mode:lab, total:inc.length, names:inc.map(nameOf)};
}
// Carga contactos si faltan (para poder resumir la audiencia). WhatsApp con timeout (puede dormir).
async function ensureContactsLoaded(){
  if(!DEST.length){ try{ const d=await api('/api/subscribers'); DEST=d.subscribers||[]; }catch(e){} }
  try{ await loadPatterns(); }catch(e){}
  let waOk=true;
  if(!WA_DEST.length){ try{ const r=await Promise.race([api('/api/whatsapp/contacts'), new Promise((_,rej)=>setTimeout(()=>rej('t'),12000))]); WA_DEST=r.contacts||[]; }catch(e){ waOk=false; } }
  return {waOk};
}
// --- contactos de WhatsApp (para armar listas de WhatsApp) ---
let WA_DEST=[], WA_PAGE=0, WA_EXCLUDED=new Set(), WA_STATEF='', WA_EXCL_PAT=[], WA_EXCEPT=new Set();
function isExcludedWa(c){ const id=String(c.id||'');
  return WA_EXCLUDED.has(id) || (nameMatchesPatterns(waName(c), WA_EXCL_PAT) && !WA_EXCEPT.has(id)); }
function waName(c){ return c.name || '(sin nombre)'; }
function waNum(c){ return String(c.id||'').split('@')[0]; }  // número desde el jid (sin @s.whatsapp.net)
async function loadWaContacts(){ $('wa_c_count').textContent='· cargando...';
  try{ const r=await api('/api/whatsapp/contacts'); WA_DEST=r.contacts||[]; WA_PAGE=0; renderWa(); }
  catch(e){ $('wa_c_count').textContent='· servicio inaccesible (¿conectado?)'; } }
async function waSyncContacts(){
  toast('Sincronizando contactos de WhatsApp…','info');
  try{ await api('/api/whatsapp/sync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    toast('✓ Sincronizado. Recargando contactos…'); setTimeout(loadWaContacts, 2000); }
  catch(e){ toast(e.message||'No se pudo sincronizar',true); } }
function waFiltered(){ let arr=WA_DEST; const q=($('wa_search').value||'').trim().toLowerCase();
  if(q) arr=arr.filter(c=> waName(c).toLowerCase().includes(q) || String(c.id||'').toLowerCase().includes(q));
  if(WA_STATEF==='inc') arr=arr.filter(c=>!isExcludedWa(c));
  else if(WA_STATEF==='exc') arr=arr.filter(c=>isExcludedWa(c));
  return arr; }
function setWaStateFilter(v){ WA_STATEF=v; WA_PAGE=0;
  document.querySelectorAll('#seg_wa button').forEach(b=>{ const on=b.dataset.v===v; b.classList.toggle('on',on); b.classList.toggle('exc',on&&v==='exc'); b.setAttribute('aria-pressed',on?'true':'false'); });
  renderWa(); }
function renderWa(){
  // Si WhatsApp NO está conectado/sincronizado, no mostramos contactos cacheados viejos
  // (las listas de WhatsApp sí persisten y se reusan al reconectar).
  if(WA_STALE){
    const t=$('wa_subs'); if(t) t.innerHTML='<tr><td colspan="3" class="hint" style="padding:12px">⚠️ WhatsApp no está conectado/sincronizado. Conéctalo (Ajustes → WhatsApp) para ver tus contactos. <b>Tus listas se conservan</b> y se reusarán al reconectar.</td></tr>';
    if($('wa_c_count')) $('wa_c_count').textContent='· WhatsApp no conectado';
    if($('wa_pageinfo')) $('wa_pageinfo').textContent='';
    return;
  }
  const f=waFiltered(); const pages=Math.max(1,Math.ceil(f.length/PAGE_SIZE));
  if(WA_PAGE>=pages)WA_PAGE=pages-1; if(WA_PAGE<0)WA_PAGE=0; const slice=f.slice(WA_PAGE*PAGE_SIZE,(WA_PAGE+1)*PAGE_SIZE);
  const t=$('wa_subs'); t.innerHTML='';
  const exCount=WA_DEST.filter(isExcludedWa).length; const inc=WA_DEST.length-exCount;
  $('wa_c_count').textContent = WA_DEST.length ? `· ${f.length} en vista · ${inc} incluidos · ${exCount} excluidos` : '';
  slice.forEach(c=>{ const id=String(c.id||''); const exM=WA_EXCLUDED.has(id); const matchP=nameMatchesPatterns(waName(c), WA_EXCL_PAT);
    const exP=!exM && matchP && !WA_EXCEPT.has(id); const exc=!exM && matchP && WA_EXCEPT.has(id); const tr=document.createElement('tr');
    const pill = exM?'<span class="pill inactive">Excluido</span>':(exP?'<span class="pill pat">Excluido · patrón</span>':(exc?'<span class="pill exc-ok">Incluido · excepción</span>':'<span class="pill active">Incluido</span>'));
    tr.innerHTML=`<td><input type="checkbox" class="wsel" data-id="${id}"></td><td><b>${bcEsc(waName(c))}</b><div class="hint" style="margin-top:2px;font-size:11px">📞 ${bcEsc(waNum(c)||id)}</div></td>`+
      `<td>${pill}</td>`; t.appendChild(tr); });
  $('wa_pageinfo').textContent=f.length?`página ${WA_PAGE+1} de ${pages}`:'sin resultados'; }
function waSelectedIds(){ return [...document.querySelectorAll('.wsel:checked')].map(c=>String(c.dataset.id)); }
function waToggleAll(v){ document.querySelectorAll('.wsel').forEach(c=>c.checked=v); }
async function persistWaExcluded(){ // B6: rollback desde el servidor si falla
  try{ await api('/api/patterns',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({whatsapp_excluded:[...WA_EXCLUDED],whatsapp_pattern_exceptions:[...WA_EXCEPT]})}); renderWa(); return true; }
  catch(e){ toast('No se pudo guardar — recargado desde el servidor',true); await loadPatterns(); return false; } }
async function waBulk(accion){ const ids=waSelectedIds(); if(!ids.length){ toast('Marca al menos un contacto',true); return; }
  ids.forEach(id=>{
    if(accion==='excluir'){ WA_EXCLUDED.add(id); WA_EXCEPT.delete(id); }
    else { WA_EXCLUDED.delete(id);
      const c=WA_DEST.find(x=>String(x.id||'')===id);
      if(c && nameMatchesPatterns(waName(c), WA_EXCL_PAT)) WA_EXCEPT.add(id); else WA_EXCEPT.delete(id); } // excepción al patrón
  });
  if(await persistWaExcluded()) toast('✓ '+ids.length+' '+(accion==='excluir'?'excluidos':'incluidos')); }
// M7: opera sobre TODOS los contactos que coinciden con el filtro/búsqueda (no solo la página visible).
async function waBulkFiltered(accion){ const ids=waFiltered().map(c=>String(c.id||'')).filter(Boolean); if(!ids.length){ toast('Sin contactos que coincidan',true); return; }
  ids.forEach(id=>{ if(accion==='excluir'){ WA_EXCLUDED.add(id); WA_EXCEPT.delete(id); }
    else { WA_EXCLUDED.delete(id); const c=WA_DEST.find(x=>String(x.id||'')===id); if(c && nameMatchesPatterns(waName(c), WA_EXCL_PAT)) WA_EXCEPT.add(id); else WA_EXCEPT.delete(id); } });
  if(await persistWaExcluded()) toast('✓ '+ids.length+' filtrados '+(accion==='excluir'?'excluidos':'incluidos')); }
function waPrev(){ WA_PAGE--; renderWa(); }
function waNext(){ WA_PAGE++; renderWa(); }
// Sub-navegación genérica (por pestaña): muestra solo las tarjetas con el data-sub elegido.
const SUB_DEFAULT={fuentes:'tg', envios:'componer', ajustes:'telegram'};
function showSub(tab,s){
  document.querySelectorAll('main>.card[data-tab="'+tab+'"][data-sub]').forEach(c=>c.classList.toggle('subhide', c.dataset.sub!==s));
  document.querySelectorAll('.subnav[data-subnav="'+tab+'"] button').forEach(b=>b.classList.toggle('on', b.dataset.sub===s));
  try{ localStorage.setItem('sub_'+tab,s); }catch(e){}
}
function showTab(t){
  document.querySelectorAll('main>.card').forEach(c=>c.classList.toggle('show', c.dataset.tab===t));
  document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('on', b.dataset.tab===t));
  // B25: en móvil la nav es una tira scrolleable; centra la pestaña activa para que se vea.
  try{ const _on=document.querySelector('.nav button.on'); if(_on&&_on.scrollIntoView) _on.scrollIntoView({inline:'center',block:'nearest'}); }catch(e){}
  try{ localStorage.setItem('tab',t); }catch(e){}
  if(t==='envios'){ sgFillLists(); sgChan(); sgType(); loadSchedules(); }
  if(SUB_DEFAULT[t]){ showSub(t, (function(){try{return localStorage.getItem('sub_'+t)}catch(e){return null}})()||SUB_DEFAULT[t]); }
  const TAB_NAMES={inicio:'Inicio',fuentes:'Fuentes y listas',envios:'Envíos',ajustes:'Ajustes y estado'};
  const _h=$('page_h1'); if(_h) _h.textContent=TAB_NAMES[t]||'Panel';  // B22: contexto de sección para lectores de pantalla
  window.scrollTo(0,0); }
// Mejora de accesibilidad aplicada una vez al arrancar (no cambia el aspecto visual).
function a11yEnhance(){ try{
  document.querySelectorAll('.help[data-tip]').forEach(e=>{ e.setAttribute('role','img'); if(!e.getAttribute('aria-label')) e.setAttribute('aria-label', e.getAttribute('data-tip')); });  // M27
  document.querySelectorAll('label:not([for])').forEach(l=>{ if(l.querySelector('input,select,textarea')) return; const n=l.nextElementSibling; if(n && /^(INPUT|SELECT|TEXTAREA)$/.test(n.tagName) && n.id) l.htmlFor=n.id; });  // M28
  ['tl_status','fp_status','cp_status','mail_save_status','bc_status','sg_status','wa_reset_out','wa_state','tg_state','sess_status','bot_status','mail_status'].forEach(id=>{ const e=$(id); if(e && !e.getAttribute('aria-live')) e.setAttribute('aria-live','polite'); });  // M30
  const m=document.querySelector('main'); if(m && !$('page_h1')){ const h=document.createElement('h1'); h.id='page_h1'; h.className='sr-only'; h.textContent='Panel'; m.insertBefore(h,m.firstChild); }  // B22
  // M4: el modo de envío (Todos/Solo/Excepto) se persiste al cambiarlo (antes solo con "Guardar listas").
  ['telegram','whatsapp'].forEach(ch=>document.querySelectorAll('input[name=mode_'+ch+']').forEach(r=>r.addEventListener('change',()=>saveLists(ch))));
}catch(e){} }
function boot(){ a11yEnhance(); showTab((()=>{try{const s=localStorage.getItem('tab');return ['inicio','fuentes','envios','ajustes'].includes(s)?s:'inicio'}catch(e){return 'inicio'}})()); loadMe(); loadCfg(); loadQueue(); loadSubs(); loadDlq(); loadDashboard(); connStartPolling(); }
if(CRED && !sessionFresca()){ logout(); }
else if(CRED){ fetch(BASE+'/api/me',{headers:hdr()}).then(r=>{ if(r.ok){ $('login').style.display='none'; $('app').style.display='block'; boot(); } else { logout(); } }).catch(()=>{}); }

// ===== Componer y enviar (POST /api/broadcast) =====
let BC_IMG_URL = '';           // URL (solo preview) devuelta tras subir un archivo a /api/image
let BC_IMG_KEY = '';           // clave S3 de la imagen subida (se re-firma al despachar)
function bcCount(){ const n=$('bc_text').value.length, el=$('bc_count');
  el.textContent = n>4096 ? (n+' / 4096 · supera el límite de Telegram') : (n+(n===1?' carácter':' caracteres'));
  el.dataset.near = (n>3600 && n<=4096) ? '1':'0'; el.dataset.over = n>4096 ? '1':'0'; bcValidate(); }
// B1: habilita/inhabilita "Enviar" según el estado, con motivo en el title (antes solo durante la petición).
function bcWaNoList(){ return $('bc_whatsapp').checked && !$('bc_wa_list').value && !BC_WA_SEL.size; }
function bcValidate(){ const btn=$('bc_send'); if(!btn) return;
  const tg=$('bc_telegram').checked, wa=$('bc_whatsapp').checked;
  const hasText=($('bc_text').value||'').trim().length>0, hasImg=!!bcEffectiveUrl();
  let reason='';
  if(!tg && !wa) reason='Elige al menos un canal (Telegram o WhatsApp)';
  else if(!hasText && !hasImg) reason='Escribe un mensaje o añade una imagen';
  else if(($('bc_text').value||'').length>4096) reason='El mensaje supera 4096 caracteres';
  else if(bcWaNoList()) reason='WhatsApp necesita una lista o contactos marcados';
  btn.disabled=!!reason; btn.title=reason||'Enviar'; }
// M40/M3/M9: el datetime-local se interpreta en la ZONA CONFIGURADA (window_tz), no en la del
// navegador, para que coincida con la ventana de envío del servidor. (Si navegador==zona, no cambia.)
function schedTz(){ return parseInt(($('window_tz')&&$('window_tz').value)||'-300',10) || -300; }
function schedTzLabel(){ const t=schedTz(), s=t<0?'-':'+', a=Math.abs(t); return 'UTC'+s+String(Math.floor(a/60)).padStart(2,'0')+':'+String(a%60).padStart(2,'0'); }
function schedEpoch(sv){ const m=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(sv||''); if(!m) return 0;
  const utc=Date.UTC(+m[1],+m[2]-1,+m[3],+m[4],+m[5]); return Math.floor((utc - schedTz()*60000)/1000); }
// --- selección de contactos (picker) en el compositor ---
let BC_TG_SEL=new Set(), BC_WA_SEL=new Set();
function bcSel(ch){ return ch==='tg'?BC_TG_SEL:BC_WA_SEL; }
function bcNameOf(ch,c){ return ch==='tg'?(c.name||'(sin nombre)'):waName(c); }
function bcNumOf(ch,c){ return ch==='tg'?String(c.phone||c.chatId||''):waNum(c); }  // número visible del contacto
function bcIdOf(ch,c){ return ch==='tg'?String(c.chatId):String(c.id); }
function bcRenderPick(ch){
  const box=$('bc_'+ch+'_pick'); if(!box) return;
  // Cuenta no sincronizada: no ofrecer contactos cacheados viejos para elegir (sí puedes usar listas).
  if(ch==='tg'?TG_STALE:WA_STALE){
    box.innerHTML='<div class="hint">⚠️ Cuenta no conectada — conéctala (Ajustes) para elegir contactos. Las listas siguen disponibles.</div>';
    return;
  }
  const data=(ch==='tg'?DEST:WA_DEST)||[], sel=bcSel(ch);
  const q=($('bc_'+ch+'_search').value||'').trim().toLowerCase();
  const f=data.filter(c=>{ if(!q) return true;
    const id=(ch==='tg'?String(c.chatId||''):String(c.id||'')).toLowerCase();
    return bcNameOf(ch,c).toLowerCase().includes(q) || id.includes(q) || bcNumOf(ch,c).toLowerCase().includes(q); }).slice(0,40);
  box.innerHTML = f.length ? f.map(c=>{ const id=bcIdOf(ch,c); const num=bcNumOf(ch,c);
    return `<label class="pickitem"><input type="checkbox" ${sel.has(id)?'checked':''} onchange="bcTogglePick('${ch}','${id}',this.checked)"> <span>${bcEsc(bcNameOf(ch,c))}</span>${num?` <span class="hint" style="margin-left:6px">📞 ${bcEsc(num)}</span>`:''}</label>`; }).join('')
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
  clearTimeout(BC_PREV_T); bcValidate();
  const tg=$('bc_telegram').checked, wa=$('bc_whatsapp').checked, out=$('bc_preview');
  // B3: sin canal elegido, guiar en vez de mostrar "—".
  if(!tg && !wa){ out.innerHTML='<span class="hint">Elige Telegram o WhatsApp arriba para seleccionar destinatarios.</span>'; return; }
  // M2: WhatsApp marcado sin lista ni contactos → avisar (no mostrar un conteo enorme tranquilizador).
  const waWarn = bcWaNoList() ? (ICO_WA+' WhatsApp: <span style="color:var(--bad)">elige una lista (no se envía a toda la agenda)</span>') : null;
  const noSync='<span class="hint">(cuenta no conectada)</span>';  // no mostramos conteos del caché viejo
  // Si TODOS los canales elegidos están sin sincronizar, ni siquiera consultamos el conteo.
  if((!tg || TG_STALE) && (!wa || WA_STALE)){
    const parts=[]; if(tg) parts.push(ICO_TG+' Telegram: '+noSync); if(wa) parts.push(waWarn || (ICO_WA+' WhatsApp: '+noSync));
    out.innerHTML=parts.join(' · '); return;
  }
  out.textContent='calculando destinatarios…';
  BC_PREV_T=setTimeout(async()=>{
    try{ const r=await api('/api/broadcast/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(bcBody())});
      const parts=[];
      if(tg) parts.push(ICO_TG+' Telegram: '+(TG_STALE?noSync:('<b>'+(r.telegram??0)+'</b>')));
      if(wa) parts.push(waWarn || (ICO_WA+' WhatsApp: '+(WA_STALE?noSync:('<b>'+(r.whatsapp??0)+'</b>'))));
      out.innerHTML='Se enviará a → '+parts.join(' · ');
    }catch(e){ out.textContent='no se pudo calcular la previsualización'; }
  }, 300);
}
function bcEffectiveUrl(){ return ($('bc_image_url').value || '').trim() || BC_IMG_URL || ''; }
function bcPreview(){
  const u=bcEffectiveUrl(), p=$('bc_imgprev');
  if(u){ p.onerror=()=>{ p.style.display='none'; toast('No se pudo cargar la imagen de esa URL',true); };  // B2: URL rota no deja un <img> roto
    p.src=u; p.style.display='block'; } else { p.onerror=null; p.style.display='none'; p.removeAttribute('src'); }
  bcValidate();
}
async function bcUploadImg(){
  const f=$('bc_imgfile').files[0]; if(!f) return;
  $('bc_status').textContent='subiendo imagen...';
  const r=new FileReader();
  r.onload=async()=>{ const b64=r.result.split(',')[1];
    try{
      // scope=compose: NO pisa la imagen por defecto de Configuración. Guardamos la KEY (no la URL)
      // para que el envío RE-FIRME la imagen al despachar (la prefirmada caduca en 1h).
      const res=await api('/api/image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:b64,content_type:f.type,scope:'compose'})});
      BC_IMG_URL = res.url || res.image_url || '';   // solo para previsualizar
      BC_IMG_KEY = res.key || '';                    // se envía al backend para re-firmar al despachar
      $('bc_imgprev').src=r.result; $('bc_imgprev').style.display='block';
      $('bc_status').textContent=''; toast('✓ Imagen lista');
    }catch(e){ $('bc_status').textContent=''; toast('Error al subir imagen',true); }
  };
  r.readAsDataURL(f);
}
function bcClear(){
  $('bc_text').value=''; $('bc_image_url').value=''; BC_IMG_URL=''; BC_IMG_KEY='';
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
  if(BC_IMG_KEY) body.image_key=BC_IMG_KEY;   // clave S3: el backend re-firma la imagen al despachar
  // Programación opcional: datetime-local -> epoch (s). Vacío = enviar ya.
  let ep=0; const sv=$('bc_sched')?$('bc_sched').value:'';
  if(sv){ ep=schedEpoch(sv); if(ep>Math.floor(Date.now()/1000)) body.scheduled_at=ep; else { toast('La fecha programada debe ser futura',true); return; } }
  // Canales EXPLÍCITOS en la confirmación: que se vea si saldrá por Telegram, WhatsApp o ambos.
  const chs=[tg&&'Telegram', wa&&'WhatsApp'].filter(Boolean).join(' + ');
  let msg = body.scheduled_at ? ('¿Programar este envío para '+sv.replace('T',' ')+' ('+schedTzLabel()+')?') : '¿Enviar este mensaje ahora?';
  msg += '\n\nSe enviará por: '+chs+'.';
  if(wa) msg+='\n\n⚠️ El envío masivo por WhatsApp puede banear tu número.';
  if(!await confirmModal(msg,{okText: body.scheduled_at ? 'Programar' : 'Enviar'})) return;
  const btn=$('bc_send'); btn.disabled=true; btn.classList.add('btn-loading'); $('bc_status').textContent='guardando...';
  try{
    await api('/api/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    // "enviando" != "entregado": el envío se fracciona y la ENTREGA real se confirma abajo en Envíos.
    toast(body.scheduled_at?'✓ Programado para más tarde':'✓ Enviando — la entrega se confirma abajo en Envíos','info'); $('bc_status').textContent='';
    bcClear();
    showTab('envios');
    loadBroadcasts();
  }catch(e){ const _m=e.message||(body.scheduled_at?'No se pudo programar':'No se pudo enviar'); $('bc_status').textContent=_m; toast(_m,true); }
  finally{ btn.disabled=false; btn.classList.remove('btn-loading'); }
}
// Al abrir la pestaña Enviar: rellenar listas + previsualizar (hook aditivo sobre showTab).
(function(){ const _s=window.showTab;
  if(typeof _s==='function'){ window.showTab=function(t){ _s(t); if(t==='envios'){ try{ bcFillLists(); bcChan(); }catch(e){} } }; }
})();
// ===== Envíos: listado + polling (GET /api/broadcasts) =====
let BC_TIMER=null;
const BC_POLL=4000;
const BC_STATUS={ queued:'En cola', sending:'Enviando', done:'Completado', failed:'Fallido', partial:'Parcial', captured:'📥 Capturada (no enviada)' };
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
const SG_DAYFULL=['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];
function sgRenderDays(){ const box=$('sg_days'); if(!box||box.children.length) return;
  box.innerHTML=SG_DAYNAMES.map((d,i)=>`<label class="chan${SG_DAYS.has(i)?' on':''}" data-d="${i}" role="checkbox" tabindex="0" aria-checked="${SG_DAYS.has(i)?'true':'false'}" aria-label="${SG_DAYFULL[i]}" onclick="sgToggleDay(${i})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();sgToggleDay(${i})}">${d}</label>`).join(''); }
function sgToggleDay(i){ SG_DAYS.has(i)?SG_DAYS.delete(i):SG_DAYS.add(i);
  const el=document.querySelector('#sg_days [data-d="'+i+'"]'); if(el){ el.classList.toggle('on',SG_DAYS.has(i)); el.setAttribute('aria-checked',SG_DAYS.has(i)?'true':'false'); } }
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
  if(!body.telegram && !body.whatsapp){ toast('Elige al menos un canal',true); return; }
  if(body.whatsapp && !body.whatsapp_list){ toast('Elige una lista de WhatsApp (no se envía a toda la agenda)',true); const s=$('sg_wa_list'); if(s){ s.focus(); } return; }
  if(t==='once'){ const v=$('sg_run_at').value; if(!v){ toast('Elige fecha y hora',true); return; }
    body.run_at=schedEpoch(v); }  // M40: en la zona configurada, no la del navegador
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
function sgChans(s){ return [s.telegram?ICO_TG:'', s.whatsapp?ICO_WA:''].filter(Boolean).join(' ')||'—'; }
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
      <td class="selcol"><input type="checkbox" class="sgsel" data-sid="${s.sid}" onchange="sgSelChanged()"></td>
      <td><b>${msg}</b><div class="hint" style="margin-top:2px">${tag}</div></td>
      <td>${sgChans(s)}</td>
      <td>${bcEsc(sgDesc(s))}</td>
      <td>${s.enabled?sgWhen(s.next_run):'—'}</td>
      <td style="white-space:nowrap;text-align:right">
        <button class="sec" style="padding:5px 10px" onclick="sgToggle('${s.sid}',${s.enabled?'false':'true'})">${s.enabled?'Pausar':'Activar'}</button>
        <button class="danger" style="padding:5px 10px;margin-left:6px" onclick="sgDelete('${s.sid}')">Borrar</button>
      </td></tr>`;
  }).join('');
  if($('sg_selall')) $('sg_selall').checked=false; sgSelChanged();
}
function sgSelAll(v){ document.querySelectorAll('.sgsel').forEach(c=>c.checked=v); sgSelChanged(); }
function sgSelectedSids(){ return [...document.querySelectorAll('.sgsel:checked')].map(c=>c.getAttribute('data-sid')).filter(Boolean); }
function sgSelChanged(){ const n=sgSelectedSids().length; const b=$('sg_delsel'); if(b){ b.disabled=!n; b.textContent='🗑 Borrar seleccionados'+(n?(' ('+n+')'):''); } }
async function sgDeleteSelected(){ const sids=sgSelectedSids(); if(!sids.length){ toast('Marca al menos un mensaje',true); return; }
  if(!await confirmModal('¿Borrar '+sids.length+' mensaje(s) programado(s)? No se puede deshacer.',{danger:true,okText:'Borrar'})) return;
  try{ const r=await api('/api/schedules/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sids})}); toast('✓ '+(r.deleted||0)+' borrados'); loadSchedules(); }
  catch(e){ toast('Error al borrar',true); } }
async function sgDeleteAll(){ if(!await confirmModal('¿Borrar TODOS los mensajes programados? No se puede deshacer.',{danger:true,okText:'Borrar todos'})) return;
  try{ const r=await api('/api/schedules/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})}); toast('✓ '+(r.deleted||0)+' borrados'); loadSchedules(); }
  catch(e){ toast('Error al borrar',true); } }
async function sgToggle(sid,en){ try{ await api('/api/schedules/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid,enabled:en})}); loadSchedules(); }catch(e){ toast('No se pudo cambiar el estado del programado',true); } }
async function sgDelete(sid){ if(!await confirmModal('¿Borrar este mensaje programado?',{danger:true,okText:'Borrar'})) return;
  try{ await api('/api/schedules/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid})}); toast('✓ Borrado'); loadSchedules(); }catch(e){ toast('No se pudo borrar el programado',true); } }
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
    `<td class="bc-msg"><b role="button" tabindex="0" title="Ver mensaje completo" style="cursor:pointer" data-full="${bcEsc(b.full_text||txt)}" onclick="bcMsgDetail(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();bcMsgDetail(this)}">${bcEsc(txt)} ›</b>`+
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
function bcMsgDetail(el){
  const full=(el.getAttribute('data-full')||'').trim();
  alertModal(full||'(sin texto / solo imagen)',{title:'Mensaje completo'});
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
const PL_ST={pending:'Pendiente',running:'En curso',done:'Completado',canceled:'Cancelado',failed:'Fallido',partial:'Parcial'};
const PL_PILL={pending:'queued',running:'sending',done:'done',canceled:'failed',failed:'failed',partial:'partial'};
function plBatchLine(e){
  const chName=e.ch==='wa'?(ICO_WA+' WhatsApp'):(ICO_TG+' Telegram');
  const n=e.n|0, env=e.enviados|0, pct=n?Math.round(env/n*100):0, full=env>=n;
  return `<div class="ch ${e.ch==='wa'?'wa':'tg'}" style="margin:6px 0">`+
    `<span class="ic"></span>`+
    `<span class="num">${chName} · lote ${ (e.idx|0)+1 } · programado <b>${bcFmtTime(e.at)}</b> → <b>${env}/${n}</b> enviados</span>`+
    `<span class="bar ${e.ch==='wa'?'wa':''} ${full?'full':''}" style="max-width:120px"><i style="width:${pct}%"></i></span></div>`;
}
function plCard(p){
  const txt=(p.text||'').trim()||'(solo imagen)';
  const st=String(p.status||'pending'); const lab=PL_ST[st]||st; const pill=PL_PILL[st]||'queued';
  const tgI=(p.tg&&p.tg.total)?`${ICO_TG} ${p.tg.next|0}/${p.tg.batches|0} lotes despachados`:'';
  const waI=(p.wa&&p.wa.enabled)?`${ICO_WA} ${p.wa.next|0}/${p.wa.batches|0} lotes despachados`+(!p.wa.resolved?' (resolviendo…)':''):'';
  const lines=(p.log||[]).map(plBatchLine).join('') || '<div class="hint" style="margin-top:6px">Aún sin lotes despachados (esperando ventana/turno).</div>';
  const activo=(st==='pending'||st==='running');
  const cancelBtn=activo?`<button class="danger" style="padding:6px 12px" onclick="cancelPlan('${p.pid}')">🛑 Cancelar este envío</button>`:'';
  // Mensaje EXACTO que se envía (ya procesado: markup, sin IPRO PARTS, footer). Scrollable para revisar.
  return `<div class="card pl-card" data-pid="${p.pid}" style="margin-bottom:12px;background:var(--elev);padding:16px">`+
    `<div style="display:flex;justify-content:space-between;gap:10px;align-items:center">`+
      `<label style="display:flex;align-items:center;gap:9px;margin:0"><input type="checkbox" class="plsel" data-pid="${p.pid}" onchange="plSelChanged()" style="width:auto"><span class="pill ${pill}">${lab}</span></label>`+
      `<span style="display:flex;gap:8px;align-items:center">${cancelBtn}<button class="danger" style="padding:6px 10px" title="Borrar definitivamente" onclick="plDelete('${p.pid}')">🗑</button></span></div>`+
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
// Borrado de envíos fraccionados (individual + masivo), mismo patrón que la tabla de Envíos.
function plSelAll(v){ document.querySelectorAll('.plsel').forEach(c=>c.checked=v); plSelChanged(); }
function plSelectedIds(){ return [...document.querySelectorAll('.plsel:checked')].map(c=>c.dataset.pid); }
function plSelChanged(){
  document.querySelectorAll('.plsel').forEach(c=>{ const card=c.closest('.pl-card'); if(card) card.classList.toggle('sel-card', c.checked); });
  const n=plSelectedIds().length, b=$('pl_delsel');
  if(b){ b.disabled=n===0; b.textContent='🗑 Borrar seleccionados'+(n?' ('+n+')':''); }
}
async function plDelete(pid){
  if(!await confirmModal('¿Borrar DEFINITIVAMENTE este envío fraccionado (y sus lotes)? No se puede deshacer (no afecta lo ya entregado).',{danger:true,okText:'Borrar'})) return;
  try{ await api('/api/plans/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pid})}); toast('✓ Borrado'); loadPlans(); }
  catch(e){ toast('Error al borrar',true); }
}
async function plDeleteSelected(){
  const ids=plSelectedIds(); if(!ids.length) return;
  if(!await confirmModal('¿Borrar DEFINITIVAMENTE '+ids.length+' envío(s) seleccionados? No se puede deshacer.',{danger:true,okText:'Borrar'})) return;
  try{ const r=await api('/api/plans/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pids:ids})}); toast('✓ '+(r.deleted||0)+' borrados'); loadPlans(); }
  catch(e){ toast('Error al borrar',true); }
}
async function plClearFinished(){
  if(!await confirmModal('¿Borrar DEFINITIVAMENTE todos los envíos fraccionados TERMINADOS (completados o cancelados)? Se conservan los pendientes/en curso.',{danger:true,okText:'Borrar terminados'})) return;
  try{ const r=await api('/api/plans/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({finished:true})}); toast('✓ '+(r.deleted||0)+' borrados'); loadPlans(); }
  catch(e){ toast('Error al borrar',true); }
}
async function loadPlans(){
  try{
    const r=await api('/api/plans'); const list=r.plans||[];
    $('pl_empty').style.display=list.length?'none':'block';
    $('pl_list').innerHTML=list.map(plCard).join('');
    if($('pl_selall')) $('pl_selall').checked=false; plSelChanged();
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
