# Tests de integración end-to-end

`Estado: propuesto · ROADMAP Fase 3.1`

## Contexto y objetivo

Las 63 pruebas actuales son **unitarias** y mockean las fronteras (boto3, SQS, Telegram). No se
ejercita el comportamiento real contra DynamoDB ni SQS. El objetivo es una suite de **integración**
contra `dynamodb-local` y una cola SQS local (ElasticMQ o LocalStack) que valide los caminos que los
mocks no cubren, formalizando `scripts/smoke_test.py`.

## Alcance

- **In scope:** tests de integración con servicios locales en Docker; verificación de GSI, dedup real,
  encolado + consumo por el worker, y el escenario "worker muere a mitad → reentrega".
- **Out of scope:** pruebas contra AWS real (eso lo cubre el smoke test post-deploy).

## Requisitos funcionales

1. **RF1** — `registrar_suscriptor` + `obtener_usuarios_activos` contra `dynamodb-local`: el GSI
   `StatusIndex` devuelve solo `active`; los `inactive` quedan fuera; paginación real.
2. **RF2** — Dedup real: `marcar_update_procesado` devuelve `True` la 1ª vez y `False` la 2ª;
   `borrar_update_procesado` permite reprocesar.
3. **RF3** — `encolar_lotes` contra SQS local + consumo por `worker.lambda_handler`: los chatIds
   llegan al worker en lotes; `batchItemFailures` reencola los fallos sistémicos.
4. **RF4** — Reentrega: tras un fallo del worker (excepción/timeout simulado) el mensaje vuelve a la
   cola y, agotado `maxReceiveCount`, cae a la DLQ.

## Requisitos no funcionales

- **Aislamiento:** cada test limpia su estado (tablas/cola) o usa nombres únicos.
- **CI-friendly:** marcar los tests que requieren Docker con `@unittest.skipUnless(...)` para poder
  saltarlos donde no hay Docker (ver `12-cicd`).
- **Sin AWS real:** todo contra endpoints locales (`DYNAMODB_ENDPOINT`, `SQS_ENDPOINT`).

## Diseño propuesto

- Añadir un servicio SQS local a `docker/docker-compose.yml` (perfil `integration`), p.ej.
  `softwaremill/elasticmq` o `localstack` (solo SQS).
- `tests/integration/` con un helper que detecte endpoints (`DYNAMODB_ENDPOINT`, `SQS_ENDPOINT`) y
  haga `skip` si no están disponibles.
- Los tests usan los módulos reales (`dynamodb_client`, `sqs_client`, `worker`, `broadcaster`) con
  `TelegramClient` **mockeado** (no llamamos a Telegram), centrando la integración en AWS local.
- Reutilizar y promover `scripts/smoke_test.py` como prueba de humo del receptor (modo inline).

```
tests/integration ──► dynamodb-local (tablas reales + GSI + TTL)
                  └──► SQS local (cola + DLQ)  ──► worker.lambda_handler (TelegramClient mock)
```

## Cambios concretos

| Archivo | Cambio |
|---------|--------|
| `docker/docker-compose.yml` | servicio SQS local bajo perfil `integration`. |
| `docker/init-sqs.sh` (nuevo) | crear cola `broadcast` + DLQ con redrive. |
| `tests/integration/__init__.py`, `tests/integration/conftest_local.py` (nuevo) | detección de endpoints + skip. |
| `tests/integration/test_dynamodb_integration.py` (nuevo) | RF1, RF2. |
| `tests/integration/test_broadcast_integration.py` (nuevo) | RF3, RF4. |
| `scripts/smoke_test.py` | documentar/formalizar como prueba de humo. |
| `README.md` | cómo correr la suite de integración (`--profile integration`). |

## Criterios de aceptación

- [ ] Con Docker arriba, la suite de integración pasa en verde.
- [ ] Sin Docker, los tests de integración se **saltan** (no fallan) y la suite unitaria sigue verde.
- [ ] Se demuestra la reentrega del worker y la caída a DLQ tras `maxReceiveCount`.
- [ ] Se demuestra que `inactive` no aparece en `obtener_usuarios_activos`.

## Plan de pruebas

(Los tests *son* el entregable.) Nombres propuestos:
`test_alta_y_listado_activos`, `test_dedup_marca_y_borra`, `test_encolar_y_consumir_worker`,
`test_reentrega_y_dlq`.

## Riesgos y mitigaciones

- **Flakiness por timing de SQS:** usar long-polling/esperas acotadas con reintentos.
- **Diferencias dynamodb-local vs real (TTL no purga):** documentar; el dedup no depende del TTL.
- **Dependencia de Docker en CI:** servicios como contenedores de servicio en GitHub Actions, o skip.

## Dependencias y orden

Tras `00`/Fases 0-1 (código a probar). Se integra con `12-cicd` (qué suite corre y dónde).
