# Plan de pruebas — Replica (Telegram/WhatsApp broadcasting)

`Actualizado: 2026-08-20 · Suite: 360 tests · Estado del stack: telegram-sync-dev (us-east-1), CI auto-deploy activo`

Este plan cubre **qué probar, cómo y con qué criterio de aceptación** en los tres niveles que tiene el
proyecto: (1) suite automatizada, (2) smoke post-deploy y (3) pruebas E2E manuales por flujo. Incluye
la matriz de regresión de los bugs corregidos en la revisión de backend (Batches 4–11) para que un
cambio futuro no los reintroduzca sin que nadie lo note.

---

## 1. Niveles de prueba

| Nivel | Qué valida | Cuándo corre | Herramienta |
|-------|------------|--------------|-------------|
| **Unitaria** (360 tests) | Dominio, casos de uso, adapters (mockeados), entrypoints, PWA, CRUD y nomenclatura | En cada push (CI) y localmente | `python -m pytest tests/ -q` |
| **Sintaxis del servicio Node** | `index.js` y `dynamoAuth.js` parsean | Local, antes de commit | `node --check whatsapp-service/src/*.js` |
| **Sintaxis del JS del panel** | El JS embebido en `_PAGE` y el `sw.js` parsean (un paréntesis suelto = panel en blanco) | Local, antes de commit | `python scripts/revisar_js_panel.py` |
| **Smoke visual de la UI** | El panel pinta en claro/oscuro y escritorio/móvil sin errores JS | Tras tocar el panel | `python scripts/capturas_ui.py` → `.build/ui/` |
| **PWA desplegada** | Service worker activo, precaché, apertura sin red, aviso de sin conexión | Tras cada deploy del panel | `PANEL_URL=<AdminUrl> python scripts/verificar_pwa_desplegada.py` |
| **Deploy del servicio WhatsApp** | Render corre el mismo código que el repo (valida que el auto-deploy sigue vivo) | Tras cada push a `main` (Render redespliega siempre, ver §3.6) | `python scripts/verificar_deploy_render.py [--esperar 900]` |
| **Smoke post-deploy** | El stack desplegado responde y está cableado | Tras cada deploy relevante | Checklist §3 (manual/CLI) |
| **E2E manual** | Flujos completos de negocio con cuentas de prueba | Antes de activar envío real / tras cambios grandes | Checklist §4 (panel + Telegram/WhatsApp reales) |
| **Integración local** (futuro) | DynamoDB/SQS reales en Docker | Propuesto — ver [specs/20-tests-integracion.md](../specs/20-tests-integracion.md) | dynamodb-local + ElasticMQ |

### Cómo correr la suite localmente

```bash
python -m pytest tests/ -q          # toda la suite (~3-20 s)
python -m pytest tests/test_application_dispatch.py -q   # un módulo
node --check whatsapp-service/src/index.js && node --check whatsapp-service/src/dynamoAuth.js
```

CI (`.github/workflows/deploy.yml`): en cada push a `main` corre los tests y, si tocan
`src/lambda/**`, `infra/cloudformation/**` o `scripts/**`, despliega a CloudFormation
(`DEPLOY_ENABLED=true`). **Criterio: el job `test-and-deploy` debe quedar `success`.**

---

## 2. Cobertura de la suite automatizada (por capa)

| Capa | Módulos de test | Qué cubre |
|------|-----------------|-----------|
| Dominio | `test_domain_*` (message, markup, scheduling, schedules, recipients, auth, models) | Limpieza de texto/teléfonos (M3), markup y monedas (B3), ventanas horarias (M1), próximos runs (M2), exclusión por patrón (B2), códigos de reseteo (B1) |
| Casos de uso | `test_application_*` (broadcasting, dispatch, deliver_batch, materialize_schedules, poll_channel, onboarding) | Captura vs envío (A12/M25), plan fraccionado, claim de lotes (A4/A13/M5/B17/M29), idempotencia por destinatario (A3), avance de horarios (A2), preview (B15/B16), id determinista (M18) |
| Adapters | `test_adapters_*` (dynamodb, whatsapp, broadcast_store, telegram…) | Dedup fail-open/estricto + métrica EMF (A8/M10/M30), `/count` dedicado (M16), estados de broadcast (M8), `registrar_error` ordenado (B18), TTL/lotes (M7/M9) |
| Entrypoints | `test_entrypoint_*` (receiver, worker, admin, dispatcher, poller) | Auth webhook fail-closed, dedup del receiver (A8), pausa/manual del worker (A9/B7), guardia de config (A12), lockout del panel (M26), vinculación WhatsApp por código (`WhatsappPairTests`: número normalizado, auditoría **enmascarada**, códigos de error propagados, y el **sello de build** del servicio Node llegando al panel) |
| PWA | `test_entrypoint_admin.PwaTests` | Manifest/`sw.js`/iconos **públicos** (sin auth) y con las rutas del stage resueltas, ámbito ampliado (`Service-Worker-Allowed`), el SW **nunca** cachea `/api/`, versión del shell que cambia con el HTML |
| CRUD | `test_entrypoint_admin.SchedulesCrudTests`, `UsersUpdateTests`, `PanelCrudTests` | Editar programados (hereda lo no enviado, valida igual que crear, no pisa el historial de ejecuciones), editar usuario/restablecer contraseña (solo admin, sin filtrar la clave a la auditoría), y que **cada operación tenga su botón en el panel** (un endpoint sin botón no existe para el usuario) |
| Nomenclatura | `test_entrypoint_admin.NomenclaturaTests` | Recorre el AST buscando las llamadas a `_audit` y exige `entidad:accion` con la entidad **igual al segmento de la ruta HTTP**, que la entidad exista como ruta y que el panel tenga etiqueta para cada entidad y verbo (`ACC_ENT`/`ACC_VRB`) |

