# Precisión del markup (contexto de moneda)

`Estado: ✅ IMPLEMENTADO · ROADMAP Fase 3.3`

> Implementado en [`domain/markup.py`](../src/lambda/domain/markup.py): solo marca números con `$` en
> formato colombiano (`$325.000`) y redondea al **mil hacia arriba** (`$325.000` +15% → `$374.000`),
> sin tocar modelos ni specs. Tests en `tests/test_markup.py`. Esta versión se priorizó al confirmar
> que el canal real (`@iproparts`) publica listas en COP. El texto abajo es el diseño original.

## Contexto y objetivo

`src/lambda/markup.py` aplica el markup a **cualquier número** que case con `PRICE_PATTERN`
(`\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})|\d+(?:\.\d{2})?`). Esto marca también cantidades,
fechas, teléfonos o porcentajes que no son precios (riesgo de negocio: alterar números que no debían).
El objetivo es exigir **contexto de moneda** para considerar un número como precio, manteniendo el
soporte de formatos US y europeo ya existente.

## Alcance

- **In scope:** detección de precios condicionada a un símbolo/código de moneda adyacente; casos
  límite documentados.
- **Out of scope:** conversión entre monedas o NLP avanzado.

## Requisitos funcionales

1. **RF1** — Solo se aplica markup a números con contexto de moneda adyacente: símbolo (`$`, `€`) o
   código (`USD`, `COP`, `EUR`, `MXN`, …) antes o después del número, con espacio opcional.
2. **RF2** — Mantener formatos soportados hoy: US (`1,234.56` / `100.00`) y europeo (`1.250,50`).
3. **RF3** — No marcar: cantidades sin moneda (`2 unidades`), fechas (`2026-06-08`, `12/05`),
   teléfonos (`3001234567`), porcentajes (`15%`), horas (`10:30`).
4. **RF4** — Preservar el formato original de cada precio (separadores) como hace `_format_price`.

## Requisitos no funcionales

- **Compatibilidad:** no romper los 3 tests actuales de `tests/test_markup.py` (ajustar entradas para
  que incluyan símbolo de moneda donde corresponda; el comportamiento "con moneda" debe seguir igual).
- **Configurable:** lista de monedas reconocidas por env var opcional (`CURRENCY_SYMBOLS`).
- **Determinismo:** `Decimal` + `ROUND_HALF_UP` como hoy.

## Diseño propuesto

- Cambiar la estrategia de `aplicar_markup` para que la regex requiera contexto de moneda. Dos
  variantes:
  - **A (regex con contexto):** un patrón que capture `(moneda)?\s*(numero)\s*(moneda)?` y solo
    sustituya el grupo numérico cuando hay moneda en alguno de los lados.
  - **B (dos pasos):** localizar tokens de precio = número precedido/seguido por moneda; aplicar
    `_parse_price`/`_format_price` (ya existentes) solo a esos.
- Reusar `_parse_price` y `_format_price` sin cambios (la lógica de formato US/EU se mantiene).
- Conjunto de monedas por defecto: `$ € USD EUR COP MXN ARS CLP PEN`; ampliable por env var.

```
texto ─► encontrar [moneda + número] o [número + moneda] ─► _parse_price ─► *1.15 ─► _format_price
        (números sin moneda quedan intactos)
```

## Cambios concretos

| Archivo | Cambio |
|---------|--------|
| `src/lambda/markup.py` | nueva detección con contexto de moneda; reutiliza `_parse_price`/`_format_price`; constante/env `CURRENCY_SYMBOLS`. |
| `tests/test_markup.py` | añadir casos negativos (cantidades/fechas/teléfonos/%) y positivos con `$`, `COP`, `€`; ajustar los 3 existentes para incluir moneda. |
| `.env.example` | documentar `CURRENCY_SYMBOLS` (opcional). |

## Criterios de aceptación

- [ ] `"Zapatillas $100.00"` → markup aplicado (`$115.00`).
- [ ] `"Precio: 1.250,50 COP"` → `1.438,08 COP`.
- [ ] `"2 unidades"`, `"2026-06-08"`, `"3001234567"`, `"15%"`, `"10:30"` → **sin cambios**.
- [ ] Los formatos US y europeo siguen preservándose.
- [ ] `tests/test_markup.py` en verde con los casos nuevos.

## Plan de pruebas

- `tests/test_markup.py`: `test_marca_con_simbolo`, `test_marca_con_codigo_moneda`,
  `test_no_marca_cantidad`, `test_no_marca_fecha`, `test_no_marca_telefono`, `test_no_marca_porcentaje`,
  + mantener `test_formato_europeo`/`test_aplica_quince_por_ciento` (con moneda).

## Riesgos y mitigaciones

- **Falsos negativos** (precio sin símbolo en la lista): hacer `CURRENCY_SYMBOLS` configurable y
  documentar el formato esperado del canal fuente.
- **Ambigüedad de formato** (`1.250` ¿mil o decimal?): el comportamiento actual de `_parse_price` se
  mantiene; los casos límite quedan documentados aquí.
- **Regresión:** cobertura ampliada en `test_markup.py`.

## Dependencias y orden

Independiente; puramente en `markup.py` (capa de negocio del receptor). No afecta infra ni seguridad.
