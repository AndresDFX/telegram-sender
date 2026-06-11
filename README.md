# Replica — difusión de listas (Telegram + WhatsApp)

**Replica** mirrorea un **canal de precios de Telegram** hacia tus contactos/suscriptores: detecta cada
lista publicada, le aplica un **markup configurable** a los precios, quita el bloque de ubicación, añade
tu footer/imagen, y la **difunde** por **Telegram** (bot a suscriptores **o** userbot a tus contactos) y
por **WhatsApp** (cuenta personal vía Baileys). Todo se gestiona desde un **panel web** y los envíos
tienen **estados** (encolado→enviando→enviado/parcial/fallido) con ritmo **anti-baneo**.

> Documento de contexto del proyecto: arquitectura, componentes, despliegue, uso y estado actual.

---

## 1. Qué hace (visión)

- **Ingesta** del canal fuente (ajeno) por **sondeo del preview público** `https://t.me/s/<canal>`
  (el bot no puede ser admin de un canal de terceros), con **high-water mark** por `message_id`.
- **Composición** del mensaje: quita ubicación (patrones), aplica **markup** (solo precios con `$`/`💸`/`💲`,
  formato colombiano, redondeo al mil ↑: `$325.000` +15% → `$374.000`; no toca modelos como `A06 4-64GB`),
  añade footer WhatsApp e **imagen** opcional.
- **Difusión** desacoplada por **SQS** → worker, con **listas de distribución** (whitelist/blacklist) por canal.
- **Telegram**: modo **bot** (a quienes dan `/start`) o **userbot** (Telethon, desde tu cuenta a tus contactos).
- **WhatsApp**: servicio Node (Baileys) que reenvía las mismas listas a tus contactos.
- **Envío manual**: componer un mensaje propio (no del canal) y enviarlo por los canales/listas elegidos.
- **Estados + anti-baneo**: cada difusión es un *job* con progreso por canal; envío con delay para reducir baneos.

---

## 2. Arquitectura

```
EventBridge (cron) ─► Lambda poller ─┐  lee t.me/s/<canal>, HWM, markup, crea job
                                     ▼
Telegram (/start /stop) ─► API GW ─► Lambda receptor ─► SQS broadcast ─► Lambda worker ─► Telegram DM
                                     (secret, dedup)        │  (bot/userbot, 403/429, delay, +estado)
Panel admin (/admin) ────► Lambda admin ────────────────────┤
   config, listas, envío manual, estados                    ▼
                                                          SQS DLQ
WhatsApp: Lambda(broadcasting) ─HTTP─► whatsapp-service (Baileys, Render) ─► WhatsApp
   (forward texto+imagen, mode+listas)     sesión+contactos en DynamoDB, reporta progreso del job
```

- **Clean Architecture** en `src/lambda/`: `domain` (puro) → `application` (casos de uso + *ports*) →
  `adapters` (infra) → `entrypoints` (handlers) → `wiring.py` (composition root).
- **Desacople SQS**: el worker sobrevive a fallos parciales (reintentos + DLQ) y escala más allá del
  timeout de Lambda. Concurrencia reservada del worker + delay mantienen el ritmo anti-baneo.

### Estructura

```
src/lambda/
├── domain/         markup.py, message.py, recipients.py (listas: all/only/except), models.py
├── application/    broadcasting.py (canal + envío manual), deliver_batch.py, onboarding.py,
│                   poll_channel.py, ports.py (interfaces)
├── adapters/       dynamodb.py (subs, dedup, HWM, config, BroadcastStore), sqs.py, telegram.py,
│                   telethon_user.py (userbot), tme.py (scrape), whatsapp.py (forwarder), s3.py, config.py
├── entrypoints/    receiver.py, poller.py, worker.py, admin.py (panel + API)
└── wiring.py
whatsapp-service/   servicio Node/Baileys (Docker) — ver su README
infra/cloudformation/template.yaml   stack AWS completo
scripts/            package-lambda.ps1, deploy.ps1, vincular-whatsapp-local.ps1, generar_sesion.py
specs/              especificaciones por fase
```

---

## 3. Despliegue AWS

Requisitos: Docker Desktop, AWS CLI v2, Python 3.12. Credenciales AWS en `.env.aws` y secretos en
`.env.deploy` (ambos **gitignored**).

```powershell
# 1) Empaquetar la Lambda (build en Linux dentro de Docker -> .build/telegram-broadcaster.zip)
./scripts/package-lambda.ps1

# 2) Desplegar (lee .env.aws + .env.deploy, sube el zip con key hasheada, deploy CFN con todos los params)
./scripts/deploy.ps1
```

`deploy.ps1` pasa: `SendMode` (bot|userbot), `Telethon*`, `Admin*`, tokens, `WorkerReservedConcurrency`.
Output `AdminUrl` = panel. Stack actual: `telegram-sync-dev` (us-east-1).

### Recursos AWS

| Recurso | Descripción |
|---|---|
| DynamoDB `subscribers` (+GSI StatusIndex) | suscriptores (modo bot) |
| DynamoDB `processed-updates` (TTL) | dedup `update_id` + high-water mark del poller |
| DynamoDB `config` | configuración editable en runtime + caché de contactos del userbot |
| DynamoDB `whatsapp-auth` | sesión Baileys (persistida por el servicio) |
| DynamoDB `broadcasts` (TTL) | **estados** de cada envío (progreso por canal) |
| Lambda poller / receiver / worker / admin | sondeo, onboarding, envío, panel |
| SQS broadcast + DLQ | cola de difusión con reintentos |
| EventBridge | cron del poller (default 5 min) |
| API Gateway HTTP API | `POST /webhook/telegram`, `GET /admin`, `ANY /admin/{proxy+}` |

