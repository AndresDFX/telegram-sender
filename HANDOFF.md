# Handoff — Replica

**Replica** es una **plataforma de difusión y envío masivo** por **Telegram** y **WhatsApp**, operada desde un **panel web**. No es una simple réplica 1:1 de un canal: combina la **réplica automática** de un canal de precios de Telegram (con markup configurable, limpieza de ubicación y footer/imagen) con el **envío masivo manual** de mensajes propios (texto + imagen) a listas, audiencias o contactos elegidos, ya o programado, por uno o ambos canales. Todo sale **fraccionado y secuencial** con ritmo anti-baneo. Este documento entrega el proyecto completo (arquitectura, despliegue, credenciales, operación, errores aprendidos y pendientes) para que otra persona quede operativa.

---

## Visión y funcionalidades

### Qué es hoy

Replica funciona en **dos modos de primera clase**:

- **Réplica automática de un canal:** mirrorea un canal de precios de Telegram, detecta cada lista publicada, le aplica un **markup configurable**, quita el bloque de ubicación, añade footer/imagen y la difunde.
- **Envío masivo manual:** redactas tu **propio mensaje** (texto + imagen) y lo envías —ya o **programado**— a listas, audiencias o contactos elegidos, a uno o ambos canales. Funciona como herramienta de broadcasting general, independiente del canal fuente.

Los canales de salida son **Telegram** (modo **bot** a suscriptores, o **userbot** Telethon desde tu cuenta a tus contactos) y **WhatsApp** (cuenta personal vía Baileys).

### Funcionalidades principales

- **Réplica de canal con markup:** sondeo del preview público `t.me/s/<canal>`, high-water mark por `message_id`; markup solo a precios (formato colombiano, redondeo al mil hacia arriba; no toca modelos como `A06 4-64GB`); limpia ubicación y añade footer/imagen.
- **Envío manual, programado y fraccionado:** componer mensaje propio; enviar ya o programado (once/daily/weekly); entrega fraccionada y secuencial (un lote a la vez con jitter).
- **Listas de distribución** por canal con tres modos: `all` (todos), `only` (whitelist) y `except` (blacklist). El envío manual a WhatsApp exige una lista (evita mandar a todos por error).
- **Exclusión por patrón de nombre** (`telegram_exclude_patterns` / `whatsapp_exclude_patterns`): excluye contactos cuyo nombre contenga un patrón (substring, sin distinguir mayúsculas), p. ej. `FAM`; admite excepciones.
- **Resolución por NÚMERO/id, no por nombre:** listas y selecciones explícitas se validan por número/id; si un contacto cambia de nombre, igual recibe. El patrón de nombre solo auto-excluye en envíos amplios.
- **Recopilación SEPARADA del envío (dos interruptores):** `capture_enabled` controla la **recopilación** del canal (el poller captura mientras esté activa, aunque el envío esté apagado) y `sending_enabled` controla el **envío automático**. Con el envío apagado, cada lista capturada **solo se ve** (en el panel como `📥 Capturada` y autoenviada a **Mensajes Guardados** del userbot), NO se difunde ni crea cola — activar **no** reenvía lo ya capturado. Con el envío activo, cada lista nueva se difunde **solo a la lista elegida por canal** (`auto_telegram_list`/`auto_whatsapp_list`); el panel **exige** elegir lista antes de activar (evita enviar a "todos"). El **envío manual siempre sale**.
- **Imagen + texto en un solo mensaje** (composición unificada).
- **Idempotencia / anti-duplicados:** dedup por lote y por destinatario (una reentrega salta los ya enviados y resume); cursor con lock optimista (sin doble despacho concurrente).
- **Ventana horaria por canal:** Telegram y WhatsApp tienen horario y delays anti-baneo **independientes**; una ventana cerrada o un WhatsApp caído no bloquean al otro.
- **Estados:** cada difusión es un job con progreso por canal (encolado → enviando → enviado/parcial/fallido), con contadores atómicos y estado derivado.
- **Roles de usuario** (`admin` | `user`): el admin gestiona usuarios y hace todo; el usuario hace todo menos gestionar usuarios. El admin principal (bootstrap) no se degrada ni se borra.
- **Recuperación de contraseña por correo:** vía **Resend**, con fallback a SNS.
- **Info de destinatarios por usuario:** patrones, excepciones y exclusiones manuales se guardan en el registro del usuario; el efectivo para envíos es la unión de todos los usuarios.

---

## Arquitectura y componentes

### Modelo: Clean Architecture con composition root

El backend vive en `src/lambda/` y sigue **arquitectura limpia (hexagonal / puertos y adaptadores)** con la regla de dependencia apuntando hacia adentro, en cuatro capas:

- **`domain/`** — Entidades y reglas de negocio puras, sin dependencias externas: objetos de valor (`Post`, `SendResult`, `BroadcastStats` en `models.py`), composición del mensaje (`message.py`: quitar ubicación/teléfono → markup de precios → footer), markup (`markup.py`), ventanas horarias y jitter anti-baneo (`scheduling.py`), horarios recurrentes (`schedules.py`), filtrado y exclusiones (`recipients.py`) y autenticación (`auth.py`).
- **`application/`** — Casos de uso que orquestan el dominio. `ports.py` define las **interfaces abstractas** (puertos: `SubscriberRepository`, `BroadcastQueue`, `MessageSender`, `ChannelReader`, `WhatsAppForwarder`, `ImageStore`, `ConfigStore`, `DedupStore`, `HighWaterMarkStore`, `QueueStats`). Los casos de uso (`BroadcastList`, `DispatchCampaigns`, `DeliverBatch`, `PollChannel`, `MaterializeSchedules`, `HandleCommand`) dependen **solo de estas abstracciones**.
- **`adapters/`** — Implementaciones concretas: `dynamodb.py` (stores de config, suscriptores, dedup, HWM, planes, broadcasts, auditoría, horarios), `sqs.py` (cola real e inline), `s3.py` (imágenes), `telegram.py` (bot HTTP), `telethon_user.py` / `telethon_login.py` (userbot), `tme.py` (lector del preview público), `whatsapp.py` (forwarder HTTP al servicio Node), `config.py` (entorno), `email_sender.py` (reseteo de clave).
- **`entrypoints/`** — Controladores finos: los handlers de Lambda. Autentican, parsean, delegan en casos de uso y formatean respuesta; sin lógica de negocio.
- **`wiring.py`** es el **composition root**: la única pieza que conoce todas las capas. Cablea adapters a casos de uso (`build_*`). Decide en runtime, leyendo la config de DynamoDB (con fallback a entorno), el **modo de envío** (`bot` → `TelegramSender` + suscriptores DynamoDB; `userbot` → `TelethonUserSender` + contactos Telethon) y conmuta cola **SQS real vs inline** según haya `BROADCAST_QUEUE_URL`. Las dependencias se cablean perezosamente (`_ensure()`), lo que permite inyectarlas en tests.

### Los cinco Lambdas (entrypoints)

1. **receiver** (`receiver.py`) — Webhook de Telegram (HTTP). Autentica por `secret_token` fail-closed (403 si falta/invalido), parsea de forma segura, **deduplica por `update_id`** (marca-antes con compensación), y enruta. Su trabajo vivo es el **onboarding** (comandos privados `/start`, `/stop` → `HandleCommand`).
2. **poller** (`poller.py`) — Cron de EventBridge. Sondea el canal público (`PollChannel`) y difunde **solo lo nuevo** vía high-water mark por `message_id` (no re-difunde backlog; la primera corrida solo siembra el HWM). Gatea por `capture_enabled` (la RECOPILACIÓN, no el envío): si está activa captura aunque `sending_enabled` esté apagado (en ese caso `BroadcastList` solo registra + previsualiza, no difunde). En userbot, **refresca el caché de contactos** en DynamoDB (TTL 30 min para evitar `FloodWait`).
3. **worker** (`worker.py`) — Consumidor de SQS. Entrega cada lote (`DeliverBatch`) y reporta fallos parciales con `batchItemFailures`. Resuelve imágenes (`image_key` → URL presigned justo antes de enviar). Tiene **idempotencia por lote y por destinatario**, honra cancelaciones en vuelo (`pid`), respeta la pausa (descarta automáticos, deja pasar manuales) y tiene **auto-pausa anti-baneo** (tras N lotes totalmente fallidos pone `sending_enabled=False`).
4. **dispatcher** (`dispatcher.py`) — Cron de EventBridge cada minuto, con **concurrencia reservada = 1** lógica. Es el corazón del **envío fraccionado/secuencial**. Cada tick: (1) materializa mensajes programados vencidos (`MaterializeSchedules`, aislado); (2) despacha **como mucho un lote** del plan activo más antiguo (`DispatchCampaigns`), respetando ventanas horarias por canal y esperando a que el lote anterior termine.
5. **admin** (`admin.py`) — Panel de administración: sirve la SPA (GET `/admin`) y expone toda la API REST (config, listas, patrones, imágenes, suscriptores, cola, DLQ, broadcasts, métricas, auditoría, planes, horarios, usuarios/roles, login y reseteo de clave, estado de Telegram/Telethon y WhatsApp, y el endpoint clave `POST /api/broadcast`).

