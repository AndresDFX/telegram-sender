# Renombra el repositorio en GitHub (telegram-sender -> replica) y deja el remoto local apuntando
# al nombre nuevo. Se hace en un script porque la API de GitHub necesita TOKEN (el alias SSH
# `github-personal` NO sirve) y esa autenticacion solo la puede dar la duena de la cuenta.
#
# Requisito: `gh` instalado y autenticado con ADMIN sobre el repo:
#     gh auth login --hostname github.com      # como AndresDFX (o PAT con scope 'repo')
# Uso:
#     ./scripts/renombrar-repo-github.ps1                 # AndresDFX/telegram-sender -> replica
#     ./scripts/renombrar-repo-github.ps1 -Nuevo otro-nombre
#
# GitHub deja REDIRECCION del nombre viejo (los clones existentes siguen funcionando), los Secrets,
# Variables e historial de Actions viajan con el repo, y el deploy usa claves estaticas (no OIDC
# atado al nombre): el CI no se rompe. Render sigue conectado por id de repo.
param(
  [string]$Duenio = "AndresDFX",
  [string]$Viejo  = "telegram-sender",
  [string]$Nuevo  = "replica"
)
$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  Write-Host "Falta la CLI de GitHub. Instalala (winget install GitHub.cli) y vuelve a intentar."
  exit 1
}

$repo = "$Duenio/$Viejo"
$perm = (gh repo view $repo --json viewerPermission --jq ".viewerPermission" 2>$null)
if ($perm -ne "ADMIN") {
  $who = (gh api user --jq ".login" 2>$null)
  Write-Host "La cuenta gh activa ('$who') tiene permiso '$perm' sobre $repo; renombrar exige ADMIN."
  Write-Host "Inicia sesion como la duena:  gh auth login --hostname github.com"
  exit 1
}

Write-Host "Renombrando $repo -> $Duenio/$Nuevo ..."
gh api -X PATCH "repos/$repo" -f name="$Nuevo" --jq ".full_name" | ForEach-Object { Write-Host "  ahora es: $_" }

# El remoto sigue funcionando por la redireccion, pero conviene dejarlo explicito.
$origen = (git remote get-url origin)
$nuevoOrigen = $origen -replace [regex]::Escape("$Duenio/$Viejo"), "$Duenio/$Nuevo"
if ($nuevoOrigen -ne $origen) {
  git remote set-url origin $nuevoOrigen
  Write-Host "  origin: $nuevoOrigen"
}

Write-Host ""
Write-Host "Listo. Comprobaciones sugeridas:"
Write-Host "  git push                                        # el push sigue saliendo (CI + Render)"
Write-Host "  python scripts/verificar_deploy_render.py       # Render sigue desplegando solo"
Write-Host "  (opcional) Render -> Settings -> Name, y actualizar la URL en el panel"