**Regla de trabajo:** todo bug corregido lleva test de regresión con el ID del hallazgo en el nombre
(`test_a12_…`, `test_m18_…`). Si un test con ID falla, se está reintroduciendo un bug conocido: no
ajustar el test sin leer el hallazgo en el HANDOFF.

### 2.1 Matriz CRUD por entidad (qué operaciones existen y dónde se prueban)

Leyenda: **C** crear · **R** leer/listar · **U** actualizar · **D** borrar. `—` = no aplica por diseño
(con el motivo). Todas las rutas cuelgan de `/admin/api/…` y auditan con la clave `entidad:accion`.

| Entidad | Dónde vive | C | R | U | D | Rutas de escritura | Prueba |
|---|---|:-:|:-:|:-:|:-:|---|---|
| **Usuarios del panel** | `Config.users` | ✅ | ✅ | ✅ | ✅ | `users` · `users/update` (correo + restablecer clave) · `users/role` · `users/delete` | `UsersUpdateTests`, `RoleTests` · F6, F8.3, **F12.1** |
| **Configuración** | `Config` (documento único) | — *(la crea el stack)* | ✅ | ✅ | — *(no se borra: se edita)* | `config` | `AdminTests` · F3.2 |
| **Reglas de exclusión por nombre** | `Config.users[u].patterns` | ✅ | ✅ | ✅ | ✅ | `patterns` (el conjunto se reescribe completo: borrar = quitar la línea) | `test_domain_recipients`, `AdminTests` · F8 |
| **Contactos / audiencia** | tabla `Subscribers` + agenda de WhatsApp | — *(los trae la fuente: `telethon/refresh-contacts`, `whatsapp/sync`)* | ✅ | ✅ *(incluir/excluir)* | **— deliberado** | `subscribers` | `AdminTests` · F8.3 |
| **Listas de contactos** | `Config.lists` (TG y WA) | ✅ | ✅ | ✅ *(**renombrar** + miembros)* | ✅ | `config` (viven dentro de la config) | `PanelCrudTests` · **F12.3** |
| **Difusiones** | tabla `Broadcasts` | ✅ *(al enviar)* | ✅ | — *(historial inmutable: lo escribe el worker)* | ✅ *(= desencolar)* | `broadcast` · `broadcasts/delete` | `AdminTests` · F2, F9.1 |
| **Envíos por partes** | tabla `Plans` | — *(los crea el fraccionado)* | ✅ | ✅ *(solo `cancel`)* | ✅ | `plans/cancel` · `plans/delete` | `AdminTests` · F4.2, F9.1 |
| **Programados** | tabla `Schedules` | ✅ | ✅ | ✅ *(**editar** + pausar/activar)* | ✅ | `schedules` · `schedules/update` · `schedules/toggle` · `schedules/delete` | `SchedulesCrudTests` · **F12.2** |
| **Cola de envío** | SQS principal | — *(encola el dispatcher)* | ✅ | — *(un mensaje en cola no se edita)* | ✅ *(vaciar)* | `queue/purge` | `AdminTests` · F9.2/F9.3 |
| **Envíos atascados** | SQS DLQ | — | ✅ | ✅ *(reintentar = mover a la principal)* | ✅ *(descartar)* | `dlq/redrive` · `dlq/purge` | `AdminTests` · F9.3 |
| **Auditoría** | tabla `Audit` (TTL 90 d) | ✅ *(implícita en cada acción)* | ✅ | — *(un registro editable no auditaría nada)* | ✅ *(deja registrado el propio borrado)* | `audit/delete` | `AdminTests`, `NomenclaturaTests` · F8.3 |
| **Imágenes** | bucket S3 | ✅ | — | — | — *(ciclo de vida del bucket)* | `image` | `AdminTests` · F2.4 |
| **Auto-excluidos de WhatsApp** | contador de fallos del servicio Node | — *(lo escribe el servicio al acumular fallos)* | ✅ | — | ✅ *(**uno solo** o todos)* | `whatsapp/blocked/clear` | `PanelCrudTests` · **F12.4** |
| **Conexión de Telegram** | `Config` + sesión Telethon | ✅ | ✅ | ✅ | ✅ *(cerrar sesión)* | `telethon/{send-code,sign-in,refresh-contacts,logout}` · `telegram/webhook` | `TelegramAccountTests` · F7 |
| **Conexión de WhatsApp** | sesión Baileys en DynamoDB | ✅ *(vincular)* | ✅ | ✅ *(sync)* | ✅ *(reset)* | `whatsapp/{pair,sync,reset}` | `WhatsappPairTests` · F7 |

