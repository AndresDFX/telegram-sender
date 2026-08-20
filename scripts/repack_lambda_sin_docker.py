"""Reempaqueta el artefacto de la Lambda reemplazando SOLO los .py del proyecto (sin Docker).

`scripts/package-lambda.ps1` necesita Docker para instalar las dependencias con wheels de Linux.
Cuando no hay Docker en la máquina y el cambio es únicamente código propio (el caso normal: el
panel, un adapter, un caso de uso), se puede partir del zip que el stack ya tiene desplegado —que
trae esas dependencias compiladas— y sustituir dentro los .py de src/lambda.

Antes de tocar nada compara los .py del zip con `git HEAD`: si el artefacto desplegado no coincide
con el commit base, aborta (no se reemplaza a ciegas código de origen desconocido). Los .py se
normalizan a LF, así el resultado es byte a byte el que produciría el pipeline en Linux.

    # 1) traer el artefacto que está desplegado ahora
    KEY=$(aws cloudformation describe-stacks --stack-name telegram-sync-dev \
            --query "Stacks[0].Parameters[?ParameterKey=='LambdaCodeS3Key'].ParameterValue" --output text)
    aws s3 cp "s3://telegram-sync-lambda-<cuenta>-us-east-1/$KEY" .build/base.zip

    # 2) reempaquetar con los .py locales
    python scripts/repack_lambda_sin_docker.py

    # 3) subir y desplegar (los parámetros no pasados conservan su valor anterior en el stack)
    aws s3 cp .build/telegram-broadcaster.zip "s3://telegram-sync-lambda-<cuenta>-us-east-1/lambda/telegram-broadcaster-<sha>.zip"
    aws cloudformation deploy --stack-name telegram-sync-dev \
      --template-file infra/cloudformation/template.yaml \
      --parameter-overrides LambdaCodeS3Key=lambda/telegram-broadcaster-<sha>.zip \
      --capabilities CAPABILITY_NAMED_IAM --region us-east-1 --no-fail-on-empty-changeset

Si cambian las DEPENDENCIAS (requirements.txt) esto NO sirve: hay que construir con Docker o
dejar que el workflow de GitHub Actions lo haga.
"""

import hashlib
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, ".build", "base.zip")
OUT = os.path.join(ROOT, ".build", "telegram-broadcaster.zip")
SRC = os.path.join(ROOT, "src", "lambda")

# .py del proyecto (misma selección que scripts/_build_lambda_pkg.py: solo .py, sin __pycache__)
locales = {}
for dirpath, dirnames, filenames in os.walk(SRC):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for f in filenames:
        if f.endswith(".py"):
            ruta = os.path.join(dirpath, f)
            rel = os.path.relpath(ruta, SRC).replace(os.sep, "/")
            # A LF: el repo suele estar con core.autocrlf=true y el zip que construye CI (runner
            # Linux) lleva LF. Normalizar deja el artefacto idéntico al del pipeline.
            with open(ruta, "rb") as fh:
                locales[rel] = fh.read().replace(b"\r\n", b"\n")

print(f"{len(locales)} .py locales")

zin = zipfile.ZipFile(BASE)
en_zip = set(zin.namelist())

# 1) Coherencia: lo desplegado debe ser el HEAD actual (salvo lo que se está cambiando ahora).
head = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD", "src/lambda"],
                      capture_output=True, text=True, check=True, cwd=ROOT).stdout.split()
head_py = [p for p in head if p.endswith(".py")]
distintos, faltan = [], []
for p in head_py:
    rel = p[len("src/lambda/"):]
    blob = subprocess.run(["git", "show", f"HEAD:{p}"], capture_output=True, check=True, cwd=ROOT).stdout
    if rel not in en_zip:
        faltan.append(rel)
    elif zin.read(rel) != blob:
        distintos.append(rel)
print("en HEAD:", len(head_py), "| no están en el zip:", faltan or "ninguno",
      "| difieren del zip:", distintos or "ninguno")
if faltan or distintos:
    print("ABORTA: el artefacto desplegado no coincide con git HEAD")
    sys.exit(1)

# 2) Reescribe el zip: todo igual salvo los .py del proyecto (reemplazados y/o nuevos).
nuevos = [r for r in locales if r not in en_zip]
cambiados = [r for r in locales if r in en_zip and zin.read(r) != locales[r]]
print("nuevos:", nuevos, "| modificados:", cambiados)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
    for info in zin.infolist():
        datos = locales.get(info.filename, zin.read(info.filename))
        zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
        zi.external_attr = info.external_attr
        zi.create_system = info.create_system
        zi.compress_type = zipfile.ZIP_DEFLATED
        zout.writestr(zi, datos)
    plantilla = next(i for i in zin.infolist() if i.filename == "wiring.py")
    for rel in nuevos:
        zi = zipfile.ZipInfo(rel, date_time=plantilla.date_time)
        zi.external_attr = plantilla.external_attr        # 0644 en Linux, como el resto
        zi.create_system = plantilla.create_system
        zi.compress_type = zipfile.ZIP_DEFLATED
        zout.writestr(zi, locales[rel])
zin.close()

# 3) Verificación del resultado.
with zipfile.ZipFile(OUT) as z:
    assert z.testzip() is None
    for rel, datos in locales.items():
        assert z.read(rel) == datos, rel
    print("entradas:", len(z.namelist()), "| bytes:", os.path.getsize(OUT))
with open(OUT, "rb") as fh:
    print("sha256[:12] =", hashlib.sha256(fh.read()).hexdigest()[:12])
