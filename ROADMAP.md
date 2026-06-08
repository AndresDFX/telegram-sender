# Hoja de ruta — Telegram → Telegram Sync

> **Objetivo del proyecto:** sincronización 1:1 orientada a eventos en tiempo real. Cuando el canal
> fuente publica una lista, AWS la intercepta vía webhook, aplica markup del 15% y la distribuye por
> mensaje directo a cada suscriptor — todo dentro de la capa gratuita y respetando los límites de la
> Bot API de Telegram (30 msg/seg).

_Estado a 2026-06-07. **Fases 0 y 1 completadas** — desacople SQS+DLQ, 429/5xx, caption/edited (Fase 1);
onboarding `/start`·`/stop`, `secret_token` fail-closed y dedup de `update_id` (Fase 0). 63 tests.
Ambas fases pasaron revisión adversarial multi-lente (y un re-review de los fixes) con sus hallazgos corregidos._

> 📋 Los **specs detallados** de cada ítem pendiente están en [`specs/`](specs/README.md) (uno por
> fase, con diseño, criterios de aceptación y plan de pruebas).

---

## 1. Estado actual

### Implementado y funcionando

| Componente | Estado | Notas |
|------------|--------|-------|
| `markup.py` | ✅ | Parseo robusto de precios (formato US y europeo), `Decimal`, markup configurable. |
| `telegram_client.py` | ✅ | `sendMessage`, 403 (bloqueado), **429 con `retry_after`** y backoff para 5xx. |
| `dynamodb_client.py` | ✅ | Query sobre GSI `StatusIndex` (no scan), paginado, `marcar_inactivo`, **`registrar_suscriptor`** (upsert), **dedup `update_id`** (PutItem condicional + TTL), imports boto3 perezosos. |
| `handler.py` (receptor) | ✅ | **secret_token fail-closed**, **parseo seguro** (400 ante body inválido), **dedup + compensación acotada**, **comandos `/start`·`/stop`**, `text`/`caption`/`edited`, encola en SQS y responde 200 rápido. |
| `broadcaster.py` | ✅ | Lógica de envío por lote compartida (worker + inline). |
| `worker.py` | ✅ | Consume SQS, respuesta parcial de lotes (`batchItemFailures`). |
| `sqs_client.py` | ✅ | Encolado de lotes con reintentos; `PartialEnqueueError` para idempotencia ante fallo parcial. |
| `template.yaml` (CloudFormation) | ✅ | DynamoDB + GSI, **tabla de dedup con TTL**, SQS + DLQ, Lambda receptor y worker (concurrencia 1), API Gateway, **secreto obligatorio**, 2 roles IAM mínimos. |
| Entorno Docker local | ✅ | dynamodb-local + init (2 tablas + TTL) + servidor Flask (inline, `ALLOW_INSECURE_WEBHOOK`) + perfil Lambda RIE. |
| Tests | ✅ | **59 tests** (`python -m unittest discover -s tests`); las unitarias no requieren AWS. |
| Scripts de empaquetado | ✅ | `package-lambda.ps1` y `.sh`. |

> **Las "correcciones de viabilidad" del documento de arquitectura ya están resueltas en el código:**
> error por usuario (`try/except`), DynamoDB por Query+GSI (no scan), timeout configurable hasta 900s.

### Brechas frente al objetivo

Las brechas se clasifican por si **bloquean** el objetivo (el sistema no lo cumple end-to-end hoy) o
si afectan **escala/robustez/operación**.

#### 🔴 Bloqueantes — todas resueltas (Fases 0 y 1)

1. ~~No existe alta de suscriptores.~~ **✅ Resuelto en Fase 0.** El receptor rutea `/start`/`/stop`
   de mensajes privados y `registrar_suscriptor` hace upsert idempotente del `status` en DynamoDB.

2. ~~El broadcast es síncrono y bloquea la respuesta del webhook.~~ **✅ Resuelto en Fase 1.** El
   receptor ahora encola en SQS y responde 200 de inmediato; el worker hace el broadcast desde la cola.

3. ~~El webhook es público y sin autenticar.~~ **✅ Resuelto en Fase 0.** Valida el `secret_token`
   (header `X-Telegram-Bot-Api-Secret-Token`) en tiempo constante y es **fail-closed**: sin secreto
   rechaza, salvo `ALLOW_INSECURE_WEBHOOK` (dev). El secreto es obligatorio en CloudFormation.

4. ~~Sin idempotencia (dedup de `update_id`).~~ **✅ Resuelto en Fase 0.** `PutItem` condicional con TTL
   en `ProcessedUpdates`; compensación acotada (solo revierte si no hubo efecto secundario) para no
   duplicar broadcasts ante reintentos de Telegram.

