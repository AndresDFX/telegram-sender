# Design System "Replica" — Panel de Administracion

> Documento definitivo del sistema de diseño del panel admin. En español, agnostico de tecnologia y orientado a la accion. Sustituye el estilo monolitico actual (tema indigo/cian embebido en `admin.py`) por una identidad consistente, modular y mantenible basada en la marca **naranja #FD531E** + **gris #4A4A49**.

---

## 1. Introduccion y principios

**Replica** es el sistema de diseño que unifica la interfaz del panel administrativo. Su objetivo es que cualquier pantalla nueva o refactorizada se construya a partir de piezas reutilizables, predecibles y accesibles, en lugar de CSS ad-hoc disperso.

### Principios rectores

1. **Modular**: cada pieza (atomo, molecula, organismo) tiene una unica responsabilidad, una API clara (props/variantes/estados) y se compone con otras sin conocer su interior. Nada de estilos "globales magicos" que se filtran entre componentes.
2. **Escalable**: la base son **tokens de diseño** (variables CSS). Cambiar la marca, la densidad o el tema se hace en un solo lugar (`tokens.css`) y se propaga a todo el sistema sin tocar componentes.
3. **Mantenible**: nombres explicitos (BEM), una sola fuente de verdad por concepto, y separacion estricta entre **tokens** (qué valores), **componentes** (cómo se ven/comportan) y **plantillas** (cómo se combinan). Cero valores hardcodeados en componentes.
4. **Atomic Design**: la jerarquia sigue Atomos → Moleculas → Organismos → Plantillas → Paginas. Esto da un vocabulario compartido entre diseño e ingenieria y permite construir de lo simple a lo complejo.
5. **Agnostico de tecnologia**: el sistema se define en HTML semantico + CSS con custom properties. No depende de React, Vue, Svelte ni de un framework de utilidades. Cualquier stack puede consumir las clases y los tokens; los frameworks solo "envuelven" el markup.

### Como se materializa el codigo

| Capa | Archivo | Contenido |
|---|---|---|
| Tokens | `tokens.css` | Todas las variables (color, tipografia, espaciado, radios, sombras, z-index, transiciones, focus). **Unica fuente de verdad.** |
| Componentes | `components.css` | Clases de atomos/moleculas/organismos (BEM) que **solo** consumen tokens. |
| Catalogo vivo | `preview.html` | Galeria navegable de todos los componentes en todos sus estados. |

> Convencion: este documento **referencia** los tokens y el HTML/CSS; no duplica el codigo fuente. La verdad esta en `tokens.css`, `components.css` y `preview.html`.

---

## 2. Identidad visual

La marca Replica se construye sobre dos colores ancla:

- **Naranja `#FD531E` (primario / marca)**: energia, accion y foco. Es el color de las acciones primarias, links, anillos de foco y acentos. Por su alta saturacion se usa de forma **deliberada y dosificada** — nunca como fondo extenso de lectura. Reservado para "lo que importa": el boton principal de una vista, el indicador de seleccion, una metrica destacada.
- **Gris `#4A4A49` (neutro base)**: estructura, texto y superficies. Es la columna vertebral neutra del sistema. De el se deriva toda la escala de grises (texto, bordes, fondos, superficies oscuras). Aporta seriedad y deja respirar al naranja.

### Tono y uso

- **Relacion 90/10**: aprox. 90% neutros (grises + blanco) y ~10% marca. El naranja gana fuerza precisamente porque es escaso.
- **Jerarquia por color**: una sola accion primaria (naranja) por bloque; el resto en secundario/ghost (grises). Si todo es naranja, nada destaca.
- **Semantica antes que marca**: exito/advertencia/error/info usan sus colores semanticos propios, no el naranja. El naranja **no** significa "peligro" ni "advertencia".
- **Contraste primero**: el texto fuerte usa los grises 700–900; el naranja se reserva para elementos interactivos y acentos, validando siempre contraste (ver seccion 6).

---

## 3. Tokens de diseño

Los tokens son la capa fundacional. Se agrupan en familias; cada familia tiene un proposito unico. **Todo el CSS de los tokens vive en `tokens.css`** — aqui se documenta su significado y se embeben las tablas oficiales.

### Familias de tokens

| Familia | Prefijo | Para que sirve |
|---|---|---|
| Paleta primaria (marca) | `--color-primary-*` | Acciones primarias, foco, acentos, estados de marca. |
| Escala de grises | `--color-gray-*` | Texto, bordes, fondos, superficies (claro y oscuro). |
| Colores semanticos | `--color-success/warning/danger/info*` | Estados de exito, advertencia, error e informacion. |
| Superficie / Texto | `--color-bg/surface/text/border/ring/overlay` | Roles aplicados, con overrides para tema oscuro. |
| Tipografia | `--text-*`, `--font-weight-*`, `--leading-*`, `--tracking-*` | Escala modular, pesos, interlineados y tracking. |
| Espaciado | `--space-*` | Sistema de ritmo vertical/horizontal base 4px. |
| Radios | `--radius-*` | Curvatura de esquinas. |
| Sombras | `--shadow-*` | Elevacion. |
| Z-index | `--z-*` | Apilamiento predecible. |
| Transiciones | `--transition-*` | Duraciones estandar de animacion. |
| Focus | `--focus-ring-*` | Anillo de foco accesible. |

### Paleta primaria (marca — derivada de #FD531E)