### El servicio Node de WhatsApp (`whatsapp-service/src/index.js`)

Servicio portable basado en **Baileys** (WhatsApp Web). Mantiene la conexión (sesión persistida en DynamoDB vía `dynamoAuth.js`), expone QR / código de emparejamiento, lista contactos y **envía las listas reenviadas desde el backend**. Protegido por bearer token compartido (`WHATSAPP_TOKEN`). Endpoints: `/health` (público), `/status`, `/qr`, `/contacts`, `/blocked`, `/pair`, `/sync`, `/reconnect`, `/reset` y `/send`. El `/send` es **fire-and-forget** (responde 202), soporta **envío fraccionado** (resuelve el set con `resolverTargets` según modo `all`/`only`/`except` + patrones de exclusión y rebana `[offset, offset+limit)`), aplica **delay aleatorio** entre mensajes, tiene **opt-out automático** (excluye tras `BLOQUEO_UMBRAL` fallos seguidos), descarga la imagen una sola vez a un Buffer y **reporta progreso** a la tabla `-broadcasts`.

### Flujo de un envío de extremo a extremo

- **A) Captura automática del canal:** el poller lee el preview público (`TmePreviewChannelReader`), detecta posts nuevos por HWM y llama a `BroadcastList.__call__(text)`. Se **compone el mensaje** (quitar ubicación/teléfono, markup, footer), se resuelven destinatarios (con exclusiones por id y patrón) y se crea un **job** (`BroadcastStore`). La captura ocurre **siempre**, aunque los envíos estén pausados.
- **B) Envío manual:** `POST /api/broadcast` → `BroadcastList.enviar_manual(...)`. El texto va tal cual (sin markup/footer). Destinatarios: contactos ad-hoc por número > lista elegida > target configurado. WhatsApp manual exige destinatarios concretos. **Sale aunque haya pausa** y permite programación a hora exacta (`scheduled_at`).
- **C) El plan y el dispatcher:** si el scheduling está activo, ambos orígenes crean un **plan** en DynamoDB (texto, imagen/clave S3, lotes de Telegram por `batch_size`, config WhatsApp, `not_before`, `source`). El dispatcher toma el plan listo más antiguo y **gotea un lote por tick**: verifica si el anterior terminó/se estancó, comprueba la ventana horaria **por canal**, reclama el lote antes de encolar y libera el siguiente — Telegram primero (`encolar_uno` → SQS), luego WhatsApp (servicio Node con el slice `[offset, limit)`). **Re-firma las URLs de imagen S3** justo antes de enviar (caducan en 1h).
- **D) La entrega:** el worker consume el lote de SQS y `DeliverBatch` envía mensaje a mensaje vía el `MessageSender` (bot o userbot), con delay aleatorio, idempotencia por destinatario, marcado de inactivos ante bloqueo (403) y reporte de progreso/errores. WhatsApp lo entrega el servicio Node en paralelo. Sin scheduler (inline, dev), `BroadcastList` entrega de inmediato.

### El panel admin

El frontend es una **SPA monolítica embebida** como un único string crudo `_PAGE` en `admin.py` (≈línea 1092 en adelante; archivo total ~3650 líneas). HTML + CSS + JS inline, autocontenido (sin CDN), servido en `GET /admin`. Incluye su **design system** (paleta naranja `#FD531E`, escala de grises cálida, colores semánticos), favicon SVG inline y la lógica JS que consume la API REST del mismo Lambda. La parte Python alrededor de `_PAGE` (helpers antes del string) son helpers (`_ensure`, `_audit`, autorización por sesión/rol con `_autorizado`/`_es_admin`, reseteo por email, saneo de config) y el router `lambda_handler` (≈línea 529).

### Archivos clave

- `D:\Projects\Personal\TelegramSender\src\lambda\wiring.py` — composition root
- `D:\Projects\Personal\TelegramSender\src\lambda\application\ports.py` — puertos/interfaces
- `D:\Projects\Personal\TelegramSender\src\lambda\application\broadcasting.py` — difusión y envío manual
- `D:\Projects\Personal\TelegramSender\src\lambda\application\dispatch.py` — despacho fraccionado/secuencial
- `D:\Projects\Personal\TelegramSender\src\lambda\entrypoints\{receiver,poller,worker,dispatcher,admin}.py` — los cinco Lambdas
- `D:\Projects\Personal\TelegramSender\whatsapp-service\src\index.js` — servicio Node WhatsApp

---

## Repositorios y servicios

- **Repositorio GitHub (personal):** `git@github-personal:AndresDFX/telegram-sender.git`. El push usa el **alias SSH `github-personal`** (configurado en `~/.ssh/config` → `id_rsa_personal`) para forzar la clave personal. Ojo: la identidad git global quedó como la de trabajo (`julian.castano@siesa.com`).
- **AWS:** stack CloudFormation `telegram-sync-dev`, región `us-east-1`, cuenta `438095550710`. Aloja los 5 Lambdas, 9 tablas DynamoDB, SQS + DLQ, API Gateway HTTP v2, EventBridge (poller + dispatcher), SNS + CloudWatch Alarms. El bucket S3 de código/imágenes (`telegram-sync-lambda-438095550710-us-east-1`) **NO lo crea el stack**.
- **Servicio WhatsApp en Render** (plan Free): `https://telegram-sender-dm43.onrender.com`. Runtime Docker, **Root Directory `whatsapp-service`**, **auto-deploy en cada push** a la rama conectada. NO está en CloudFormation ni en el workflow de AWS. Alternativas equivalentes (mismo contenedor): Fly.io, Koyeb, Oracle Always Free.

---

## Despliegue paso a paso

Todo el AWS se define en `infra/cloudformation/template.yaml`. El deploy del backend es **local (PowerShell)** o por CI.

### 1) Empaquetar (build en Linux)

```
./scripts/package-lambda.ps1
```
Construye dentro de Docker `python:3.12-slim` ejecutando `scripts/_build_lambda_pkg.py`: `pip install -r src/lambda/requirements.txt -t .build/pkg`, copia el árbol `domain/application/adapters/entrypoints/wiring.py`, limpia `__pycache__` y genera `.build/telegram-broadcaster.zip`. **El empaquetado DEBE ser en Linux** (Docker o runner Linux de CI); `Compress-Archive` en Windows mete rutas con `\` y binarios `.pyd/.exe` que rompen en Lambda.

### 2) Desplegar

```
./scripts/deploy.ps1
```
Lee `.env.aws` y `.env.deploy` (gitignored), exporta credenciales, calcula `account` con `aws sts get-caller-identity`, sube el zip a `s3://telegram-sync-lambda-<account>-<region>/lambda/telegram-broadcaster-<hash12>.zip` y ejecuta:
```
aws cloudformation deploy --stack-name telegram-sync-dev \
  --template-file infra/cloudformation/template.yaml \
  --parameter-overrides <todos los params> \
  --capabilities CAPABILITY_NAMED_IAM --region $AWS_REGION
```
Al final imprime el `AdminUrl`. **La key del zip lleva hash de contenido** para que CloudFormation detecte el cambio de código.

### 3) Verificar SHA

Tras el deploy, confirma que el código activo corresponde al zip recién subido (la key con hash en S3 = la `LambdaCodeS3Key` aplicada al stack). Verifica también que las reglas EventBridge (poller, dispatcher) y el EventSourceMapping del worker siguen **ENABLED** (los cambios out-of-band no se revierten solos).

### Parámetros del stack (los que pasa el deploy)

`ProjectName=telegram-sync`, `EnvironmentName=dev`, `LambdaCodeS3Bucket`, `LambdaCodeS3Key` (hasheada), `WorkerReservedConcurrency=0`, `WorkerTimeoutSeconds=300`, `BroadcastBatchSize=150`, `AlertEmail`, `AdminUser=admin`, `SendMode=userbot`, y los secretos (solo por nombre): `TelegramBotToken`, `WebhookSecretToken`, `AdminPassword`, `TelethonApiId`, `TelethonApiHash`, `TelethonSession`. El resto usa sus Defaults del template.

### Alternativa CI (`.github/workflows/deploy.yml`)

En push a `main` (paths `src/lambda/`, `infra/cloudformation/`, `scripts/`, el workflow) o `workflow_dispatch`. Job `test` (unittest) → job `deploy` gated por la variable `DEPLOY_ENABLED == "true"`. En CI se empaqueta **nativo en runner Linux** (sin Docker) con `BUILD_ROOT=$GITHUB_WORKSPACE`, key `…-<sha256[:12]>.zip`, mismo `cloudformation deploy`. Secrets en GitHub Actions. Diferencia: `deploy.ps1` pasa `WorkerTimeoutSeconds=300` explícito; el CI no lo pasa (queda en el default del template, que también es 300).

### Gotchas de despliegue

