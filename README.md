# Replica — plataforma de difusión y envío masivo (Telegram + WhatsApp)

**Replica** es una **plataforma de difusión y envío masivo** por **Telegram** (bot a suscriptores **o**
userbot a tus contactos) y **WhatsApp** (cuenta personal vía Baileys), gestionada desde un **panel web**.
Tiene **dos modos**, ambos de primera clase:

- **Réplica automática de un canal:** mirrorea un **canal de precios de Telegram**, detecta cada lista
  publicada, le aplica un **markup configurable**, quita el bloque de ubicación, añade footer/imagen y la difunde.
- **Envío masivo manual:** **redacta tu propio mensaje** (texto + imagen) y envíalo —ya o **programado**— a
  **listas, audiencias o contactos** elegidos por número, a uno o ambos canales. No depende del canal fuente:
  sirve como herramienta de **broadcasting** general cuando lo necesites.

Los envíos van **fraccionados y secuenciales** con ritmo **anti-baneo** (jitter + ventana horaria por canal),
tienen **estados** (encolado→enviando→enviado/parcial/fallido) y son **idempotentes** (nadie recibe un mensaje
dos veces, ni ante reintentos). El interruptor maestro pausa solo lo **automático**; el envío **manual** siempre sale.

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
- **Envío masivo manual**: componer un mensaje propio (texto + imagen) y enviarlo —ya o **programado**— a
  listas/audiencias/contactos elegidos, a uno o ambos canales. Herramienta de broadcasting independiente del canal.
- **Resolución por NÚMERO/id, no por nombre**: las listas y selecciones explícitas se envían validando por
  número de teléfono/id; si un contacto cambia de nombre, igual recibe (el patrón de nombre solo auto-excluye
  en envíos amplios "todos/excepto", nunca en una lista o selección explícita).
- **Idempotencia**: nadie recibe un mensaje dos veces — dedup por lote y **por destinatario** (en una reentrega
  se saltan los ya enviados y el lote resume), cursor con lock optimista (sin doble-despacho concurrente).
- **Estados + anti-baneo**: cada difusión es un *job* con progreso por canal; envío fraccionado con delay
  aleatorio y **ventana horaria por canal** (Telegram y WhatsApp independientes) para reducir baneos.

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
├── domain/         markup.py, message.py, recipients.py (listas all/only/except + exclusión por patrón),
│                   models.py, auth.py, scheduling.py, schedules.py
├── application/    broadcasting.py (canal + envío manual), deliver_batch.py, dispatch.py (fraccionado),
│                   materialize_schedules.py, onboarding.py, poll_channel.py, ports.py (interfaces)
├── adapters/       dynamodb.py (subs, dedup, HWM, config, Broadcast/Plan/Schedule/Audit stores), sqs.py,
│                   telegram.py, telethon_user.py + telethon_login.py (userbot), tme.py (scrape),
│                   whatsapp.py (forwarder), email_sender.py (Resend), s3.py, config.py
├── entrypoints/    receiver.py, poller.py, worker.py, dispatcher.py, admin.py (panel + API)
└── wiring.py
whatsapp-service/   servicio Node/Baileys (Docker) — ver su README
infra/cloudformation/template.yaml   stack AWS completo
scripts/            package-lambda.ps1, _build_lambda_pkg.py, deploy.ps1, vincular-whatsapp-local.ps1, generar_sesion.py
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

`AdminUrl` (HTTP Basic Auth: `AdminUser`/`AdminPassword`). El panel es un único HTML/CSS/JS embebido
(`_PAGE` en `entrypoints/admin.py`). Sobre todas las pestañas, una **barra global de estado de envíos**
siempre visible muestra si los envíos automáticos están **ACTIVOS** (verde) o **EN PAUSA** (rojo) con acción
directa para activar/pausar (la pausa solo frena lo automático; el envío manual sigue disponible; al activar
avisa cuántas difusiones hay en cola). El header muestra: la **identidad de Telegram que envía** (en userbot, el
**teléfono** de la cuenta con ✓ si la sesión es válida o **«renovar»** clicable si caducó/se revocó → lleva a
Ajustes → 🔌 Conexiones; en modo bot, `@usuario · ID`), el **canal fuente** al que está integrado
(`📡 @canal`), el **número de WhatsApp conectado**, y un badge con el **rol** del usuario.