### Telegram

- **Bot**: registra el webhook con `setWebhook?url=<WebhookUrl>&secret_token=<WEBHOOK_SECRET_TOKEN>`. Suscriptores con `/start`·`/stop`.
- **Userbot** (envía desde tu cuenta a tus contactos): genera la sesión con `scripts/generar_sesion.py`
  (my.telegram.org → API_ID/HASH → StringSession en `.env.deploy`), deploy con `SendMode=userbot`.
  ⚠️ Automatizar tu cuenta personal puede implicar baneo; el usuario aceptó el riesgo.

### WhatsApp (servicio Baileys)

Servicio Node portable (Render free) en `whatsapp-service/`. La sesión y los contactos viven en DynamoDB
(sobreviven reinicios/spin-down). **Vincular** (una vez) desde tu IP residencial — WhatsApp bloquea el
linking desde IPs de datacenter:

```powershell
./scripts/vincular-whatsapp-local.ps1            # QR en vivo (o -Pair <num> para código de 8 dígitos)
```

Escaneas, la sesión queda en DynamoDB, paras el local y Render la reutiliza (`POST /reconnect`). Detalles
y endpoints (`/status`, `/qr`, `/pair`, `/reset`, `/reconnect`, `/sync`, `/send`) en `whatsapp-service/README.md`.

---

## 4. Panel de administración

`AdminUrl` (HTTP Basic Auth: `AdminUser`/`AdminPassword`). Pestañas:

- **📝 Mensaje**: markup %, canal fuente, símbolos, footer, patrones de ubicación, imagen.
- **✈️ Telegram**: cuenta (bot/userbot), destinatarios (buscador+paginación), **listas de distribución**
  (whitelist/blacklist), exclusiones.
- **🟢 WhatsApp**: conectar (URL/token, QR/código, estado), destinatarios, listas, exclusiones.
- **📨 Enviar**: **Componer y enviar** (texto + imagen + canales + **selector de lista "Enviar a"** +
  **previsualización** de destinatarios) y **Envíos** (estado + barras de progreso por canal, "en vivo").
- **📊 Estado**: profundidad de cola/DLQ.

API (Basic Auth): `GET|POST /admin/api/config`, `/api/subscribers`, `/api/queue`, `/api/image`,
`/api/whatsapp/{status,contacts,pair,reset,reconnect,sync}`, `/api/broadcast` (envío manual),
`/api/broadcast/preview`, `/api/broadcasts` (estados).

---

## 5. Listas de distribución y estados

- **Listas con nombre** por canal (`telegram_lists`/`whatsapp_lists`) + **modo** (`telegram_target`/
  `whatsapp_target`): `all` (todos), `only` (solo listas activas = whitelist), `except` (excluir = blacklist).
  Regla pura en `domain/recipients.py`. El envío manual a WhatsApp **exige** una lista (no manda a todos por error).
- **Estados** (`adapters/dynamodb.py:DynamoDbBroadcastStore`): contadores atómicos por canal; estado
  **derivado** (`queued/sending/done/partial/failed`, con cierre por antigüedad). El worker (Telegram) y el
  servicio (WhatsApp) reportan progreso vía `broadcast_id`.

---

## 6. Desarrollo local y pruebas

```powershell
docker compose -f docker/docker-compose.yml up --build     # stack local (DynamoDB + webhook inline)
python -m unittest discover -s tests                        # 113 tests (sin AWS; boto3 perezoso + fakes)
```

Los tests cubren markup, composición, recipients/listas, cliente Telegram (403/429/5xx), envío por lote,
SQS, receptor, worker (parcial + estados), onboarding, WhatsApp forwarder, BroadcastStore (estados) y
envío manual. Validación JS del servicio: `node --check whatsapp-service/src/index.js`.

---

## 7. Variables de entorno (resumen)

`TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET_TOKEN`, `SOURCE_CHANNEL_USERNAME`, `MARKUP_PERCENTAGE`,
`SEND_DELAY_SECONDS`, `SEND_MODE`(bot|userbot), `TELETHON_API_ID|HASH|SESSION`, `BROADCAST_QUEUE_URL`,
`BROADCAST_BATCH_SIZE`, `SUBSCRIBERS_TABLE`, `CONFIG_TABLE`, `PROCESSED_UPDATES_TABLE`, `BROADCASTS_TABLE`,
`IMAGES_BUCKET`, `ADMIN_USER`, `ADMIN_PASSWORD`. Servicio WhatsApp: `WHATSAPP_TOKEN`, `WHATSAPP_AUTH_TABLE`,
`BROADCASTS_TABLE`, `AWS_*`, `SEND_DELAY_MS`.

---

## 8. Estado actual y pendientes

- ✅ Telegram (bot+userbot), WhatsApp (Baileys), listas, estados, envío manual, panel moderno — desplegado y verificado.
- ✅ Despliegue reproducible (`deploy.ps1`); 113 tests.
- ⏳ CI/CD auto-deploy en push a `main` (diferido).
- ⏳ Secretos en SSM/Secrets Manager; alarmas CloudWatch (DLQ, errores).
- ⚠️ WhatsApp/userbot: riesgo de baneo por envío masivo — usar listas pequeñas y delays altos.

Ver también `ROADMAP.md`, `specs/` y la guía del servicio en `whatsapp-service/README.md`.
