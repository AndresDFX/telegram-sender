"""Adapter userbot (MTProto/Telethon): envía las listas como TU cuenta y lista tus contactos.

Los imports de Telethon son perezosos para que las pruebas puedan importar y mockear
sin tenerlo instalado. Credenciales (api_id/api_hash/session) desde entorno o constructor.

⚠️  Enviar masivamente desde una cuenta de usuario puede llevar a límites/baneo de Telegram.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from application.ports import ChannelReader, MessageSender
from domain.models import Post, SendResult

logger = logging.getLogger(__name__)
_MAX_FLOOD_WAIT = 60.0  # si Telegram pide esperar más que esto, fallamos el envío


class _TelethonBase:
    def __init__(self, api_id=None, api_hash=None, session=None):
        # Credenciales perezosas: no se exigen al construir (solo al conectar).
        self._api_id_raw = api_id
        self._api_hash_raw = api_hash
        self._session_raw = session
        self._client = None

    def _c(self):
        if self._client is None:
            from telethon.sessions import StringSession
            from telethon.sync import TelegramClient

            api_id = int(self._api_id_raw or os.environ["TELETHON_API_ID"])
            api_hash = self._api_hash_raw or os.environ["TELETHON_API_HASH"]
            session = self._session_raw or os.environ["TELETHON_SESSION"]
            self._client = TelegramClient(StringSession(session), api_id, api_hash)
            self._client.connect()
        return self._client

    def desconectar(self) -> None:
        """Cierra la conexión y resetea el cliente. Útil para no mantener DOS clientes con la MISMA
        sesión conectados a la vez en un mismo invoke (p. ej. preview + refresh de contactos en el
        poller), que Telegram puede penalizar/revocar. La próxima llamada reconecta limpio."""
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None


class TelethonChannelReader(_TelethonBase, ChannelReader):
    """Lee las últimas publicaciones del canal con el USERBOT (respaldo del preview t.me).

    El preview público t.me/s/<canal> puede dejar de servir el feed sin aviso (el 6 jul 2026
    empezó a REDIRIGIR a la tarjeta del canal y la ingesta quedó muerta). El userbot lee el canal
    directamente (get_messages): trae los mismos message_id (el HWM sigue siendo válido), incluye
    los captions de fotos/álbumes y detecta si el post trae imagen (has_photo). Semántica M14:
    nunca lanza — ante cualquier fallo devuelve [] y se reintenta el próximo tick."""

    def __init__(self, api_id=None, api_hash=None, session=None, limit: int = 20):
        super().__init__(api_id, api_hash, session)
        self._limit = limit

    def leer_publicaciones(self, channel: str) -> list[Post]:
        try:
            ch = str(channel or "").strip()
            entidad: object = int(ch) if ch.lstrip("-").isdigit() else ch.lstrip("@")
            posts: list[Post] = []
            for m in self._c().get_messages(entidad, limit=self._limit) or []:
                texto = (getattr(m, "message", None) or "").strip()
                if not texto:
                    continue  # A7 (diferido): los posts SOLO-imagen se saltan, igual que con el preview
                posts.append(Post(message_id=int(m.id), text=texto, has_photo=bool(getattr(m, "photo", None))))
            return posts
        except Exception:
            logger.exception("Userbot no pudo leer el canal %s; sin publicaciones este ciclo", channel)
            return []
        finally:
            # M17: no dejar la conexión abierta (el poller usa OTRO cliente Telethon para el preview
            # a Mensajes Guardados y el refresh de contactos; nunca dos a la vez con la misma sesión).
            self.desconectar()


class TelethonUserSender(_TelethonBase, MessageSender):
    def __init__(self, api_id=None, api_hash=None, session=None, sleep: Callable[[float], None] = time.sleep):
        super().__init__(api_id, api_hash, session)
        self._sleep = sleep

    def _bloqueado(self, error) -> bool:
        from telethon import errors

        return isinstance(
            error,
            (
                errors.UserIsBlockedError,
                errors.UserPrivacyRestrictedError,
                errors.PeerIdInvalidError,
                errors.InputUserDeactivatedError,
            ),
        )

    def _ejecutar(self, accion):
        from telethon import errors

        try:
            accion()
            return SendResult(ok=True)
        except errors.FloodWaitError as fw:
            if fw.seconds > _MAX_FLOOD_WAIT:
                raise
            self._sleep(fw.seconds)
            accion()
            return SendResult(ok=True)
        except Exception as error:
            if self._bloqueado(error):
                return SendResult(ok=False, blocked=True)
            raise

    @staticmethod
    def _entidad(chat_id):
        # id numérico → int; "me", username u otro → tal cual (Telethon los resuelve).
        s = str(chat_id)
        return int(s) if s.lstrip("-").isdigit() else s

    def enviar(self, chat_id: str, text: str) -> SendResult:
        return self._ejecutar(lambda: self._c().send_message(self._entidad(chat_id), text))

    def enviar_foto(self, chat_id: str, image_url: str, caption: str = "") -> SendResult:
        import io

        import requests

        resp = requests.get(image_url, timeout=20)
        resp.raise_for_status()   # M13: no enviar una página de error 404/403 como si fuera la foto
        data = resp.content

        def _enviar():
            # BytesIO CON nombre+extensión => Telegram lo trata como FOTO (no como documento
            # 'unnamed'). Se recrea por intento porque BytesIO se consume al leerse (reintentos).
            archivo = io.BytesIO(data)
            archivo.name = "lista.jpg"
            return self._c().send_file(
                self._entidad(chat_id), archivo, caption=caption or None, force_document=False
            )

        return self._ejecutar(_enviar)


class TelethonContacts(_TelethonBase):
    """Lista los contactos de la cuenta (para usarlos como destinatarios / mostrarlos en el panel)."""

    def listar(self) -> list[dict]:
        from telethon.tl.functions.contacts import GetContactsRequest

        result = self._c()(GetContactsRequest(hash=0))
        contactos = []
        for u in getattr(result, "users", []):
            nombre = " ".join(filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")]))
            telefono = str(getattr(u, "phone", "") or "")  # para buscar/mostrar por número en el panel
            contactos.append(
                {"id": str(u.id), "name": nombre or (getattr(u, "username", "") or str(u.id)), "phone": telefono}
            )
        return contactos


class TelethonAccount(_TelethonBase):
    """Estado de la sesión userbot: ¿sigue autorizada (válida) o hay que RENOVARLA? + identidad.

    La usa el panel para mostrar en el header si la cuenta de Telegram con la que se envía está
    conectada correctamente o caducó/se revocó (en cuyo caso hay que volver a iniciar sesión)."""

    def estado(self) -> dict:
        c = self._c()
        try:
            if not c.is_user_authorized():
                # Hay sesión guardada pero Telegram ya no la acepta → hay que renovar (re-login).
                return {"authorized": False, "me": None}
            me = c.get_me()
            nombre = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")]))
            return {
                "authorized": True,
                "me": {
                    "id": str(getattr(me, "id", "")),
                    "name": nombre or (getattr(me, "username", "") or ""),
                    "username": getattr(me, "username", "") or "",
                    "phone": str(getattr(me, "phone", "") or ""),
                },
            }
        finally:
            # El verificador se llama en un poll (60s): cerramos la conexión para no acumularlas
            # en contenedores Lambda calientes. M11: usar desconectar() (resetea self._client) para
            # que una reutilización del contenedor no use un cliente ya desconectado (muerto).
            self.desconectar()


class ContactRecipients:
    """Adapta los contactos de Telethon a la interfaz que usan BroadcastList/DeliverBatch."""

    def __init__(self, contacts: TelethonContacts):
        self._contacts = contacts

    def listar_activos(self) -> list[str]:
        return [c["id"] for c in self._contacts.listar()]

    def listar_todos(self) -> list[dict]:
        return [{"chatId": c["id"], "name": c["name"], "phone": c.get("phone", "")} for c in self._contacts.listar()]

    def registrar(self, chat_id: str, status: str) -> None:
        pass  # no aplica a contactos de una cuenta

    def marcar_inactivo(self, chat_id: str) -> None:
        pass  # los bloqueos no se persisten para contactos


class CachedContacts:
    """Contactos desde la caché en DynamoDB (rápido, para el panel; sin Telethon en vivo)."""

    def __init__(self, config_store):
        self._cfg = config_store

    def _items(self):
        return self._cfg.get_contacts()

    def listar_activos(self) -> list[str]:
        return [str(c.get("id") or c.get("chatId")) for c in self._items()]

    def listar_todos(self) -> list[dict]:
        return [
            {
                "chatId": str(c.get("id") or c.get("chatId")),
                "name": c.get("name", ""),
                "phone": str(c.get("phone", "") or ""),
            }
            for c in self._items()
        ]

    def registrar(self, chat_id: str, status: str) -> None:
        pass

    def marcar_inactivo(self, chat_id: str) -> None:
        pass