Las tres ausencias son decisiones, no huecos — si alguien las «arregla», rompe una garantía:

- **Contactos sin D:** dar de baja a alguien se expresa como **exclusión reversible** (por número o por
  regla de nombre). En modo bot un borrado sería irreversible (la agenda no se puede reconstruir), y por
  eso las tablas de contactos tampoco tienen «eliminar todos». Ver HANDOFF.
- **Difusiones y envíos por partes sin U:** son el registro de lo que pasó. Editarlos falsearía el
  historial y el comparador de precios; para cambiar algo se crea un envío nuevo.
- **Imágenes solo con C:** es media de solo escritura, referenciada **por URL** desde el compositor y los
  programados. Borrar una imagen usada por un repetido lo dejaría enviando un enlace roto: se limpia por
  ciclo de vida del bucket, no a mano.

### 2.2 Nomenclatura: un nombre significa lo mismo en todas partes

Regla verificada por `NomenclaturaTests` (falla el CI si se rompe):

| Eje | Convención | Ejemplo |
|---|---|---|
| Clave de auditoría | `entidad:accion`, verbo en infinitivo | `schedules:actualizar` |
| Entidad | **exactamente** el segmento de la ruta HTTP | `schedules` ↔ `/api/schedules` |
| Ruta de escritura | `POST /api/<entidad>/update` y `/delete` como formas canónicas; los verbos propios se mantienen cuando la operación no es un update genérico | `/api/users/update`, `/api/schedules/toggle` |
| Etiqueta del panel | cada entidad y cada verbo tienen su traducción al español en `ACC_ENT`/`ACC_VRB` | `schedules:actualizar` → «Programados · editar» (la clave cruda queda en el `title`) |

Al añadir una acción nueva hay que tocar **los tres sitios** (ruta, `_audit`, etiqueta del panel); si
falta alguno, `NomenclaturaTests` lo dice con el nombre de la acción que falla.

---

## 3. Smoke test post-deploy (10 min)

Correr tras cada deploy que toque Lambdas o template. Credenciales: `.env.aws` (gitignored).

1. **Stack sano:**
   `aws cloudformation describe-stacks --stack-name telegram-sync-dev --query "Stacks[0].StackStatus"`
   → `UPDATE_COMPLETE`.
2. **CI verde:** `gh run list --limit 1` → `test-and-deploy … success`.
3. **Webhook fail-closed:** `curl -s -o /dev/null -w "%{http_code}" <ReceiverUrl>` sin secret token → **403**.
4. **Panel arriba:** abrir `AdminUrl` → carga la SPA; login con credenciales válidas → 200; con clave
   mala 5 veces → **429** (lockout, no 401).
5. **Guardia A12 (backend):** `POST /api/config {"sending_enabled":true}` sin `auto_telegram_list`
   → **400** con mensaje de lista.
6. **Servicio WhatsApp:** `curl https://telegram-sender-dm43.onrender.com/health` → `{ok:true, commit, src, …}`;
   `/status` (con token) reporta `connected` y nº de contactos. `python scripts/verificar_deploy_render.py`
   → **`AL DÍA`** (Render redespliega en **cada** push a `main`: ~5 min si el push tocó `whatsapp-service/`,
   ~30 s si no; si sale `DESFASADO` pasados ~10 min, el auto-deploy está apagado — ver HANDOFF §Despliegue
   del servicio WhatsApp). El `commit` de `/health` debe coincidir con el `HEAD` de `main`; y como el
   contenedor es nuevo, **`connected` puede tardar unos segundos** en volver a `true` (retoma la sesión
   desde DynamoDB).