Cinco pestañas (orientadas a verbo, rediseño julio 2026):

- **🏠 Inicio**: KPIs de 30 días clicables (enviados, tasa, lotes pendientes, DLQ) + mini-gráfico + **sala
  de control**: switches «Recopilar listas del canal» y «Envíos automáticos activos», **«Lista del envío
  automático»** por canal, card **«Última lista capturada»** (con «Enviar a…») y primeros pasos.
- **✍️ Enviar**: compositor **único** (texto + imagen + canales + «Enviar a» + previsualización + contador
  4096) con selector **«¿Cuándo se envía?»**: **⚡ Ahora / 📅 Una vez el… / 🔁 Recurrente** (diario/semanal).
  El botón cambia a «Programar» según el modo; no hay formulario de programación aparte.
- **📡 Actividad**: **Historial** (estado + barras «en vivo», borrado individual/masivo, clic → texto
  completo, error clickeable, y **filtro segmentado** Todas / 📥 Capturadas / En curso / Enviadas / Fallidas
  + botón **«Enviar a…»** en las capturadas) · **Envíos fraccionados** (monitor de planes) · **⏰ Programados**
  (recurrentes: pausar/reanudar/borrar) · **⚠️ Problemas** (cola SQS + DLQ). Borrado masivo consistente.
- **👥 Contactos** (sub-nav **✈️ Telegram / 🟢 WhatsApp**): **Destinatarios** con **filtro Todos / ✅ Incluidos
  / ⛔ Excluidos** y contador; **listas de distribución** (whitelist/blacklist); **auto-exclusión por patrón
  de nombre** (`telegram_exclude_patterns` / `whatsapp_exclude_patterns`); en WhatsApp además auto-excluidos
  por fallos.
- **⚙️ Ajustes** (sub-nav): **🔌 Conexiones** (Telegram bot/userbot, WhatsApp reenvío + QR/pairing) · **📥
  Captura** (canal fuente, markup %, símbolos, footer, patrones, imagen, **probar procesamiento**) · **📤
  Ritmo y horarios** (anti-baneo lote/delays, ventanas, **zona horaria como select legible**) · **👤 Acceso**
  (usuarios con roles, correo de recuperación Resend, cambio de contraseña) · **🛠️ Sistema** (auditoría).

API (Basic Auth) bajo `/admin/api/`: `me`, `config`, `subscribers`, `image`, `queue`, `dlq[/redrive|/purge]`,
`audit`, `users[/role|/delete]`, `metrics`, `broadcast` (envío manual), `broadcast/preview`, `broadcasts[/delete]`,
`plans[/cancel|/delete]`, `schedules[/toggle|/delete]`, `auth/{forgot,reset,change-password}`,
`telethon/{send-code,sign-in,logout}`, `whatsapp/{status,contacts,pair,reset,reconnect,sync,blocked}`,
`telegram/{me,account,webhook}` (`account` = estado de la sesión userbot: válida o a renovar). Endpoints
**públicos** (sin auth, con anti-fuerza-bruta): `auth/forgot`, `auth/reset`.

**Roles** (`role` en el registro del usuario: `admin` | `user`): un **administrador** gestiona usuarios (crear,
borrar, promover/degradar) y hace todo lo demás; un **usuario** normal hace **todo MENOS gestionar usuarios**.
Los endpoints `users[/role|/delete]` exigen rol admin (403 si no). El **admin principal** (bootstrap `ADMIN_USER`)
no se degrada ni se borra (piso garantizado). Usuarios previos sin `role` se tratan como admin (compatibilidad);
los nuevos se crean con rol explícito (por defecto `user`). El front muestra/oculta la gestión según `/api/me`.

---

## 5. Listas, exclusiones, programación y estados