| Token | HEX | Uso |
|---|---|---|
| `--color-primary-50` | `#FFF2ED` | Fondos muy suaves, hover de items de lista, badges tenues |
| `--color-primary-100` | `#FFE0D3` | Fondos de chips/tags de marca, estados seleccionados leves |
| `--color-primary-200` | `#FEC2A8` | Bordes suaves de elementos de marca, ilustraciones |
| `--color-primary-300` | `#FD9E76` | Estados deshabilitados de botones primarios, decoracion |
| `--color-primary-400` | `#FD7848` | Hover en tema oscuro, acentos secundarios |
| `--color-primary-500` | `#FD531E` | **Color de marca**. Botones primarios, links, foco, acentos |
| `--color-primary-600` | `#E84410` | Hover de accion primaria (tema claro) |
| `--color-primary-700` | `#BD350B` | Active/pressed de accion primaria |
| `--color-primary-800` | `#8F280A` | Texto de marca sobre fondos muy claros, alta densidad |
| `--color-primary-900` | `#5E1B08` | Maximo contraste de marca, sombreados profundos |

### Escala de grises (armonizada con #4A4A49)

| Token | HEX | Uso |
|---|---|---|
| `--color-gray-50` | `#FAFAF9` | Fondo de pagina (claro) |
| `--color-gray-100` | `#F4F4F3` | Superficie elevada / filas zebra |
| `--color-gray-200` | `#E7E7E5` | Bordes sutiles, divisores |
| `--color-gray-300` | `#D2D2CF` | Bordes fuertes, inputs en reposo |
| `--color-gray-400` | `#A8A8A5` | Placeholders, iconos desactivados |
| `--color-gray-500` | `#7C7C7A` | Texto secundario / muted |
| `--color-gray-600` | `#5E5E5C` | Texto terciario, iconos |
| `--color-gray-700` | `#4A4A49` | **Base neutra**. Texto fuerte, superficies oscuras, bordes fuertes |
| `--color-gray-800` | `#333332` | Superficie oscura (dark surface), headers |
| `--color-gray-900` | `#1C1C1B` | Texto principal (claro) / fondo de pagina (oscuro) |

### Colores semanticos

| Token | HEX | Uso |
|---|---|---|
| `--color-success` | `#1E9E5A` | Texto/icono/borde de exito |
| `--color-success-bg` | `#E5F6EC` | Fondo suave de alertas de exito |
| `--color-success-border` | `#A7E0BF` | Borde de banners de exito |
| `--color-warning` | `#E0900B` | Texto/icono de advertencia |
| `--color-warning-bg` | `#FDF3DF` | Fondo suave de advertencia |
| `--color-warning-border` | `#F4D58A` | Borde de banners de advertencia |
| `--color-danger` | `#DC362E` | Errores, acciones destructivas |
| `--color-danger-bg` | `#FCEAE9` | Fondo suave de error |
| `--color-danger-border` | `#F3B5B2` | Borde de banners de error |
| `--color-info` | `#1E6FE0` | Informacion neutra, tips |
| `--color-info-bg` | `#E8F0FD` | Fondo suave informativo |
| `--color-info-border` | `#AECBF6` | Borde de banners informativos |

### Superficie / Texto (tema claro -> overrides oscuro)

| Token | Claro | Oscuro | Uso |
|---|---|---|---|
| `--color-bg` | `#FAFAF9` | `#1C1C1B` | Fondo de pagina |
| `--color-surface` | `#FFFFFF` | `#333332` | Tarjetas, paneles, modales |
| `--color-surface-2` | `#F4F4F3` | `#2A2A29` | Superficie elevada / alterna |
| `--color-text` | `#1C1C1B` | `#FAFAF9` | Texto principal |
| `--color-text-muted` | `#7C7C7A` | `#A8A8A5` | Texto secundario |
| `--color-border` | `#E7E7E5` | `#3A3A39` | Bordes/divisores |
| `--color-border-strong` | `#D2D2CF` | `#5E5E5C` | Bordes de enfasis |
| `--color-ring` | `#FD531E` | `#FD7848` | Color del focus ring |
| `--color-overlay` | `rgba(28,28,27,.55)` | `rgba(0,0,0,.65)` | Scrim de modal/drawer |

### Tipografia — escala de tamaños (base 16px, ratio ~1.2)

| Token | rem | px | Uso |
|---|---|---|---|
| `--text-xs` | 0.75 | 12 | Captions, labels, metadatos |
| `--text-sm` | 0.875 | 14 | Texto secundario, tablas densas |
| `--text-base` | 1 | 16 | Cuerpo por defecto |
| `--text-md` | 1.125 | 18 | Cuerpo destacado, subtitulos |
| `--text-lg` | 1.25 | 20 | Titulos de tarjeta (h5/h4) |
| `--text-xl` | 1.5 | 24 | Encabezado de seccion (h3) |
| `--text-2xl` | 1.875 | 30 | Titulo de pagina (h2) |
| `--text-3xl` | 2.25 | 36 | Titulo principal (h1) |
| `--text-4xl` | 3 | 48 | Hero / display |

| Pesos | Valor | | Line-heights | Valor | | Letter-spacing | Valor |
|---|---|---|---|---|---|---|---|
| `--font-weight-regular` | 400 | | `--leading-none` | 1 | | `--tracking-tighter` | -0.05em |
| `--font-weight-medium` | 500 | | `--leading-tight` | 1.25 | | `--tracking-tight` | -0.025em |
| `--font-weight-semibold` | 600 | | `--leading-snug` | 1.375 | | `--tracking-normal` | 0 |
| `--font-weight-bold` | 700 | | `--leading-normal` | 1.5 | | `--tracking-wide` | 0.025em |
| | | | `--leading-relaxed` | 1.625 | | `--tracking-wider` | 0.05em |

