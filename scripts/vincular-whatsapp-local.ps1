# Vincula tu WhatsApp ejecutando el servicio LOCALMENTE (desde tu IP residencial, que
# WhatsApp acepta mejor que una IP de datacenter). La sesion se guarda en DynamoDB, asi
# que Render la reutiliza despues sin re-escanear.
#
# NOTA: solo ASCII a proposito. Windows PowerShell 5.1 lee los .ps1 UTF-8-sin-BOM como
# ANSI; un acento puede decodificarse como comilla tipografica y romper el parseo.
#
# Uso (desde la raiz del repo):
#   ./scripts/vincular-whatsapp-local.ps1                        # QR en vivo en el navegador
#   ./scripts/vincular-whatsapp-local.ps1 -Pair 573001234567     # codigo de 8 digitos
#   ./scripts/vincular-whatsapp-local.ps1 -Reset                 # borra la sesion y re-vincula
#
# Requisitos: Docker Desktop corriendo; .env.aws y .env.deploy en la raiz.
param(
  [switch]$Reset,
  [int]$Port = 8099,
  [string]$Pair = ""
)
# Continue (no Stop): en PowerShell 5.1 el stderr de comandos nativos (docker) con Stop
# se vuelve error fatal. Los fallos que importan se validan por exit code / try-catch.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot

function Load-EnvFile($path) {
  $h = @{}
  if (-not (Test-Path $path)) { throw "No existe $path" }
  foreach ($l in Get-Content $path) {
    $t = $l.Trim(); if (-not $t -or $t.StartsWith('#')) { continue }
    $t = $t -replace '^\$env:', ''; $i = $t.IndexOf('='); if ($i -lt 1) { continue }
    $h[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
  }
  return $h
}

$aws = Load-EnvFile (Join-Path $Root '.env.aws')
$dep = Load-EnvFile (Join-Path $Root '.env.deploy')
$tok = $dep['WHATSAPP_TOKEN']
$tbl = 'telegram-sync-dev-whatsapp-auth'
$hdr = @{ Authorization = "Bearer $tok" }

Write-Host "==> Construyendo imagen sender-wa..." -ForegroundColor Cyan
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
docker build -t sender-wa (Join-Path $Root 'whatsapp-service') | Out-Host
$code = $LASTEXITCODE; $ErrorActionPreference = $prev
if ($code -ne 0) { throw "docker build fallo ($code)" }

docker rm -f sender-wa-local 2>$null | Out-Null
Write-Host "==> Levantando contenedor en :$Port..." -ForegroundColor Cyan
docker run -d --name sender-wa-local -p "$($Port):8080" `
  -e WHATSAPP_TOKEN=$tok -e WHATSAPP_AUTH_TABLE=$tbl -e AWS_REGION=$($aws['AWS_REGION']) `
  -e AWS_ACCESS_KEY_ID=$($aws['AWS_ACCESS_KEY_ID']) -e AWS_SECRET_ACCESS_KEY=$($aws['AWS_SECRET_ACCESS_KEY']) `
  sender-wa | Out-Null

# esperar a que el servicio responda
$base = "http://localhost:$Port"
for ($i = 0; $i -lt 20; $i++) {
  try { Invoke-RestMethod "$base/health" -TimeoutSec 3 | Out-Null; break } catch { Start-Sleep -Seconds 1 }
}

if ($Reset) {
  Write-Host "==> Reiniciando sesion (POST /reset)..." -ForegroundColor Cyan
  try {
    Invoke-RestMethod "$base/reset" -Method POST -Headers $hdr -TimeoutSec 20 | Out-Null
    Write-Host "   sesion reiniciada"
    Start-Sleep -Seconds 3
  } catch { Write-Host "   /reset fallo: $_" -ForegroundColor Red }
}

if ($Pair) {
  $num = ($Pair -replace '[^0-9]', '')
  Write-Host "==> Pidiendo codigo de emparejamiento para $num..." -ForegroundColor Cyan
  Start-Sleep -Seconds 3
  $body = @{ number = $num } | ConvertTo-Json -Compress
  try {
    $r = Invoke-RestMethod "$base/pair" -Method POST -Headers $hdr -ContentType 'application/json' -Body $body -TimeoutSec 90
    Write-Host ""
    Write-Host "  CODIGO: $($r.pairingCode)" -ForegroundColor Green
    Write-Host "  WhatsApp -> Dispositivos vinculados -> Vincular con numero de telefono -> ingresa el codigo." -ForegroundColor Yellow
  } catch { Write-Host "  /pair fallo: $_" -ForegroundColor Red }
}
else {
  $url = "$base/qr?token=$tok"
  Write-Host "==> Abriendo QR en vivo: $url" -ForegroundColor Cyan
  Start-Process $url
  Write-Host "  WhatsApp -> Dispositivos vinculados -> Vincular un dispositivo -> escanea el QR." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Esperando vinculacion (Ctrl+C para salir)..." -ForegroundColor Cyan
for ($i = 0; $i -lt 100; $i++) {
  try {
    $s = Invoke-RestMethod "$base/status" -Headers $hdr -TimeoutSec 8
    if ($s.connected) { Write-Host "OK: CONECTADO como $($s.me.id). La sesion quedo en DynamoDB; Render la reutilizara." -ForegroundColor Green; break }
  } catch {}
  Start-Sleep -Seconds 3
}
Write-Host "(El contenedor sigue corriendo como 'sender-wa-local'. Detenlo con: docker rm -f sender-wa-local)"
