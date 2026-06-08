# Comandos de gestión y administración

`Estado: propuesto · ROADMAP Fase 3.2`

## Contexto y objetivo

El receptor ya rutea comandos privados `/start` y `/stop` (`_comando_privado` y `_manejar_comando` en
`src/lambda/handler.py`). Faltan comandos para que un usuario consulte su estado y para que un
administrador vea métricas básicas, además de garantizar cumplimiento de opt-out.

## Alcance

- **In scope:** `/status` (usuario), `/help`; comandos admin `/stats` restringidos por allowlist.
- **Out of scope:** panel web (los stats se consultan por chat o dashboard de `11-observabilidad`).

## Requisitos funcionales

1. **RF1** — `/status`: responde si el chat está `active`/`inactive`/no registrado (lee
   `SubscriptoresTelegram` por `chatId`).
2. **RF2** — `/help`: lista de comandos (hoy el texto por defecto ya hace esto para comandos
   desconocidos; formalizarlo).
3. **RF3** — `/stats` (admin): nº de suscriptores `active` (y opcionalmente `inactive`). Solo para
   chat IDs en una allowlist (`ADMIN_CHAT_IDS`).
4. **RF4** — Opt-out: un usuario con `/stop` no recibe broadcasts (ya garantizado por el GSI
   `StatusIndex` que filtra `active`); `/status` debe reflejarlo.

## Requisitos no funcionales

- **Autorización:** los comandos admin se rechazan (respuesta neutra) si el `chatId` no está en la
  allowlist; nunca exponer datos a no-admins.
- **Eficiencia:** evitar `Scan` de la tabla para contar; ver diseño.
- **Coherencia:** reusar el ruteo existente sin romper `/start`·`/stop`.

## Diseño propuesto

- Extender `_manejar_comando` (`handler.py`) con casos `/status`, `/help`, `/stats`.
- Nueva función de lectura en `dynamodb_client.py`: `obtener_estado(chat_id)` (`GetItem`).
- Conteo de activos para `/stats`:
  - Opción A (simple): reutilizar `obtener_usuarios_activos()` y `len(...)` — O(activos), aceptable a
    baja escala; ya pagina.
  - Opción B (escalable): mantener un contador con `UpdateItem ADD` en un item agregado al dar de
    alta/baja (consistencia eventual). Empezar con A; migrar a B si crece.
- Allowlist admin vía env var `ADMIN_CHAT_IDS` (lista separada por comas), leída en `handler.py`.

```
/status  ─► dynamodb_client.obtener_estado(chat_id) ─► "activo/inactivo/no registrado"
/stats   ─► (admin?) ─► len(obtener_usuarios_activos())  ─► "N activos"
```

## Cambios concretos

| Archivo | Cambio |
|---------|--------|
| `src/lambda/handler.py` | casos `/status`, `/help`, `/stats`; chequeo de allowlist admin; textos. |
| `src/lambda/dynamodb_client.py` | `obtener_estado(chat_id)` (GetItem); (opción B) contador agregado. |
| `infra/cloudformation/template.yaml` | env var `ADMIN_CHAT_IDS` en el receptor; permiso `dynamodb:GetItem` ya implícito por `Query`/`UpdateItem`? añadir `GetItem` al rol receptor. |
| `.env.example` | documentar `ADMIN_CHAT_IDS`. |

## Criterios de aceptación

- [ ] `/status` devuelve el estado correcto para activo, inactivo y no registrado.
- [ ] `/stats` responde el conteo solo a chat IDs admin; a no-admins responde algo neutro (no el dato).
- [ ] `/start`·`/stop` siguen funcionando (sin regresión; tests previos verdes).
- [ ] El rol IAM del receptor incluye `dynamodb:GetItem` sobre la tabla de suscriptores.

## Plan de pruebas

- `tests/test_handler_fase0.py` (o nuevo `test_comandos.py`): `/status` activo/inactivo/no-registrado
  (mock `obtener_estado`); `/stats` admin vs no-admin (mock allowlist + `obtener_usuarios_activos`);
  `/help`.
- `tests/test_dynamodb_client.py`: `obtener_estado` (GetItem mockeado) devuelve el item / `None`.

## Riesgos y mitigaciones

- **Fuga de datos a no-admins:** test explícito de rechazo; respuesta neutra.
- **Conteo costoso a escala:** empezar con opción A; migrar a contador agregado (B).
- **Spam de comandos:** el dedup por `update_id` ya evita reproceso de reintentos.

## Dependencias y orden

Tras Fases 0-1. Independiente de `10`/`11`/`12`; `/stats` se complementa con el dashboard de
`11-observabilidad`.
