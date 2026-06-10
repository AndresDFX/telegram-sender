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

from application.ports import MessageSender
from domain.models import SendResult

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

    def enviar(self, chat_id: str, text: str) -> SendResult:
        return self._ejecutar(lambda: self._c().send_message(int(chat_id), text))

    def enviar_foto(self, chat_id: str, image_url: str, caption: str = "") -> SendResult:
        import requests

        data = requests.get(image_url, timeout=20).content
        return self._ejecutar(lambda: self._c().send_file(int(chat_id), data, caption=caption or None))


class TelethonContacts(_TelethonBase):
    """Lista los contactos de la cuenta (para usarlos como destinatarios / mostrarlos en el panel)."""

    def listar(self) -> list[dict]:
        from telethon.tl.functions.contacts import GetContactsRequest

        result = self._c()(GetContactsRequest(hash=0))
        contactos = []
        for u in getattr(result, "users", []):
            nombre = " ".join(filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")]))
            contactos.append({"id": str(u.id), "name": nombre or (getattr(u, "username", "") or str(u.id))})
        return contactos


class ContactRecipients:
    """Adapta los contactos de Telethon a la interfaz que usan BroadcastList/DeliverBatch/admin."""

    def __init__(self, contacts: TelethonContacts):
        self._contacts = contacts

    def listar_activos(self) -> list[str]:
        return [c["id"] for c in self._contacts.listar()]

    def listar_todos(self) -> list[dict]:
        return [{"chatId": c["id"], "status": c["name"]} for c in self._contacts.listar()]

    def registrar(self, chat_id: str, status: str) -> None:
        pass  # no aplica a contactos de una cuenta

    def marcar_inactivo(self, chat_id: str) -> None:
        pass  # los bloqueos no se persisten para contactos
