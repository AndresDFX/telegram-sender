# Telegram → Telegram Sync

Sincronización serverless 1:1: cuando el canal fuente publica una lista, AWS la detecta, aplica markup del 15% a los precios y la distribuye por mensaje directo a cada suscriptor.

## Arquitectura (desacoplada por SQS)

La **ingesta** del canal fuente es por **sondeo del preview público** (`https://t.me/s/<canal>`): el
canal de precios no es nuestro y el bot no puede ser admin, así que un Lambda en cron (EventBridge) lo
lee, detecta publicaciones nuevas (por `message_id`, con high-water mark) y las encola. El **onboarding**
(`/start`·`/stop`) sigue por el webhook del bot.

```
EventBridge (cron) ──► Lambda poller ──┐
   (lee t.me/s/canal, HWM)             │
                                       ▼
Telegram (DM al bot) ─► API Gateway ─► Lambda receptor ──► SQS broadcast ─► Lambda worker ─► Telegram (DM)
   /start /stop                        (secret, dedup,         │           (envío 403/429,
                                        markup, encola)        ▼            concurrencia≤1)
                                       responde 200 rápido   SQS DLQ (lotes que agotan reintentos)
```

El worker hace el broadcast desde la cola: sobrevive a fallos parciales (reintentos + DLQ) y escala más
allá del timeout de Lambda. La concurrencia reservada del worker (=1, donde el límite de cuenta lo
permita) más el delay por envío mantienen el ritmo global bajo 30 msg/s.

> **Markup:** solo marca números con símbolo de moneda (`$`) en formato colombiano (`$325.000`), y
> redondea el resultado al **mil hacia arriba** (`$325.000` +15% → `$374.000`). No toca modelos ni
> specs (`A06 4-64GB`). Ver [`src/lambda/markup.py`](src/lambda/markup.py) y `specs/22`.

## Estructura

```
TelegramSender/
├── docker/                 # Entorno local (DynamoDB + webhook dev en modo inline)
├── infra/cloudformation/   # Stack AWS (API Gateway, Lambdas, SQS+DLQ, DynamoDB, EventBridge)
├── scripts/                # Empaquetado de Lambda (build en Linux) + smoke test
├── specs/                  # Especificaciones por fase
└── src/lambda/             # poller, handler (receptor), worker, broadcaster, clientes
```

## Requisitos

- Docker Desktop
- AWS CLI v2 (para despliegue)
- Python 3.12 (para empaquetar Lambda fuera de Docker)

## Desarrollo local

1. Copia variables de entorno:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Edita `.env` con tu `TELEGRAM_BOT_TOKEN`.

3. Levanta el stack local:

   ```powershell
   docker compose -f docker/docker-compose.yml up --build
   ```

4. Servicios disponibles:

   | Servicio        | URL                          |
   |-----------------|------------------------------|
   | Webhook local   | http://localhost:8080/webhook/telegram |
   | Health check    | http://localhost:8080/health |
   | DynamoDB Local  | http://localhost:8000        |

5. Prueba manual del webhook:

   ```powershell
   curl -X POST http://localhost:8080/webhook/telegram `
     -H "Content-Type: application/json" `
     -d '{"channel_post":{"chat":{"id":"-100123"},"text":"Producto A $100.00"}}'
   ```

6. Insertar suscriptor de prueba en DynamoDB Local:

   ```powershell
   aws dynamodb put-item `
     --table-name SubscriptoresTelegram `
     --item '{\"chatId\":{\"S\":\"123456789\"},\"status\":{\"S\":\"active\"}}' `
     --endpoint-url http://localhost:8000 `
     --region us-east-1
   ```

### Runtime Lambda en contenedor (opcional)

Para probar la imagen compatible con AWS Lambda Runtime Interface:

```powershell
docker compose -f docker/docker-compose.yml --profile lambda up --build lambda-runtime
```

Invocación local vía RIE en `http://localhost:9000/2015-03-31/functions/function/invocations`.

## Despliegue AWS (CloudFormation)

### 1. Empaquetar Lambda

```powershell
.\scripts\package-lambda.ps1
```

Genera `.build/telegram-broadcaster.zip`.

### 2. Subir artefacto a S3

```powershell
aws s3 cp .build/telegram-broadcaster.zip s3://TU-BUCKET/lambda/telegram-broadcaster.zip
```

### 3. Desplegar stack

```powershell
aws cloudformation deploy `
  --template-file infra/cloudformation/template.yaml `
  --stack-name telegram-sync-dev `
  --parameter-overrides `
    ProjectName=telegram-sync `
    EnvironmentName=dev `
    TelegramBotToken="TU_TOKEN" `
    WebhookSecretToken="UN_SECRETO_ALEATORIO" `
    LambdaCodeS3Bucket=TU-BUCKET `
    LambdaCodeS3Key=lambda/telegram-broadcaster.zip `
  --capabilities CAPABILITY_NAMED_IAM
```