- **Parámetros no pasados conservan el valor PREVIO del stack** (no el Default del template). Por eso `WorkerTimeoutSeconds=300` se pasa explícito (commit `9d1852e`). `deploy.ps1` pasa TODOS los params.
- **`SendMode=userbot`**: sin Telethon ApiId/Hash/Session válidos, el worker no envía como tu cuenta.
- **`WorkerReservedConcurrency=0`**: la cuenta tiene límite de concurrencia bajo (=10); reservar dejaría <10 sin reservar y AWS lo rechaza. La secuencialidad NO depende de esto (la garantizan el gate `in_flight` + un lote por tick + `BatchSize=1`).
- **El bucket S3 NO lo crea el stack**: debe existir como `telegram-sync-lambda-<account>-<region>` o el deploy falla.
- **No usar `--platform/--only-binary` en pip** (deps sdist-only como `pyaes` de Telethon romperían).
- **Cambios out-of-band no se revierten solos**: si deshabilitas reglas EventBridge o el ESM del worker a mano, un `deploy` posterior NO los re-habilita salvo que el cambio toque ese recurso.
- **Variables de entorno OBLIGATORIAS en el worker** (`CONFIG_TABLE`, `PROCESSED_UPDATES_TABLE`): si faltan, las stores caen a nombres por defecto → `AccessDenied`. Sin `CONFIG_TABLE`: ningún envío se entrega (fallo silencioso → DLQ). Sin `PROCESSED_UPDATES_TABLE`: se rompe la idempotencia → reentrega en bucle → mensajes DUPLICADOS.
- **Secretos**: viven en `.env.aws`/`.env.deploy` (local, gitignored) y en GitHub Secrets (CI); el template los marca `NoEcho`. `WebhookSecretToken` y `AdminPassword` son fail-closed.

### Despliegue del servicio WhatsApp (Render)

New → Web Service → conectar repo → **Root Directory `whatsapp-service`** → Runtime **Docker** → auto-deploy en push. Variables: `WHATSAPP_TOKEN`, `WHATSAPP_AUTH_TABLE`, `WHATSAPP_SESSION_ID` (default `default`), `AWS_ACCESS_KEY_ID/SECRET/REGION`, `SEND_DELAY_MS` (default 2000), `PORT` (default 8080). La sesión Baileys se persiste en la tabla DynamoDB `telegram-sync-dev-whatsapp-auth`, por eso sobrevive a reinicios sin re-escanear QR. Caveat: el plan Free hace spin-down a ~15 min; al despertar reutiliza la sesión (`POST /reconnect`). El build en Render requiere instalar `git`+`ca-certificates`, `npm install --legacy-peer-deps` y reescribir URLs git SSH→HTTPS (Baileys clona libsignal por SSH).

---

## Credenciales y accesos

> REGLA: los **valores** de secretos NUNCA van al repo, PR, chat ni a este documento. Aquí solo se listan **nombres** y dónde viven.

### Inventario

- **`.env.aws` (GITIGNORED):** `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` — usuario IAM (`andresdfx`, cuenta AWS `438095550710`, `us-east-1`) para AWS CLI / deploy. OJO: en `.env.aws` las líneas 2-3 llevan prefijo `$env:` (formato PowerShell mixto); parsear sin imprimir el valor.
- **`.env.deploy` (GITIGNORED):** `STACK_NAME`, `LAMBDA_CODE_S3_BUCKET`, `WEBHOOK_SECRET_TOKEN` (secreto del webhook de Telegram), `TELEGRAM_BOT_TOKEN` (bot `@ipro_listas_bot`), `ADMIN_PASSWORD` (Basic Auth del panel `/admin`), `TELETHON_API_ID`, `TELETHON_API_HASH`, `TELETHON_SESSION` (⚠️ acceso TOTAL a la cuenta de Telegram userbot — máxima sensibilidad), `WHATSAPP_TOKEN` (bearer del servicio WhatsApp en Render), `WHATSAPP_AUTH_TABLE`, `SEND_DELAY_MS`, `AWS_REGION`.
- **`.env.example` (SÍ versionado):** plantilla SIN secretos; documenta las variables.
- **Acceso al panel:** usuario `admin` (`ADMIN_USER`, default `admin`) + `ADMIN_PASSWORD` (en `.env.deploy`); URL = `AdminUrl` del stack.
- **GitHub:** push por alias SSH `github-personal` (clave `~/.ssh/config` → `id_rsa_personal`); URL `git@github-personal:AndresDFX/telegram-sender.git`.
- **Servicio WhatsApp en Render:** la sesión de Baileys persiste en DynamoDB (tabla `whatsapp-auth`); se vincula escaneando QR desde IP residencial. `TELETHON_SESSION` y la sesión de WhatsApp = control de las cuentas; entregar con extremo cuidado.

> NOTA: la sesión userbot **"viva"** está en la tabla `config` de DynamoDB (`telethon_session`, ~353 chars), no necesariamente en `.env.deploy` (ese valor pudo quedar REVOCADO). Los `api_id`/`api_hash` vienen del entorno del Lambda (= `.env.deploy`).

### Cómo entregar los valores de forma SEGURA

- Comparte los valores **solo por un gestor de contraseñas** (1Password, Bitwarden) o un **canal cifrado de un solo uso** (link que expira). **Nunca** los pegues en git, PR, issues, chat, correo plano ni en este documento.
- Entrega `.env.aws` y `.env.deploy` como archivos completos por ese canal seguro; el receptor los coloca en la raíz del repo (ya están gitignored). Verifica que `.gitignore` los cubre antes de cualquier commit.
- Trata `TELETHON_SESSION` y la sesión de WhatsApp como los secretos de **máxima** sensibilidad: dan control total de las cuentas personales. Limita quién los recibe.

### Rotación básica por credencial

- **AWS (`AWS_ACCESS_KEY_ID`/`SECRET`):** crear una nueva access key del usuario IAM `andresdfx` en IAM, actualizar `.env.aws` y GitHub Secrets, y **desactivar/borrar** la anterior.
- **`ADMIN_PASSWORD`:** cambiar el valor en `.env.deploy` (y GitHub Secrets) y redeploy del stack; el cambio aplica vía el parámetro `AdminPassword`.
- **`WEBHOOK_SECRET_TOKEN`:** rotar el valor, redeploy, y re-registrar el webhook de Telegram con el nuevo `secret_token`.
- **`TELEGRAM_BOT_TOKEN`:** regenerar con @BotFather, actualizar secreto y re-registrar webhook.
- **`TELETHON_SESSION`:** si se compromete, **cerrar la sesión** desde la app de Telegram (Dispositivos activos), generar una sesión nueva (login Telethon) y actualizar el `telethon_session` en la tabla `config` de DynamoDB (el valor vivo) además de `.env.deploy`. `api_id`/`api_hash` se regeneran en my.telegram.org si hace falta.
- **`WHATSAPP_TOKEN`:** rotar el valor en `.env.deploy` y en las variables de entorno del servicio en Render (ambos deben coincidir).
- **Sesión de WhatsApp (Baileys):** si se compromete, cerrar el dispositivo vinculado desde WhatsApp (Dispositivos vinculados) y/o `POST /reset`, luego re-vincular por QR/pairing desde IP residencial.

---

## Operación del panel

> **Manual para usuarios invitados:** [docs/MANUAL_USUARIO_INVITADO.md](docs/MANUAL_USUARIO_INVITADO.md) — cómo una segunda persona opera el panel compartiendo el mismo canal de origen del admin (qué es global vs por-usuario, flujos de envío, qué no tocar). El canal de origen y la captura son GLOBALES (no hay nada que configurar por usuario); lo único personal son las exclusiones, y los envíos aplican la UNIÓN de las de todos.

Acceso por `AdminUrl` con HTTP Basic Auth (usuario `admin` + `ADMIN_PASSWORD`). El panel es un único HTML/CSS/JS embebido. Sobre todas las pestañas hay una **barra global de estado de envíos** siempre visible (ACTIVOS en verde / EN PAUSA en ÁMBAR; el rojo queda para fallos) con acción directa para activar/pausar. El header muestra la identidad de Telegram que envía (en userbot, el teléfono con ✓ o "renovar" si caducó), el canal fuente (`📡 @canal`), el número de WhatsApp conectado y un badge con el rol del usuario.

> **Rediseño UX/colores (julio 2026, 6 batches del workflow `ux-color-redesign-panel`):** la navegación pasó a **5 pestañas por intención**: 🏠 Inicio (sala de control: switches de captura/envío automático + lista destino + última capturada) · ✍️ Enviar (compositor ÚNICO con modos ⚡Ahora/📅Una vez/🔁Recurrente — el formulario duplicado de "Programar un mensaje" se eliminó) · 📡 Actividad (historial con filtro Todas/Capturadas/En curso/Enviadas/Fallidas + botón «Enviar a…» en capturadas, programados, ⚠️ Problemas con cola/DLQ) · 👥 Contactos (antes "Fuentes y listas") · ⚙️ Ajustes (🔌 Conexiones / 📥 Captura / 📤 Ritmo y horarios / 👤 Acceso / 🛠️ Sistema). `goStep` mantiene ALIASES de rutas viejas→nuevas (retirar en 2-3 releases). Paleta consolidada: un solo `:root`, botón primario con texto oscuro (AA), canal Telegram=azul/WhatsApp=verde separado del estado, pausado=ámbar (rojo solo fallos), zona horaria como select legible, confirmaciones de envío siempre con N destinatarios.

