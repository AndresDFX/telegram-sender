# Despliega el stack telegram-sync-dev en AWS.
# Lee credenciales/secretos de .env.aws y .env.deploy (gitignored); no los incrusta.
# Usa una S3 key con hash del zip para que CloudFormation detecte el cambio de código.
# Requisito previo: scripts/package-lambda.ps1 (genera .build/telegram-broadcaster.zip).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Load-EnvFile($path) {
  $h = @{}
  if (-not (Test-Path $path)) { throw "No existe $path" }
  foreach ($line in Get-Content $path) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith('#')) { continue }
    $t = $t -replace '^\$env:', ''
    $i = $t.IndexOf('=')
    if ($i -lt 1) { continue }
    $val = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
    $h[$t.Substring(0, $i).Trim()] = $val
  }
  return $h
}

$aws = Load-EnvFile (Join-Path $Root '.env.aws')
$dep = Load-EnvFile (Join-Path $Root '.env.deploy')
$env:AWS_ACCESS_KEY_ID = $aws['AWS_ACCESS_KEY_ID']
$env:AWS_SECRET_ACCESS_KEY = $aws['AWS_SECRET_ACCESS_KEY']
$env:AWS_REGION = $aws['AWS_REGION']
$env:AWS_DEFAULT_REGION = $aws['AWS_REGION']

$account = (aws sts get-caller-identity --query Account --output text).Trim()
$bucket = "telegram-sync-lambda-$account-$($aws['AWS_REGION'])"
$zip = Join-Path $Root ".build/telegram-broadcaster.zip"
if (-not (Test-Path $zip)) { throw "No existe $zip. Corre scripts/package-lambda.ps1 primero." }

$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$key = "lambda/telegram-broadcaster-$hash.zip"
Write-Host "Subiendo artefacto a s3://$bucket/$key ..."
aws s3 cp $zip "s3://$bucket/$key" | Out-Null

$params = @(
  "ProjectName=telegram-sync",
  "EnvironmentName=dev",
  "TelegramBotToken=$($dep['TELEGRAM_BOT_TOKEN'])",
  "WebhookSecretToken=$($dep['WEBHOOK_SECRET_TOKEN'])",
  "LambdaCodeS3Bucket=$bucket",
  "LambdaCodeS3Key=$key",
  "WorkerReservedConcurrency=0",
  "BroadcastBatchSize=150",
  "AdminUser=admin",
  "AdminPassword=$($dep['ADMIN_PASSWORD'])",
  "SendMode=userbot",
  "TelethonApiId=$($dep['TELETHON_API_ID'])",
  "TelethonApiHash=$($dep['TELETHON_API_HASH'])",
  "TelethonSession=$($dep['TELETHON_SESSION'])"
)
Write-Host "Desplegando stack telegram-sync-dev ..."
aws cloudformation deploy `
  --stack-name telegram-sync-dev `
  --template-file (Join-Path $Root 'infra/cloudformation/template.yaml') `
  --parameter-overrides $params `
  --capabilities CAPABILITY_NAMED_IAM `
  --region $env:AWS_REGION
if ($LASTEXITCODE -ne 0) { throw "cloudformation deploy falló (exit $LASTEXITCODE)" }

$adminUrl = (aws cloudformation describe-stacks --stack-name telegram-sync-dev `
  --query "Stacks[0].Outputs[?OutputKey=='AdminUrl'].OutputValue" --output text).Trim()
Write-Host "OK. AdminUrl: $adminUrl"