> **Familia tipografica**: `Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif` para UI; `ui-monospace, SFMono-Regular, Menlo, monospace` para codigo/plantillas.

### Espaciado (base 4px)

| Token | rem | px | | Token | rem | px |
|---|---|---|---|---|---|---|
| `--space-0` | 0 | 0 | | `--space-7` | 2 | 32 |
| `--space-1` | 0.25 | 4 | | `--space-8` | 2.5 | 40 |
| `--space-2` | 0.5 | 8 | | `--space-9` | 3 | 48 |
| `--space-3` | 0.75 | 12 | | `--space-10` | 4 | 64 |
| `--space-4` | 1 | 16 | | `--space-11` | 5 | 80 |
| `--space-5` | 1.25 | 20 | | `--space-12` | 6 | 96 |
| `--space-6` | 1.5 | 24 | | | | |

### Radios / Sombras / Z-index / Transiciones / Focus

| Radio | Valor | | Sombra | Elevacion |
|---|---|---|---|---|
| `--radius-sm` | 4px | | `--shadow-sm` | Bordes, inputs, divisores elevados |
| `--radius-md` | 8px | | `--shadow-md` | Tarjetas, dropdowns |
| `--radius-lg` | 12px | | `--shadow-lg` | Popovers, menus flotantes |
| `--radius-xl` | 16px | | `--shadow-xl` | Modales, drawers |
| `--radius-full` | 9999px | | | |

| Z-index | Valor | | Transicion | Valor | | Focus | Valor |
|---|---|---|---|---|---|---|---|
| `--z-base` | 0 | | `--transition-fast` | 120ms | | `--focus-ring-width` | 2px |
| `--z-dropdown` | 1000 | | `--transition-base` | 200ms | | `--focus-ring-offset` | 2px |
| `--z-sticky` | 1100 | | `--transition-slow` | 320ms | | `--focus-ring-color` | var(--color-ring) |
| `--z-modal` | 1300 | | | | | `--focus-ring` | doble box-shadow |
| `--z-toast` | 1400 | | | | | | |

> **Implementacion**: estos tokens se declaran una sola vez en `tokens.css` (en `:root` para tema claro y `[data-theme="dark"]` / `@media (prefers-color-scheme: dark)` para los overrides oscuros). Los componentes **nunca** redefinen estos valores; solo los consumen via `var(--token)`.

---

## 4. Jerarquia de componentes (Atomic Design)

La jerarquia se deriva del analisis del panel actual. La columna izquierda es el **estado actual** (alias internos monoliticos del CSS embebido); la derecha, la **direccion del sistema Replica** sobre tokens nuevos.

### 4.1 Atomos

Piezas indivisibles. No tienen sentido por si solas pero son la base de todo.

- **Tokens de color (actuales):** `--bg`, `--bg2`, `--card`, `--card2`, `--elev`, `--bd`, `--bd2`, `--tx`, `--tx2`, `--mut`, `--mut2`, `--ac`, `--ac-h`, `--ac2`, `--ok`, `--warn`, `--bad`, `--info`, `--ac-soft`, `--ac2-soft`, `--ok-soft`, `--warn-soft`, `--bad-soft`, `--tx3`.
  → **Mapeo a Replica:** colapsan a los roles `--color-bg`, `--color-surface`, `--color-surface-2`, `--color-border`, `--color-border-strong`, `--color-text`, `--color-text-muted`, `--color-primary-*`, semanticos y `*-bg` suaves.
- **Tokens de espaciado/radio (actuales):** `--r` (14px), `--r-sm` (10px), `--r-lg` (18px), `--fs` (13px), `--sp-xs` (4px), `--sp-sm` (6px), `--sp-md` (12px), `--sp-lg` (18px), `--sp-xl` (24px), `--sp-2xl` (32px, propuesto, no presente).
  → **Mapeo:** `--space-*` (escala 4px) y `--radius-sm/md/lg/xl/full`.
- **Tokens de sombra (actuales):** `--sh`, `--sh-sm`, `--ring` → `--shadow-md/sm` y `--focus-ring`.
- **Tipografia (actual):** Inter / system-ui; pesos 400/600/700/800; tamaños 11px–30px **sin escala modular** → escala `--text-*` con ratio 1.2 y pesos `--font-weight-*`.
- **Transiciones (actual):** `.15s`, `.12s`, `.2s`, `.25s` **sin duracion estandar** → `--transition-fast/base/slow`.
- **Bordes/radios (actual):** rounded (`--r`, `--r-sm`, `--r-lg`), full (`999px`), sharp (`0px`), custom (`11px/16px/20px` hardcodeados) → tokens `--radius-*` (los custom se normalizan a la escala).

> Otros atomos puros del sistema: **Icono**, **Spinner**, **Avatar/Logo (brand)**, **Divider**.

### 4.2 Moleculas

Grupos pequeños de atomos que funcionan como una unidad.

