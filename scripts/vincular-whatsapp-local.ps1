# Vincula tu WhatsApp ejecutando el servicio LOCALMENTE (desde tu IP residencial,
# que WhatsApp acepta mejor que una IP de datacenter). La sesión se guarda en DynamoDB,
# así que Render la reutiliza después sin re-escanear.
#
# Uso (desde la raíz del repo):
#   ./scripts/vincular-whatsapp-local.ps1                 # QR en vivo en el navegador
#   ./scripts/vincular-whatsapp-local.ps1 -Pair 573001234567   # código de 8 dígitos
#   ./scripts/vincular-whatsapp-local.ps1 -Reset          # borra la sesión y re-vincula
#
# Requisitos: Docker Desktop corriendo; .env.aws y .env.deploy presentes en la raíz.
param(
  [switch]$Reset,
  [int]$Port = 8099,
  [string]$Pair = ""
)
$ErrorActionPreference = "Stop"
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

Write-Host "==> Construyendo imagen sender-wa..." -ForegroundColor Cyan
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
docker build -t sender-wa (Join-Path $Root 'whatsapp-service') | Out-Host
$code = $LASTEXITCODE; $ErrorActionPreference = $prev
if ($code -ne 0) { throw "docker build falló ($code)" }

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
  Write-Host "==> Reiniciando sesión (POST /reset)..." -ForegroundColor Cyan
  try {
    Invoke-RestMethod "$base/reset" -Method POST -Headers @{Authorization = "Bearer $tok" } -TimeoutSec 20 | Out-Null
    Write-Host "   sesión reiniciada"
    Start-Sleep -Seconds 3
  } catch { Write-Host "   /reset falló: $_" -ForegroundColor Red }
}

if ($Pair) {
  $num = ($Pair -replace '[^0-9]', '')
  Write-Host "==> Pidiendo código de emparejamiento para $num..." -ForegroundColor Cyan
  Start-Sleep -Seconds 3
  $r = Invoke-RestMethod "$base/pair" -Method POST -Headers @{Authorization = "Bearer $tok" } -ContentType 'application/json' -Body "{`"number`":`"$num`"}" -TimeoutSec 30
  Write-Host ""
  Write-Host "  CÓDIGO: $($r.pairingCode)" -ForegroundColor Green
  Write-Host "  WhatsApp -> Dispositivos vinculados -> Vincular con número de teléfono -> ingresa el código." -ForegroundColor Yellow
}
else {
  $url = "$base/qr?token=$tok"
  Write-Host "==> Abriendo QR en vivo: $url" -ForegroundColor Cyan
  Start-Process $url
  Write-Host "  WhatsApp -> Dispositivos vinculados -> Vincular un dispositivo -> escanea el QR." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Esperando vinculación (Ctrl+C para salir)..." -ForegroundColor Cyan
for ($i = 0; $i -lt 100; $i++) {
  try {
    $s = Invoke-RestMethod "$base/status" -Headers @{Authorization = "Bearer $tok" } -TimeoutSec 8
    if ($s.connected) { Write-Host "✅ CONECTADO como $($s.me.id). La sesión quedó en DynamoDB; Render la reutilizará." -ForegroundColor Green; break }
  } catch {}
  Start-Sleep -Seconds 3
}
Write-Host "(El contenedor sigue corriendo como 'sender-wa-local'. Páralo con: docker rm -f sender-wa-local)"