7. **`/count` desplegado (M16):** `POST /count` (con token) devuelve `{count, mode}` — si da 404, el
   servicio Node de Render está desactualizado (el adapter caerá al fallback `count_only`).
8. **Alarmas:** `aws cloudwatch describe-alarms --alarm-name-prefix telegram-sync-dev` → incluye
   `…-dedup-infra` (nueva), `…-dlq-no-vacia`, `…-worker-errores`; todas `OK` o `INSUFFICIENT_DATA`.
9. **Reglas activas:** EventBridge (dispatcher/poller) `ENABLED`; ESM del worker `Enabled`, BatchSize **1** (invariante B8).
10. **DLQ vacía:** `ApproximateNumberOfMessagesVisible = 0` en la DLQ.
11. **PWA sana** (si el deploy tocó el panel): `PANEL_URL=<AdminUrl> python scripts/verificar_pwa_desplegada.py`
    → **`PWA OK`** (service worker activo con ámbito por encima de `/admin`, shell+iconos en caché, la app
    abre sin red, `/api/` nunca cacheado, sin errores JS). Detalle manual en §4 F11.

---

## 4. Pruebas E2E manuales por flujo

**Datos de prueba** (HANDOFF §checklist): Telegram chat_id `6053071541`, WhatsApp
`573188468892@s.whatsapp.net`, canal fuente `@iproparts`. **Precondición de seguridad:** listas de
prueba con SOLO esas cuentas; `sending_enabled=False` salvo en el caso que lo pruebe explícitamente.

### F1 — Captura (recopilación sin envío)
1. Con envío automático EN PAUSA, publicar un post con precio en el canal fuente.
2. ✅ Aparece en **📡 Actividad → Historial (filtro 📥 Capturadas)** como **capturado** (no "enviado"), con `full_text` visible en el modal.
3. ✅ Llega el preview a Mensajes Guardados del userbot; si NO llega, el job muestra el error de
   preview (B16). Ningún contacto recibe nada.
4. ✅ Posts sin contenido tras limpieza (solo ubicación/branding) NO se capturan (M4).

### F2 — Envío manual (✍️ Enviar)
1. ✍️ Enviar → modo **⚡ Ahora** → texto con precio → elegir lista de prueba TG → **enviar**.
2. ✅ Llega al chat de prueba aunque la ventana horaria esté cerrada y el sistema en pausa (manual salta ambos).
3. ✅ El markup NO se aplica (manual va crudo); el progreso llega a 100% en **📡 Actividad → Historial**.
4. Repetir con imagen subida: ✅ imagen+texto llegan (caption si cabe, dos mensajes si >1024).
5. Manual WhatsApp a la lista de prueba: ✅ llega al número de prueba; requiere servicio configurado.
6. ✅ Enviar a lista vacía / todos excluidos → error visible con causa, no silencio.

### F3 — Envío automático por lista (A12/M25)
1. En **🏠 Inicio**, elegir la lista por canal en «Lista del envío automático» y activar «Envíos automáticos activos».
2. ✅ Intentar activar SIN lista (vía panel y vía API directa) → bloqueado (front) y **400** (backend).
3. Publicar en el canal: ✅ difunde SOLO a la lista elegida, con markup y footer aplicados.
4. Borrar/renombrar la lista elegida y publicar: ✅ el job registra el error de auto-lista (M25), no
   difunde a todos ni cierra como "enviado" vacío silencioso.

### F4 — Programación y fraccionado
1. Crear un **repetido** (daily/weekly) desde **✍️ Enviar → 🔁 Se repite** → ✅ dispara a su hora,
   respeta ventana y avanza `next_run` aunque el envío falle (A2 — no re-dispara en bucle). El envío
   único NO se crea como horario desde el panel: es el modo **📅 Una vez** de ✍️ Enviar (aparece en
   **📡 Actividad → Historial**, no en Programados). Un schedule `type=once` puro solo se crea por API.
2. Envío fraccionado (lista > batch_size): ✅ el dispatcher libera UN lote por tick; el progreso
   avanza por lotes; cancelar pendientes detiene los lotes en vuelo (worker los descarta).
3. ✅ Con el servicio WhatsApp caído (suspender Render), Telegram sigue saliendo (independencia) y el
   canal WA queda marcado fallido con causa, sin colgar el job (A13/M15).

### F5 — Pausa / anti-baneo
1. Pausar envíos: ✅ lo automático se descarta sin enviar; lo manual sigue saliendo.
2. ✅ Dos lotes automáticos totalmente fallidos seguidos → auto-pausa (`sending_enabled=False`);
   fallos de envíos MANUALES no pausan (A9).