> **Cambios de comportamiento tras la revisión UX/UI/a11y (junio 2026, 6 tandas):** dos que el operador debe conocer:
> 1. **La sesión NO se persiste** (credencial solo en memoria): **al recargar la página hay que volver a iniciar sesión** (más seguro que dejarla en `sessionStorage`).
> 2. **Las listas de distribución se guardan SOLAS** al activar/desactivar una lista, añadir/quitar contactos marcados o cambiar el modo (Todos/Solo/Excepto) — ya no hace falta pulsar "Guardar listas".
>
> Además: borrado masivo en los 3 flujos (Envíos/Programados/Fraccionados); clic en el mensaje del historial → texto completo; validaciones (HH:MM, lote 1–150, delays, lista de WhatsApp obligatoria al programar); bloqueo de login devuelve 429 con "espera N min"; rate-limit en recuperación de contraseña; y mejoras de accesibilidad/responsive. La marca **Replica** (naranja) se mantiene.

### Pestañas (5, orientadas a verbo tras el rediseño de julio 2026)

- **🏠 Inicio:** KPIs de 30 días (clicables) + mini-gráfico + **sala de control**: switches «Recopilar listas del canal» y «Envíos automáticos activos», selector **«Lista del envío automático»** por canal, card **«Última lista capturada»** (con «Enviar a…») y checklist de primeros pasos. Aquí viven los interruptores del negocio (antes enterrados en Ajustes).
- **✍️ Enviar:** compositor **ÚNICO**: texto + imagen + canales + «Enviar a» (listas/contactos) + previsualización + contador (límite 4096) + selector **«¿Cuándo se envía?»** con modos **⚡ Ahora / 📅 Una vez el… / 🔁 Recurrente** (diario/semanal). El botón cambia a «Programar» según el modo. No hay formulario de programación aparte.
- **Detalle de difusión (clic en el mensaje):** modal **ancho** (`ds-wide`) con **fechas** (📥 recibido / 🚀 primer envío / 🏁 último envío) y los dos mensajes **LADO A LADO** (no apilados, para no alargar el modal): **📥 mensaje anterior (original del canal)** = `original_text` (raw, antes de limpiar/markup) a la izquierda y **📤 el mensaje FINAL que se envía** (`full_text`: sin ubicación/teléfonos, con markup y footer) a la derecha; en pantallas <640px se apilan. Debajo, **💰 comparador de precios** (anterior→nuevo por producto). `dsModal` acepta `wide`/`bodyClass`.
- **Limpieza de marca acento-insensible:** el patrón de branding es `ipr[oó][\s_\-]*parts` (y el handle `@?\s*ipr[oó]parts`) para eliminar tanto «IPRO PARTS» como «IPRÓ PARTS» (el canal publica el encabezado con tilde; antes colaba en el mensaje a enviar). `strip_patterns` no está guardado en config → usa `DEFAULT_LOCATION_PATTERNS`. El comparador y el original se guardan al capturar (`componer_con_desglose` → `desglosar_precios`) en `price_diff`/`original_text`; solo en difusiones NUEVAS del canal (las previas no los tienen). El envío MANUAL va crudo → sin comparador/original.
- **Grid de Actividad — columnas y matriz de estado:** el historial tiene columna **Fechas** (recibido/1º/último envío; `created_at` + `first_sent_at`/`last_sent_at`, estos sellados en `incr_telegram` (TG) y `bcIncr` (Node WA) con `if_not_exists`). El filtro segmentado es una **matriz de estado con conteos**: Todas / 📥 Capturadas / 🆕 Creadas (queued) / ⏳ En proceso (sending) / ✅ Enviadas (done) / ⚠️ Con fallos (partial+failed). Un banner ámbar avisa cuando el envío automático está EN PAUSA. Etiquetas de estado: creada/en proceso/capturada/enviada/parcial/fallida.
- **Borrar = DESENCOLAR (detiene el envío):** `/api/broadcasts/delete` (modos `id` e `ids`) ahora llama `plan_store.borrar(bid)` ANTES de `broadcast_store.borrar(bid)`. Como el plan se crea con el mismo id que la difusión (**pid == broadcast_id**, `broadcasting._crear_plan`), borrar el plan corta el dispatcher (desaparece de `activos()`) y hace que el worker descarte los lotes ya en vuelo en SQS (`plans.descartar` → plan inexistente ⇒ ack sin enviar). No-op seguro si no hay plan (captura/inmediato). Con `scheduling_enabled=True` (prod) esto cubre todo el envío. Límite conocido: la ruta inmediata NO fraccionada (`scheduling_enabled=False`, `sqs.encolar` sin `pid`) no es desencolable por este mecanismo; y un slice WhatsApp ya aceptado por el servicio Node no se revierte. El panel quita las filas del DOM optimistamente y refresca (`bcQuitarFilas`).
- **Grid tras borrar — consistencia:** `DynamoDbBroadcastStore._scan_todo` usa **`ConsistentRead=True`** para que el re-listado inmediato tras borrar no devuelva el item recién borrado (antes, con Scan eventualmente consistente, las filas reaparecían unos segundos — más visible en el borrado masivo).
- **Pasada de claridad + móvil (auditoría UX):** se renombraron etiquetas técnicas a español natural para usuarios NO técnicos y se reforzó el responsive. Nombres clave: «Recopilación/Recopilar» → **«Captura/Capturar»** (unificado con el resto); KPIs Inicio «lotes pendientes/en DLQ» → **«por enviar»/«atascados»**; Actividad «Cola de envío (SQS)» → **«Cola de envío»**, «Cola de fallidos (DLQ)» → **«Envíos atascados»**, stats «en vuelo/DLQ» → **«enviándose ahora»/«fallidos»**, «Purgar cola» → **«Vaciar cola»**, «Envíos fraccionados/lote» → **«Envíos por partes/grupo»**; Contactos «Destinatarios» → **«Contactos»**, «patrón/excepción» → **«por nombre»/«a mano»**, «Listas de distribución» → **«Listas de contactos»**, modos «Solo/Excluir listas activas» → **«Solo las listas marcadas»/«Todos, excepto las listas marcadas»**, «Excluir filtrados» → **«Excluir todas las coincidencias»**, quitados whitelist/blacklist; Ajustes «Registrar webhook» → **«Conectar bot»**, «Userbot» → **«Mi cuenta personal»**, «Patrones a quitar (ubicación)» → **«Textos a eliminar de cada lista»**, «Footer» → **«Texto al final del mensaje»**, «Delay … (s/ms)» → **«Pausa entre mensajes … (seg/mseg)»**, «Tamaño de lote» → **«Mensajes por grupo»**, subnav «Sistema» → **«Auditoría»**. **Móvil:** bloque `@media(max-width:620px)` reforzado (nav de 5 pestañas envuelve en vez de scroll; sub-nav y segmentado a ancho completo con tap targets ≥44px; inputs a 16px para evitar zoom en iOS; login fluido; columna Fechas oculta en el historial; paginadores táctiles; barra de acciones de Contactos con clase `.act-bar` a 2 por fila; tooltip ⓘ como barra inferior legible al tocar; `.stat` sin recortar su tooltip). Auditoría base en workflow de 6 agentes (72 hallazgos).
- **Design system de grids (buscador + paginación + eliminar todos):** helper reutilizable `GV` en el panel (`gvInit(key,cfg)` una vez + `gvSet(key,array)` en el loader) que filtra (búsqueda de texto + filtro «extra» opcional), pagina y delega el pintado de la página en `cfg.render(items,meta)`, con un pager consistente (`.gv-pager`) y buscador (`.grid-tools .gv-search`). Aplicado a: **difusiones** (`bc`, además del filtro segmentado + «Eliminar todas»), **envíos fraccionados** (`pl` + «Eliminar todos»), **programados** (`sg`, ya tenía «Borrar todos»), **usuarios** (`usr` + «Eliminar todos» que respeta admin principal y usuario actual — sin lockout) y **auditoría** (`audit` + «Limpiar auditoría»). Contactos TG/WA ya traían su propio buscador+paginación (se mantienen). **Eliminar todos** por backend: `/api/broadcasts/delete {all}` (itera y desencola cada plan), `/api/plans/delete {all}` → `plan_store.borrar_todos()`, `/api/users/delete {all}` (excluye `admin_user()` y el usuario actual), `/api/audit/delete {all}` → `audit_store.borrar_todos()` (requiere `dynamodb:DeleteItem`/`BatchWriteItem` en AuditTable, añadido al IAM del admin). **Contactos NO tiene «eliminar todos»**: la audiencia se gestiona por incluir/excluir (reversible) y en modo bot un borrado sería irreversible; decisión deliberada de seguridad. Límites de listado subidos para que la paginación tenga datos: broadcasts 1000, plans 1000, audit 500 (schedules ya devuelve todo). Ver memoria [[grids-design-system]].
- **Estado SQS en vivo + purga de la cola principal:** `SqsQueueStats.profundidades()` devuelve `broadcast`/`dlq` (en cola, visibles) y `en_vuelo`/`dlq_en_vuelo` (NotVisible = lotes que el worker entrega ahora). El panel muestra una **tira SQS en vivo** en Actividad → Historial (en cola · en vuelo · en DLQ, refrescada con el poll del grid) y la card **«Cola de envío (SQS)»** en Problemas con 4 métricas. **Purgar la cola principal**: `SqsQueueStats.purgar_principal()` → endpoint `POST /api/queue/purge` → botón «🗑 Purgar cola» (descarta lo encolado; no revierte lo entregado; los lotes en vuelo pueden completarse). `purgar_principal` y `dlq_purgar` manejan `PurgeQueueInProgress` (1 purge/60s) devolviendo `en_progreso` en vez de reventar. IAM ya concede `sqs:PurgeQueue` sobre la cola principal (template.yaml) — sin cambios de infra.
- **📡 Actividad** (sub-nav): **Historial** (tabla con estado, progreso «en vivo», borrado individual/masivo, error clickeable y **filtro segmentado** Todas / 📥 Capturadas / En curso / Enviadas / Fallidas + botón **«Enviar a…»** en las capturadas) · **Envíos fraccionados** (monitor de planes) · **⏰ Programados** (solo lista de recurrentes: pausar/reanudar/borrar) · **⚠️ Problemas** (cola SQS + DLQ con reintentar/descartar).
- **👥 Contactos** (sub-nav Telegram / WhatsApp): **destinatarios** con filtro Todos / Incluidos / Excluidos y contador, **listas de distribución** (whitelist/blacklist) y **auto-exclusión por patrón de nombre**.
- **⚙️ Ajustes** (sub-nav): **🔌 Conexiones** (cuenta de Telegram bot/userbot, WhatsApp reenvío + QR/pairing) · **📥 Captura** (canal fuente, markup, símbolos, footer, patrones, imagen, «probar procesamiento») · **📤 Ritmo y horarios** (anti-baneo lote/delays, ventanas, **zona horaria como select legible**) · **👤 Acceso** (usuarios con roles, correo de recuperación, cambio de contraseña) · **🛠️ Sistema** (auditoría).

