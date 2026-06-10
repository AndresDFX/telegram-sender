# Activa el modo userbot: empaqueta, sube y redespliega con SEND_MODE=userbot usando
# las credenciales Telethon que dejó scripts/generar_sesion.py en .env.deploy.
# Uso:  .\scripts\activar_userbot.ps1     (tras correr generar_sesion.py)
# EAP=Continue: docker/aws escriben warnings/progreso a stderr; no deben ser fatales.
# Validamos los pasos críticos por código de salida.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Read-EnvValue($file, $key) {
    $line = Get-Content $file | Where-Object { $_ -like "$key=*" } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -split '=', 2)[1].Trim()
}

# Credenciales AWS desde .env.aws
Get-Content .env.aws | ForEach-Object {
    $l = $_ -replace '^\s*\$env:', ''
    if ($l -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?([^"]*)"?') { Set-Item -Path "env:$($matches[1])" -Value $matches[2] }
}
$env:AWS_DEFAULT_REGION = "us-east-1"

# Valores del despliegue / Telethon desde .env.deploy
$token   = Read-EnvValue .env.deploy "TELEGRAM_BOT_TOKEN"
$secret  = Read-EnvValue .env.deploy "WEBHOOK_SECRET_TOKEN"
$adminpw = Read-EnvValue .env.deploy "ADMIN_PASSWORD"
$apiId   = Read-EnvValue .env.deploy "TELETHON_API_ID"
$apiHash = Read-EnvValue .env.deploy "TELETHON_API_HASH"
$session = Read-EnvValue .env.deploy "TELETHON_SESSION"

if (-not $session -or -not $apiId -or -not $apiHash) {
    throw "Faltan TELETHON_API_ID/HASH/SESSION en .env.deploy. Corre primero: python scripts/generar_sesion.py"
}

Write-Host "Empaquetando..."
& "$PSScriptRoot\package-lambda.ps1"
if (-not (Test-Path .build\telegram-broadcaster.zip)) { throw "No se genero el zip" }

$acct   = (aws sts get-caller-identity --query Account --output text).Trim()
$bucket = "telegram-sync-lambda-$acct-us-east-1"
$hash   = (Get-FileHash .build\telegram-broadcaster.zip -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$key    = "lambda/telegram-broadcaster-$hash.zip"
aws s3 cp .build\telegram-broadcaster.zip "s3://$bucket/$key" | Out-Null

Write-Host "Desplegando con SEND_MODE=userbot..."
aws cloudformation deploy `
    --template-file infra/cloudformation/template.yaml `
    --stack-name telegram-sync-dev `
    --parameter-overrides ProjectName=telegram-sync EnvironmentName=dev `
        "TelegramBotToken=$token" "WebhookSecretToken=$secret" "LambdaCodeS3Bucket=$bucket" "LambdaCodeS3Key=$key" `
        WorkerReservedConcurrency=0 AdminUser=admin "AdminPassword=$adminpw" `
        SendMode=userbot "TelethonApiId=$apiId" "TelethonApiHash=$apiHash" "TelethonSession=$session" `
    --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset

Write-Host "Listo. SEND_MODE=userbot activo. El panel ya lista tus contactos."
