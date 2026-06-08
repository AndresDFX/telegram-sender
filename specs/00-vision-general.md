# Visión general del sistema

`Estado: vigente · documenta lo construido (Fases 0-1)`

## Contexto y objetivo

Sincronización 1:1 orientada a eventos: cuando el **canal fuente** de Telegram publica una lista,
el sistema la intercepta vía webhook, aplica un **markup del 15 %** a los precios y la **distribuye
por mensaje directo** a cada suscriptor. Restricciones de diseño: mantenerse en la **capa gratuita**
de AWS y respetar los límites de la Bot API de Telegram (**30 msg/s** globales de envío; el webhook
debe responder rápido un `200`).

Este documento es la referencia base; el resto de specs (`10-*` … `22-*`) describen el trabajo
pendiente sobre esta arquitectura.

## Alcance

- **In scope:** arquitectura actual, componentes, modelo de datos, flujos y modelo de seguridad ya
  implementados.
- **Out of scope:** trabajo futuro (secretos gestionados, observabilidad, CI/CD, etc.), cubierto en
  los demás specs.

## Requisitos funcionales (implementados)

1. **RF1** — Alta/baja de suscriptores vía `/start` y `/stop` por chat privado.
2. **RF2** — Detección de listas del canal fuente (`channel_post` y `edited_channel_post`, texto o
   `caption`) y aplicación de markup.
3. **RF3** — Distribución por DM a todos los suscriptores activos.
4. **RF4** — Idempotencia: una misma actualización de Telegram no se procesa dos veces.
5. **RF5** — Autenticación del webhook mediante `secret_token`.

## Requisitos no funcionales

- **Coste:** capa gratuita (Lambda, DynamoDB PAY_PER_REQUEST, SQS, API Gateway HTTP).
- **Límite de envío:** ≤ 30 msg/s globales (delay por envío + concurrencia reservada del worker).
- **Resiliencia:** el envío sobrevive fallos parciales (reintentos SQS + DLQ).
- **Seguridad:** endpoint fail-closed; IAM de mínimo privilegio por función.

## Arquitectura

```
EventBridge cron ─► Lambda poller ─┐ (lee t.me/s/<canal>, HWM por message_id)
                    (poller.py)     │
                                    ▼
Telegram (DM) ─► API Gateway ─► Lambda receptor ─► SQS broadcast ─► Lambda worker ─► Telegram (DM)
 /start /stop    /webhook/...   (handler.py)          │   ▲         (worker.py →           │
                                secret→dedup→ruteo    │   │ redrive  broadcaster.py)        ▼
                                markup→encola, 200     ▼   └──────── SQS DLQ (lotes agotados)
                                                  (lotes de N chatIds)
   Onboarding: /start·/stop (privado) ──► DynamoDB SubscriptoresTelegram (GSI StatusIndex)
   Dedup / HWM: update_id · __hwm__<canal> ─► DynamoDB ProcessedUpdates (TTL en expiresAt)
```

> **Ingesta (opción C):** el canal de precios es público y NO controlado por nosotros, así que el bot
> no puede ser admin. En vez de recibir `channel_post` por webhook, el **poller** sondea el preview
> público y difunde solo lo nuevo (high-water mark por `message_id`). El webhook queda para el
> onboarding. (La ruta `channel_post` del receptor se conserva por si algún día el bot es admin.)

## Componentes

| Componente | Archivo / recurso | Responsabilidad |
|------------|-------------------|-----------------|
| Poller | `src/lambda/poller.py` | Sondea `t.me/s/<canal>` (EventBridge cron), detecta posts nuevos por HWM, aplica markup y encola en SQS. **Ingesta viva.** |
| Receptor | `src/lambda/handler.py` | Valida `secret_token` (fail-closed), parsea seguro, deduplica `update_id`, rutea `/start`·`/stop`, encola en SQS y responde `200` rápido. |
| Worker | `src/lambda/worker.py` | Consume SQS; respuesta parcial de lotes (`batchItemFailures`). |
| Envío por lote | `src/lambda/broadcaster.py` | `procesar_lote`: envía a cada chat con delay; 403→inactivo, errores→`failed`. |
| Cliente Telegram | `src/lambda/telegram_client.py` | `send_message` con 403, `429 retry_after` y backoff 5xx. |
| Encolado | `src/lambda/sqs_client.py` | `encolar_lotes` (lotes de chatIds) + `PartialEnqueueError`. |
| Acceso a datos | `src/lambda/dynamodb_client.py` | suscriptores (Query GSI, upsert, inactivar), dedup (`marcar_/borrar_update_procesado`) y HWM del poller (`obtener_/guardar_hwm`). |
| Markup | `src/lambda/markup.py` | `aplicar_markup`: solo precios con `$` en formato colombiano, redondeo al mil hacia arriba. |
| Infra | `infra/cloudformation/template.yaml` | DynamoDB ×2, SQS+DLQ, Lambda ×3 (poller/receptor/worker), EventBridge, API Gateway, 3 roles IAM. |
| Dev local | `docker/` | dynamodb-local + init + servidor Flask (modo inline). |

## Modelo de datos

- **`SubscriptoresTelegram`** — PK `chatId` (S); atributos `status` (`active`/`inactive`), `createdAt`,
  `updatedAt`. GSI **`StatusIndex`** (HASH `status`) para listar activos (`obtener_usuarios_activos`).
- **`ProcessedUpdates`** — PK `updateId` (S); atributo `expiresAt` (epoch, **TTL**). `PutItem`
  condicional `attribute_not_exists(updateId)` para deduplicar.

## Flujos

1. **Broadcast:** webhook `channel_post`/`edited_channel_post` del canal fuente → markup → query de
   activos (GSI) → `encolar_lotes` → `200 queued`. El worker consume y envía con delay.
2. **Onboarding:** `message` privado `/start`→`active`, `/stop`→`inactive` (upsert idempotente),
   respuesta de confirmación best-effort.
3. **Dedup:** marca-antes del `update_id`; duplicado → `200 duplicate`.

## Invariantes (garantías del sistema)

- **I1** — Sin `secret_token` válido el receptor responde `403` (fail-closed; bypass solo con
  `ALLOW_INSECURE_WEBHOOK` fuera de Lambda).
- **I2** — Una misma `update_id` no dispara dos broadcasts: la compensación del dedup solo revierte
  ante fallos de preparación o idempotentes; `PartialEnqueueError` (con lotes ya encolados) y el
  broadcast inline **no** revierten.
- **I3** — El worker solo reencola (vía `batchItemFailures`) los mensajes con fallo sistémico; los
  fallos por usuario no reenvían el lote.
- **I4** — Un body inválido devuelve `400` sin marcar dedup.

## Dependencias y orden

Base para todos los specs. El orden sugerido de avance: `10` (secretos) → `11` (observabilidad) →
`12` (CI/CD) → `20` (tests integración) → `21` (comandos) → `22` (markup).
