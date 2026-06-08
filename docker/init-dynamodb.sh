#!/bin/sh
set -eu

ENDPOINT="${DYNAMODB_ENDPOINT:-http://dynamodb-local:8000}"
TABLE_NAME="${SUBSCRIBERS_TABLE:-SubscriptoresTelegram}"
PROCESSED_TABLE="${PROCESSED_UPDATES_TABLE:-ProcessedUpdates}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "Esperando DynamoDB Local en ${ENDPOINT}..."
until aws dynamodb list-tables --endpoint-url "${ENDPOINT}" --region "${REGION}" >/dev/null 2>&1; do
  sleep 1
done

create_if_missing() {
  table="$1"
  shift
  if aws dynamodb describe-table \
    --table-name "${table}" \
    --endpoint-url "${ENDPOINT}" \
    --region "${REGION}" >/dev/null 2>&1; then
    echo "La tabla ${table} ya existe."
    return 0
  fi
  echo "Creando tabla ${table}..."
  aws dynamodb create-table \
    --table-name "${table}" \
    --billing-mode PAY_PER_REQUEST \
    --endpoint-url "${ENDPOINT}" \
    --region "${REGION}" \
    "$@"
  echo "Tabla ${table} creada."
}

create_if_missing "${TABLE_NAME}" \
  --attribute-definitions \
    AttributeName=chatId,AttributeType=S \
    AttributeName=status,AttributeType=S \
  --key-schema AttributeName=chatId,KeyType=HASH \
  --global-secondary-indexes \
    "[{\"IndexName\":\"StatusIndex\",\"KeySchema\":[{\"AttributeName\":\"status\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}}]"

create_if_missing "${PROCESSED_TABLE}" \
  --attribute-definitions AttributeName=updateId,AttributeType=S \
  --key-schema AttributeName=updateId,KeyType=HASH

# Habilita TTL en expiresAt para igualar el template (DynamoDB Local no purga de
# forma activa, pero así la configuración local es fiel a la desplegada).
aws dynamodb update-time-to-live \
  --table-name "${PROCESSED_TABLE}" \
  --time-to-live-specification "Enabled=true,AttributeName=expiresAt" \
  --endpoint-url "${ENDPOINT}" \
  --region "${REGION}" >/dev/null 2>&1 || echo "TTL ya configurado o no soportado en local."

echo "Inicialización completada."