- **Listas con nombre** por canal (`telegram_lists`/`whatsapp_lists`) + **modo** (`telegram_target`/
  `whatsapp_target`): `all` (todos), `only` (solo listas activas = whitelist), `except` (excluir = blacklist).
  Regla pura en `domain/recipients.py`. El envío manual a WhatsApp **exige** una lista (no manda a todos por error).
- **Auto-exclusión por patrón de nombre** (`telegram_exclude_patterns` / `whatsapp_exclude_patterns`):
  cualquier contacto cuyo **nombre contenga** un patrón (substring, sin distinguir mayúsculas) se excluye
  solo de los envíos — p. ej. `FAM` para no enviar a la familia. Aplica también a la selección ad-hoc
  (guardrail). Telegram: `domain/recipients.ids_excluidos_por_patron` (en modo bot no hay nombres → no-op);
  WhatsApp: el servicio Node lo aplica en `resolverTargets`. Se pueden **incluir excepciones**: un contacto
  que coincide pero se incluye igual.
- **Info de destinatarios POR USUARIO**: TODO lo de "a quién enviar" — patrones de exclusión, excepciones y
  exclusiones manuales (`excluded_ids`/`whatsapp_excluded`, ambos canales) — se guarda en el **registro del
  usuario** (`__users__`, vía `/api/patterns`), no en la config global. El efectivo para los envíos es la
  **UNIÓN** de todos los usuarios (`ConfigStore.get()` los unifica con `union_ordenada`); el panel muestra
  LO TUYO. Así no se pierden al guardar otra config (no pasan por `/api/config`). La infraestructura
  compartida (tokens, sesión, canal fuente, markup, listas, interruptor, anti-baneo) sigue **global**.
- **Canales INDEPENDIENTES** (Telegram y WhatsApp): cada uno tiene su **horario de envío propio**
  (`tg_window_*` / `wa_window_*`, con `window_tz` compartido; si falta, hereda la ventana global). El dispatcher
  evalúa la ventana **por canal**: una ventana cerrada en un canal NO frena al otro, y un WhatsApp caído (no
  resuelve su total) **no bloquea** los envíos de Telegram (Telegram se despacha primero, sin depender de WhatsApp).
  Los delays anti-baneo también son por canal (`tg_delay_*` / `wa_delay_*`). **El envío MANUAL INMEDIATO**
  (✍️ Enviar → modo ⚡ Ahora, `source="manual"` y `not_before=0`) **salta la ventana** y sale al instante;
  la ventana sigue aplicando a lo automático del canal y a los envíos manuales **programados**.
- **Recopilación y envío SEPARADOS** (dos interruptores independientes):
  - **Recopilación** (`capture_enabled`): el poller lee `@iproparts` y, por cada lista nueva, la registra
    (visible en el panel como **📥 Capturada**; se guarda el **texto completo** y el historial de *Envíos*
    permite **clic en el mensaje → ver el texto completo**) y se la **autoenvía a Mensajes Guardados** del userbot
    (`enviar("me", …)`) para verla; el envío del preview se **loguea explícito** (éxito/fallo) y desconecta el
    cliente para no chocar con el refresh de contactos. Es independiente del envío.
  - **Envío automático** (`sending_enabled`): cuando está **apagado**, las listas capturadas **solo se ven**
    (panel + Mensajes Guardados), NO se difunden, NO crean plan ni cola — activar **no** reenvía lo ya capturado.
    Cuando está **activo**, cada lista nueva se difunde **solo a la lista elegida por canal**
    (`auto_telegram_list` / `auto_whatsapp_list`); el panel **exige** elegir lista antes de activar (evita enviar
    a "todos"). El **envío MANUAL** (✍️ Enviar) **SIEMPRE sale**, aun con el envío apagado (`source="manual"`;
    lo del canal es `source="channel"`/`"capture"`). Si un manual queda **sin destinatarios** se rechaza con motivo
    y se audita (`broadcast:rechazado`). Auto-pausa anti-baneo (solo el envío) tras 2 lotes totalmente fallidos.