#### 🟠 Importantes — escala, robustez y operación

5. ~~Sin desacople SQS + DLQ.~~ **✅ Resuelto en Fase 1.** Receptor → SQS → worker, con DLQ
   (`maxReceiveCount=5`) y respuesta parcial de lotes. El worker (concurrencia reservada 1) no choca
   con el timeout porque cada invocación procesa un solo lote acotado.

6. ~~No honra el `429 Too Many Requests`.~~ **✅ Resuelto en Fase 1.** `telegram_client` respeta
   `parameters.retry_after` en 429 y aplica backoff exponencial acotado ante 5xx, con tope de reintentos.

7. ~~Solo procesa `channel_post.text`.~~ **✅ Resuelto en Fase 1.** El receptor extrae `text` o
   `caption` y procesa tanto `channel_post` como `edited_channel_post`.

8. **El bot token viaja como variable de entorno en texto plano.** Visible en la consola de Lambda.
   Conviene SSM Parameter Store / Secrets Manager.

9. **Cobertura de tests** (mejorada en Fase 1: 27 tests sobre markup, telegram, broadcaster, sqs,
   handler y worker). Pendiente: test de integración contra dynamodb-local y `dynamodb_client` real.

10. **Sin CI/CD.** Empaquetado y deploy manuales.

11. **Sin observabilidad.** Solo logs; sin alarmas CloudWatch (envíos fallidos, profundidad de DLQ)
    ni dashboard.

