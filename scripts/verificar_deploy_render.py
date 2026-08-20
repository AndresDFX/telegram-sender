"""Comprueba si el servicio WhatsApp de Render corre el MISMO código que hay en el repo.

Sirve para validar que el auto-deploy de Render funciona: haz push de un cambio en
`whatsapp-service/` y ejecuta este script con `--esperar`; si el host despliega solo, la huella
del servicio pasa a coincidir con la del repo en pocos minutos.

    python scripts/verificar_deploy_render.py                 # foto del estado ahora
    python scripts/verificar_deploy_render.py --esperar 900   # espera hasta 15 min a que despliegue
    WHATSAPP_URL=https://otro-host.example python scripts/verificar_deploy_render.py

Cómo compara (sin necesitar el token del servicio ni la API de Render):
  * `src`    — huella sha256 de `whatsapp-service/src/*.js` + `package.json`, que el propio
               servicio calcula al arrancar y publica en `/health`. Es la señal fuerte: si
               coincide, el código desplegado es idéntico al del working tree (los CRLF se
               normalizan, porque el checkout de Windows los tiene y el de Linux no).
  * `commit` — sólo informativo: lo inyecta Render (`RENDER_GIT_COMMIT`) y puede faltar en otros
               hosts. Se compara con `HEAD` (Render redespliega en CADA push a `main`, aunque no
               toque `whatsapp-service/`) y con el último commit que sí tocó el servicio.

Salida: 0 si el servicio está al día · 1 si está desfasado o no se pudo comprobar.
"""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:  # la consola de Windows es cp1252 y reventaría al imprimir flechas/avisos
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SERVICIO = RAIZ / "whatsapp-service"
URL_DEFECTO = "https://telegram-sender-dm43.onrender.com"


def huella_local() -> str:
    """La misma huella que calcula el servicio en `huellaFuente()` (index.js)."""
    h = hashlib.sha256()
    for f in sorted(p.name for p in (SERVICIO / "src").glob("*.js")):
        h.update((f + "\n").encode())
        h.update((SERVICIO / "src" / f).read_text(encoding="utf-8").replace("\r", "").encode())
    h.update(b"package.json\n")
    h.update((SERVICIO / "package.json").read_text(encoding="utf-8").replace("\r", "").encode())
    return h.hexdigest()[:12]


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=RAIZ, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return ""


def salud(url: str, timeout: int = 90) -> dict | None:
    """GET /health. El plan Free duerme a los 15 min: el primer intento puede tardar ~1 min."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        print(f"  · /health no respondió: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=os.environ.get("WHATSAPP_URL") or URL_DEFECTO)
    ap.add_argument("--esperar", type=int, default=0, metavar="SEG",
                    help="reintenta hasta SEG segundos a que el host despliegue el código nuevo")
    ap.add_argument("--intervalo", type=int, default=30, metavar="SEG")
    args = ap.parse_args()

    esperada = huella_local()
    commit_servicio = git("log", "-1", "--format=%h %ct", "--", "whatsapp-service")
    head = git("rev-parse", "--short", "HEAD")
    sucio = bool(git("status", "--porcelain", "--", "whatsapp-service"))
    print(f"servicio:        {args.url}")
    print(f"huella del repo: {esperada}" + ("  (⚠ hay cambios SIN COMMIT: el host nunca los tendrá)" if sucio else ""))
    if commit_servicio:
        print(f"último commit de whatsapp-service/: {commit_servicio.split()[0]}"
              + (f" · HEAD del repo: {head}" if head else ""))

    limite = time.time() + max(0, args.esperar)
    intento = 0
    while True:
        intento += 1
        h = salud(args.url)
        if h is not None:
            desplegada, commit = h.get("src"), h.get("commit")
            print(f"[{intento}] desplegado: src={desplegada or 'sin sello'} commit={commit or 'sin sello'} "
                  f"arranque={h.get('started_at') or '?'}")
            if desplegada == esperada:
                print("\nAL DÍA: el servicio corre exactamente el código del repo.")
                if commit:
                    print(f"  commit desplegado: {commit}")
                    # Render redespliega en cada push: un commit distinto a HEAD con la misma huella
                    # significa contenedor de antes del último push (mismo código, sin consecuencias).
                    if head and not head.startswith(commit) and not commit.startswith(head):
                        print(f"  · el contenedor es anterior al último push ({head}), pero el código es el mismo.")
                return 0
            if desplegada is None:
                # El servicio aún no tiene el sello -> es una versión ANTERIOR a este cambio.
                print("  · sin sello de build: corre una versión anterior (el sello se añadió en agosto 2026).")
            else:
                print("  · huella distinta: el código desplegado NO es el del repo.")
        if time.time() >= limite:
            print("\nDESFASADO: el servicio no corre el código del repo.")
            print("  Si acabas de hacer push y no cambia en ~10 min, el auto-deploy de Render está APAGADO:")
            print("  Render → el servicio → Settings → Build & Deploy → Auto-Deploy = On (rama main),")
            print("  o dispara el Deploy Hook (Settings → Deploy Hook) con un POST.")
            return 1
        time.sleep(args.intervalo)


if __name__ == "__main__":
    sys.exit(main())