### F6 — Autenticación y recuperación
1. Login/logout, cambio de contraseña, creación de usuario secundario.
2. ✅ 5 fallos → lockout 429 5 min; al expirar, UN fallo no re-bloquea (M26).
3. Reset de contraseña: ✅ llega código por Resend (o SNS si está confirmado); el código es de
   dígitos uniformes (B1) y expira/limita intentos.

### F7 — Vinculación WhatsApp (solo si hay que re-vincular)
> ⚠️ Hacer desde IP residencial; ver HANDOFF. Riesgo de invalidar la sesión activa.

**F7.a — Desde el panel, en el mismo teléfono (flujo guiado, sin scripts).** Abrir el panel en el
teléfono donde está WhatsApp: **Ajustes → 🔌 Conexiones → Vincular WhatsApp**.
1. Con la sesión **ya conectada**, pulsar «Pedir código»: ✅ sale el modal de confirmación avisando que
   se cierra la sesión actual; al **cancelar** no pasa nada (sigue conectado, sin código).
2. Sin sesión (o tras confirmar): número → «Pedir código de 8 dígitos» → ✅ código grande en formato
   `XXXX XXXX` (<20 s) con botón **Copiar** que funciona, y los 4 pasos visibles.
3. Escribir el código en WhatsApp (⋮ → Dispositivos vinculados → **Vincular con número de teléfono**) y
   volver al panel: ✅ **se confirma solo** (sin recargar ni pedir credenciales otra vez) con
   «WhatsApp vinculado · N contactos» y las pastillas del header en verde.
4. Fallos honestos: sin URL/token → ✅ dice que faltan (no «error desconocido»); desde Render (IP de
   datacenter) → ✅ explica el bloqueo y ofrece reintentar/vincular local; pasados 5 min sin escribir el
   código → ✅ avisa que caducó y ofrece **Pedir otro código**. Salir de 🔌 Conexiones ✅ detiene el sondeo.
5. **🖥 Con QR (otro aparato)**: el segundo botón del segmentado ✅ muestra el QR y oculta el flujo por código.
6. ✅ En **Ajustes → 🛠️ Auditoría** queda `whatsapp:pair` con el número **enmascarado** (solo los últimos 4).

**F7.b — Servicio Node (CLI, cuando el panel no puede vincular).**
1. `/pair` con número: ✅ código de 8 dígitos en <20 s, o **504 con detalle** y vuelta limpia a modo
   QR (B12 — sin códigos fantasma en logs). Con sesión conectada devuelve **409 `ya_conectado`**.
2. `/reset`: ✅ borra la sesión completa (B11 — si el borrado falla, se reintenta al próximo arranque,
   no re-vincula sobre sesión a medias). Render retoma la sesión desde DynamoDB tras vincular local.
3. `/blocked/clear` + `/reconnect` inmediato: ✅ los opt-outs limpiados NO reviven (M19).

### F8 — Gestión de datos del panel (grids: buscador + paginación + eliminar todos)
1. Borrado masivo/selectivo en **📡 Actividad → Historial / Programados** y **👥 Contactos → listas**: ✅ borra lo seleccionado y **las filas desaparecen del grid al instante** (no reaparecen: `_scan_todo` usa `ConsistentRead` + quitado optimista del DOM).
2. ✅ "Limpiar terminados" NO borra un envío por partes largo aún en vuelo (M8) ni las listas capturadas (M27).
3. **Design system de grids** (difusiones, envíos por partes, programados, usuarios, auditoría):
   - ✅ El **🔎 buscador** filtra por texto (mensaje/origen/estado/usuario según el grid) y el **paginador ‹ ›** recorre páginas (25/pág).
   - ✅ **Eliminar todos** borra todo el grid con confirmación. En **usuarios** conserva al admin principal y al usuario actual (sin lockout); **auditoría** deja registrado el propio borrado; **contactos NO** tiene eliminar-todos (audiencia por incluir/excluir).

### F9 — Borrar = desencolar · cola en vivo · detalle enriquecido
1. **Borrar detiene el envío**: con un envío por partes en curso, borrar su difusión en **Historial** (o «Eliminar todas») ✅ borra también el plan (pid == broadcast_id) → el dispatcher deja de despachar y el worker descarta los lotes ya en vuelo (no siguen llegando). No revierte lo ya entregado.
2. **Cola de envío en vivo** (**📡 Actividad → Problemas**): ✅ muestra *en cola / enviándose ahora / atascados* y se refresca solo. `profundidades()` incluye `en_vuelo` (NotVisible), no solo visibles.
3. **Vaciar cola** (principal): ✅ descarta lo encolado; un segundo intento en <60s devuelve "en progreso" sin reventar (`PurgeQueueInProgress` manejado). **Reintentar/Descartar** en «Envíos atascados» (DLQ) siguen operando.
4. **Detalle enriquecido** (tocar el mensaje en Historial): ✅ ventana ancha con **fechas** (recibido/1º/último envío), **📥 mensaje anterior (original)** y **📤 mensaje que se envía** lado a lado, y **💰 comparador de precios** (anterior→nuevo); en móvil se apilan. Difusiones de canal traen anterior+comparador; el manual va crudo (sin ellos).
5. **Marca acento-insensible**: ✅ el encabezado «IPRÓ PARTS» (con tilde) NO aparece en el «mensaje que se envía» (`test_quita_marca_ipro_parts`).