12. **`markup` demasiado agresivo.** `PRICE_PATTERN` captura cualquier número (cantidades, fechas,
    teléfonos) y le aplica markup. Riesgo de negocio: marcar precios donde no los hay.
    _Archivo: [markup.py:9-11](src/lambda/markup.py#L9-L11)._

---

## 2. Hoja de ruta

### Fase 0 — Cerrar el lazo (MVP funcional end-to-end) ✅ COMPLETADA

> **Meta:** que un usuario real pueda suscribirse y reciba una lista publicada, una sola vez, de forma
> segura. Resuelve las 4 brechas bloqueantes. Sin esto, el proyecto no cumple su objetivo.

- [x] **0.1 Alta/baja de suscriptores.** El receptor rutea `message` privados: `/start` → `active`,
      `/stop` → `inactive` (`registrar_suscriptor`, upsert que preserva `createdAt`), con respuesta de
      confirmación **best-effort** (un fallo al responder no revierte el alta). Soporta `/cmd@bot`.
      _Archivos: [handler.py](src/lambda/handler.py), [dynamodb_client.py](src/lambda/dynamodb_client.py)._
- [x] **0.2 Respuesta 200 inmediata + envío asíncrono.** Resuelto vía el desacople SQS de Fase 1: el
      receptor valida, encola y devuelve 200 al instante; el worker hace el broadcast.
- [x] **0.3 Validar `secret_token`.** Comparación en tiempo constante (`hmac.compare_digest`) del header
      `X-Telegram-Bot-Api-Secret-Token`; **fail-closed** (sin secreto → 403, salvo `ALLOW_INSECURE_WEBHOOK`
      en dev). El secreto es **obligatorio** en la plantilla (MinLength/AllowedPattern, sin Default).
      _Archivos: [handler.py](src/lambda/handler.py), [template.yaml](infra/cloudformation/template.yaml)._
- [x] **0.4 Dedup de `update_id`.** `PutItem` condicional (`attribute_not_exists`) con TTL en
      `ProcessedUpdates`; **compensación acotada**: solo revierte la marca si el fallo ocurrió antes de
      cualquier efecto secundario (encolado/alta), evitando broadcasts duplicados en el reintento.
      _Archivos: [handler.py](src/lambda/handler.py), [dynamodb_client.py](src/lambda/dynamodb_client.py)._

**Pruebas (36 añadidas, 63 en total):** `test_handler_fase0.py` (secret fail-closed + unit de
`_secret_valido` + `compare_digest`; parseo seguro 400/None/dict/lista/número/bytes/vacío; dedup
duplicado/primera-vez/parcial/inline/compensación y no-enmascaramiento; comandos
`/start`·`/stop`·`@bot`·no-privado·fallo-al-responder) y `test_dynamodb_client.py` (upsert, dedup con
`expiresAt` int futuro, paginación y filtro del GSI).

> **Revisión adversarial:** ambas fases se sometieron a un workflow multi-lente (seguridad, corrección,
> IAM, tests) con verificación de cada hallazgo. Los 16 hallazgos confirmados se corrigieron
> (fail-open del secreto, idempotencia del encolado parcial, parseo del body, TTL local, etc.).

**Limitaciones conocidas (aceptadas, documentadas):**
- `SOURCE_CHANNEL_ID` vacío acepta cualquier `channel_post` (cómodo para bot de un solo canal). En prod,
  fíjalo como control de autorización de contenido. Con el `secret_token` activo el riesgo es acotado.
- El GSI `StatusIndex` tiene partition key de cardinalidad 2 (`status`). A muy alto volumen sería un
  *hot partition*, pero `PAY_PER_REQUEST` (adaptive capacity) lo mitiga; si crece, sharding del status.

---

### Fase 1 — Robustez y escala (desacoplar el envío) ✅ COMPLETADA

> **Meta:** sobrevivir fallos parciales y escalar más allá de ~3.000 usuarios sin tocar el timeout.

- [x] **1.1 SQS + Lambda worker + DLQ.** Receptor encola un mensaje por lote de N chatIds; el worker
      lo consume con concurrencia reservada 1 (respeta los 30 msg/s) y reintentos; los lotes que agotan
      `maxReceiveCount` caen a la DLQ. Respuesta parcial de lotes evita reenviar a quienes sí recibieron.
      _Archivos: [worker.py](src/lambda/worker.py), [sqs_client.py](src/lambda/sqs_client.py),
      [broadcaster.py](src/lambda/broadcaster.py), [template.yaml](infra/cloudformation/template.yaml)._
- [x] **1.2 Honrar `429 retry_after`.** `telegram_client` lee `parameters.retry_after` en 429 y aplica
      backoff exponencial acotado en 5xx, con tope de reintentos.
      _Archivo: [telegram_client.py](src/lambda/telegram_client.py)._
- [x] **1.3 Soportar `caption` y `edited_channel_post`.** El receptor extrae `text`/`caption` y procesa
      ambos tipos de update. _Archivo: [handler.py](src/lambda/handler.py)._

**Pruebas (27, todas en verde):** `tests/test_telegram_client.py` (403/429/5xx/agota reintentos),
`test_broadcaster.py` (enviado/bloqueado→inactivo/fallido + delay), `test_sqs_client.py` (lotes +
payload), `test_handler.py` (text/caption/edited/filtro de canal/inline), `test_worker.py` (respuesta
parcial: éxito, fallo sistémico, parcial no reencola, body malformado).

> **Nota de alcance:** el criterio "worker matado a mitad de lote → reentrega" y "5.000 suscriptores"
> se valida por diseño (SQS redrive + DLQ, lotes acotados) y por los tests de respuesta parcial; falta
> una prueba de integración end-to-end contra SQS/dynamodb-local local (movida a Fase 3.1).

---

### Fase 2 — Seguridad y operación

- [ ] **2.1 Bot token en SSM Parameter Store / Secrets Manager** (resuelto en arranque, no en texto plano).
- [ ] **2.2 Observabilidad:** alarmas CloudWatch (tasa de fallos, profundidad de DLQ, errores Lambda),
      métricas custom (enviados/bloqueados/fallidos) y un dashboard. **Incluir una alarma sobre el log
      `Encolado parcial`**: hoy un encolado parcial mantiene el dedup (no duplica) pero pierde los lotes
      no encolados de forma silenciosa; debe ser visible para reenviar manualmente.
- [ ] **2.3 CI/CD** (GitHub Actions): `test → package → subir a S3 → cloudformation deploy`.

---

### Fase 3 — Calidad y producto

- [ ] **3.1 Tests de integración** end-to-end contra dynamodb-local + SQS local (el resto de
      `handler`/`telegram_client`/`dynamodb_client` ya tiene cobertura unitaria en Fases 0-1).
- [ ] **3.2 Comandos de gestión:** `/status`, panel de stats para admin; cumplimiento de opt-out.
- [ ] **3.3 Precisión del markup:** exigir contexto de moneda (símbolo `$`, `COP`, etc.) para no marcar
      cantidades, fechas ni teléfonos.

---

## 3. Prioridad sugerida

```
Fase 0 (bloqueante)  ──►  Fase 1 (escala)  ──►  Fase 2 (seguridad/ops)  ──►  Fase 3 (calidad)
   0.1 alta usuarios
   0.2 200 inmediato
   0.3 secret_token
   0.4 dedup
```

El orden dentro de la Fase 0 importa: **0.1 (alta de usuarios)** es el primer paso porque sin
suscriptores nada del resto es observable end-to-end. **0.2** debe ir antes de cualquier prueba con
carga real, porque hasta entonces cada publicación se duplica.
