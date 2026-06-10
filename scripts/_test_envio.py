"""Prueba de envío del userbot: manda un mensaje de prueba a un número, usando la
sesión ya generada en .env.deploy. No interactivo. Uso:  python scripts/_test_envio.py [+57...]"""

import os
import sys

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.deploy")


def val(key):
    for ln in open(ENV, encoding="utf-8").read().splitlines():
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1].strip()
    raise SystemExit(f"Falta {key} en .env.deploy")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "+573188468892"
    msg = (
        "✅ Prueba TelegramSender\n\n"
        "Así llegarán las listas del canal: con el +15% aplicado, sin la ubicación, "
        "y con tu footer/imagen si los configuras."
    )
    client = TelegramClient(StringSession(val("TELETHON_SESSION")), int(val("TELETHON_API_ID")), val("TELETHON_API_HASH"))
    client.connect()
    try:
        if not client.is_user_authorized():
            raise SystemExit("La sesión no está autorizada.")
        try:
            entidad = client.get_entity(target)
        except Exception:
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact

            res = client(ImportContactsRequest([InputPhoneContact(client_id=0, phone=target, first_name="Prueba", last_name="")]))
            entidad = res.users[0] if getattr(res, "users", None) else target
        client.send_message(entidad, msg)
        print(f"OK: enviado a {target}")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