### F10 — Responsive (móvil)
1. Abrir el panel en un teléfono (o DevTools ~390px): ✅ las 5 pestañas se ven como **barra inferior fija** (icono + etiqueta, sin cortar «Ajustes»); sub-nav y filtros a ancho completo; botones/tap targets cómodos (≥44px).
2. ✅ Al enfocar un campo NO hace zoom automático (iOS: inputs a 16px); la caja de login no desborda; en el Historial la columna **Fechas** se oculta (su info sigue en el detalle).
3. ✅ Ritmo de espaciado: los botones de acción («Guardar…») no quedan pegados al campo de arriba.
4. ✅ El contenido no queda tapado por la barra inferior (hay `padding-bottom`) y los avisos (toast, sin conexión, versión nueva) salen **encima** de ella; en teléfonos con notch/barra de gestos nada se corta (`env(safe-area-inset-*)`).
5. ✅ Los KPIs de **🏠 Inicio** se ven en 2×2 (no uno por fila).

### F11 — App instalable (PWA), tema y modo sin conexión
> Automatizable en su mayor parte: `PANEL_URL=<AdminUrl> python scripts/verificar_pwa_desplegada.py`.
1. **Estáticos públicos sin auth** (los pide el navegador sin credenciales):
   `curl -s -o /dev/null -w "%{http_code} %{content_type}\n" <AdminUrl>/manifest.webmanifest` → **200
   `application/manifest+json`**; igual para `/sw.js` (**200 `text/javascript`**, con cabecera
   `Service-Worker-Allowed` y `cache-control: no-cache`) y para `/icon-192.png`, `/icon-512.png`,
   `/icon-maskable-512.png`, `/apple-touch-icon.png` (**200 `image/png`**).
   ✅ `<AdminUrl>/api/config` sin auth sigue dando **401** (la PWA no abrió ningún hueco).
2. **Instalar**: en Chrome/Edge (Android o escritorio), **ya dentro del panel** (el botón vive en el
   header; la pantalla de acceso no lo tiene), aparece el **📲** (rótulo «Instalar Replica como app») → instala y abre en
   ventana propia sin barra del navegador; ✅ una vez instalada el botón **desaparece**. En iOS/Safari el
   botón muestra las instrucciones de *Compartir → Añadir a pantalla de inicio* (iOS no expone el
   evento de instalación).
3. **Icono y arranque**: el icono en el escritorio/launcher es el isotipo de Replica (en Android,
   recortado en círculo sin comerse el glifo — icono *maskable*); al abrir, el color de la barra de
   estado coincide con el tema.
4. **Atajos** (mantener pulsado el icono en Android): ✍️ Enviar, 📡 Actividad, 👥 Contactos → abre el
   panel en esa pestaña (deep link `?tab=`).
5. **Sin conexión**: activar modo avión y abrir la app → ✅ carga (shell desde caché) y muestra
   **📴 Sin conexión**; cualquier acción que necesite red falla con mensaje en castellano («Sin
   conexión: revisa tu red…»), **no** «Failed to fetch». Al volver la red → toast «✓ Conexión
   restablecida» y el aviso se retira.
6. **Nunca cachea datos**: con red y sesión abierta, ✅ los datos que se ven son los del servidor (el SW
   no guarda `/api/`): un cambio hecho en otro dispositivo se ve al recargar la pestaña.
7. **Versión nueva**: tras desplegar un cambio del panel, con la app abierta aparece **✨ Versión nueva**;
   ✅ «Después» la deja para luego (no interrumpe un envío en curso) y «Actualizar» recarga y **vuelve a
   pedir credenciales** (la credencial vive solo en memoria, M17). ✅ Nunca recarga sin que el usuario lo pida.
8. **Tema**: el botón alterna **automático → claro → oscuro** y persiste tras recargar; ✅ al recargar en
   claro **no hay destello oscuro** (se aplica antes del primer pintado); en modo automático, cambiar el
   tema del sistema cambia el panel; ✅ en claro todo se lee (contraste AA) y el logotipo conserva el
   degradado (no sale como bloque naranja).
9. **Accesibilidad**: con Tab el primer foco es «Saltar al contenido»; ✅ la pestaña activa se anuncia
   (`aria-current`) y los avisos se leen por lector de pantalla (`aria-live`).

