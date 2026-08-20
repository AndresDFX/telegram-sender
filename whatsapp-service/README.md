# whatsapp-service (Opción A · Baileys)

Servicio portable que **reenvía por WhatsApp** las mismas listas que el sistema difunde por
Telegram. Mantiene la conexión de WhatsApp Web (Baileys), expone el **QR** para vincular tu
WhatsApp desde el panel, lista tus contactos y envía. La **sesión se persiste en DynamoDB**, así
que sobrevive a reinicios/spin-down **sin re-escanear** el QR (por eso funciona en hosts de disco
efímero como Render).

> ⚠️ Automatizar WhatsApp personal va contra sus términos y puede **banear tu número**. Usa cadencia
> alta entre envíos (`SEND_DELAY_MS`) y empieza excluyendo casi todos los contactos para probar.

## Endpoints (todos con `Authorization: Bearer <WHATSAPP_TOKEN>`, salvo `/` y `/health`)
- `GET /` — página informativa (estado del servicio).
- `GET /health` — para el healthcheck del host (sin token). Devuelve además el **sello del build**
  (`commit`, `src`, `started_at`, `uptime_s`) para poder validar el despliegue — ver más abajo.
- `GET /status` — `{connected, me, qr (dataURL), pairingCode, lastClose, contacts, build}`.
- `GET /qr?token=...` — **página de QR en vivo** (se auto-renueva; el token va por query para abrirla en el navegador).
- `POST /pair` — `{number}` (con código de país, solo dígitos) → `{pairingCode}` para vincular **por código de 8 dígitos** (alternativa al QR). Empieza limpio; si falla devuelve `{error, detalle}` con la causa real.
- `POST /reset` — borra la sesión guardada y regenera QR (re-vincular desde cero, self-service).
- `GET /contacts` — `[{id, name}]`.
- `POST /send` — `{text, image_url?, exclude?:[ids], mode?, list_ids?}` → envía según el modo de listas (`all`/`only`/`except`).

## Variables de entorno
| Var | Descripción |
|-----|-------------|
| `WHATSAPP_TOKEN` | Secreto compartido con el backend (Lambda) y el panel. |
| `WHATSAPP_AUTH_TABLE` | Tabla DynamoDB para la sesión (la crea el stack: `telegram-sync-dev-whatsapp-auth`). |
| `WHATSAPP_SESSION_ID` | Id de sesión (default `default`). |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` | Credenciales con acceso a esa tabla DynamoDB. |
| `SEND_DELAY_MS` | Delay entre envíos (default 2000). Súbelo para reducir riesgo de baneo. |
| `PORT` | Puerto HTTP (default 8080). |

## Desplegar en Render (free)
1. Sube este repo a GitHub (ya está).
2. En Render → **New → Web Service** → conecta el repo → **Root Directory:** `whatsapp-service` →
   Runtime **Docker** (usa el `Dockerfile`).
3. Añade las variables de entorno de arriba (token, tabla, AWS creds, región).
4. Deploy. Copia la **URL pública** del servicio.
5. En el **panel admin** → sección WhatsApp: pega esa URL + el token, activa WhatsApp y **escanea el
   QR** que aparece con tu teléfono (WhatsApp → Dispositivos vinculados).

> Alternativas equivalentes (mismo contenedor): **Fly.io** o **Koyeb** (socket más estable, también
> free), u **Oracle Always Free** (VM gratis siempre). Como la sesión vive en DynamoDB, puedes mover
> el contenedor de host sin re-vincular.

### Despliegue automático (y cómo comprobarlo)

Render trae **Auto-Deploy** activado: cada `git push` a `main` que toque `whatsapp-service/` dispara
build + publicación, sin pasos manuales. Verificado el 2026-08-20: **~5 min** de push a producción.

Para saber qué código corre de verdad, el servicio sella su build y lo publica en `/health` (sin token):

```jsonc
{ "ok": true,
  "commit": "329008b",       // RENDER_GIT_COMMIT (7 chars); null en hosts que no lo inyectan
  "src": "99be42076190",     // sha256 de src/*.js + package.json, calculado al arrancar
  "started_at": "2026-08-20T21:52:41.012Z", "uptime_s": 312 }
```

`src` es la señal fiable (no depende del host y normaliza CRLF, porque el checkout de Windows los
tiene y el de Linux no). Desde la raíz del repo:

```bash
python scripts/verificar_deploy_render.py                 # ¿corre el código del repo? -> AL DÍA / DESFASADO
python scripts/verificar_deploy_render.py --esperar 900    # espera al despliegue tras hacer push
```

Si sale `DESFASADO` pasados ~10 min del push, el auto-deploy se apagó: Render → el servicio →
**Settings → Build & Deploy → Auto-Deploy = On** (rama `main`), o dispara el **Deploy Hook** de esa
misma pantalla (`curl -X POST "<deploy-hook-url>"`). El panel también muestra el sello: Ajustes →
🔌 Conexiones → estado de WhatsApp (`· build <sha>`).

> Cada despliegue **reinicia el socket** (contenedor nuevo): la sesión se retoma desde DynamoDB, pero
> un envío en vuelo puede cortarse. Evita desplegar en medio de una difusión grande.

## Vinculación MANUAL (recomendado: desde tu IP, no la de Render)

WhatsApp suele rechazar el linking ("inténtalo más tarde") cuando el socket sale de una **IP de
datacenter** (Render/AWS). La forma fiable es vincular **localmente** una sola vez: como la sesión
queda en DynamoDB, **Render la reutiliza** y no necesita escanear (reconectar una sesión existente
no tiene ese bloqueo).

Un comando (desde la raíz del repo, con Docker Desktop abierto):

```powershell
# QR en vivo (se abre en el navegador, se auto-renueva)
./scripts/vincular-whatsapp-local.ps1

# o por código de 8 dígitos (más fiable si el QR falla)
./scripts/vincular-whatsapp-local.ps1 -Pair 573001234567

# si quedó en mal estado, empieza limpio
./scripts/vincular-whatsapp-local.ps1 -Reset
```

Luego en el teléfono: **WhatsApp → Dispositivos vinculados → Vincular un dispositivo** (o *Vincular
con número de teléfono* para el código). Cuando diga "CONECTADO", listo: Render ya puede reenviar.

## Limitaciones / cosas a saber
- **Máximo 4 dispositivos vinculados** por cuenta de WhatsApp. Si ya tienes 4, quita uno antes.
- **Rate-limit / "inténtalo más tarde":** reintentar muchas veces seguidas hace que WhatsApp bloquee
  temporalmente (30–60 min). Espera y reintenta **una** vez; el servicio ya evita la "tormenta" de
  intentos (un solo socket a la vez).
- **App del teléfono actualizada:** una versión vieja de WhatsApp no completa el protocolo nuevo.
- **loggedOut auto-recupera:** si WhatsApp invalida la sesión, el servicio **borra solos** los datos
  en DynamoDB y vuelve a mostrar QR (no hay que limpiar nada a mano). Requiere permiso `dynamodb:Scan`
  + `DeleteItem` sobre la tabla de auth en las credenciales del servicio.
- **Riesgo de baneo:** automatizar WhatsApp personal puede banear el número; usa `SEND_DELAY_MS` alto
  y listas pequeñas (modo *Solo listas activas*) al principio.
