# Replica — Roadmap (qué falta para avanzar)

> _Actualizado 2026-06-11. Reemplaza la hoja de ruta original de 2026-06-07 (diseño webhook 1:1),
> hoy superada. El histórico queda en el historial de git._

**Estado actual:** la plataforma **funciona y está desplegada** (AWS + Render), hoy **pausada**
(`sending_enabled=False`). Núcleo sólido. Lo que sigue es lo que falta para **operar con confianza**
y para **venderlo como producto**.

Prioridad: **P0** = necesario para operar fiable ya · **P1** = importante para producto serio ·
**P2** = escala/SaaS/más adelante. Esfuerzo: **S** (horas) · **M** (1–3 días) · **L** (semana+).

---

## ✅ Ya implementado (esta etapa)
Mirror del canal (`poll t.me/s/iproparts`) · **markup robusto** (apóstrofo/COP/posición, ancla de
moneda) · **limpieza** (ubicación, marca IPRO PARTS, **teléfonos colombianos**) · listas de
distribución whitelist/blacklist (Telegram + WhatsApp) · envío manual con pickers de contactos y
búsqueda por nombre/número · **estados de envío** (tabla Broadcasts) · **envío FRACCIONADO y
secuencial** (dispatcher EventBridge, un lote a la vez) con **ventana horaria**, **delay aleatorio
(jitter)** y **tamaño de lote** configurables · **interruptor maestro** (activar/desactivar) ·
**cancelar** pendientes/por-envío · monitor de envíos fraccionados con el mensaje completo · gestión
de bot/cuenta (bot o userbot) · panel admin moderno (Basic Auth). 163 tests.

---

## 🚦 FASE 0 — Operar con confianza ✅ (completada salvo lo que depende de ti)

### Funcional
- ✅ **Preview del mensaje YA PROCESADO** — tarjeta "Probar procesamiento" (pegas el texto del canal
  y ves el resultado con markup/limpieza/footer). `POST /api/preview/process`.
- ✅ **Salud/estado de conexiones visible** — badges de Telegram (bot) y WhatsApp en el header, con
  auto-poll cada 60 s (verde/rojo).
- ✅ **Detección de baneo / auto-pausa** — tras 2 lotes totalmente fallidos seguidos el worker activa
  `sending_enabled=False` (se reinicia al primer envío exitoso).
- ✅ **Gestión de DLQ desde el panel** — ver muestra, reintentar (redrive) y descartar (purge).
- ✅ **Dedup/idempotencia** — `batch_id` por lote + el worker no reenvía un lote ya entregado.

### Fiabilidad / Ops
- ⚠️ **WhatsApp always-on** — *requiere acción tuya*: Render Free duerme a los 15 min. Mitigado con
  **keep-alive** (el dispatcher hace ping a `/health` cuando hay envíos activos). Para envíos largos
  sin cortes, sube a plan pago / Fly / Koyeb / Oracle.
- ✅ **Alarmas CloudWatch + SNS** — DLQ no vacía + errores de worker/dispatcher/poller. *Suscribe tu
  email al tópico `…-alerts` (o redeploy con `AlertEmail=tu@correo`).*
- ⚠️ **WorkerReservedConcurrency=1** — *requiere acción tuya*: pedir a AWS subir el límite de
  concurrencia de la cuenta (hoy 0 por el límite). El gate `in_flight` ya da secuencialidad.
- ✅ **PITR / backups de DynamoDB** — activado en todas las tablas.

### Seguridad
- ✅ **Rate-limit / lockout** del Basic Auth (en memoria) **+ throttling de API Gateway** (20 rps /
  40 burst, freno a fuerza bruta independiente del contenedor) **+ expiración de sesión** (8 h).
- 🔜 **Secretos a SSM/Secrets Manager** — *follow-up acotado* (los params ya son `NoEcho`; migrar a
  SecureString es un cambio de despliegue que conviene hacer con cuidado en una ventana aparte).