`goStep` mantiene ALIASES de rutas viejas→nuevas (retirar en 2-3 releases); `showSub` cae al sub default si el guardado en `localStorage` ya no existe.

### Operación típica

- **Activar/pausar lo automático:** switch «Envíos automáticos activos» en 🏠 Inicio o la barra global. La pausa solo frena lo automático; el **manual siempre sale**.
- **Envío puntual:** ✍️ Enviar → texto + imagen → canales (ninguno preseleccionado) → «Enviar a» (listas/contactos) → modo ⚡ Ahora (o 📅 Una vez el…) → confirmar (muestra N destinatarios) → seguir en 📡 Actividad → Historial.
- **Listas y exclusiones:** en 👥 Contactos, listas por canal (`all`/`only`/`except`) y patrones de auto-exclusión por nombre. El envío manual a WhatsApp **exige** una lista/destinatarios concretos.

---

## Errores y lecciones (gotchas)

Lista accionable para no repetir fallos:

### Lambdas y variables de entorno de tablas

- **Toda Lambda que use un store DynamoDB NECESITA su env `*_TABLE`**; si no, cae al nombre por defecto → `AccessDeniedException` SILENCIOSO.
  - El `worker` sin `CONFIG_TABLE` → default `"Config"` → AccessDenied antes del try/except → tumbaba el lote → reintentos → DLQ sin tocar contadores → job "queued" eterno leído como "enviado" (DM nunca llegaba).
  - El `worker` sin `PROCESSED_UPDATES_TABLE` → **envío DUPLICADO** (un contacto recibió ~5 veces): `dedup.procesado()` es fail-open pero `dedup.marcar()` RE-LANZABA el AccessDenied → el lote ya entregado se reencolaba hasta `maxReceiveCount≈5`. Fix: añadir env + IAM y hacer `marcar()` **fail-open**.
  - **Acción:** ante cualquier Lambda nueva que llame `build_*` o use un store, verificar que tenga TODAS las `*_TABLE` en env + el permiso IAM correspondiente.

### Anti-duplicado (vectores y mitigaciones)

- **Timeout a mitad de lote:** `WorkerTimeoutSeconds=120` no alcanzaba para 150 destinatarios × jitter → timeout → SQS reentregaba DESDE EL INICIO. Fix: idempotencia POR DESTINATARIO (`ProcessedUpdates` key `batch_id:chat_id`, resume sin duplicar) + subir timeout a **300**.
- **`aws cloudformation deploy` reusa el valor PREVIO del stack** para params no pasados (no el default del template). Por eso `deploy.ps1` pasa `WorkerTimeoutSeconds=300` explícito.
- **Dispatcher concurrente** (cron solapado): reserved-concurrency=0 no limita a 1 → **lock optimista** en `registrar_dispatch` (`ConditionExpression` sobre el cursor).
- **Destinatarios repetidos** en un lote → `dict.fromkeys` al crear el plan.
- **`broadcast_id` determinista por `update_id` (M18):** el receiver pasa el `update_id` del post como `dedup_key`; el camino de plan (scheduler) deriva un `broadcast_id` determinista. Si crear el plan falla a mitad (BatchWrite parcial) y el receiver revierte el dedup → Telegram reintenta el webhook → el reintento reusa el MISMO id y **sobrescribe** el plan en vez de crear otro → no duplica la difusión (el envío manual sigue usando id aleatorio).
- **Claim-then-enqueue (A4/A13):** `registrar_dispatch` avanza el cursor (reclama el lote) ANTES de encolar (TG) / llamar al servicio (WA). Si ese encolar/forward LANZA después del claim, el lote quedaba reclamado pero sin enviar → el `in_flight` colgaba ~900s y el lote se perdía en silencio. Fix: ambas llamadas van en `try/except` → liberan el `in_flight` (`limpiar_inflight`) y registran el fallo VISIBLE en el job (no re-encolan con un `batch_id` nuevo, que duplicaría si el envío sí salió; el operador reenvía ese lote).
- **Dedup fail-open con semántica opuesta según el llamador (A8):** `DynamoDbDedupStore.marcar()` devolvía `False` tanto por duplicado real como por fallo de infra (throttle/permiso/tabla). El **worker** quiere ese fail-open (no reentregar un lote ya enviado), pero el **receiver** lo leía como "duplicado" y descartaba en silencio updates legítimos (post del canal / onboarding) que Telegram daba por entregados (200) sin reintentar. Fix: nuevo `marcar_estricto()` que **propaga** el fallo de infra; el receiver lo usa y, si no puede confirmar, **procesa igual** (no asume duplicado). `marcar()` sigue fail-open para worker/poller.
- **Lote en vuelo sin `bid` (M5):** si no había `broadcast_id` el progreso se sustituía por `target` → el lote se daba por terminado al instante y rompía la secuencialidad. Ahora sin bid el progreso es desconocido (0) y el lote espera al corte por estancamiento. **`in_flight_at==0` (B17)** ya no cuenta como antigüedad gigante (no abandona un lote recién reclamado); se exige `in_flight_at>0` para declarar stale.
- **Lote estancado deja rastro (M29):** al liberar por `stale_seconds` se hace `registrar_error(bid, "lote … estancado")` para que el job no quede colgado sin causa (no se tocan contadores: un slice WA estancado no debe corromper el tracking de los slices restantes).
- **Re-firma de imagen WhatsApp (B5):** se re-firma ANTES de reclamar el lote; si falla y hay `wa_image_key`, NO se envía con la URL prefirmada (posible caducada) — se reintenta el próximo tick sin avanzar el cursor.
- **`dispatch_log` acotado (M9):** la bitácora del plan se limita a las últimas 200 entradas con un `SET` (no `list_append` sin cota), evitando que el item meta crezca hacia 400 KB en planes con miles de lotes (abortaría el claim condicional → despacho atascado).
- **Items de lote TG con TTL (M7):** los items `(pid, TG#…)` llevan el mismo `ttl` que el meta; antes no expiraban y quedaban huérfanos para siempre.
- **`borrar terminados` no borra envíos en vuelo (M8):** se cruzan los broadcast_ids de planes `pending/running` (`PlanStore.activos()`) y se excluyen; antes la heurística por edad (`_EDAD_TERMINAL=1h`) daba por terminal un envío fraccionado largo y lo borraba mientras seguía despachando.
- **Fallo de encolado no deja WhatsApp colgado (M6):** en el envío no programado, si `encolar()` lanza (p. ej. `PartialEnqueueError`), se registra el error, se intenta el forward de WhatsApp (independiente) y se re-lanza (para la compensación de dedup del receiver) — antes WhatsApp quedaba 'enviando' eterno.
- **Imagen no firmable NO se entrega como texto-solo (B7):** si el lote trae `image_key`, la firma S3 falla y NO hay `image_url` de respaldo, el worker **reintenta** el lote (itemFailure → SQS → DLQ si persiste) en vez de entregar texto-sin-imagen marcándolo como éxito (perdía la foto en silencio).
- **Auto-lista borrada/renombrada deja rastro (M25):** si el envío automático está activo pero la lista elegida resuelve a 0 destinatarios, se registra un error en el job en vez de cerrarlo como 'enviado-vacío' silencioso (`auto_<canal>_list` guarda el NOMBRE; borrar/renombrar la lista lo deja apuntando a la nada).
- **`marcar_inactivo` de un chat inexistente es no-op (B6):** captura el `ConditionalCheckFailedException` benigno (chat nunca registrado / purgado) en vez de propagar un stacktrace que ensuciaba los logs.
- **No-solape del dispatcher = lock optimista, NO concurrencia reservada (M28):** `DispatcherReservedConcurrency` default es 0; la no-duplicación la garantiza el `ConditionExpression` del cursor en `registrar_dispatch`. Dos ticks simultáneos podrían reclamar TG y WA del mismo plan a la vez (no duplica, pero rompe el ritmo "un lote a la vez"); para secuencialidad estricta, fijar `DispatcherReservedConcurrency=1` en el stack.
- **Fallo de preview de captura visible (B16):** si el preview de una lista capturada no llega a Mensajes Guardados (FloodWait/sesión), se registra el error en el job (`registrar_error`) — el panel lo muestra; antes solo quedaba en logs y el HWM ya había avanzado.
- **`AlertEmail` validado + recordatorio SNS (B14):** el parámetro lleva `NoEcho` y `AllowedPattern` de email. Recordar: una suscripción SNS por email **solo entrega tras confirmarla** desde el correo; si es el único canal de reseteo de contraseña (sin Resend), hay que confirmarla o el código no llega.