- **Input group:** label + input/textarea/select (hoy sin wrapper `.input-group` explicito) → `Field`.
- **Form row:** `.row` (2 columnas iguales) que contiene varios inputs → `.form-row`.
- **Button group:** botones `.sec`/`.ghost` + primario, en flex-wrap → grupo de `Button`.
- **Badge / Pill:** `.pill` (base) + `.active/.inactive/.queued/.sending/.done/.partial/.failed` (estado) → `Badge` semantico.
- **Stat card:** `.stat` (`b` + `span`, con stripe `::after`) → `StatTile`.
- **Table row:** `th`/`td` con padding, hover y zebra (`tr:nth-child(even)`) → fila de `Table`.
- **Toast notification:** `.toast` (fixed, `::before` icono, estado `.show`, barra `.toast.show::after`) → `Toast`.
- **Callout banner:** `.callout` (flex, border-left, `::before` icono, variantes warn/danger/ok) → `Callout`/`Alert`.
- **Empty-state:** `.empty-state` (`.ico` + h3 + p, centrado) → `Empty-state`.
- **Skeleton loader:** `.skeleton > .sk-line` (shimmer, variantes `.lg/.sm`) → `Skeleton`.
- **Channel chip:** `.chan` (`.tg/.wa`, estado `.on`, sub-elemento `.dot`) → `Badge` de canal / `Toggle` de canal.
- **Live indicator:** `.live` (sub-elemento `.ping`, estado `.on` animado) → indicador "en vivo" (Badge con dot pulsante).
- **Progress bar:** `.bar` + `.bar>i` (width dinamico, variantes `.wa/.full/.err`) → `Progress bar`.

### 4.3 Organismos

Secciones complejas y autonomas de interfaz, compuestas por moleculas y atomos.

- **Login form:** `#login > .box` (card contenedor, brand, inputs, boton).
- **App header:** `header` (brand + badges + usuario + boton logout).
- **Tab navigation:** `.nav` (sticky, botones `data-tab`, estado `.on`, indicador `::after`).
- **Card container:** `.card` (base + variante `.accent`, titulo h2, slot de contenido).
- **Markup widget:** `.markup` (flex, input grande + descripcion).
- **Destinatarios picker:** `.pickbox` + `.pickitem` (lista scrollable con checkboxes).
- **Listas de distribucion:** divs generados por JS con hint badges.
- **Tabla de suscriptores:** `table` (thead + tbody con filas dinamicas, botones de paginacion).
- **Tabla de auditoria:** `table` (cuando/usuario/accion/detalle).
- **Tabla de envios (broadcasts):** `table` (columna mensaje, estado en pill, progreso `.bar`).
- **Composicion y envio:** card con textarea + file input + selector de canal + preview + datetime + boton enviar.
- **Stats dashboard:** `.stats` (grid de 4 tiles `.stat` con cifras grandes).
- **Queue/DLQ panel:** dos `.stats` (programados / SQS / DLQ) + botones de accion.

### 4.4 Plantillas

Estructuras de pagina (layout) que organizan organismos. Definen el esqueleto sin datos reales.

- **Inicio (dashboard):** `.card` accent de resumen + `.stats` KPIs + `.card` de pasos guiados + acciones.
- **Configuracion (mensaje):** varias `.card` apiladas (markup, canal, mensaje de prueba, imagen).
- **Telegram:** `.card` con select de modo bot/userbot + seccion de labels + inputs + badge de estado + botones verify/webhook.
- **WhatsApp:** `.card` con checkbox enable + inputs + botones estado/status/qr/pair + callout de advertencia + suscriptores + listas.
- **Programacion:** `.card` accent con switch de envio + `.card` anti-baneo (batch + delays con callout) + `.card` de ventana (horario) + `.card` de planes con indicador `.live`.
- **Estado:** `.card` cola/stats + `.card` lista DLQ + `.card` tabla de auditoria + botones de refresh.
- **Enviar:** `.card` de composicion (textarea, imagen, canales, preview, datetime, schedule) + `.card` tabla de broadcasts + `.live` polling.

### 4.5 Paginas

Instancias concretas de plantillas con contenido real y comportamiento.

- **`#login` (publica, sin auth):** flex fullscreen, `.box` centrada de 372px, brand + inputs usuario/contraseña + slot de error + boton.
- **`#app` (privada, autenticada):** header sticky + `.nav` sticky + `main` con grid de cards por pestaña (inicio/msg/telegram/whatsapp/prog/estado/enviar).

---

## 5. Catalogo de componentes

Indice de referencia con **anatomia, props, variantes y estados**. El HTML/CSS de cada componente vive en `components.css` y se muestra vivo en `preview.html`. Convencion de clases: BEM (`bloque__elemento--modificador`).

### Botones (Buttons)

- **Button** — *Variantes:* `primary` (accion principal, fondo solido de marca `--color-primary` sobre `--color-on-primary`), `secondary` (superficie con borde `--color-surface`/`--color-border-strong`), `ghost`/outline (sin relleno, solo borde sutil y texto atenuado; util en toolbars), `danger` (fondo `--color-danger` sobre blanco), `link` (apariencia de hipervinculo, sin fondo ni borde, subrayado al hover; respeta el alto del tamaño para alinear). *Estados:* default, hover, active (`translateY(1px)`), focus-visible (anillo `--focus-ring`, sin outline nativo), disabled (opacidad reducida, sin puntero/transform), loading (spinner visible, contenido transparente, `aria-busy`, no interactivo). *Props:* `variant`, `size`, `type`, `disabled`, `loading`, `block`, `iconStart`, `iconEnd`, `aria-label`.
- **IconButton** — *Variantes:* primary, secondary, ghost, danger; forma cuadrada (radio del tamaño) o circular (`.btn--icon-round`). *Estados:* default, hover, active, focus-visible, disabled, loading. *Props:* `variant`, `size`, `aria-label`, `type`, `disabled`, `loading`, `round`.

