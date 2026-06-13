# Guia de Marca — IntegraTech

> **Documento maestro de marca.** Esta es la fuente de verdad de la identidad de IntegraTech: nombre, mision, voz, publico, identidad visual (logo, color, tipografia) y reglas de uso de los assets. Si hay conflicto entre este documento y cualquier pieza suelta, **manda este documento**.
>
> **Assets oficiales** (no se reproduce el codigo aqui; referenciar siempre estos archivos):
> - `logo.svg` — logo completo (isotipo + wordmark "IntegraTech").
> - `isotipo.svg` — solo el isotipo (dos nodos enlazados por el trazo en "S").
> - `banner-lista.svg` — banner 1080x540 para encabezar envios de listas de precios.
> - `tokens.css` — variables de diseño `--it-color-*`, `--it-font-*`, `--it-radius-*` (fuente de verdad de los valores de color, tipografia y radios).

---

## Indice

1. [El nombre: razon y significado](#1-el-nombre-razon-y-significado)
2. [Mision y posicionamiento](#2-mision-y-posicionamiento)
3. [Propuesta de valor](#3-propuesta-de-valor)
4. [Voz y tono](#4-voz-y-tono)
5. [Publico objetivo](#5-publico-objetivo)
6. [Taglines](#6-taglines)
7. [Identidad visual: logo](#7-identidad-visual-logo)
8. [Color](#8-color)
9. [Tipografia](#9-tipografia)
10. [Forma: radios y geometria](#10-forma-radios-y-geometria)
11. [Aplicacion: donde y como usar la marca](#11-aplicacion-donde-y-como-usar-la-marca)
12. [Do / Dont](#12-do--dont)
13. [Banner de lista de precios](#13-banner-de-lista-de-precios)
14. [Como exportar los SVG a PNG](#14-como-exportar-los-svg-a-png)
15. [Inventario de assets y gobernanza](#15-inventario-de-assets-y-gobernanza)

---

## 1. El nombre: razon y significado

**IntegraTech = Integra + Tech.**

- **Integra** evoca tres ideas que son el corazon del negocio:
  1. **Integrar** sistemas y canales que antes vivian aislados (un canal de origen, Telegram, WhatsApp, contactos, listas de precios).
  2. **Integral / completo**: una solucion de punta a punta, no un parche.
  3. **Integridad**: los datos llegan intactos, fieles al original y sin perdidas.
- **Tech** ancla la marca en lo tecnologico: automatizacion, ingenieria y plataformas confiables.

> En una frase: *IntegraTech conecta lo que estaba separado y lo automatiza con tecnologia confiable.*

**Escritura correcta:** una sola palabra, **CamelCase**: `IntegraTech`. Nunca "Integra Tech", "integratech", "INTEGRATECH" (salvo versalitas tipograficas deliberadas) ni "Integratech". En contextos de codigo/dominio/usuario donde no se admiten mayusculas intermedias, usar todo minuscula: `integratech`.

---

## 2. Mision y posicionamiento

**Mision**
Eliminar el trabajo manual y repetitivo de mover informacion entre canales, conectando plataformas de mensajeria y automatizacion para que la informacion correcta llegue a la persona correcta, al instante y sin esfuerzo.

**Posicionamiento**
La casa de marca de **plataformas de automatizacion y mensajeria** que integran canales (Telegram, WhatsApp y mas) bajo una sola capa confiable.

- **Categoria:** automatizacion de mensajeria y distribucion de informacion multicanal.
- **Para quien:** equipos y negocios que dependen de reenviar informacion (precios, catalogos, avisos) de forma rapida y sin errores.
- **Frente a la competencia** (bots sueltos, reenvio manual, integraciones a medida): IntegraTech ofrece una solucion **integrada, automatica y confiable**, no scripts fragiles ni copia-pega manual.
- **Frase de posicionamiento:** *Cuando la informacion tiene que moverse sola, IntegraTech la mueve bien.*

---

## 3. Propuesta de valor

**Promesa central:** *Captura una vez, distribuye en todos lados — automatico y sin errores.*

Pilares de valor:

| Pilar | Que resuelve | Beneficio |
|---|---|---|
| **Automatizacion real** | Elimina el reenvio manual de listas/mensajes | Ahorro de tiempo y cero tareas repetitivas |
| **Multicanal nativo** | Telegram + WhatsApp desde un solo flujo | Llega a todos sin duplicar esfuerzo |
| **Fidelidad de datos** | Reenvia la informacion intacta y al instante | Sin errores de transcripcion ni demoras |
| **Confiabilidad operativa** | Plataforma que corre sola, 24/7 | Tranquilidad: funciona aunque no estes mirando |
| **Listo para escalar** | De un caso de uso a muchos | Crece contigo sin rehacer todo |

**Pitch de un parrafo:**
IntegraTech captura la informacion en su origen y la distribuye automaticamente por Telegram, WhatsApp y otros canales — fiel, instantanea y sin intervencion manual. Lo que antes era copiar, pegar y reenviar a mano, ahora simplemente sucede.

---

## 4. Voz y tono

**Personalidad de marca:** experta, directa y tranquilizadora. Habla como un ingeniero que te resuelve el problema sin marearte con jerga.

**Principios de voz:**
- **Clara antes que ingeniosa.** Frases cortas, beneficios concretos.
- **Confiable, no grandilocuente.** Promete lo que cumple; evita superlativos vacios.
- **Tecnica cuando aporta, simple por defecto.** Profundidad disponible, no impuesta.
- **Orientada a la accion.** Verbos guia: *integra, automatiza, conecta, distribuye, fluye*.

**Ajustes de tono por contexto:**

| Contexto | Tono | Ejemplo |
|---|---|---|
| Marketing / web | Cercano y aspiracional | "Deja de reenviar a mano." |
| Producto / UI | Funcional y guiado | "Conecta tu canal en 2 pasos." |
| Soporte / errores | Empatico y resolutivo | "Algo no se envio. Lo reintentamos por ti." |
| Documentacion tecnica | Preciso y neutral | "El webhook recibe el payload y lo reenvia a cada destino configurado." |

**Evitar:** hype de moda sin sustancia ("revolucionario", "disruptivo"), jerga innecesaria, tono robotico.

---

## 5. Publico objetivo

**Primario — Negocios que distribuyen informacion recurrente**
- Distribuidoras, mayoristas y comercios que envian **listas de precios, catalogos y promociones** a clientes/vendedores por mensajeria.
- Dolor: reenvio manual, errores, demoras, alcance limitado.

**Secundario — Operaciones y comunicaciones internas**
- Equipos que necesitan **propagar avisos** desde un canal central hacia grupos/contactos.
- Dolor: informacion que no llega a tiempo o se pierde entre canales.

**Tecnico / decisor de implementacion**
- Desarrolladores, encargados de TI o fundadores tecnicos que evaluan y montan la solucion.
- Valoran: confiabilidad, facilidad de integracion y que "simplemente funcione".

**Perfil comun:** valoran el tiempo, dependen de la mensajeria como canal de negocio y quieren automatizar sin construir todo desde cero.

---

## 6. Taglines

1. **"Integra. Automatiza. Conecta."** — modular, resume los tres verbos clave; ideal como firma de marca.
2. **"La informacion correcta, en cada canal, al instante."** — orientado a beneficio y multicanal.
3. **"Captura una vez. Llega a todos."** — describe el flujo del producto de forma memorable.
4. **"Tu informacion fluye sola."** — corto, evocador, enfatiza automatizacion.
5. **"Conectamos tus canales, tu negocio fluye."** — vincula la integracion con el resultado de negocio.

**Uso oficial:**
- **Tagline corporativo permanente:** **#1 "Integra. Automatiza. Conecta."** Acompaña al logo en firmas, presentaciones y pie de marca.
- **Tagline de campana/producto (rotativo):** **#3 "Captura una vez. Llega a todos."**
- Los demas (#2, #4, #5) quedan como variantes para piezas especificas; no sustituyen al tagline corporativo en el lockup de marca.

---

## 7. Identidad visual: logo

> Archivos: **`logo.svg`** (isotipo + wordmark) e **`isotipo.svg`** (solo simbolo). Toda la geometria es vectorial, sin `<image>` ni recursos externos.

### 7.1 Concepto

El **isotipo** son dos nodos enlazados por un trazo en "S" que sube y baja: representa la **integracion y el flujo de datos** entre dos sistemas.

- El **nodo superior** usa el azul primario `#2563EB` (confianza / tecnologia).
- El **nodo inferior** usa el cian secundario `#06B6D4` (conexion / flujo).
- El **trazo de union** es un degradado azul -> cian que materializa el "puente" de integracion.
- Los **nucleos claros** (anillos huecos) dan sensacion de puertos/endpoints y aligeran la marca en tamaños pequeños.
- La **diagonal de la "S"** sugiere ademas una "i" estilizada de IntegraTech.

> La diagonal azul -> cian es **direccional**: representa la integracion de origen a destino. Es un elemento con significado, no decorativo.

### 7.2 Wordmark

- "IntegraTech" se compone en **Space Grotesk** (display), con fallback a Inter y a la pila de sistema (identico a `--it-font-family-display` en `tokens.css`).
- Color del texto sobre fondo claro: **"Integra"** en `#1A40A8` (primary-700) y **"Tech"** en `#0E7490` (secondary-700). Este reparto maximiza el contraste y cumple **WCAG AA** en texto grande/bold.
- Para independencia total de fuentes (impresion o sistemas sin la familia), convertir el `<text>` a **paths/outlines**. El SVG ya funciona con la pila de fallback en cualquier navegador.

### 7.3 Variantes

| Variante | Cuando usarla | Notas de color |
|---|---|---|
| **Logo completo** (`logo.svg`) | Encabezados, web, presentaciones, firma de marca | Isotipo a color + wordmark bicolor |
| **Isotipo** (`isotipo.svg`) | Favicon, avatar, espacios cuadrados, usos < 120 px de ancho | Legible hasta 16x16 px |
| **Monocromo claro** | Una sola tinta sobre fondo claro | Todo en `#2563EB` |
| **Monocromo oscuro** | Una sola tinta sobre fondo oscuro | Todo en `#FFFFFF` |
| **Adaptacion dark** | Sobre `--it-color-gray-900` | Nodos `#608AFA` (primary-400) y `#22CCEE` (secondary-400); texto `#DBE6FE` / `#CFF9FE` |

### 7.4 Area de respeto y tamaño minimo

- **Area de respeto:** margen libre alrededor del logo igual al **diametro de un nodo**. No invadir esa zona con texto, imagenes ni bordes.
- **Tamaño minimo:**
  - Wordmark / logo completo: **desde 120 px de ancho**.
  - Isotipo: legible **hasta 16x16 px** (favicon).
- **Escalado:** todo es vectorial; escala libremente sin perdida.

---

## 8. Color

> Fuente de verdad de los valores: **`tokens.css`** (`--it-color-*`). Los HEX listados aqui son de referencia; en codigo, **usar siempre las variables**.

### 8.1 Colores de marca

| Token | HEX | Uso |
|---|---|---|
| `--it-color-primary-500` | `#2563EB` | Color primario (azul integracion): botones, enlaces, acentos clave |
| `--it-color-primary-600` | `#1D4FD0` | Hover del primario |
| `--it-color-primary-700` | `#1A40A8` | Activo/pressed del primario; texto "Integra" del wordmark |
| `--it-color-primary-50` | `#EFF4FF` | Fondo tenue de marca (chips, hovers sutiles, secciones destacadas); fondo opcional del isotipo |
| `--it-color-on-primary` | `#FFFFFF` | Texto/iconos sobre superficies primarias |
| `--it-color-secondary-500` | `#06B6D4` | Color secundario (cian/teal conexion): acentos, badges de integracion, graficos |
| `--it-color-secondary-600` | `#0892B3` | Hover del secundario |
| `--it-color-secondary-700` | `#0E7490` | Activo/pressed del secundario; texto "Tech" del wordmark |
| `--it-color-on-secondary` | `#06222B` | Texto/iconos sobre superficies secundarias |

### 8.2 Neutros

| Token | HEX | Uso |
|---|---|---|
| `--it-color-gray-50` | `#F8FAFC` | Fondo de pagina (tema claro) |
| `--it-color-gray-100` | `#F1F5F9` | Superficie elevada / filas zebra |
| `--it-color-gray-200` | `#E2E8F0` | Bordes sutiles, divisores |
| `--it-color-gray-300` | `#CBD5E1` | Bordes fuertes, inputs |
| `--it-color-gray-400` | `#94A3B8` | Placeholders, iconos deshabilitados |
| `--it-color-gray-500` | `#64748B` | Texto secundario / muted |
| `--it-color-gray-600` | `#475569` | Texto terciario, labels |
| `--it-color-gray-700` | `#334155` | Texto enfatico secundario |
| `--it-color-gray-800` | `#1E293B` | Superficie en tema oscuro |
| `--it-color-gray-900` | `#0F172A` | Texto principal (claro) / fondo (oscuro) |
| `--it-color-white` | `#FFFFFF` | Superficies de tarjetas y paneles (tema claro) |
| `--it-color-black` | `#0A0F1C` | Negro de marca (azulado) para maximo contraste |

### 8.3 Estados semanticos

| Token | HEX | Uso |
|---|---|---|
| `--it-color-success` | `#15803D` | Exito: texto/iconos, confirmaciones |
| `--it-color-success-bg` | `#E7F6EC` | Fondo de alertas/badges de exito |
| `--it-color-success-border` | `#A7E0BC` | Borde de componentes de exito |
| `--it-color-warning` | `#B45309` | Advertencia: texto/iconos |
| `--it-color-warning-bg` | `#FCF1DD` | Fondo de alertas/badges de advertencia |
| `--it-color-warning-border` | `#F2D49A` | Borde de componentes de advertencia |
| `--it-color-danger` | `#DC2626` | Error/destructivo: texto, botones de borrar |
| `--it-color-danger-bg` | `#FCE9E9` | Fondo de alertas/badges de error |
| `--it-color-danger-border` | `#F3B4B4` | Borde de componentes de error |
| `--it-color-info` | `#2563EB` | Informativo (alineado al primario) |
| `--it-color-info-bg` | `#E8F0FE` | Fondo de alertas/badges informativas |
| `--it-color-info-border` | `#AECBFA` | Borde de componentes informativos |

### 8.4 Alias semanticos (cambian segun tema)

| Token | Apunta a | Uso |
|---|---|---|
| `--it-color-bg` | `gray-50` | Fondo de pagina segun tema |
| `--it-color-surface` | `white` | Superficie de tarjetas/paneles segun tema |
| `--it-color-text` | `gray-900` | Texto principal segun tema |
| `--it-color-text-muted` | `gray-500` | Texto secundario segun tema |
| `--it-color-border` | `gray-200` | Borde sutil segun tema |
| `--it-color-ring` | `primary-500` | Focus ring (accesibilidad) |
| `--it-color-overlay` | `rgba(15,23,42,.55)` | Scrim/overlay de modales y drawers |

> **Regla de uso:** construir la UI con los **alias** (`--it-color-bg`, `--it-color-surface`, `--it-color-text`...) para soportar tema claro/oscuro automaticamente. Reservar los HEX directos solo para piezas portables (SVG, email) donde no se admiten variables CSS.

### 8.5 Tema oscuro

- Fondo: `--it-color-gray-900` (`#0F172A`); superficies en `--it-color-gray-800` (`#1E293B`).
- Logo/isotipo en dark: nodos `#608AFA` (primary-400) y `#22CCEE` (secondary-400); texto del wordmark `#DBE6FE` / `#CFF9FE`. Quitar el `<rect>` de fondo del isotipo o cambiarlo a `#1E293B`.

### 8.6 Accesibilidad de color

- Mantener contraste **WCAG AA** como minimo (4.5:1 texto normal, 3:1 texto grande/UI).
- No usar "Integra"/"Tech" ni textos clave en tonos `300`-`400` sobre fondo claro.
- El focus visible siempre con `--it-color-ring`.

---

## 9. Tipografia

> Fuente de verdad: **`tokens.css`** (`--it-font-*`).

### 9.1 Familias

| Token | Familia | Uso |
|---|---|---|
| `--it-font-family-base` | **Inter** + pila de sistema | Interfaz y cuerpo de texto |
| `--it-font-family-display` | **Space Grotesk** + Inter | Titulos, heros, wordmark; tono tech y moderno |
| `--it-font-family-mono` | **JetBrains Mono** + sistema | Codigo, IDs, payloads de integracion |

### 9.2 Escala y pesos

| Token | Valor | Uso |
|---|---|---|
| `--it-font-size-base` | `1rem` (16px) | Tamaño base de texto |
| `--it-font-size-xl` | `1.5rem` (24px) | Subtitulos / encabezados de seccion |
| `--it-font-size-3xl` | `2.25rem` (36px) | Titulos de pagina |
| `--it-font-weight-semibold` | `600` | Enfasis para titulos y botones |
| `--it-font-leading-normal` | `1.5` | Interlineado de cuerpo |

**Jerarquia recomendada:** titulos de pagina (display, 36px, 600) -> encabezados de seccion (display/base, 24px, 600) -> cuerpo (base, 16px, 1.5) -> codigo/IDs (mono).

---

## 10. Forma: radios y geometria

> Fuente de verdad: **`tokens.css`** (`--it-radius-*`).

| Token | Valor | Uso |
|---|---|---|
| `--it-radius-sm` | `0.25rem` (4px) | Inputs y chips pequeños |
| `--it-radius-md` | `0.5rem` (8px) | Botones y campos (por defecto) |
| `--it-radius-lg` | `0.75rem` (12px) | Tarjetas y paneles |
| `--it-radius-xl` | `1rem` (16px) | Modales y contenedores grandes |
| `--it-radius-full` | `9999px` | Pills, avatares y badges circulares |

---

## 11. Aplicacion: donde y como usar la marca

### 11.1 Donde usar el logo

| Soporte | Variante recomendada | Notas |
|---|---|---|
| Web / app (header) | `logo.svg` completo | Sobre `--it-color-surface` o `--it-color-bg` |
| Favicon / pestaña | `isotipo.svg` | Fondo `#EFF4FF` opcional para mejor recorte |
| Avatar / perfil (Telegram, WhatsApp Business, redes) | `isotipo.svg` (cuadrado) | Exportar a PNG; ver seccion 14 |
| Presentaciones / documentos | `logo.svg` + tagline #1 | Respetar area de respeto |
| Banner de envio de listas | `banner-lista.svg` -> PNG | Ver seccion 13 |
| Email / firmas | Logo en PNG (sin SVG en email) | Adjuntar o alojar en CDN |
| Impresion | Logo con texto en outlines | Garantiza fidelidad sin la fuente |

### 11.2 Fondos validos

- **Fondo claro** (`--it-color-bg` `#F8FAFC` / surface blanco): version a color por defecto, tal cual.
- **Fondo oscuro** (`--it-color-gray-900` `#0F172A`): usar la adaptacion dark (nodos primary-400 / secondary-400, texto aclarado; sin `<rect>` o en gray-800).
- **Fondo de color / fotografia:** usar la **variante transparente** (sin el `<rect>` de fondo) o la **monocroma** (`#2563EB` sobre claro, `#FFFFFF` sobre oscuro), asegurando contraste suficiente.
- El fondo `#EFF4FF` del isotipo es **opcional**: util para favicon/avatar; **no** colocarlo encima de otra superficie de color (usar la version transparente).

---

## 12. Do / Dont

### Si (Do)

- **Si** mantener la proporcion (aspect ratio) original al escalar.
- **Si** respetar el area de respeto (un diametro de nodo alrededor).
- **Si** usar la variante transparente o monocroma sobre fondos de color o fotografia.
- **Si** usar los tonos dark (primary-400 / secondary-400) sobre fondo oscuro.
- **Si** convertir el wordmark a outlines para impresion o sistemas sin Space Grotesk.
- **Si** construir UI con los alias `--it-color-*` para soportar tema claro/oscuro.
- **Si** mantener contraste WCAG AA en el wordmark y los textos clave.

### No (Dont)

- **No** deformar ni cambiar la proporcion del logo.
- **No** rotar el isotipo ni reordenar los nodos (la diagonal azul -> cian es direccional: origen -> destino).
- **No** aplicar sombras duras, contornos ni efectos 3D.
- **No** recolorear con colores fuera de la paleta `--it-`.
- **No** colocar la version con fondo `#EFF4FF` encima de otra superficie de color (usar la transparente).
- **No** reducir el contraste del texto: evitar "Integra"/"Tech" en tonos 300-400 sobre fondo claro.
- **No** usar el isotipo por debajo de 16x16 px ni el wordmark por debajo de 120 px de ancho.
- **No** escribir "Integra Tech" separado, todo minuscula en titulares ni "Integratech".
- **No** insertar SVG en correos electronicos (muchos clientes no lo renderizan): usar PNG.

---

## 13. Banner de lista de precios

> Archivo: **`banner-lista.svg`**.

**Que es:** banner **1080x540** (relacion **2:1**), ideal para encabezar/adjuntar un envio de lista de precios. Autocontenido, sin imagenes ni fuentes externas (usa Segoe UI / Roboto / Arial, seguras en web y movil). Coherente con los tokens IntegraTech: fondo azul de marca (primary-600 -> primary-900), acentos cian/teal (secondary-500/400) que evocan "integracion / flujo de datos", titulo en blanco con leve degradado y pie discreto en primary-300.

**Personalizacion:** para otra fecha o variante, editar los textos ("Actualizada al dia", el titulo) directamente en `banner-lista.svg` **antes de exportar**.

### Como usarlo en el envio (`image_url`)

1. Exporta el SVG a PNG (ver seccion 14).
2. Sube el PNG a tu bucket/CDN (este proyecto ya tiene infra AWS/S3) y obten su URL publica.
3. Usa esa URL como `image_url` para adjuntar el banner **antes** del documento de lista de precios.

- **Telegram:** `sendPhoto` con `photo` = URL (o file) del PNG, e inmediatamente despues `sendDocument` con el PDF/Excel de la lista. El banner queda como portada visual del envio.
- **WhatsApp Cloud API:** mensaje `type: "image"` con `image.link` = URL del PNG. (PNG/JPG son los formatos aceptados; **SVG no se renderiza**, por eso se exporta a PNG.)
- **Recomendaciones:** PNG con fondo solido (este banner ya lo tiene, no requiere transparencia), peso **< 5 MB**, y exportar a **2x** para nitidez en pantallas retina.

---

## 14. Como exportar los SVG a PNG

Aplica a `logo.svg`, `isotipo.svg` y `banner-lista.svg`. Los SVG son la fuente vectorial; para Telegram/WhatsApp/email/redes se entrega **PNG**.

1. **Guarda el SVG** con un nombre claro (p. ej. `banner-lista-precios.svg`).
2. **Inkscape** (recomendado, control de DPI):
   ```
   inkscape banner-lista.svg --export-type=png --export-filename=banner.png -w 2160 -h 1080
   ```
   (2160x1080 = 2x para nitidez retina; usa `-w 1080 -h 540` para tamaño 1:1.)
3. **ImageMagick:**
   ```
   magick -background none -density 300 banner-lista.svg banner.png
   ```
4. **Node / Sharp** (si ya usas Node en este proyecto):
   ```js
   sharp(svgBuffer).png().resize(2160, 1080).toFile('banner.png')
   ```
5. **rsvg-convert:**
   ```
   rsvg-convert -w 2160 -h 1080 banner-lista.svg -o banner.png
   ```

**Notas por asset:**
- **Isotipo / favicon:** exportar cuadrado (p. ej. 512x512, 256x256, 64x64, 32x32, 16x16) desde `isotipo.svg`. Verificar legibilidad a 16x16.
- **Logo completo:** mantener aspect ratio; fijar solo el ancho (`-w`) y dejar que la altura se calcule, o usar ambos respetando la proporcion original.
- **Banner:** siempre 2:1; preferir 2x. Para avatar/portada de color usar la variante transparente del logo (`-background none` en ImageMagick).

---

## 15. Inventario de assets y gobernanza

| Asset | Archivo | Contenido |
|---|---|---|
| Logo completo | `logo.svg` | Isotipo + wordmark "IntegraTech" |
| Isotipo | `isotipo.svg` | Dos nodos + trazo en "S" (favicon/avatar) |
| Banner de lista | `banner-lista.svg` | Banner 1080x540 para envios |
| Tokens de diseño | `tokens.css` | `--it-color-*`, `--it-font-*`, `--it-radius-*` |

**Reglas de gobernanza:**
- **`tokens.css` es la fuente de verdad** de color, tipografia y radios. Cualquier valor de marca se cambia ahi primero; este documento se actualiza despues.
- Los SVG son **vectoriales y autocontenidos** (sin `<image>` ni fuentes externas); no incrustar binarios.
- Los PNG son **derivados**: se generan desde los SVG, no se editan a mano.
- Cualquier nueva pieza de marca debe usar exclusivamente la paleta `--it-` y las familias tipograficas oficiales.

---

*IntegraTech — Integra. Automatiza. Conecta.*
