# Sube a GitHub los Secrets y Variables que usa .github/workflows/deploy.yml, leyendo los valores
# de .env.aws y .env.deploy (gitignored). NO imprime los valores (van por stdin a `gh`).
#
# Requisito: `gh` autenticado como una cuenta con ADMIN sobre el repo (la dueña, AndresDFX).
# Como la API de GitHub usa TOKEN (no SSH), el alias github-personal NO sirve aquí; primero:
#     gh auth login --hostname github.com    # elige/inicia sesion como AndresDFX
# (o `gh auth login` con un Personal Access Token de AndresDFX con scope 'repo').
# Luego:  ./scripts/set-github-secrets.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Repo = "AndresDFX/telegram-sender"

function Load-EnvFile($path) {
  $h = @{}
  if (-not (Test-Path $path)) { throw "No existe $path" }
  foreach ($line in Get-Content $path) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith('#')) { continue }
    $t = $t -replace '^\$env:', ''
    $i = $t.IndexOf('=')
    if ($i -lt 1) { continue }
    $h[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
  }
  return $h
}

# Verifica permiso de la cuenta gh activa sobre el repo (necesita ADMIN para secrets).
$perm = (gh repo view $Repo --json viewerPermission --jq ".viewerPermission" 2>$null)
if ($perm -ne "ADMIN") {
  $who = (gh api user --jq ".login" 2>$null)
  Write-Host "La cuenta gh activa ('$who') tiene permiso '$perm' sobre $Repo; se requiere ADMIN."
  Write-Host "Inicia sesion como la cuenta duena del repo:  gh auth login --hostname github.com"
  exit 1
}

$aws = Load-EnvFile (Join-Path $Root '.env.aws')
$dep = Load-EnvFile (Join-Path $Root '.env.deploy')

# (nombre en GitHub, valor) -> Secret cifrado. OJO: gh lee de stdin SOLO si se OMITE --body;
# pasar `--body -` guarda el LITERAL "-" (no stdin) y corrompe el secret (deploy falla con
# "security token invalid"). Usamos `--body $val` (forma documentada). gh cifra el valor con la
# clave pública del repo antes de subirlo; en la máquina del dueño el riesgo de args/log es menor.
$secrets = [ordered]@{
  AWS_ACCESS_KEY_ID    = $aws['AWS_ACCESS_KEY_ID']
  AWS_SECRET_ACCESS_KEY= $aws['AWS_SECRET_ACCESS_KEY']
  TELEGRAM_BOT_TOKEN   = $dep['TELEGRAM_BOT_TOKEN']
  WEBHOOK_SECRET_TOKEN = $dep['WEBHOOK_SECRET_TOKEN']
  ADMIN_PASSWORD       = $dep['ADMIN_PASSWORD']
  TELETHON_API_ID      = $dep['TELETHON_API_ID']
  TELETHON_API_HASH    = $dep['TELETHON_API_HASH']
  TELETHON_SESSION     = $dep['TELETHON_SESSION']
}
foreach ($name in $secrets.Keys) {
  $val = $secrets[$name]
  if ([string]::IsNullOrEmpty($val)) { Write-Host "  - ${name}: OMITIDO (vacio en .env)"; continue }
  gh secret set $name --repo $Repo --body $val   # valor EXACTO (no usar `--body -`: gh lo toma literal)
  Write-Host "  - secret ${name}: OK"
}

# Variables (no secretas) que controlan el workflow.
gh variable set DEPLOY_ENABLED --repo $Repo --body "true"
gh variable set ALERT_EMAIL    --repo $Repo --body "andres_dfx@hotmail.com"
Write-Host "  - variables DEPLOY_ENABLED, ALERT_EMAIL: OK"

Write-Host "Listo. El proximo push a main desplegara via GitHub Actions."