### Formularios

- **Label** — *Variantes:* default, `--md` (mayor tamaño para formularios espaciados), con required, con optional. *Estados:* default, disabled (hereda de `.field--disabled`). *Props:* `for`, `required`, `size`.
- **Text input** — *Variantes:* `--sm`, base, `--lg`, dentro de `.input-group` con prefijo/sufijo. *Estados:* default, hover, focus, filled, disabled, error. *Props:* `type`, `size`, `disabled`, `aria-invalid`, `placeholder`, `aria-describedby`.
- **Textarea** — *Variantes:* base (resize vertical), `--fixed` (sin resize), `--mono` (monospace, util para plantillas/patrones). *Estados:* default, hover, focus, filled, disabled, error. *Props:* `rows`, `resize`, `mono`, `disabled`, `aria-invalid`.
- **Select** — *Variantes:* `--sm`, base, `--lg`. *Estados:* default, hover, focus, filled, disabled, error. *Props:* `size`, `disabled`, `aria-invalid`, `multiple`.
- **Checkbox** — *Variantes:* nativo (`accent-color` de marca), `--custom` (caja estilizada con check SVG). *Estados:* default, hover, focus, checked, indeterminate, disabled. *Props:* `checked`, `indeterminate`, `disabled`, `variant`.
- **Radio** — *Variantes:* nativo (`accent-color` de marca), `--custom` (circulo + punto), grupo vertical (`.radio-group`), grupo horizontal (`.radio-group--inline`). *Estados:* default, hover, focus, checked, disabled. *Props:* `name`, `value`, `checked`, `disabled`, `variant`.
- **Toggle / Switch** — *Variantes:* `--sm`, base, `--lg`, con etiqueta de texto. *Estados:* default (off), checked (on), hover, focus, disabled. *Props:* `checked`, `disabled`, `size`, `role`.
- **Hint / Help** — *Variantes:* default (muted), `--info` (acento informativo), con `<code>`. *Estados:* default, disabled (hereda de `.field--disabled`). *Props:* `id`, `tone`.
- **Mensaje de error** — *Variantes:* default (error), con icono. *Estados:* oculto (sin mensaje), visible (error activo). *Props:* `id`, `role`, `visible`.
- **Field (campo / form group)** — *Variantes:* stack vertical (base), `--inline`, dentro de `.form-row` (2 columnas), dentro de `.fieldset` (seccion con leyenda). *Estados:* default, error, disabled. *Props:* `layout`, `state`.

### Tarjetas (Cards)

- **Card** — *Variantes:* `card` (base: superficie, borde sutil, sombra sm, radio lg), `card--outlined` (borde definido sin sombra), `card--elevated` (sin borde, sombra md), `card--raised` (sombra lg con hover xl), `card--flat` (plana), `card--ghost` (transparente), `card--accent` / `--accent-success` / `--accent-warning` / `--accent-danger` / `--accent-info` (banda superior de color), `card--interactive` (clicable con feedback), `card--compact` / `--cozy` / `--spacious` (densidad). *Estados:* default, hover (solo si `--interactive`/`--raised`), active (se hunde 1px si interactiva), focus-visible (anillo, solo si interactiva), disabled/`aria-disabled` (opacidad reducida, sin puntero). *Props:* variante de borde/elevacion, acento, densidad/padding, interactiva, `aria-disabled`.
- **StatTile (KPI)** — *Variantes:* `stat-tile` (base con acento primario), `stat-tile--primary` / `--success` / `--warning` / `--danger` / `--info`, `stat-tile--center` / `--start` (alineacion), `stat-tile--loading` (skeleton), contenedores `stat-grid` / `--2` / `--3` / `--4`. *Estados:* default, loading (skeleton en el valor), delta positivo (up, verde), delta negativo (down, rojo), hover (realce de borde si el grid es interactivo). *Props:* tono del delta, acento del tile, alineacion, loading, columnas del grid.
- **Callout (Banner informativo)** — *Variantes:* `callout--info` / `--success` / `--warning` / `--danger` / `--neutral`, `callout--solid` (fondo saturado), `callout--dismissible`, `callout--no-icon`. *Estados:* default, con acciones, dismissible (boton de cierre con hover/focus). *Props:* tono, role/aria, dismissible, enfasis, con icono.

### Modales

- **Modal / Dialog** — *Variantes:* por tamaño `.modal__dialog--sm` (max 420px, confirmaciones), `--md` (560px, default), `--lg` (760px, formularios densos), `--full` (casi pantalla completa); por tono `.modal__dialog--danger` / `--success` / `--warning` / `--info` (color de icono y titulo); `role="alertdialog"` + `data-dismissible="false"` para confirmaciones criticas; sin footer (solo body) o sin header (autocontenido). *Estados:* cerrado (`display:none`, fuera del tab order), abierto (`data-open`: scrim opaco + animacion de entrada escala+fade), scroll-body (header/footer fijos), focus-trap activo (gestionado por JS del consumidor), hover/active/focus-visible en `.modal__close` y botones del footer. *Props:* tamaño, tono, abierto, dismissible, role, `aria-modal`, `aria-labelledby`, `aria-describedby`.
- **Modal Trigger (disparador)** — *Variantes:* cualquier variante de `Button` del DS; trigger de cierre interno via `data-modal-close` (en `.modal__close`, footer o scrim). *Estados:* reposo (`aria-expanded="false"`), modal abierto (`aria-expanded="true"`), al cerrar el foco se restaura sobre el trigger. *Props:* `aria-haspopup`, `aria-controls`, `aria-expanded`, `data-modal-open`.
- **Drawer (panel lateral)** — *Variantes:* por lado `.drawer__panel--right` (default), `--left`, `--top`, `--bottom`; por tamaño `--sm` (320px), `--md` (400px), `--lg` (520px) para left/right (altura para top/bottom); sin footer (navegacion/filtros) o con footer de acciones. *Estados:* cerrado (overlay `display:none`), abierto (`data-open`: scrim + panel deslizante), scroll-body (header/footer fijos), focus-trap + cierre por Escape/scrim (si dismissible), hover/active/focus-visible en `.drawer__close` y acciones. *Props:* lado, tamaño, abierto, dismissible, role, `aria-modal`, `aria-labelledby`.

