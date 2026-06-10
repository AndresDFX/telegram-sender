# whatsapp-service (Opción A · Baileys)

Servicio portable que **reenvía por WhatsApp** las mismas listas que el sistema difunde por
Telegram. Mantiene la conexión de WhatsApp Web (Baileys), expone el **QR** para vincular tu
WhatsApp desde el panel, lista tus contactos y envía. La **sesión se persiste en DynamoDB**, así
que sobrevive a reinicios/spin-down **sin re-escanear** el QR (por eso funciona en hosts de disco
efímero como Render).

> ⚠️ Automatizar WhatsApp personal va contra sus términos y puede **banear tu número**. Usa cadencia
> alta entre envíos (`SEND_DELAY_MS`) y empieza excluyendo casi todos los contactos para probar.

## Endpoints (todos con `Authorization: Bearer <WHATSAPP_TOKEN>`, salvo /health)
- `GET /health` — para el healthcheck del host.
- `GET /status` — `{connected, me, qr (dataURL si hay que vincular), contacts}`.
- `GET /contacts` — `[{id, name}]`.
- `POST /send` — `{text, image_url?, exclude?:[ids]}` → envía a tus contactos no excluidos.

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