### Trade-offs conocidos del diseño fail-open (NO son bugs nuevos)
- **Doble-conteo / reenvío si `dedup.marcar()` cae por infra (M10/M30/B4):** `marcar()`/`procesado()` son fail-open a propósito (priorizan no-bloquear sobre no-duplicar; un re-lanzar reentregaría en bucle). Ante un fallo de infra REAL (no `ConditionalCheckFailed`) entre incrementar contadores y marcar, una reentrega SQS puede re-contar/re-enviar. **Mitigación IMPLEMENTADA:** el except de infra emite la métrica `Replica/Backend / DedupInfraError` vía **EMF** (log estructurado → CloudWatch la extrae sin permisos extra) y el stack tiene la alarma `…-dedup-infra` → SNS. Si la alarma suena, revisar `ProcessedUpdates` y los contadores de los jobs del período.
- **`paused`/strikes se leen una vez por invoke (B8):** correcto con `WorkerEventSourceMapping BatchSize=1` (invariante actual). El worker ahora **advierte en logs** si detecta más de 1 record por invoke; si se sube el BatchSize por throughput, mover la lectura de pausa/strikes a por-record.

### Plan de pruebas
El plan operativo (suite automatizada, smoke post-deploy, E2E manual por flujo y matriz de regresión bug→test) vive en **[docs/PLAN_PRUEBAS.md](docs/PLAN_PRUEBAS.md)**. Regla: todo bug corregido lleva test de regresión con el ID del hallazgo en el nombre (`test_a12_…`); si uno de esos tests falla, se está reintroduciendo un bug conocido.

### FloodWait de Telegram (Telethon)

- **NUNCA listar contactos con Telethon EN VIVO** (`GetContactsRequest`) desde el panel/preview: Telegram responde `FloodWaitError` (hasta ~1000s) y reventaba `enviar_manual`/`previsualizar` con HTTP 500. Fix: listar desde la **CACHÉ** (DynamoDB `__contacts__`); el envío real (`sendMessage`) no sufre ese FloodWait. El poller refresca la caché máx cada 30 min.
- **Estado de sesión del header también va por CACHÉ** (evita dos clientes con la MISMA `StringSession` a la vez): el panel llamaba `/api/telegram/account` cada 60s, que abría Telethon y podía solaparse con el lector/preview del poller (que desde que t.me murió corre cada 5 min). Fix: el **poller** escribe el estado `{connected, me, checked_at}` en `__tg_status__` (config table) en cada run (ya tiene Telethon abierto); el **panel solo LEE** la caché y solo abre conexión propia si está vencida (`_TG_STATUS_TTL=390s` > los 5 min del poller → mientras el poller corra, el panel nunca conecta). `me` se conserva entre escrituras (identidad estable). El poller cachea también con la captura pausada.

### WhatsApp / Baileys

- Los **contactos + nombres SOLO llegan en el VÍNCULO INICIAL** (`messaging-history.set`); reconexión, `syncFullHistory` y `resyncAppState` NO los re-entregan. Vincular UNA vez desde IP residencial, persistir en DynamoDB y que Render reuse la sesión.
- **Linking desde IP de datacenter (Render) da "inténtalo más tarde".** Lo fiable: vincular localmente y que Render reuse la sesión de DynamoDB. Alternativa: `/pair` (código de 8 dígitos) + `Browsers.macOS('Desktop')`.
- **Un solo host activo a la vez:** el ciclo de vida del socket se reescribió (mutex de arranque, un solo socket por `gen`, en 440/connectionReplaced CEDE en vez de reconectar) para acabar con la "guerra" local↔Render. `loggedOut` es seguro (no auto-borra). Timeout 8s en `fetchLatestBaileysVersion` (sin él colgaba `/pair`).
- **Build en Render** requiere: `git`+`ca-certificates`, `npm install --legacy-peer-deps`, reescribir URLs git SSH→HTTPS. Hay ruta `/` informativa para evitar "Cannot GET /".
- **Render Free duerme a los 15 min** → primer request da timeout; despertar con `/health` antes (el dispatcher hace keep-alive cuando hay planes activos).
- **`enviarLote` usa el socket GLOBAL (A10):** el bucle de envío dura minutos (jitter); si un `/reset`, `/reconnect` o `scheduleReconnect` reemplaza el socket a mitad (`sock.end()`+`sock=null`, `gen++`), `sock.sendMessage` lanzaba `TypeError` (ventana `sock===null`) o enviaba sobre OTRA sesión, contando fallos espurios que **auto-excluían contactos válidos** (opt-out por `failures[jid]`). Fix: snapshot al entrar (`const s = sock; const myGen = gen`), usar `s.sendMessage`, y abortar limpio el resto del lote si `gen !== myGen || !connected` (sin marcar fallos).
- **Endpoint `/count` dedicado (M16):** el conteo previo al fraccionado usa `POST /count` (solo cuenta, NUNCA envía), no `/send` con `count_only`. Si el flag se perdiera (proxy/regresión), `/send` habría difundido a todos. El adapter cae a `/send?count_only` solo si `/count` responde 404 (servicio viejo). **Al desplegar el servicio Node hay que incluir esta versión** o el conteo usará el fallback.
- **`/send` valida `offset`/`limit` (M20):** un valor no numérico/negativo daba `NaN` → `slice` vacío → 202 `{targets:0}` sin enviar a nadie en silencio. Ahora responde **400**. El `bcSetTotal` (wa_total/wa_started) se hace solo en el **primer slice** (`offset===0`) para no pisarlo ni correr carreras (M21). El jitter anti-baneo va solo ENTRE mensajes, no tras el último (B10).
- **Opt-outs (`failures`) se cargan UNA vez (M19):** antes `doStart` re-fusionaba los conteos del store en cada `/reconnect`, resucitando opt-outs ya limpiados por `/blocked/clear`. Ahora se cargan solo en el primer arranque del proceso y `/blocked/clear` persiste **sincrónicamente** (flush del debounce) para que un reconnect inmediato no reviva los conteos.
- **Persistencia de sesión por lotes (M22/B11):** `keys.set` y `clearAll` (`dynamoAuth.js`) usan `BatchWriteItem` (25/lote) con reintento de `UnprocessedItems`; antes eran `PutItem`/`DeleteItem` sueltos en serie → un fallo parcial dejaba la sesión inconsistente. `clearAll` lanza si no termina y `doStart` NO marca `clearOnStart` consumido hasta que el borrado completa (no re-vincular sobre creds a medio borrar).
- **`/pair` cancela el pairing pendiente (B12):** el `setTimeout` de `requestPairingCode` se guarda en `pairTimer` y se cancela al reemplazar el socket; la vuelta a QR tras timeout es `await restart()`. Evita generar un código sobre un socket viejo y solapes con un `/pair` posterior.
- **Preview reutiliza conexión por corrida (B15):** en captura, el poller activa `_diferir_cierre_preview` y cierra el cliente Telethon UNA vez al final (`cerrar_preview()`) en vez de conectar/desconectar por post — menos latencia y menos conexiones a la sesión.

