$ErrorActionPreference = "Stop"

# Empaqueta la Lambda para el runtime python3.12 (Linux). Construye DENTRO de un
# contenedor Linux para que las dependencias y los separadores de ruta del zip sean
# nativos de Linux (empaquetar con Compress-Archive en Windows genera rutas con "\"
# y binarios .pyd/.exe que rompen en AWS Lambda). La lógica vive en
# scripts/_build_lambda_pkg.py para evitar problemas de quoting/CRLF.

$RootDir = Split-Path -Parent $PSScriptRoot
$BuildDir = Join-Path $RootDir ".build"
$ZipPath = Join-Path $BuildDir "telegram-broadcaster.zip"

New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }

Write-Host "Empaquetando dentro de python:3.12-slim (Linux)..."
$mount = "$($RootDir):/work"
docker run --rm -v $mount -w /work python:3.12-slim python /work/scripts/_build_lambda_pkg.py
if ($LASTEXITCODE -ne 0) { throw "Fallo el empaquetado en Docker (exit $LASTEXITCODE)" }

Write-Host "Artefacto listo: $ZipPath"
