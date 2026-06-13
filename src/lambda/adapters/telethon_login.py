"""Login userbot (Telethon) DESDE la plataforma: genera la StringSession sin scripts locales.

Flujo en dos pasos, sin estado en memoria entre requests (Lambda):
  1. ``enviar_codigo(api_id, api_hash, phone)`` -> crea una sesión temporal y pide el código;
     devuelve ``{session, phone_code_hash}`` para persistir mientras el usuario teclea el código.
  2. ``confirmar(api_id, api_hash, session, phone, phone_code_hash, code=..., password=...)``:
     - con ``code``: inicia sesión; si la cuenta tiene verificación en dos pasos, devuelve
       ``{status:'needs_password', session}`` (sesión tras aceptar el código).
     - con ``password``: completa el 2FA.
     Éxito -> ``{status:'ok', session, me:{id, username, name}}`` (la StringSession definitiva).

Imports de Telethon perezosos (igual que telethon_user.py) para no exigirlo al importar.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TelethonLoginError(Exception):
    """Error de login con un mensaje apto para mostrar al usuario."""


def _client(api_id, api_hash, session_str: str = ""):
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    if not api_id or not api_hash:
        raise TelethonLoginError("Faltan API ID / API HASH de Telegram (configúralos primero).")
    c = TelegramClient(StringSession(session_str or ""), int(api_id), api_hash)
    c.connect()
    return c


def enviar_codigo(api_id, api_hash, phone: str) -> dict:
    """Pide el código de acceso a Telegram. Devuelve la sesión temporal + phone_code_hash."""
    from telethon import errors

    phone = (phone or "").strip()
    if not phone:
        raise TelethonLoginError("Indica el número de teléfono (formato internacional, ej. +57...).")
    c = _client(api_id, api_hash, "")
    try:
        sent = c.send_code_request(phone)
        return {"session": c.session.save(), "phone_code_hash": sent.phone_code_hash}
    except errors.FloodWaitError as fw:
        raise TelethonLoginError(f"Telegram pide esperar {fw.seconds}s antes de reintentar.")
    except errors.PhoneNumberInvalidError:
        raise TelethonLoginError("Número inválido (usa formato internacional, ej. +573001234567).")
    except TelethonLoginError:
        raise
    except Exception as e:  # pragma: no cover - red/telegram
        raise TelethonLoginError(f"No se pudo enviar el código: {e}")
    finally:
        try:
            c.disconnect()
        except Exception:
            pass


def confirmar(api_id, api_hash, session_str, phone, phone_code_hash, code=None, password=None) -> dict:
    """Completa el login con el código (y la contraseña 2FA si la cuenta la tiene)."""
    from telethon import errors

    c = _client(api_id, api_hash, session_str)
    try:
        if password:
            c.sign_in(password=password)
        else:
            try:
                c.sign_in(phone=phone, code=str(code or "").strip(), phone_code_hash=phone_code_hash)
            except errors.SessionPasswordNeededError:
                # El código fue aceptado pero la cuenta exige verificación en dos pasos.
                return {"status": "needs_password", "session": c.session.save()}
        me = c.get_me()
        nombre = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")]))
        return {
            "status": "ok",
            "session": c.session.save(),
            "me": {"id": getattr(me, "id", None), "username": getattr(me, "username", None), "name": nombre},
        }
    except errors.PhoneCodeInvalidError:
        raise TelethonLoginError("Código incorrecto.")
    except errors.PhoneCodeExpiredError:
        raise TelethonLoginError("El código expiró; pide uno nuevo.")
    except errors.PasswordHashInvalidError:
        raise TelethonLoginError("Contraseña de verificación en dos pasos incorrecta.")
    except errors.FloodWaitError as fw:
        raise TelethonLoginError(f"Telegram pide esperar {fw.seconds}s antes de reintentar.")
    except TelethonLoginError:
        raise
    except Exception as e:  # pragma: no cover - red/telegram
        raise TelethonLoginError(f"No se pudo iniciar sesión: {e}")
    finally:
        try:
            c.disconnect()
        except Exception:
            pass