### Feedback

- **Badge / Tag** — *Variantes:* semanticas `badge--neutral` / `--primary` / `--info` / `--success` / `--warning` / `--danger`; apariencia `badge--soft` (default), `--solid`, `--outline`; forma/tamaño `badge--pill`, `--sm`; compuesta de estado (cola de envios): pill + dot + variante (encolado=info, enviando=primary, completado=success, parcial=warning, fallido=danger). *Estados:* default, with-dot (estado en vivo), removable (hover/active/focus-visible en `.badge__remove`), disabled (sobre la "x"). *Props:* `variant`, `appearance`, `size`, `pill`, `dot`, `removable`.
- **Toast / Notification** — *Variantes:* `toast--info` / `--success` / `--warning` / `--danger`; region `toast-region--bottom-end` (default), `--top-end`, `--bottom-start`, `--top-start`. *Estados:* hidden (opacity 0, desplazado), is-open (entrada animada), is-leaving (salida animada), hover sobre `.toast__close` (focus-visible/active) y `[disabled]`. *Props:* `variant`, `placement`, `open`, `dismissible`, `autoDismiss`, `role`.
- **Alert / Callout** — *Variantes:* `alert--info` / `--success` / `--warning` / `--danger`; enfasis `alert--subtle` (default), `--solid`; layout `alert--banner`. *Estados:* default, dismissible (hover/active/focus-visible en `.alert__close`, `[disabled]`), with-actions. *Props:* `variant`, `emphasis`, `dismissible`, `hasIcon`, `banner`, `role`.
- **Skeleton loader** — *Variantes:* `skeleton--text`, `--title`, `--circle`, `--rect`, `--thumb`, `--static` (sin shimmer); composiciones `.skeleton-group`, patron de card y de fila de tabla. *Estados:* loading (shimmer activo), reduced-motion (shimmer desactivado por `prefers-reduced-motion`). *Props:* `shape`, `lines`, `width`, `animated`, `rounded`.
- **Empty-state** — *Variantes:* `empty--compact`, `--lg`, `--inline`, `--danger` (vacio por error). *Estados:* default (sin datos), error (tono danger: fallo de carga), interactive (CTAs con hover/active/focus-visible y `[disabled]`). *Props:* `size`, `context`, `tone`, `hasMedia`, `actions`.

### Navegacion

- **Tabs / Segmented nav (ds-tabs)** — *Variantes:* `underline` (barra inferior, default), `segmented` (pastilla/fondo activo), `sm`/`md`/`lg` (densidad), `wrap` (multifila) vs scroll horizontal (default), `start`/`center`/`stretch` (alineacion). *Estados:* default (`text-muted`), hover (texto mas oscuro + fondo `surface-2`), active/pressed (`:active`, leve reduccion), selected (`--active`: color primario + indicador visible), focus-visible (focus ring), disabled. *Props:* `variant`, `size`, `wrap`, `align`, `role`, `aria-selected`, `aria-controls`, `disabled`.
- **App header / Barra superior (ds-appbar)** — *Variantes:* `solid` (surface opaco), `blur` (translucido + backdrop-filter), `bordered` (borde inferior, default); badge `success`/`warning`/`danger`/`info`/`neutral`. *Estados:* default, scrolled (`ds-appbar--scrolled` intensifica sombra/borde), badge con punto pulsante (`ds-appbar__badge--live`), menu-btn hover/active/focus-visible/`aria-expanded=true`. *Props:* `variant`, `sticky`, badge variant, `role`, `aria-expanded`.
- **Breadcrumb (ds-breadcrumb)** — *Variantes:* `sm` (compacto, default), `md` (mas espaciado), con icono inicial (`ds-breadcrumb__icon`), colapsado con elipsis (`ds-breadcrumb__item--ellipsis`). *Estados:* link default (`text-muted`), hover (primario + subrayado), active (`:active`), focus-visible (focus ring), current (color text, no clicable, medium). *Props:* `separator`, `size`, `aria-current`, `aria-label`.

### Datos

