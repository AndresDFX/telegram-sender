"""Construye el zip de la Lambda. Pensado para correr DENTRO de python:3.12-slim (Linux),
de modo que dependencias y separadores de ruta del zip sean nativos de Linux.

    docker run --rm -v "<root>:/work" -w /work python:3.12-slim python /work/scripts/_build_lambda_pkg.py
"""

import os
import shutil
import subprocess
import sys

ROOT = "/work"
SRC = os.path.join(ROOT, "src", "lambda")
PKG = os.path.join(ROOT, ".build", "pkg")
REQ = os.path.join(SRC, "requirements.txt")

shutil.rmtree(PKG, ignore_errors=True)
os.makedirs(PKG)

subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "-r", REQ, "-t", PKG]
)

# Copia el árbol de paquetes (domain/, application/, adapters/, entrypoints/, wiring.py)
# preservando la estructura, para que los imports de paquete funcionen en Lambda.
for dirpath, dirnames, filenames in os.walk(SRC):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    rel = os.path.relpath(dirpath, SRC)
    dest = PKG if rel == "." else os.path.join(PKG, rel)
    os.makedirs(dest, exist_ok=True)
    for f in filenames:
        if f.endswith(".py"):
            shutil.copy(os.path.join(dirpath, f), dest)

# Limpia cachés de bytecode para un zip determinista.
for dirpath, dirnames, _ in os.walk(PKG):
    for d in list(dirnames):
        if d == "__pycache__":
            shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)

out = shutil.make_archive(os.path.join(ROOT, ".build", "telegram-broadcaster"), "zip", PKG)
print(f"OK: {out} ({os.path.getsize(out)} bytes)")
