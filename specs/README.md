# Specs — Telegram → Telegram Sync

Especificaciones técnicas del proyecto. El spec de [visión general](00-vision-general.md) documenta lo
**ya construido** (Fases 0-1); el resto define el **trabajo pendiente** del [ROADMAP](../ROADMAP.md),
uno por ítem.

## Índice

| Spec | Título | ROADMAP | Estado |
|------|--------|---------|--------|
| [00](00-vision-general.md) | Visión general del sistema | — | vigente (incluye ingesta por poller) |
| [10](10-gestion-de-secretos.md) | Gestión de secretos (SSM / Secrets Manager) | Fase 2.1 | propuesto |
| [11](11-observabilidad.md) | Observabilidad y alarmas | Fase 2.2 | propuesto |
| [12](12-cicd.md) | CI/CD con GitHub Actions | Fase 2.3 | propuesto |
| [20](20-tests-integracion.md) | Tests de integración end-to-end | Fase 3.1 | propuesto |
| [21](21-comandos-gestion.md) | Comandos de gestión y administración | Fase 3.2 | propuesto |
| [22](22-precision-markup.md) | Precisión del markup (contexto de moneda) | Fase 3.3 | ✅ implementado |

## Orden de avance sugerido

```
10 secretos ─► 11 observabilidad ─► 12 CI/CD ─► 20 tests integración ─► 21 comandos ─► 22 markup
```

`10` antes que `12` (el pipeline inyecta nombres de parámetro, no secretos). `11` se apoya en `10`
(alarma ante fallo de resolución de secreto) y habilita verificar en prod las invariantes de
idempotencia. `20` formaliza `scripts/smoke_test.py` y se coordina con `12` (qué suite corre en CI).

## Convención de specs

Cada spec sigue esta estructura: Contexto y objetivo · Alcance (in/out) · Requisitos funcionales (RF)
· Requisitos no funcionales · Diseño propuesto (con referencias a archivos/funciones reales) · Cambios
concretos · Criterios de aceptación · Plan de pruebas · Riesgos y mitigaciones · Dependencias y orden.

- Numeración: `0x` base/transversal, `1x` Fase 2, `2x` Fase 3.
- Los specs deben ser fieles al código existente (citar `src/lambda/...`, recursos del template) y
  reutilizar la terminología del repo (receptor, worker, broadcaster, dedup, `PartialEnqueueError`,
  `ProcessedUpdates`, `StatusIndex`, fail-closed).
- Pruebas en la convención existente: `unittest`, archivos `tests/test_*.py`.