- 🔜 **Cambio de contraseña desde el panel** — *follow-up* (requiere almacenar hash + ruta de auth
  alterna; se difiere para no arriesgar el acceso en producción).

---

## 📈 FASE 1 — Producto serio (ola 1 ✅ desplegada)

### Funcional
- ✅ **Métricas de entrega** — `metricas(días)` agrega enviados/fallidos/tasa + serie diaria;
  `GET /api/metrics`. (Falta el desglose de "top fallos" por razón → ola 2.)
- ✅ **Programar envío a hora/fecha exacta** — `not_before` en el plan; el dispatcher difiere hasta
  esa hora; input datetime en el compositor.
- ✅ **Opt-out / bloqueados de WhatsApp** — el servicio cuenta fallos por jid y **auto-excluye** a los
  que fallan ≥ umbral (3); endpoints `/blocked` + `/blocked/clear`; tarjeta "Auto-excluidos por
  fallos" en la pestaña WhatsApp (ver / reincluir).
- 🔜 **[P2·L] Plantillas y personalización** (`Hola {nombre}`).

### UI / UX
- ✅ **Dashboard / Home (pestaña Inicio)** — estado, KPIs (30 d), mini-gráfico de actividad, último
  envío, accesos rápidos.
- ✅ **Aviso de sesión expirada + re-login** (8 h) — hecho en Fase 0.
- ✅ **Onboarding / wizard** — tarjeta "Primeros pasos" en Inicio (checklist: cuenta/bot → canal →
  listas → WhatsApp → activar). Pendiente (P2): accesibilidad, tema claro, import/export CSV.

### Seguridad / Ops
- ✅ **Audit log** — `DynamoDbAuditStore` (tabla `Audit`, TTL 90 d) registra acciones del panel
  (config, envíos, cancelaciones, DLQ); `GET /api/audit` + tarjeta "Auditoría" en Estado.
- ✅ **CI/CD** — `.github/workflows/deploy.yml` (tests siempre; deploy a CFN gated por la variable
  `DEPLOY_ENABLED`). *Acción tuya: añadir los secrets de AWS + `DEPLOY_ENABLED=true` para activar el
  auto-deploy.*
- 🔜 **[P1·M] HTTPS + dominio + WAF** (requiere registrar dominio) · **[P1·L] logs estructurados** ·
  staging/prod → ola 4.
- ✅ **Alarmas por email** — suscripción de `castano.julian@correounivalle.edu.co` al tópico SNS
  (pendiente: confirmar el correo de AWS).

---

## 🏢 FASE 2 — SaaS / escala (varios clientes)
- **[P0·L (para SaaS)] Multi-tenant:** aislar config/listas/datos por cliente + auth multi-tenant.
- **[P1] Planes/cuotas, billing (Stripe), super-admin, onboarding self-service, white-label.**
- **[P2] API pública, múltiples canales fuente por cliente, landing/marketing, docs, compliance, DR.**

---

## ✅ Quick wins para empezar (orden sugerido)
1. **Preview del mensaje procesado** (P0·S) — cierra lo de "ver cómo se envían".
2. **Badge de estado WhatsApp/Telegram** (P0·S/M).
3. **WhatsApp always-on** (P0·S) — desbloquea envíos masivos.
4. **Alarmas CloudWatch** (P0·M).
5. **Auto-pausa por baneo** (P0·M).
6. **Rate-limit + cambio de contraseña** (P0·M / P1·S).

## 🔍 A verificar (marcados por la auditoría, posible falsa alarma)
- Serialización poller↔dispatcher y chequeo de ventana bajo carga: el dispatcher YA valida
  `ventana_abierta` + gate `in_flight`; confirmar que no hay solapamiento de ticks.
- `SQS VisibilityTimeout` (120s) == `WorkerTimeoutSeconds` (120s): dejar un buffer (visibility >).

*(Auditoría completa: 77 gaps en 5 dimensiones —funcional/UI-UX/ops/seguridad/producto—; este roadmap
prioriza ~35, el resto queda como backlog de escala/producto.)*