- **Table** — *Variantes:* `table` (base, solo lineas inferiores), `table--zebra` (alternas con `--color-surface-2`), `table--bordered`, `table--hover`, `table--compact`, `table--sticky` (thead fijo). *Estados:* default, row:hover (solo con `--hover`), `th[aria-sort]` (asc/desc), `th:focus-visible` (encabezado ordenable por teclado), loading (Spinner inline), empty (`.table__empty`), selected (`.is-selected`). *Props:* `variant`, `hover`, `stickyHeader`, `density`, `cell-align`, `sort`.
- **Progress bar** — *Variantes:* `progress` (base, primario), `progress--telegram` / `--whatsapp` (color por canal), `progress--success` / `--warning` / `--danger` / `--info`, `progress--sm` / `--md` / `--lg`, `progress--striped`, `progress--indeterminate`. *Estados:* default (0–100 estable), in-progress (animacion de width), indeterminate (barra deslizante), complete (100%, `.is-complete` → success), error (`.is-error` → danger), disabled (`[aria-disabled=true]`). *Props:* `value`, `channel`, `tone`, `size`, `indeterminate`, `striped`, `showLabel`.
- **Spinner / loader inline** — *Variantes:* `spinner--xs` / `--sm` / `--md` / `--lg`, `spinner--primary` / `--muted` / `--on-primary` / `--current`, `spinner-inline` (spinner + texto). *Estados:* spinning (girando), en boton (`.btn--loading` reutiliza el atomo y oculta el contenido), reduced-motion (estatico). *Props:* `size`, `tone`, `thickness`, `label`, `inline`.

---

## 6. Guia de uso y convenciones

### Convencion de nombres (BEM)

`bloque__elemento--modificador`, en minusculas con guiones.

- **Bloque**: el componente raiz. `.card`, `.btn`, `.modal`, `.badge`.
- **Elemento**: parte interna que no vive sin su bloque. `.card__title`, `.modal__close`, `.badge__remove`.
- **Modificador**: variante o estado declarativo. `.btn--primary`, `.card--accent-danger`, `.table--zebra`.
- **Estados dinamicos (JS)**: prefijo `is-`/`has-` o atributos. `.is-open`, `.is-selected`, `data-open`, `aria-busy`.
- Prohibido: selectores de etiqueta global con estilos de marca (p. ej. estilizar todos los `button`/`input` directamente). Siempre via clase de componente.

### Accesibilidad (no negociable)

- **Foco visible**: todo elemento interactivo usa `:focus-visible` con el `--focus-ring` (doble box-shadow, `--focus-ring-width` 2px + `--focus-ring-offset` 2px, color `--color-ring`). Nunca `outline:none` sin reemplazo.
- **Contraste**: texto normal ≥ 4.5:1, texto grande/iconos ≥ 3:1 (WCAG AA). El naranja `#FD531E` sobre blanco es valido para elementos grandes/iconos y para texto blanco encima de el; para texto pequeño de marca sobre fondo claro usar `--color-primary-700/800`.
- **Roles y aria**: modales `role="dialog"`/`alertdialog` + `aria-modal` + `aria-labelledby`/`aria-describedby`; toasts en region con `role="status"`/`alert`; tabs con `role="tab"`/`tablist` + `aria-selected` + `aria-controls`; tablas ordenables con `aria-sort`; botones de icono con `aria-label`; estados de carga con `aria-busy`.
- **Teclado**: focus-trap en modales/drawers, cierre por `Escape`, restauracion del foco al trigger al cerrar. Orden de tabulacion logico.
- **Movimiento**: respetar `prefers-reduced-motion` (skeleton, spinner, transiciones largas se desactivan o reducen).
- **Targets**: area clicable minima ~40px en controles interactivos densos.

### Tema claro / oscuro

- Los componentes consumen **solo roles** (`--color-bg`, `--color-surface`, `--color-text`, `--color-border`, `--color-ring`, etc.), nunca HEX directos. Cambiar de tema = cambiar los valores de los roles en `tokens.css`.
- Activacion: `[data-theme="dark"]` en `<html>` y/o `@media (prefers-color-scheme: dark)`. El ring pasa de `#FD531E` a `#FD7848` para mejor contraste en oscuro; el overlay se oscurece.
- Regla: si un componente "necesita" un color que no existe como rol, primero se añade el token; no se hardcodea.

---

## 7. Mapa de refactor (monolito actual → componente del sistema)

El panel actual concentra todo el CSS embebido en `src/lambda/entrypoints/admin.py` (tema indigo/cian, valores hardcodeados). Esta tabla guia la migracion pieza por pieza hacia Replica. Las lineas referencian la posicion en ese archivo.