### Imagen en los envíos (fix crítico)

- El compositor guardaba la **URL S3 PREFIRMADA** (caduca 1h) → en fraccionados/programados moría ("imagen no disponible"). Fix: la imagen viaja como `image_key` extremo a extremo y se **RE-FIRMA al despachar**; claves S3 únicas por subida (uuid).
- **Una URL S3 prefirmada solo es válida si el ROL que la firma tiene `s3:GetObject`.** El rol del **dispatcher** NO lo tenía → 403 al descargar (texto llegaba, imagen no). Fix: añadir `s3:GetObject` sobre `images/*` al dispatcher.
- Checkbox Telegram nacía `checked` → mensajes por AMBOS canales. Fix: ningún canal preseleccionado + confirmación de canales.
- WhatsApp manual exige el servicio CONFIGURADO (url+token), NO `whatsapp_enabled` (que es el auto-reenvío del canal).
- **Envío automático SIEMPRE por lista elegida (A12):** el ENVÍO automático del canal difunde SOLO a la lista `auto_<canal>_list` elegida en 🏠 Inicio. Si un canal no tiene lista elegida NO difunde (antes caía a `<canal>_target`=`{mode:'all'}` → toda la agenda); si NINGÚN canal tiene lista, el post se trata como **captura** (registra + preview, no envía). En consecuencia el automático va siempre en modo `only` y NO aplica la auto-exclusión por patrón de nombre (esa solo actúa en envíos AMPLIOS/manuales). La guardia "elige lista antes de activar" se enforza también en el **backend**: `/api/config POST` rechaza con **400** activar `sending_enabled` sin `auto_telegram_list` (y sin `auto_whatsapp_list` si `whatsapp_enabled`), no solo en el front.

### Truncado de mensajes

- `DynamoDbPlanStore.crear` guardaba `text[:280]` y ESE texto se envía → DMs cortados a 280 chars. Subido a `[:4096]` (límite Telegram). `Broadcasts.text` (solo display) a 600.

### Markup / limpieza de texto

- El markup EXIGE un marcador de moneda adyacente (`$`/💸/💲/`COP`); números "pelados" NO se tocan (para no marcar teléfonos/fechas/modelos/specs). El canal usa los tres símbolos.
- Redondeo al **mil hacia arriba** (decisión del usuario): `$325.000` +15% → `$374.000`.
- Quitar teléfonos CO EXIGE una señal de teléfono (separador/paréntesis/`+57`/palabra de contacto); un número de 10 díg pegado y sin etiqueta NO se toca (evita borrar `REF 3001234567`).
- **Teléfono se quita por COINCIDENCIA, no por línea (M3):** `quitar_telefonos` sustituye solo el match del teléfono dentro de la línea; si la línea queda sin contenido útil (era solo "Cel 300…") se descarta entera. Antes se borraba la línea completa → una línea mixta `A06 4-64GB 300 123 4567 $325.000` perdía el producto Y el precio. Los patrones se combinan en un único regex (una pasada) para que el etiquetado consuma keyword+número juntos y no deje la etiqueta huérfana.
- **Markup NO toca precios con coma decimal/mixta (B3):** si tras el entero sigue `,<dígito>` (`$1.500,50`, `$1.150,000`) el precio se deja intacto en vez de escalar solo la parte entera y arrastrar la fracción suelta (que daba valores corruptos como `$2.000,50`).

### Infra / drift / packaging

- **`aws cloudformation deploy` NO corrige drift:** tras `aws events disable-rule` a mano, un redeploy NO re-habilita la regla. Re-habilitar con `aws events enable-rule`; el ESM del worker con `aws lambda update-event-source-mapping --uuid <> --enabled` (UUID worker ESM: `756b6f9e-c3ae-42be-8683-2c56be0ecb27`).
- **Empaquetar DEBE hacerse en Linux** (`package-lambda.ps1` en `python:3.12-slim`); `Compress-Archive` en Windows generaba rutas con `\` y binarios que rompen. Si Docker está caído y el cambio es .py puro: refrescar los `.py` en `.build/pkg` y re-zipear con `zipfile` (arcnames forward-slash, `external_attr=(0o755)<<16`).
- **`aws cloudformation deploy` resetea params no pasados a su default** → `deploy.ps1` pasa TODOS los params.
- **Concurrencia Lambda de la cuenta = 10** → no se puede reservar; param `WorkerReservedConcurrency` con Condition (este deploy usó 0). Para prod, subir el límite y poner 1.
- **`deploy.ps1` sube el zip con key hasheada por contenido** para que CFN detecte el cambio de código.

### Estructura de código / tests

- Añadir una clase nueva anclando entre dos métodos de otra clase dejó `PlanStore.listar` DENTRO de `AuditStore` → rompió `/api/plans` y `/api/audit`. **Al añadir una clase al final de un módulo, anclar en el ÚLTIMO método de la clase previa.** Hay test de regresión (`EstructuraStoresTests`).
- Los fakes WhatsApp en tests necesitan `exclude_patterns=None`/`**kwargs` en `forward`/`contar`.
- CI estaba en rojo: el job `test` no instalaba deps → `adapters/telegram.py` importa `requests` al cargarse → ModuleNotFoundError. Fix: `pip install -r src/lambda/requirements.txt`.

### Ingesta del canal: el preview t.me puede morir (6 jul 2026)

- **Síntoma:** capturas con solo "📌"/"(OJO) 📌" y luego CERO capturas; el poller loguea "Preview de <canal> sin publicaciones con texto" en cada tick. **Causa:** Telegram dejó de servir `t.me/s/<canal>` (redirige a la tarjeta del canal, sin mensajes) — no es un bug del pipeline; el preview simplemente desapareció. Las capturas "📌" fueron posts cuyo contenido real estaba EN LA IMAGEN (el preview solo exponía el caption).
- **Fix:** `FallbackChannelReader` — el poller intenta el preview (barato) y, si viene vacío, lee el canal con el **userbot** (`TelethonChannelReader`, `get_messages`): mismos `message_id` (el HWM sigue válido), captions incluidos, detecta `has_photo` y desconecta al terminar (M17). Si la sesión Telethon no está configurada, el respaldo devuelve `[]` sin romper (M14).
- **Posts con imagen:** la CAPTURA anota "📷 La publicación original incluye una imagen (este texto es su caption)" para que un caption mínimo no parezca captura vacía. El envío a contactos NO lleva la nota. Los posts SOLO-imagen (sin caption) siguen saltándose (A7, decisión de producto diferida).

### Falsa alarma "se borraron los patrones"

- NO fue bug ni deploy (la config persiste en DynamoDB; los deploys no tocan la tabla). Fue un paso de "restaurar config" en pruebas en vivo que hacía `*_exclude_patterns:[]`. Llevó a mover los 6 campos de destinatarios a **POR USUARIO** (registro `__users__[user]`), fuera de `/api/config`.

### Git / identidad y Local / Windows

- La identidad git global es de trabajo (`julian.castano@siesa.com`) y quedó así en el primer commit. Push vía alias SSH `github-personal` con URL `git@github-personal:AndresDFX/...` para forzar la clave personal.
- El proxy del sistema intercepta `localhost:8080` (404/timeout); el smoke-test del webhook local se hace dentro del contenedor (`telegram-sync-webhook`).
- El script de vinculación WhatsApp es **solo ASCII** (PS5.1 corrompe UTF-8 sin BOM) y usa `ErrorActionPreference=Continue` (stderr de docker con Stop es fatal).

---

## Estado actual

> Notas revisadas por última vez tras la revisión post-rediseño (HEAD `2857100`, julio 2026). Verificar contra el código actual antes de afirmar como hecho.

- **Recopilación ACTIVA + envío PAUSADO** (`capture_enabled=True` por defecto, `sending_enabled=False`): el poller captura `@iproparts` y autoenvía cada lista a **Mensajes Guardados** del userbot (se ven como `📥 Capturada` en el panel), SIN difundir a nadie. El envío MANUAL (✍️ Enviar → ⚡ Ahora) SIEMPRE sale. Para encender el envío automático: en 🏠 Inicio elegir la lista por canal en «Lista del envío automático» (el panel lo exige) y activar el switch «Envíos automáticos activos».
- **Modo de envío Telegram: `userbot` (Telethon)** — envía como la cuenta del usuario a sus contactos (no como bot a suscriptores), aceptando riesgo de baneo. La sesión viva NO está en `.env.deploy` (esa está REVOCADA); la válida vive en DynamoDB config (`telethon_session`, ~353 chars); `api_id`/`api_hash` vienen del env del Lambda. El bot `getMe` responde `@ipro_listas_bot`.
- **Fuente de ingesta: `@iproparts`** (público, ajeno) → ingesta por **poller** (sondea `https://t.me/s/iproparts` por cron, high-water mark del `message_id`); el webhook solo para onboarding `/start`·`/stop`.
- **WhatsApp 100% operativo en producción**, desplegado en **Render** (`https://telegram-sender-dm43.onrender.com`, Docker, plan Free → duerme a 15 min). Conectado como número **573243198985**, ~2180-2307 contactos (2180 con nombre de agenda), sesión + contactos persistidos en DynamoDB. Resuelve `list_ids` como JIDs (no nombres de lista).
- **Cuentas de PRUEBA (e2e):** `3188468892` → Telegram chat_id **6053071541** (nombre "Prueba"/"Andrés Castaño"); WhatsApp **573188468892@s.whatsapp.net**.
- **Planes/backlog:** 0 planes pendientes, 0 schedules (todo el backlog viejo de iproparts —18 planes, ~24k envíos cada lote— fue CANCELADO y BORRADO; los registros de broadcast se conservan). `telegram_lists=[]`; en WhatsApp queda la lista "Test Whastsapp 2".
- **Reglas EventBridge (poller, dispatcher) + ESM del worker: ENABLED.** DLQ: 0.
- **AlertEmail SNS** (`castano.julian@correounivalle.edu.co`): suscripción quedó **PendingConfirmation** (el usuario debe confirmar el correo).
- **Marca actual del producto: "Replica"** (tagline "Tu lista de precios, replicada y enviada en segundos."). README = documento de contexto canónico.
- **~243 tests** pasando; 2 workflows CI (`tests.yml` + `deploy.yml`, deploy gated por `DEPLOY_ENABLED`).
- **Infra clave:** stack `telegram-sync-dev` (cuenta `438095550710`, `us-east-1`). Tablas: subscribers, processed-updates, config, broadcasts, plans, schedules, audit, whatsapp-auth. Lambdas: receiver, poller, worker, dispatcher, admin. Panel admin: `.../dev/admin` (Basic Auth, usuario `admin`). Roles `admin`/`user` activos.

