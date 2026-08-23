# CI/CD con GitHub Actions

`Estado: propuesto · ROADMAP Fase 2.3`

## Contexto y objetivo

El empaquetado y despliegue son manuales (`scripts/package-lambda.ps1` → `aws s3 cp` →
`aws cloudformation deploy`, ver `README.md`). El objetivo es un pipeline en GitHub Actions que corra
tests, empaquete de forma reproducible (Linux) y despliegue a AWS, usando **OIDC** (rol asumible) en
vez de claves estáticas.

## Alcance

- **In scope:** workflow de CI (tests) y CD (package → upload S3 → `cloudformation deploy`); auth OIDC;
  entornos `dev`/`prod`; secretos vía GitHub Secrets.
- **Out of scope:** despliegues blue/green o canary de Lambda (futuro).

## Requisitos funcionales

1. **RF1** — En cada push/PR: instalar deps de test y correr `python -m unittest discover -s tests`.
2. **RF2** — En push a `main` (o tag): empaquetar el zip en Linux (reutilizando la lógica de
   `scripts/_build_lambda_pkg.py`), subir a S3 y `cloudformation deploy`.
3. **RF3** — Autenticación con AWS por **OIDC** (`aws-actions/configure-aws-credentials` asumiendo un
   rol), sin `AWS_ACCESS_KEY_ID`/`SECRET` estáticos en el repo.
4. **RF4** — `WebhookSecretToken` y nombres de parámetros desde **GitHub Secrets/Environments**.
5. **RF5** — `deploy` solo si los tests pasan (job dependiente).

## Requisitos no funcionales

- **Reproducibilidad:** empaquetado dentro de `python:3.12-slim` (idéntico a local).
- **Seguridad:** sin credenciales de larga vida; rol OIDC con permisos mínimos (S3 put, CFN, IAM
  passrole acotado a los roles del stack).
- **Coste:** GitHub Actions gratis para repos personales; AWS en capa gratuita.

## Diseño propuesto

```
push/PR ──► job "test" (ubuntu, python 3.12, unittest)
push main ─► job "deploy" (needs: test)
              ├─ configure-aws-credentials (OIDC → rol arn)
              ├─ build zip  (python -m ... _build_lambda_pkg.py, o pip -t + zip en runner Linux)
              ├─ aws s3 cp .build/telegram-broadcaster.zip s3://$BUCKET/lambda/...
              └─ aws cloudformation deploy --parameter-overrides ... --capabilities CAPABILITY_NAMED_IAM
```

- Workflow `.github/workflows/ci.yml` (job `test`) y `.github/workflows/deploy.yml` (job `deploy`,
  `environment: dev`), o un único archivo con dos jobs.
- El empaquetado en el runner Linux puede llamar directamente a `pip install -r requirements.txt -t`
  + `zip` (el runner ya es Linux), reutilizando el espíritu de `scripts/_build_lambda_pkg.py`.
- **Bootstrap (una vez, fuera del pipeline):** crear el proveedor OIDC de GitHub en IAM y un rol
  `telegram-sync-ci` con confianza al repo `AndresDFX/replica` y permisos: `s3:PutObject` en
  el bucket de artefactos, `cloudformation:*` sobre el stack, `iam:PassRole` sobre los roles del
  stack, y los permisos de creación de recursos del template. Documentar este bootstrap.

## Cambios concretos

| Archivo | Cambio |
|---------|--------|
| `.github/workflows/ci.yml` (nuevo) | job `test` (unittest) en push/PR. |
| `.github/workflows/deploy.yml` (nuevo) | job `deploy` (OIDC → build → S3 → CFN) en push a `main`. |
| `scripts/` | (opcional) script Linux de build reutilizable por local y CI. |
| `README.md` | sección CI/CD y bootstrap del rol OIDC. |
| `docs/` o este spec | pasos de bootstrap del proveedor OIDC + rol. |

## Criterios de aceptación

- [ ] Un PR ejecuta los tests y bloquea el merge si fallan.
- [ ] Un push a `main` despliega el stack `telegram-sync-dev` sin intervención manual.
- [ ] No existen claves AWS estáticas en el repo ni en los workflows.
- [ ] El zip generado en CI es Linux-nativo (sin binarios Windows, rutas con `/`).

## Plan de pruebas

- Validar el workflow con un push de prueba a una rama y revisar el run.
- Verificar que el job `deploy` falla si `test` falla (dependencia `needs`).
- `aws cloudformation describe-stacks` post-deploy confirma `UPDATE_COMPLETE`.

## Riesgos y mitigaciones

- **Permisos OIDC excesivos:** acotar el rol; revisar con Access Analyzer.
- **Deriva entre build local y CI:** centralizar la lógica de empaquetado.
- **Deploy roto a medias:** `cloudformation deploy` hace rollback automático; alarmas de `11`.

## Dependencias y orden

Tras `10-gestion-de-secretos` (el pipeline inyecta **nombres** de parámetro SSM, no secretos en claro).
Complementa `20-tests-integracion` (qué suite corre en CI y cuál requiere Docker).