| Patron monolitico actual | Lineas (CSS/HTML) | Componente Replica | Notas de migracion |
|---|---|---|---|
| Botones primarios / secundarios / fantasma | CSS 625–638; HTML esparcido | **Button** (`--primary`/`--secondary`/`--ghost`/`--danger`) | Reemplazar `button`, `.sec`, `.ghost` por clases BEM; estilo en `components.css`, no global. |
| Input / Textarea / Select | CSS 605–622; HTML disperso | **Text input / Textarea / Select** | Envolver en `Field`; quitar estilos globales sobre `input,textarea,select`. |
| Label / Hint / Error | CSS 604, 645–648; HTML extenso | **Label / Hint / Mensaje de error** | Integrar en `Field` con `aria-describedby` para hint/error. |
| Tarjeta (Card) | CSS 593–596 | **Card** (+ `--accent*`) | `.card` base + variantes de acento/densidad. |
| Tab / Nav (pestañas) | CSS 575–590, 910–914 | **Tabs (ds-tabs `segmented`)** + **App header (ds-appbar `blur`)** | `data-tab` → roles `tab`/`tablist` + `aria-selected`. |
| Badge / Pill de estado | CSS 659–667; HTML amplio | **Badge** (pill + dot semantico) | Mapear `.active/.inactive/.queued/.sending/.done/.partial/.failed` a `--success/--warning/--info/--primary/--danger`. |
| Stat tile / KPI | CSS 670–674 | **StatTile** (+ `stat-grid--4`) | `.stat` + stripe → `stat-tile` con acento token. |
| Tabla (suscriptores, auditoria, broadcasts) | CSS 651–656; HTML amplio | **Table** (`--zebra`/`--hover`/`--sticky`) | Zebra y hover por modificador, no por `:nth-child` global. |
| Markup widget (porcentaje) | CSS 641–642; HTML 1025–1028 | **Field** + **Text input `--lg`** (numero) | Componer dentro de Card; cifra grande via tipografia tokenizada. |
| Canales toggleables (TG/WA chips) | CSS 705–717; HTML 1202–1203 y otros | **Badge de canal / Toggle** | `.chan.tg/.wa` + `.on` → variante + estado `checked`; `.dot` → `badge__dot`. |
| Selector de listas (pickbox/pickitem) | CSS 700–703; varias | **Card** + lista de **Checkbox** (Field) | `.pickbox` scrollable como contenedor; `.pickitem` como fila con checkbox. |
| Barra de progreso | CSS 745–749; HTML por JS | **Progress bar** (`--telegram`/`--whatsapp`/`--danger`) | `.bar>i` width → `value`; variantes `.wa/.full/.err` a tokens de canal/semantica. |
| Indicador "en vivo" (polling) | CSS 751–755; `#bc_live`, `#pl_live` | **Badge with-dot (live)** | `.ping` pulsante → `badge__dot` animado (respeta reduced-motion). |
| Toast / Notificacion | CSS 677–685, 890–905 | **Toast** | `.show` → `is-open`; barra `::after` → autoDismiss; region `bottom-end`. |
| Callout / Banner de advertencia | CSS 821–842; HTML 1080, 1267 | **Callout / Alert** | `border-left` + `::before` icono → variantes `--warning/--danger/--success`. |
| Empty state / Skeleton | CSS 848–874; HTML `bc_empty`, `pl_empty`, `subsempty` | **Empty-state** + **Skeleton** | `.sk-line` → `skeleton--text`; `.empty-state` → `empty` con `hasMedia/actions`. |
| Boton con carga / estado OK | CSS 879–887 | **Button** estado `loading` + **Spinner** | Contenido transparente + `aria-busy`; OK transitorio via Toast. |
| Contador de caracteres | CSS 696, 969–970; HTML `#bc_count` | **Hint** (tono dinamico) | Texto muted que pasa a `--warning/--danger` al acercarse al limite. |
| Refinamientos globales (border-left, padding, transitions) | CSS 791–805, 815–842, 966–977 | **Tokens** (`--space-*`, `--transition-*`, `--radius-*`) | Eliminar fixes puntuales; normalizar a tokens. |
| Login | CSS 550–561; HTML `#login` | **Card** + **Field** + **Button** (pagina publica) | `.box` → `card--elevated`; barra superior → `card--accent`. |
| Header / Navigation bar | CSS 565–572; HTML `header` | **App header (ds-appbar)** | `blur` + badges semanticos + menu-btn accesible. |
| Main layout / Responsivo | CSS 573, 757–766 | **Plantillas** (grid de Cards) | `main` grid con `--space-*`; breakpoints documentados en `components.css`. |
| Row (grid 2 columnas) | CSS 623 | **form-row** | `.row` → `.form-row` (2 cols, colapsa en movil). |
| Brand / Logo | CSS 538–548; HTML (SVGs) | **Atomo Brand/Logo** | Wordmark con tipografia tokenizada; el gradiente indigo/cian se sustituye por marca naranja/gris. |

---

## 8. Proximos pasos

La fase actual deja **definidos** los tokens (`tokens.css`), el catalogo y el mapa de refactor. La siguiente fase es de **composicion / refactor de fragmentos** y depende de los fragmentos de markup que aporte el usuario.

1. **Crear `tokens.css`** con todas las tablas de la seccion 3 (claro + overrides oscuro) como unica fuente de verdad.
2. **Implementar `components.css`** atomo por atomo, en el orden del catalogo (Botones → Formularios → Cards → Feedback → Navegacion → Datos → Modales), validando contra `preview.html`.
3. **Construir `preview.html`** como catalogo vivo: cada componente en todos sus estados (incluido focus, disabled, loading, error, dark).
4. **Migrar el panel por organismos**, siguiendo el mapa de refactor de la seccion 7: empezar por los de mayor reuso (Button, Field, Card, Table, Badge) y luego los compuestos (login, header/nav, dashboards, tablas, composicion/envio).
5. **Recibir e integrar los fragmentos del usuario**: mapear cada fragmento entrante al componente correspondiente del catalogo, extraer cualquier valor hardcodeado a token, y registrar la equivalencia en el mapa de refactor.
6. **Verificar accesibilidad y contraste** de cada componente migrado (focus-visible, aria, AA) y el comportamiento en tema oscuro.
7. **Eliminar el CSS monolitico** de `admin.py` a medida que cada patron quede cubierto por `components.css`, evitando duplicidad durante la transicion.

> Pendiente de los fragmentos del usuario para la fase de composicion: hasta entonces, el catalogo y los tokens son estables y pueden adoptarse en pantallas nuevas.

---

## Archivos de este sistema

- [`tokens.css`](./tokens.css) — variables CSS (fuente de verdad de los valores).
- [`components.css`](./components.css) — estilos de los componentes (importa tokens.css).
- [`preview.html`](./preview.html) — galeria abrible en el navegador para *ver* los componentes.