- **Programación y fraccionado**: **schedules** (once/daily/weekly; `application/materialize_schedules.py` los
  materializa en envíos) y **plans** (envío fraccionado y secuencial: un lote a la vez con jitter y ventana
  horaria; `application/dispatch.py` los gotea). Un EventBridge cron dispara el dispatcher cada minuto.
- **Estados** (`adapters/dynamodb.py:DynamoDbBroadcastStore`): contadores atómicos por canal; estado
  **derivado** (`queued/sending/done/partial/failed`, con cierre por antigüedad). El worker (Telegram) y el
  servicio (WhatsApp) reportan progreso vía `broadcast_id`.
- **Recuperación de contraseña por correo**: vía **Resend** (`RESEND_API_KEY`/`MAIL_FROM`, capa gratis
  100/día) con fallback a SNS; ver `adapters/email_sender.py`.

---

## 6. Desarrollo local y pruebas

```powershell
docker compose -f docker/docker-compose.yml up --build     # stack local (DynamoDB + webhook inline)
python -m unittest discover -s tests                        # 207 tests (sin AWS; boto3 perezoso + fakes)
```

Los tests cubren markup, composición, recipients/listas + exclusión por patrón, cliente Telegram
(403/429/5xx), envío por lote, SQS, receptor, worker (parcial + estados), dispatcher/planes, schedules,
onboarding, WhatsApp forwarder, email_sender (Resend), BroadcastStore (estados) y envío manual.
Validación JS del servicio y del panel: `node --check whatsapp-service/src/index.js`.

---

## 7. Variables de entorno (resumen)

`TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET_TOKEN`, `SOURCE_CHANNEL_USERNAME`, `MARKUP_PERCENTAGE`,
`SEND_DELAY_SECONDS`, `SEND_MODE`(bot|userbot), `TELETHON_API_ID|HASH|SESSION`, `BROADCAST_QUEUE_URL`,
`BROADCAST_BATCH_SIZE`, `SUBSCRIBERS_TABLE`, `CONFIG_TABLE`, `PROCESSED_UPDATES_TABLE`, `BROADCASTS_TABLE`,
`PLANS_TABLE`, `SCHEDULES_TABLE`, `AUDIT_TABLE`, `IMAGES_BUCKET`, `ADMIN_USER`, `ADMIN_PASSWORD`,
`RESEND_API_KEY`, `MAIL_FROM` (correo de recuperación; opcionales, sin ellos usa SNS), `ALERTS_TOPIC_ARN`.
Muchos valores son **editables en runtime** desde el panel (tabla `config` de DynamoDB) y tienen prioridad
sobre el entorno; los `*_exclude_patterns`, listas y ventanas viven solo en esa config, no en el entorno.
Solo-config (no en el entorno): `capture_enabled` (recopilación, default `True`), `sending_enabled` (envío
automático) y `auto_telegram_list`/`auto_whatsapp_list` (lista que usa el envío automático por canal).
Servicio WhatsApp: `WHATSAPP_TOKEN`, `WHATSAPP_AUTH_TABLE`, `BROADCASTS_TABLE`, `AWS_*`, `SEND_DELAY_MS`.

---

## 8. Estado actual y pendientes

- ✅ Telegram (bot+userbot), WhatsApp (Baileys), listas, **exclusión por patrón de nombre**, estados,
  envío manual, **programado y fraccionado**, **recopilación separada del envío** (captura + preview a
  Mensajes Guardados, envío auto a lista elegida por canal), **correo Resend**, panel moderno
  (filtros, borrado individual/masivo, barra de estado) — desplegado y verificado.
- ✅ Despliegue reproducible (`package-lambda.ps1` + `deploy.ps1`); **CI en GitHub Actions** (tests en cada
  push; deploy gated por `DEPLOY_ENABLED`); **244 tests**.
- ⏳ Secretos en SSM/Secrets Manager; encriptación KMS de la tabla config; rate-limit distribuido del login.
- ⚠️ WhatsApp/userbot: riesgo de baneo por envío masivo — usar listas pequeñas y delays altos.

Ver también `ROADMAP.md`, `specs/` y la guía del servicio en `whatsapp-service/README.md`.
