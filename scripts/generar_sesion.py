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

# .env.deploy en la raíz del repo (un nivel arriba de scripts/).
ENV_DEPLOY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.deploy")


def _pedir(nombre: str) -> str:
    return os.environ.get(nombre) or input(f"{nombre}: ").strip()


def _guardar_env(valores: dict) -> None:
    """Actualiza/añade las claves en .env.deploy (gitignored) sin duplicar."""
    lineas = []
    if os.path.exists(ENV_DEPLOY):
        with open(ENV_DEPLOY, encoding="utf-8") as f:
            lineas = f.read().splitlines()
    restantes = dict(valores)
    out = []
    for ln in lineas:
        clave = ln.split("=", 1)[0].strip() if "=" in ln else None
        if clave in restantes:
            out.append(f"{clave}={restantes.pop(clave)}")
        else:
            out.append(ln)
    for clave, valor in restantes.items():
        out.append(f"{clave}={valor}")
    with open(ENV_DEPLOY, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def main() -> None:
    api_id = _pedir("API_ID")
    api_hash = _pedir("API_HASH")
    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        yo = client.get_me()
        session = client.session.save()
        print(f"\nAutenticado como: {getattr(yo, 'first_name', '')} (@{getattr(yo, 'username', '')})")
        _guardar_env({"TELETHON_API_ID": api_id, "TELETHON_API_HASH": api_hash, "TELETHON_SESSION": session})
        print(f"\n✓ Guardado en {ENV_DEPLOY} (gitignored):")
        print("  TELETHON_API_ID, TELETHON_API_HASH, TELETHON_SESSION")
        print("\nAvísame y activo el modo userbot (redeploy con SEND_MODE=userbot).")
        print("⚠️  Ese TELETHON_SESSION da acceso TOTAL a tu cuenta: no lo compartas.")


if __name__ == "__main__":
    main()
