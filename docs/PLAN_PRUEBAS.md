# Plan de pruebas — Replica (Telegram/WhatsApp broadcasting)

`Actualizado: 2026-07-19 · Suite: 286 tests · Estado del stack: telegram-sync-dev (us-east-1), CI auto-deploy activo`

Este plan cubre **qué probar, cómo y con qué criterio de aceptación** en los tres niveles que tiene el
proyecto: (1) suite automatizada, (2) smoke post-deploy y (3) pruebas E2E manuales por flujo. Incluye
la matriz de regresión de los bugs corregidos en la revisión de backend (Batches 4–11) para que un
cambio futuro no los reintroduzca sin que nadie lo note.

---

## 1. Niveles de prueba

| Nivel | Qué valida | Cuándo corre | Herramienta |
|-------|------------|--------------|-------------|
| **Unitaria** (286 tests) | Dominio, casos de uso, adapters (mockeados), entrypoints | En cada push (CI) y localmente | `python -m pytest tests/ -q` |
| **Sintaxis del servicio Node** | `index.js` y `dynamoAuth.js` parsean | Local, antes de commit | `node --check whatsapp-service/src/*.js` |
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
| Entrypoints | `test_entrypoint_*` (receiver, worker, admin, dispatcher, poller) | Auth webhook fail-closed, dedup del receiver (A8), pausa/manual del worker (A9/B7), guardia de config (A12), lockout del panel (M26) |

**Regla de trabajo:** todo bug corregido lleva test de regresión con el ID del hallazgo en el nombre
(`test_a12_…`, `test_m18_…`). Si un test con ID falla, se está reintroduciendo un bug conocido: no
ajustar el test sin leer el hallazgo en el HANDOFF.

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
6. **Servicio WhatsApp:** `curl https://telegram-sender-dm43.onrender.com/health` → `{ok:true}`;
   `/status` (con token) reporta `connected` y nº de contactos.
7. **`/count` desplegado (M16):** `POST /count` (con token) devuelve `{count, mode}` — si da 404, el
   servicio Node de Render está desactualizado (el adapter caerá al fallback `count_only`).
8. **Alarmas:** `aws cloudwatch describe-alarms --alarm-name-prefix telegram-sync-dev` → incluye
   `…-dedup-infra` (nueva), `…-dlq-no-vacia`, `…-worker-errores`; todas `OK` o `INSUFFICIENT_DATA`.
9. **Reglas activas:** EventBridge (dispatcher/poller) `ENABLED`; ESM del worker `Enabled`, BatchSize **1** (invariante B8).
10. **DLQ vacía:** `ApproximateNumberOfMessagesVisible = 0` en la DLQ.

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
1. Crear un **recurrente** (daily/weekly) desde **✍️ Enviar → 🔁 Recurrente** → ✅ dispara a su hora,
   respeta ventana y avanza `next_run` aunque el envío falle (A2 — no re-dispara en bucle). El envío
   único NO se crea como horario desde el panel: es el modo **📅 Una vez el…** de ✍️ Enviar (aparece en
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
1. `/pair` con número: ✅ código de 8 dígitos en <20 s, o **504 con detalle** y vuelta limpia a modo
   QR (B12 — sin códigos fantasma en logs).
2. `/reset`: ✅ borra la sesión completa (B11 — si el borrado falla, se reintenta al próximo arranque,
   no re-vincula sobre sesión a medias). Render retoma la sesión desde DynamoDB tras vincular local.
3. `/blocked/clear` + `/reconnect` inmediato: ✅ los opt-outs limpiados NO reviven (M19).

### F8 — Gestión de datos del panel
1. Borrado masivo/selectivo en **📡 Actividad → Historial / Programados** y **👥 Contactos → listas**: ✅ borra lo seleccionado.
2. ✅ "Borrar terminados" NO borra un fraccionado largo aún en vuelo (M8) ni las listas capturadas (M27).

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
| «Una vez» diferido | Un 📅 «Una vez el…» sale YA (no se difiere) con el fraccionado apagado o sin store de planes | `test_diferido_crea_plan_aunque_el_fraccionado_este_apagado`, `test_diferido_sin_store_de_planes_se_rechaza` (broadcasting) | F4.1 |
| tz UTC=0 | Offset horario 0 (UTC) tratado como ausente → ventana y `next_run` mal calculados | `test_dentro_y_fuera_utc` (scheduling), `test_zona_horaria_utc_menos_5` (schedules) | F4.1 / F2.2 |
| Init pestaña Enviar | La pestaña ✍️ Enviar carga en blanco (sin compositor ni listas) al entrar directo | — (panel/SPA, sin suite) | F2.1 |

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