---

## Pendientes / roadmap

### Antes de producción (críticos)

- **Revisar/cancelar el backlog de "held plans" antes de reactivar `sending_enabled`**: activar drena TODO el backlog ready (modelo capturar-siempre de iproparts) → riesgo de inundar. La confirmación de "Activar envíos" debería mostrar el conteo de pendientes (ya se lista en el modal).
- Subir el **límite de concurrencia Lambda** de la cuenta y poner `WorkerReservedConcurrency=1` (respeta 30 msg/s).
- Migrar **secretos a SSM/Secrets Manager (SecureString)**: hoy la tabla `config` guarda secretos sin KMS.

### Operación / always-on

- **Host always-on para WhatsApp** (Render Free duerme; mitigado con keep-alive, pero ideal Fly/Koyeb/Oracle para socket estable). Riesgo de baneo del número por envío masivo.
- Confirmar la **suscripción SNS de AlertEmail** (pendiente del usuario).
- **Pegar la API key de Resend** en el panel (hoy sin configurar → recuperación de contraseña cae a SNS).

### Roadmap / futuro

- ✅ CI/CD auto-deploy en push a main: **ACTIVADO y validado** (Secrets de AWS/app + `DEPLOY_ENABLED=true` cargados con `scripts/set-github-secrets.ps1`; push a `main` corre tests y despliega a CFN). Nota: `set-github-secrets.ps1` sube los secrets con `gh secret set --body $val` (NO `--body -`, que gh toma como literal "-" y corrompe el secret → deploy falla con "security token invalid").
- Dominio propio / HTTPS / WAF.
- Logs estructurados.
- Onboarding wizard.
- "Top fallos" por razón.
- Plantillas / personalización de mensajes.
- Cambio de contraseña desde el panel.

### Hallazgos diferidos (NO son bugs nuevos, no re-investigar)

- El preview de WhatsApp en el panel cuenta solo `len(list_ids)` → NO refleja exclusiones del servicio (patrón/manual/fallos); es estimado, el envío real sí excluye.
- Rate-limit del login es in-memory por contenedor (no distribuido); mitigado con throttling de API Gateway (20 rps/40 burst).
- Sin CORS explícito en el panel.
- `/api/auth/forgot|reset` son públicos (con anti-fuerza-bruta local).

### Hardening de backend (bug-hunt "otros errores") — RESUELTO

Se cerraron todas las altas y todas las medias/bajas accionables (commits `fix(backend) Batch 4..10`, `fix(whatsapp-service) Batch 5`, `fix(backend) Batch 11`). Los últimos cerrados (sesión/vinculación Baileys + perf de captura):

- **M22 / B11 (sesión Baileys, `whatsapp-service/src/dynamoAuth.js`):** `keys.set` y `clearAll` ahora escriben/borran con `BatchWriteItem` (lotes de 25) + reintento de `UnprocessedItems` (backoff), en vez de `PutItem`/`DeleteItem` sueltos en serie. `clearAll` LANZA si quedan ítems sin borrar, y `doStart` solo da por consumido `clearOnStart` si `clearAll` terminó (no re-vincula sobre sesión a medio borrar).
- **B12 (`/pair`):** el `setTimeout` del `requestPairingCode` se guarda en `pairTimer` y se cancela en el teardown de `doStart` (no genera un código sobre un socket que se va a reemplazar); el `restart()` de la rama de timeout ahora es `await`.
- **B15 (perf captura):** el preview a Mensajes Guardados REUTILIZA una sola conexión Telethon en toda la corrida del poller (`_diferir_cierre_preview` + `cerrar_preview()`), en vez de conectar/desconectar por post.
- **B18:** `registrar_error` separa el `ADD` al set (siempre) del `SET last_error` (condicional al timestamp), así `last_error` refleja el fallo más reciente y no un last-writer-wins ciego.
- **B9:** ya estaba mitigado — `preview_sender` solo se construye en userbot y con conexión perezosa.

Siguen como **trade-offs conscientes** (no bugs): M10/M30/B4 (doble-conteo posible si `marcar()` cae por infra — fail-open prioriza no-bloquear) y B8 (`paused`/strikes por-invoke, correcto con `BatchSize=1`). Ver sección "Trade-offs conocidos del diseño fail-open".

---

## Checklist de handoff

Pasos concretos para que el receptor quede operativo:

1. **Clonar el repo** por SSH personal: configurar el alias `github-personal` en `~/.ssh/config` (apuntando a `id_rsa_personal`) y `git clone git@github-personal:AndresDFX/telegram-sender.git`.
2. **Pedir los secretos por canal seguro:** solicitar `.env.aws` y `.env.deploy` completos por gestor de contraseñas / canal cifrado (nunca por chat/PR/correo plano). Colocarlos en la raíz del repo (están gitignored). Confirmar que `.gitignore` los cubre antes de cualquier commit.
3. **Instalar herramientas:** AWS CLI (configurado con la cuenta `438095550710`, `us-east-1`) y Docker (necesario para empaquetar la Lambda en Linux).
4. **Verificar acceso AWS:** `aws sts get-caller-identity` con las credenciales de `.env.aws` debe devolver la cuenta `438095550710`.
5. **Build + deploy:** `./scripts/package-lambda.ps1` y luego `./scripts/deploy.ps1`. Tomar el `AdminUrl` que imprime al final. Verificar que la key del zip con hash es la aplicada al stack y que reglas EventBridge + ESM del worker siguen ENABLED.
6. **Verificar el panel:** abrir el `AdminUrl`, entrar con usuario `admin` + `ADMIN_PASSWORD`, comprobar el header (identidad Telegram, canal fuente `@iproparts`, número de WhatsApp, badge de rol) y la barra global de estado (debe mostrar EN PAUSA, ya que `sending_enabled=False`).
7. **Servicio WhatsApp en Render:** confirmar que `https://telegram-sender-dm43.onrender.com/health` responde; revisar las variables de entorno (`WHATSAPP_TOKEN`, `WHATSAPP_AUTH_TABLE`, AWS, `SEND_DELAY_MS`, `PORT`). Si la sesión no está activa, **vincular por QR/pairing desde IP residencial** (`./scripts/vincular-whatsapp-local.ps1`, con `-Pair <número>` o `-Reset`) para que Render reutilice la sesión de DynamoDB.
8. **Prueba de extremo a extremo controlada:** con las cuentas de prueba (Telegram chat_id `6053071541`, WhatsApp `573188468892@s.whatsapp.net`), hacer un envío manual (que sale aun en pausa) y seguir el progreso en la tabla de Envíos.
9. **Tareas pendientes del receptor:** confirmar la suscripción SNS de AlertEmail; pegar la API key de Resend en el panel; **NO reactivar `sending_enabled`** hasta revisar/cancelar el backlog de planes en espera.
10. **Rotación recomendada tras el traspaso:** rotar al menos `ADMIN_PASSWORD`, `WHATSAPP_TOKEN` y las access keys de AWS si el emisor anterior ya no debe tener acceso (ver sección "Rotación").
