# Gestión de secretos (SSM / Secrets Manager)

`Estado: propuesto · ROADMAP Fase 2.1`

## Contexto y objetivo

Hoy `TELEGRAM_BOT_TOKEN` y `WEBHOOK_SECRET_TOKEN` viajan como **variables de entorno en texto plano**
en las Lambdas (`infra/cloudformation/template.yaml`): visibles en la consola para cualquiera con
`lambda:GetFunctionConfiguration`. El objetivo es moverlos a un almacén de secretos (SSM Parameter
Store `SecureString` o Secrets Manager) y resolverlos en runtime, manteniendo el coste en capa
gratuita.

## Alcance

- **In scope:** almacenamiento y resolución de `TELEGRAM_BOT_TOKEN` y `WEBHOOK_SECRET_TOKEN`; permisos
  IAM; caché en memoria; cambios en `handler.py`/`telegram_client.py`/template.
- **Out of scope:** rotación automática del token de bot (Telegram no la soporta nativamente).

## Requisitos funcionales

1. **RF1** — El receptor obtiene `WEBHOOK_SECRET_TOKEN` desde el almacén para validar el header.
2. **RF2** — El worker (y el receptor, para responder `/start`) obtiene `TELEGRAM_BOT_TOKEN` desde el
   almacén.
3. **RF3** — Compatibilidad dev: en local (`docker`/tests) se sigue permitiendo el valor por env var
   directa cuando no hay ARN de secreto configurado.

## Requisitos no funcionales

- **Coste:** SSM Parameter Store `Standard` `SecureString` es gratis (vs. Secrets Manager ~$0.40/secreto/mes).
  **Recomendado: SSM Parameter Store.**
- **Cold start:** una llamada `GetParameter` por contenedor; cachear en memoria de módulo.
- **Seguridad:** cifrado con KMS (clave gestionada por AWS `aws/ssm`); IAM mínimo por función.

## Diseño propuesto

Nuevo módulo `src/lambda/secrets.py` con resolución perezosa y caché:

```
get_secret(nombre_logico) ->
  1. si hay <NOMBRE>_PARAM (ARN/nombre SSM) en env: GetParameter(WithDecryption=True), cachear, devolver
  2. si no, devolver os.environ[<NOMBRE>]  (camino dev/local actual)
```

- `telegram_client.TelegramClient.__init__` usa `get_secret("TELEGRAM_BOT_TOKEN")` en vez de
  `os.environ["TELEGRAM_BOT_TOKEN"]` (`src/lambda/telegram_client.py`).
- `handler._secret_valido` usa `get_secret("WEBHOOK_SECRET_TOKEN")` (`src/lambda/handler.py`).
- Caché a nivel de módulo (`dict`) para no llamar a SSM en cada invocación; el contenedor Lambda se
  reutiliza entre invocaciones.
- Import perezoso de boto3 dentro de `secrets.py` (coherente con `dynamodb_client.py`).

## Cambios concretos

| Archivo | Cambio |
|---------|--------|
| `src/lambda/secrets.py` (nuevo) | `get_secret()` con caché y fallback a env var. |
| `src/lambda/telegram_client.py` | token vía `get_secret`. |
| `src/lambda/handler.py` | secreto del webhook vía `get_secret`. |
| `infra/cloudformation/template.yaml` | recursos `AWS::SSM::Parameter` (o referencia a existentes); env vars `TELEGRAM_BOT_TOKEN_PARAM`/`WEBHOOK_SECRET_TOKEN_PARAM`; IAM `ssm:GetParameter`(+`kms:Decrypt`) por función. |
| `.env.example`, `docker/docker-compose.yml` | doc del modo dev (env var directa). |

> Nota: un parámetro SSM `SecureString` **no** puede crearse con valor vía CloudFormation puro
> (CFN no soporta el tipo `SecureString` en `AWS::SSM::Parameter`). Crear el parámetro fuera del
> stack (CLI/script de bootstrap) y pasar su **nombre/ARN** como parámetro del stack; el stack solo
> concede permisos de lectura.

## Criterios de aceptación

- [ ] Las Lambdas desplegadas **no** exponen los valores de token/secreto en sus env vars.
- [ ] El receptor valida el `secret_token` resolviéndolo desde SSM.
- [ ] El worker envía usando el token resuelto desde SSM.
- [ ] En local sin ARN configurado, todo sigue funcionando con env vars (tests verdes).
- [ ] IAM: cada función solo puede leer los parámetros que necesita.

## Plan de pruebas

- `tests/test_secrets.py`: `get_secret` devuelve env var cuando no hay `*_PARAM`; cuando sí, llama a
  SSM (mock de boto3) y **cachea** (segunda llamada no invoca SSM); error de SSM se propaga.
- Ajustar `tests/test_telegram_client.py` y `tests/test_handler_fase0.py` para el fallback por env var
  (sin regresión).

## Riesgos y mitigaciones

- **Cold start extra:** mitigado con caché de módulo (1 llamada por contenedor).
- **Parámetro inexistente/permiso faltante:** fallo explícito en arranque; cubrir con alarma (spec `11`).
- **Coste:** usar SSM Standard (gratis), no Secrets Manager.

## Dependencias y orden

Independiente; conviene antes de `12-cicd` (el pipeline debe inyectar nombres de parámetro, no
secretos). Relacionado con `11-observabilidad` (alarma ante fallo de resolución).