### F12 — CRUD de entidades (editar sin borrar y volver a crear)
> Cubre las operaciones que faltaban (§2.1). Precondición: `sending_enabled=False` y listas de prueba.

1. **Usuario: editar correo y restablecer contraseña.** Ajustes → 👤 Acceso → Usuarios del panel → **✏️**
   en una fila que no sea la tuya.
   - ✅ Cambiar solo el correo guarda y **no** invalida la contraseña (la persona sigue entrando con la suya).
   - ✅ Escribir una contraseña nueva la restablece **sin pedir la actual** (es el caso "se le olvidó");
     la persona entra con la nueva y no con la vieja.
   - ✅ Dejar los dos campos vacíos → «Sin cambios», no un guardado fantasma. Contraseña de <8 → error.
   - ✅ Con un usuario de rol **Usuario**: el botón ✏️ no está y la llamada directa a la API da **403**.
   - ✅ En 🛠️ Auditoría queda «Usuarios · editar» con *qué* se cambió (correo/contraseña) y **nunca la clave**.
   - ✅ El admin principal se identifica solo (lo dice el backend): el panel no asume que se llama `admin`.
2. **Programado: editar en vez de borrar y rehacer.** 📡 Actividad → Programados → **✏️ Editar**.
   - ✅ El formulario abre **con todo lo que ya tenía** (texto, canales, listas, frecuencia, hora, días) y
     se ve completo en móvil (una columna) y en escritorio (dos).
   - ✅ Cambiar solo el texto conserva canales/listas/horario y **recalcula el próximo envío**.
   - ✅ Cambiar de *diario* a *semanal* exige días; quitar la lista de WhatsApp se rechaza; una imagen que
     no empiece por `https://` se rechaza — **las mismas reglas que al crear**.
   - ✅ Un error no pierde lo escrito: el formulario vuelve a abrirse con el borrador y el aviso.
   - ✅ El contador de ejecuciones y la última ejecución **no se reinician** al editar.
   - ✅ Pausar/reanudar sigue funcionando y la auditoría distingue «activar» de «pausar».
3. **Lista de contactos: renombrar arrastrando las referencias.** 👥 Contactos → **✏️** en una lista.
   - ✅ Renombrar avisa **cuántos programados** apuntan a esa lista y los actualiza al confirmar.
   - ✅ Si la lista era la del **envío automático**, el nuevo nombre queda elegido ahí (no se queda apuntando
     al nombre viejo → si no, F3.4: deja de difundir). Se guarda en una sola operación.
   - ✅ Nombre duplicado → error antes de tocar nada. Si el guardado falla, el nombre viejo se restaura.
4. **Auto-excluido de WhatsApp: reincluir a UNO.** Ajustes → 🔌 Conexiones (WhatsApp) → «Auto-excluidos
   por fallos».
   - ✅ Cada contacto trae su botón **Reincluir**: vuelve a recibir envíos y los demás siguen excluidos.
   - ✅ «Reincluir a todos» sigue disponible. El servicio Node ya está desplegado (Render auto-deploy);
     confirma con `python scripts/verificar_deploy_render.py` → `AL DÍA` antes de dar por bueno el fallo,
     porque con una versión anterior el botón por contacto limpiaría **todos** los contadores.
   - ✅ El estado de WhatsApp en 🔌 Conexiones muestra `· build <sha>`: así se ve si el servicio quedó
     desfasado sin salir del panel.
5. **Auditoría legible.** Ajustes → 🛠️ Auditoría: ✅ las acciones se leen en español («Programados · editar»)
   y el buscador encuentra tanto por la etiqueta como por la clave cruda; los registros antiguos (anteriores
   a normalizar los nombres) siguen mostrándose tal cual, sin huecos.

---

## 5. Matriz de regresión (bugs corregidos → prueba que lo protege)

