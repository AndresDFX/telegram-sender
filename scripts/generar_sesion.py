"""Genera un StringSession de Telethon para enviar las listas como TU cuenta (userbot).

PASOS (se corre en TU computador, una sola vez):
  1. Entra a https://my.telegram.org → API development tools → crea una app.
     Anota API_ID (número) y API_HASH (cadena).
  2. Instala Telethon:   pip install telethon
  3. Corre:              python scripts/generar_sesion.py
     Te pedirá tu teléfono (formato +57...), el código que te llega por Telegram y,
     si tienes verificación en dos pasos, tu contraseña 2FA.
  4. Copia el StringSession que imprime y guárdalo (NO lo compartas: da acceso TOTAL
     a tu cuenta de Telegram). Lo pondrás como TELETHON_SESSION en el despliegue.

⚠️  Enviar masivamente desde una cuenta de usuario puede hacer que Telegram la limite
    o banee. Úsalo con cadencia moderada y solo con tus clientes reales.
"""

import os

try:
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient
except ImportError:
    raise SystemExit("Falta Telethon. Instálalo con:  pip install telethon")


def _pedir(nombre: str) -> str:
    return os.environ.get(nombre) or input(f"{nombre}: ").strip()


def main() -> None:
    api_id = int(_pedir("API_ID"))
    api_hash = _pedir("API_HASH")
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        yo = client.get_me()
        print(f"\nAutenticado como: {getattr(yo, 'first_name', '')} (@{getattr(yo, 'username', '')})")
        print("\n=== TU StringSession (guárdalo en SECRETO) ===\n")
        print(client.session.save())
        print(
            "\nGuárdalo así en .env.deploy (no se sube a git):\n"
            "  TELETHON_API_ID=<API_ID>\n"
            "  TELETHON_API_HASH=<API_HASH>\n"
            "  TELETHON_SESSION=<lo de arriba>\n"
            "y avísame para activar el modo userbot y desplegar."
        )


if __name__ == "__main__":
    main()
