"""Pasa por `node --check` el JS que el panel lleva embebido (y el del service worker).

El panel es una sola cadena HTML dentro de entrypoints/admin.py: un paréntesis mal cerrado no lo
detecta ningún test de Python (la cadena es válida) y solo se vería como pantalla en blanco ya
desplegado. Esto lo caza antes.

    python scripts/revisar_js_panel.py
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "lambda"))
from entrypoints import admin  # noqa: E402

TMP = os.path.join(ROOT, ".build", "js")
os.makedirs(TMP, exist_ok=True)

trozos = [("panel", js) for js in re.findall(r"<script>(.*?)</script>", admin._PAGE, re.S)]
trozos.append(("sw.js", admin._sw_resp("/admin")["body"]))

ok = True
for i, (etiqueta, js) in enumerate(trozos):
    ruta = os.path.join(TMP, f"t{i}.js")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(js)
    r = subprocess.run(["node", "--check", ruta], capture_output=True, text=True)
    print(f"{etiqueta} #{i} ({len(js)} chars):", "OK" if r.returncode == 0 else r.stderr.strip()[:500])
    ok = ok and r.returncode == 0

print("TODO OK" if ok else "HAY ERRORES DE SINTAXIS")
sys.exit(0 if ok else 1)