| ID | Riesgo si regresa | Test automatizado | E2E |
|----|-------------------|-------------------|-----|
| A4/A13 | Lote reclamado y perdido en silencio; cursor colgado 900 s | `test_a4_…`, `test_a13_…` (dispatch) | F4.3 |
| A8 | Webhook descarta posts legítimos como "duplicate" | `test_a8_…` (receiver) | F1.2 |
| A10 | Reset a mitad de lote WA rompe envío y auto-excluye contactos | — (Node, sin suite) | F4.3 + logs |
| A12/M25 | Difusión a TODA la agenda sin lista elegida | `test_a12_…` (admin+broadcasting), `test_m25_…` | F3.2/F3.4 |
| M18 | Retry del webhook duplica la difusión del mismo post | `test_m18_…` (broadcasting) | F1 (retry) |
| A3/M5/B17/M29 | Duplicados o abandono de lotes en reentregas/stale | `test_a3_…`, `test_b17_…`, `test_m29_…` | F4.2 |
| M16/M20/M21 | Un conteo dispara envío real; slices NaN silenciosos | `test_contar_usa_endpoint_count…` | §3.7 |
| M3/B3 | Limpieza borra producto+precio; markup corrompe decimales | `test_m3_…`, `test_b3_…` | F1.2 (texto real) |
| M6/B7 | Job colgado 'enviando'; foto perdida entregada como éxito | `test_m6_…`, `test_b7_…` x2 | F2.4 |
| M10/M30 | Doble-conteo invisible si el dedup cae por infra | `test_m10_…` (métrica EMF) + alarma `…-dedup-infra` | §3.8 |
| M26/B12 | Lockout roto; códigos de pairing fantasma | `test_lockout…` | F6.2 / F7.1 |
| Poller dedup_key | Reintento del poller (fallo de HWM/timeout) duplica la difusión del mismo post | `test_poller_pasa_dedup_key_determinista` (poll_channel) | F1 (retry) |
| «Una vez» diferido | Un 📅 «Una vez» sale YA (no se difiere) con el fraccionado apagado o sin store de planes | `test_diferido_crea_plan_aunque_el_fraccionado_este_apagado`, `test_diferido_sin_store_de_planes_se_rechaza` (broadcasting) | F4.1 |
| tz UTC=0 | Offset horario 0 (UTC) tratado como ausente → ventana y `next_run` mal calculados | `test_dentro_y_fuera_utc` (scheduling), `test_zona_horaria_utc_menos_5` (schedules) | F4.1 / F2.2 |
| Init pestaña Enviar | La pestaña ✍️ Enviar carga en blanco (sin compositor ni listas) al entrar directo | — (panel/SPA, sin suite) | F2.1 |
| Editar programado | Editar deja pasar lo que crear rechaza (WA sin lista → toda la agenda), pierde canales/horario no enviados o reinicia el historial de ejecuciones | `SchedulesCrudTests` (`…valida_igual_que_crear`, `…hereda_lo_que_no_se_manda`, `…no_pisa_el_historial…`) | F12.2 |
| Renombrar lista | Renombrar deja `auto_<canal>_list` y los programados apuntando al nombre viejo → el envío automático deja de difundir (variante de M25) | `PanelCrudTests.test_renombrar_una_lista` | F12.3 / F3.4 |
| Restablecer contraseña | Un usuario sin rol admin restablece la clave de otro; o la clave acaba escrita en la auditoría | `UsersUpdateTests` (`…un_usuario_normal_no_puede…`, `…no_registra_la_contrasena…`) | F12.1 |
| Nomenclatura | Una acción nueva se audita con un nombre distinto al de su ruta o sin etiqueta → la auditoría muestra jerga o queda ilegible | `NomenclaturaTests` (AST de `_audit` + `ACC_ENT`/`ACC_VRB`) | F12.5 |
| Sello de build | El panel deja de mostrar qué versión corre el servicio Node (o el proxy filtra `build`) → un Render desfasado parece «al día» y se depuran bugs ya arreglados | `WhatsappPairTests` (`…deja_pasar_el_sello_de_build…`, `…el_panel_muestra_el_build…`) + `scripts/verificar_deploy_render.py` | F12.4 |

---

## 6. Criterios de aceptación globales

- **Suite:** 100% verde local y en CI antes de cualquier merge a `main`.
- **Cero duplicados:** ninguna prueba E2E debe producir mensajes duplicados en las cuentas de prueba
  (es LA propiedad crítica del sistema; ante duda, tratar como bloqueante).
- **Cero difusión no intencional:** ninguna acción de prueba debe alcanzar contactos fuera de las
  listas de prueba.
- **Errores visibles:** todo fallo de envío debe dejar rastro consultable en el panel (job con
  `last_error`/razones), nunca solo en CloudWatch.

## 7. Riesgos residuales asumidos (no probar como bug)

- **Fail-open del dedup (M10/M30/B4):** ante fallo de infra de `ProcessedUpdates` puede haber
  doble-conteo; mitigado con métrica EMF + alarma `dedup-infra`. Diseño aceptado.
- **B8:** `paused`/strikes por-invoke — válido mientras el ESM tenga `BatchSize=1` (el worker
  advierte en logs si detecta más de 1 record).
- **A7/A14 (diferidos de producto):** posts solo-imagen se saltan con HWM avanzado; descartes
  transitorios por destinatario. Requieren decisión de producto antes de "arreglarse".
- Preview de conteo WhatsApp del panel es estimado (no aplica exclusiones del servicio).