> Genera el secreto con, p.ej., `[guid]::NewGuid().ToString("N")`. Debe ser el **mismo** valor que
> pasas a `setWebhook` abajo: así el receptor rechaza cualquier `POST` que no venga de Telegram.

### 4. Registrar webhook en Telegram

Tras el deploy, toma la URL del output `WebhookUrl`:

```powershell
aws cloudformation describe-stacks `
  --stack-name telegram-sync-dev `
  --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" `
  --output text
```

Regístrala con la Bot API **incluyendo el `secret_token`**:

```text
https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WebhookUrl>&secret_token=<MISMO_SECRETO>
```

### 5. Alta de suscriptores

Los usuarios se suscriben escribiéndole `/start` al bot por privado (esto es además requisito de
Telegram para que el bot pueda enviarles DMs). `/stop` los da de baja. El receptor responde la
confirmación y mantiene su `status` en DynamoDB.

## Recursos AWS creados

| Recurso          | Descripción                                              |
|------------------|----------------------------------------------------------|
| DynamoDB         | Tabla de suscriptores con GSI `StatusIndex`              |
| DynamoDB (dedup) | `ProcessedUpdates` (TTL): dedup `update_id` + high-water mark del poller |
| Lambda poller    | Sondea `t.me/s/<canal>`, detecta posts nuevos (HWM), markup, encola |
| EventBridge      | Regla cron que dispara el poller (default cada 5 min)    |
| Lambda receptor  | Valida secret, deduplica, rutea `/start`·`/stop`, encola |
| SQS + DLQ        | Cola de broadcast con reintentos; DLQ para lotes fallidos|
| Lambda worker    | Consume la cola y envía por DM (403/429), concurrencia ≤1 |
| API Gateway      | HTTP API `POST /webhook/telegram` (onboarding)          |
| IAM Roles        | Poller, receptor y worker, permisos mínimos por función  |

## Variables de entorno

| Variable                   | Función  | Descripción                                  |
|----------------------------|----------|----------------------------------------------|
| `TELEGRAM_BOT_TOKEN`       | ambos    | Token del bot (worker envía; receptor responde /start) |
| `WEBHOOK_SECRET_TOKEN`     | receptor | Secreto del header de Telegram. Vacío ⇒ sin validar (dev) |
| `SOURCE_CHANNEL_USERNAME`  | poller   | Username del canal público a sondear (sin @, default `iproparts`) |
| `SOURCE_CHANNEL_ID`        | receptor | Filtra `channel_post` si el bot fuera admin del canal |
| `MARKUP_PERCENTAGE`        | poller/receptor | Markup sobre precios (default 15)     |
| `SEND_DELAY_SECONDS`       | worker   | Delay entre envíos (default 0.05)            |
| `BROADCAST_QUEUE_URL`      | receptor | URL de la cola SQS. Vacío ⇒ envío inline (dev)|
| `BROADCAST_BATCH_SIZE`     | receptor | Chat IDs por mensaje SQS (default 100)       |
| `SUBSCRIBERS_TABLE`        | ambos    | Nombre tabla DynamoDB de suscriptores        |
| `SUBSCRIBERS_STATUS_INDEX` | receptor | GSI para query de activos                    |
| `PROCESSED_UPDATES_TABLE`  | receptor | Tabla de dedup de `update_id`                |
| `DEDUP_TTL_SECONDS`        | receptor | TTL del registro de dedup (default 86400)    |

> En desarrollo local el servidor Flask no usa SQS: si `BROADCAST_QUEUE_URL` está vacío, el receptor
> hace el broadcast **inline** invocando la misma lógica del worker.

## Pruebas

```powershell
python -m unittest discover -s tests -v
```

63 tests cubren markup, cliente Telegram (403/429/5xx), envío por lote, encolado SQS, enrutado del
receptor (`text`/`caption`/`edited_channel_post`), respuesta parcial del worker, validación de
`secret_token` (fail-closed + tiempo constante), parseo seguro del body, dedup de `update_id` (con
compensación acotada e idempotencia del inline) y comandos `/start`/`/stop`. No requieren AWS: los
imports de boto3 son perezosos y las fronteras se mockean.

## Próximos pasos

- Bot token en SSM/Secrets Manager; alarmas CloudWatch (fallos, profundidad de DLQ)
- CI/CD con empaquetado y deploy automatizado
- Test de integración end-to-end contra dynamodb-local + SQS local
- Comandos de gestión adicionales (`/status`, panel admin) y precisión del markup
