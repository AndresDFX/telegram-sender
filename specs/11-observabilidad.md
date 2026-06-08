# Observabilidad y alarmas

`Estado: propuesto · ROADMAP Fase 2.2`

## Contexto y objetivo

Hoy solo hay logs en CloudWatch (`logger.info/exception`). No hay métricas ni alarmas, así que
fallos importantes pasan desapercibidos: en particular, el **encolado parcial** del receptor
(`handler.py` registra `logger.error("Encolado parcial ...")` y **mantiene el dedup**, perdiendo
silenciosamente los lotes no encolados) y la **acumulación en la DLQ** `BroadcastDLQ`. El objetivo es
métricas, alarmas y un dashboard, sin salir de la capa gratuita.

## Alcance

- **In scope:** métricas de negocio, alarmas CloudWatch sobre fallos clave, dashboard.
- **Out of scope:** tracing distribuido (X-Ray) y logs estructurados completos (posible iteración).

## Requisitos funcionales

1. **RF1** — Emitir métricas: `enviados`, `bloqueados`, `fallidos`, `duplicados_detectados`,
   `lotes_encolados`, `encolado_parcial`.
2. **RF2** — Alarma sobre **profundidad de `BroadcastDLQ`** (`ApproximateNumberOfMessagesVisible > 0`).
3. **RF3** — Alarma sobre **errores** de las Lambdas (`Errors`) y throttles.
4. **RF4** — Alarma sobre **encolado parcial** (métrica `encolado_parcial` o metric filter del log).
5. **RF5** — Dashboard con envíos, fallidos, profundidad de cola/DLQ y errores.

## Requisitos no funcionales

- **Coste:** preferir **EMF (Embedded Metric Format)** — emitir métricas como JSON estructurado en los
  logs, sin coste de `PutMetricData`. 10 alarmas y métricas EMF caben en capa gratuita.
- **Bajo acoplamiento:** la emisión de métricas no debe alterar la lógica de envío.

## Diseño propuesto

- Helper `src/lambda/metrics.py` con `emit(metric, value, unit, dims)` que imprime una línea EMF
  (`{"_aws": {...}, ...}`) al log; CloudWatch la convierte en métrica del namespace `TelegramSync`.
- Puntos de emisión:
  - `broadcaster.procesar_lote` → `enviados`/`bloqueados`/`fallidos` por lote.
  - `handler.lambda_handler` → `duplicados_detectados` (rama `status=duplicate`), `lotes_encolados`,
    y `encolado_parcial=1` en el `except PartialEnqueueError` con `error.enqueued > 0`.
- Alarmas en `template.yaml` (`AWS::CloudWatch::Alarm`):
  - `BroadcastDLQ` `ApproximateNumberOfMessagesVisible >= 1` (período 5 min).
  - `Errors` de `ReceiverFunction` y `WorkerFunction` `>= 1`.
  - métrica `encolado_parcial` `>= 1`.
- `AWS::CloudWatch::Dashboard` con los widgets clave.
- (Opcional) `AWS::SNS::Topic` para notificación de alarmas por email.

```
broadcaster/handler ──(EMF en logs)──► CloudWatch Metrics (TelegramSync) ──► Alarmas ──► SNS (email)
                                                                          └─► Dashboard
SQS BroadcastDLQ ─(métrica nativa ApproximateNumberOfMessagesVisible)──────► Alarma DLQ
```

## Cambios concretos

| Archivo | Cambio |
|---------|--------|
| `src/lambda/metrics.py` (nuevo) | `emit()` EMF (stdlib, sin boto3). |
| `src/lambda/broadcaster.py` | emitir `enviados`/`bloqueados`/`fallidos`. |
| `src/lambda/handler.py` | emitir `duplicados_detectados`, `lotes_encolados`, `encolado_parcial`. |
| `infra/cloudformation/template.yaml` | alarmas DLQ/Errors/encolado_parcial, dashboard, (opcional) SNS + param `AlarmEmail`; permisos ya cubiertos (logs). |

## Criterios de aceptación

- [ ] Tras un broadcast, aparecen métricas `enviados`/`fallidos` en el namespace `TelegramSync`.
- [ ] Un mensaje en `BroadcastDLQ` dispara la alarma en ≤ 5 min.
- [ ] Un encolado parcial (forzado en prueba) incrementa `encolado_parcial` y dispara su alarma.
- [ ] El dashboard muestra envíos, fallidos, profundidad de cola/DLQ y errores.
- [ ] Sin coste fuera de capa gratuita (EMF, no `PutMetricData`).

## Plan de pruebas

- `tests/test_metrics.py`: `emit()` produce JSON EMF válido con namespace/dimensiones correctos
  (capturar stdout).
- Ajustes en `tests/test_broadcaster.py` y `tests/test_handler_fase0.py`: aseverar que se emite la
  métrica esperada en cada rama (mock de `metrics.emit`), sin alterar los asserts existentes.
- Validación de plantilla (parseo + `aws cloudformation validate-template`).

## Riesgos y mitigaciones

- **Ruido de alarmas:** umbrales y períodos prudentes; agrupar en SNS.
- **EMF mal formado → métrica ausente:** test unitario del formato.
- **Coste inadvertido:** evitar `PutMetricData`; usar EMF.

## Dependencias y orden

Tras `10-gestion-de-secretos` (alarma ante fallo de resolución de secreto). Habilita verificar en
producción el trade-off de idempotencia documentado en `00-vision-general` (I2) y `handler.py`.
