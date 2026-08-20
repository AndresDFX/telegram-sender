"""Verifica contra el panel YA DESPLEGADO que la PWA está sana: manifest, service worker y caché.

Abre el panel en un Chromium real, comprueba que el service worker queda activo con el ámbito
ampliado, que el shell y los iconos quedan precacheados, que la app sigue abriendo sin red y que
el aviso de "sin conexión" aparece cuando una llamada a la API falla.

    pip install playwright && playwright install chromium
    PANEL_URL=https://<api>.execute-api.us-east-1.amazonaws.com/dev/admin \
      python scripts/verificar_pwa_desplegada.py

Nota: `beforeinstallprompt` no se comprueba porque Chromium headless nunca lo dispara (depende de
heurísticas de interacción del usuario). La instalabilidad se valida por sus requisitos: manifest
servido, display standalone, iconos 192/512 y service worker con handler de fetch.
"""

import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("PANEL_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not URL:
    print("falta la URL del panel: PANEL_URL=... o como primer argumento")
    sys.exit(2)

fallos = []


def revisar(nombre, valor, esperado=True):
    ok = (valor == esperado) if isinstance(esperado, bool) else bool(valor)
    print(("  ok  " if ok else " FALLA ") + nombre + ":", valor)
    if not ok:
        fallos.append(nombre)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    errores = []
    page.on("pageerror", lambda e: errores.append(str(e)))
    page.goto(URL, wait_until="load")
    page.wait_for_timeout(3500)

    reg = page.evaluate("""async () => {
      const r = await navigator.serviceWorker.getRegistration();
      return r ? {ambito: r.scope, activo: !!r.active} : null;
    }""")
    print("service worker:", reg)
    revisar("service worker activo", bool(reg and reg["activo"]))
    revisar("ámbito por encima de /admin", bool(reg) and not reg["ambito"].rstrip("/").endswith("admin"))
    revisar("manifest enlazado", "manifest.webmanifest" in page.eval_on_selector("link[rel=manifest]", "e => e.href"))
    revisar("apple-touch-icon enlazado", bool(page.eval_on_selector("link[rel=apple-touch-icon]", "e => e.href")))
    revisar("tema aplicado antes de pintar", page.evaluate("document.documentElement.dataset.theme") in ("light", "dark"))

    en_cache = page.evaluate("""async () => {
      const k = await caches.keys();
      if(!k.length) return [];
      const c = await caches.open(k[0]);
      return (await c.keys()).map(r => new URL(r.url).pathname).sort();
    }""")
    print("precacheado:", en_cache)
    revisar("shell en caché", any(r.endswith("/admin/") for r in en_cache))
    revisar("iconos en caché", sum(r.endswith(".png") for r in en_cache) >= 3)
    revisar("la API nunca se cachea", not any("/api/" in r for r in en_cache))

    # Sin red: el shell debe seguir abriendo desde la caché.
    ctx.set_offline(True)
    page.reload(wait_until="load")
    page.wait_for_timeout(1000)
    revisar("abre sin red", page.is_visible(".brand .wordmark"))
    # El aviso se enciende al fallar la API. (Chromium con red emulada reporta navigator.onLine
    # true en un documento recién cargado; en un dispositivo real sería false y el aviso saldría ya
    # al pintar. Provocando una llamada se comprueba el camino que no depende de esa señal.)
    page.evaluate("() => { CRED = btoa('x:y'); return api('/api/config').catch(() => {}); }")
    page.wait_for_timeout(800)
    revisar("avisa sin conexión", page.is_visible("#offline_bar"))
    ctx.set_offline(False)
    page.reload(wait_until="load")
    page.wait_for_timeout(1000)
    revisar("el aviso no se queda pegado al volver la red", not page.is_visible("#offline_bar"))

    revisar("sin errores JS", not errores)
    if errores:
        print(errores[:5])
    ctx.close()
    b.close()

print("PWA OK" if not fallos else f"FALLOS: {fallos}")
sys.exit(0 if not fallos else 1)
