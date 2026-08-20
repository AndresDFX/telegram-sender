# Manual — Usar Replica con el mismo canal del administrador

`Para: un usuario NUEVO del panel · No requiere conocimientos técnicos · Actualizado: 2026-08-20`

Este manual explica cómo una segunda persona usa la plataforma **Replica** aprovechando el mismo
canal de origen que ya tiene configurado el administrador: ver las listas que se capturan del canal,
enviarlas a sus contactos y programar envíos — sin tocar nada de la configuración técnica.

> El panel funciona bien en **computador y en el celular** (el menú, las tablas y los formularios se
> adaptan a la pantalla). Puedes operar todo desde el teléfono, donde el menú se ve como una **barra
> de botones abajo**, al alcance del pulgar.

### Instálalo como app (recomendado, 10 segundos)

El panel es una **app instalable**: queda con su ícono en tu teléfono o escritorio y abre a pantalla
completa, sin la barra del navegador.

- **Android / Chrome / Edge:** entra al panel y toca **⬇ Instalar app** (arriba a la derecha, o en la
  pantalla de ingreso). Si no aparece, usa el menú **⋮ → Instalar aplicación / Añadir a pantalla de inicio**.
- **iPhone / iPad (Safari):** toca **Compartir** (el cuadrito con la flecha ↑) → **Añadir a pantalla de
  inicio**. El botón ⬇ del panel te muestra estos mismos pasos, porque iOS no permite instalar solo.
- **Computador (Chrome/Edge):** el mismo botón ⬇, o el icono de instalar en la barra de direcciones.

Otras dos cosas útiles:

- **☀️/🌙 Tema claro u oscuro:** el botón junto a ⬇ alterna **automático → claro → oscuro** y recuerda
  tu elección. En *automático* sigue lo que tenga configurado tu teléfono.
- **Aviso de versión nueva:** cuando se publica una mejora del panel aparece abajo «✨ Versión nueva».
  Actualiza cuando puedas: **recarga y te pedirá tu usuario y contraseña otra vez** (por seguridad la
  sesión no se guarda en el dispositivo). Puedes dejarlo para después.
- **Sin conexión:** si te quedas sin datos, la app abre igual y muestra lo último cargado con el aviso
  **📴 Sin conexión**. Los envíos y los cambios necesitan red: se reanudan cuando vuelve.

---

## 1. La idea en 60 segundos

Replica hace tres cosas:

1. **Captura** automáticamente cada lista de precios que se publica en el canal de Telegram de origen
   (ya configurado por el administrador — no tienes que hacer nada para "conectarte" al canal).
2. **Te las muestra** en el panel, con el aumento de precio (markup) ya aplicado.
3. **Las envía** a contactos de Telegram y/o WhatsApp — solo cuando alguien lo decide (envío manual)
   o cuando el envío automático está activado con una lista elegida.

**Regla de oro: capturar ≠ enviar.** Que una lista aparezca en el panel NO significa que se envió a
nadie. Las filas marcadas **📥 Capturada (no enviada)** son solo material recopilado; tú decides si
va a alguien, a quién y cuándo.

## 2. Tu cuenta (la crea el administrador)

1. El administrador entra a **Ajustes → 👤 Acceso → Usuarios del panel** y crea tu usuario
   (nombre, correo y contraseña temporal, rol **Usuario**).
2. Te comparte la URL del panel y la contraseña temporal **por un canal seguro** (nunca por chat plano).
3. En tu primer ingreso: **Ajustes → 👤 Acceso → Cambiar contraseña**. Usa una de 8+ caracteres.
4. Si la olvidas: «¿Olvidaste tu contraseña?» en el login te envía un código al correo registrado.
5. Si el correo del código ya no te llega (cambiaste de correo, por ejemplo), el administrador puede
   **corregir tu correo o darte una contraseña nueva** desde el ✏️ de tu fila en **Usuarios del panel**
   — sin borrar tu usuario y sin necesitar tu contraseña actual. Cámbiala en cuanto entres.

> Con el rol *Usuario* puedes operar todo el flujo de envíos. Lo único reservado al administrador es
> gestionar usuarios/roles del panel (crear, editar correo, restablecer contraseñas, cambiar rol, borrar).

## 3. Qué compartes con el administrador y qué es solo tuyo

| Compartido (una sola configuración para todos) | Personal (solo tuyo) |
|---|---|
| **Canal de origen** y su captura automática | **Tus reglas** de excluir por nombre (ej. `FAM`) |
| **Historial** de envíos y listas capturadas | **Tus contactos excluidos** por número/id |
| **Listas de contactos** guardadas (TG/WA) | Tus **excepciones** (incluidos a mano) |
| Aumento de precio (markup), texto final, imagen | Tu contraseña y tu correo |
| Conexiones (bot, cuenta Telegram, WhatsApp) | |
| Interruptores de **captura** y **envío automático** | |
| Ventanas horarias y ritmo anti-baneo | |

Dos consecuencias importantes:

- **No necesitas configurar el canal**: al entrar ya ves las mismas capturas que el administrador.
- Las **exclusiones son personales pero se suman**: al enviar, la plataforma excluye la **unión** de
  las exclusiones de todos los usuarios. Si tú excluyes a alguien, tampoco recibirá los envíos que
  haga el administrador (y viceversa). Coordina antes de excluir contactos compartidos.

## 4. Ver lo que llega del canal (📡 Actividad → Historial)

Cada lista capturada aparece como una fila con estado **📥 Capturada (no enviada)**.

- **Matriz de estado (filtros con conteo):** arriba de la tabla hay una barra que filtra y muestra
  cuántas hay de cada tipo: **Todas · 📥 Capturadas · 🆕 Creadas · ⏳ En proceso · ✅ Enviadas ·
  ⚠️ Con fallos**. Toca una para ver solo ese grupo.
- **Buscador y paginación:** usa la caja **🔎 Buscar** para encontrar una lista por su texto u origen,
  y las flechas **‹ ›** de abajo para pasar de página cuando hay muchas.
- **Detalle de la difusión (toca el mensaje):** se abre una ventana con todo lo importante:
  - **Fechas**: 📥 recibido · 🚀 primer envío · 🏁 último envío.
  - **📥 Mensaje anterior (original del canal)**: el texto tal como llegó (con ubicación, teléfonos y
    marca) — a la izquierda.
  - **📤 Mensaje que se envía**: el texto ya limpio (sin ubicación/teléfonos/marca), con el aumento de
    precio y el texto final — a la derecha.
  - **💰 Comparador de precios**: precio anterior → precio nuevo, producto por producto.
- En **🏠 Inicio** tienes un atajo: la card **"Última lista capturada"** muestra la captura más
  reciente sin salir de la sala de control.
- Si una fila dice *"📷 La publicación original incluye una imagen (este texto es su caption)"*, el
  contenido real de ese post estaba en la foto del canal — ábrelo en Telegram para verla.
- Si el administrador dejó el **preview a Mensajes Guardados** activo, las capturas también llegan a
  los Mensajes Guardados de la cuenta de Telegram conectada.

## 5. Enviar una lista a TUS contactos (flujo típico)

1. Abre **✍️ Enviar** (el compositor único). Atajo: en **📡 Actividad → Historial**, cada fila
   **📥 Capturada** trae un botón **"Enviar a…"** que abre el compositor con ese texto ya cargado.
2. Pega el texto (puedes copiarlo desde el detalle de una captura) y añade imagen si quieres
   (archivo o URL).
3. Marca el/los **canales** (Telegram y/o WhatsApp).
4. En **Enviar a**, elige una **lista de contactos** guardada o busca y marca contactos concretos.
   ⚠️ Evita el modo "todos": el texto final y las guardias existen porque enviar a toda la agenda
   por error es el mayor riesgo de la plataforma.
5. En **¿Cuándo se envía?** elige el modo:
   - **⚡ Ahora**: el botón dice **Enviar** y la lista sale de inmediato (aunque el sistema esté
     "EN PAUSA": la pausa solo frena lo automático).
   - **📅 Una vez**: elige fecha y hora; el botón cambia a **Programar**. Se interpreta en la
     zona horaria configurada del sistema (Colombia), no en la de tu navegador.
   - **🔁 Se repite**: para envíos que se repiten (ver §7).
6. Sigue el progreso en **📡 Actividad → Historial** (barras por canal, errores visibles por fila).

Notas:
- El envío manual respeta las exclusiones (las tuyas + las de los demás usuarios).
- El texto manual va **tal cual** lo escribes (sin markup automático); las capturas ya lo traen aplicado.
- WhatsApp manual exige elegir lista (no permite "toda la agenda").

## 6. Crear tu propia lista de contactos

1. **👥 Contactos → Telegram** (o WhatsApp): usa el **🔎 buscador** para encontrar contactos,
   márcalos y pulsa **➕ Nueva lista con los marcados** con un nombre claro (ej. `Clientes Pedro`).
2. Esa lista queda disponible para todos los usuarios en el compositor **✍️ Enviar** (en cualquiera
   de sus modos).
3. Para elegir a quién enviar, marca ☑ las listas que quieras usar y abajo elige **¿A quién se envía?**:
   *Todos mis contactos*, *Solo las listas marcadas* o *Todos, excepto las listas marcadas*.
4. **Renombrar una lista:** el botón **✏️** de su fila. El panel te avisa cuántos envíos programados usan
   esa lista y, al confirmar, **los cambia solos** (y también la «Lista del envío automático» si era esa),
   así que el nuevo nombre no deja nada apuntando al vacío. No se permiten dos listas con el mismo nombre.
5. ⚠️ **No borres listas que no creaste**: si una lista está elegida como destino del envío automático y
   **desaparece**, el sistema deja de difundir y lo marca como error. Renombrar es seguro; borrar no.

Para **dejar de enviarle a alguien** no hace falta borrar el contacto: márcalo y usa **Excluir
marcados**, o escribe una regla en **"⛔ Excluir si el nombre contiene…"** (una palabra por línea,
ej. `FAM`). Es reversible.

## 7. Programar envíos que se repiten

No hay un formulario aparte: todo se hace desde el compositor único **✍️ Enviar**.

- **Se repite** (diario / semanal): en **¿Cuándo se envía?** elige **🔁 Se repite**, define la
  frecuencia y la hora. Escribe el texto, marca canales y elige una **lista de contactos** como destino
  (los repetidos solo admiten listas, no contactos sueltos). Si añades imagen, usa una **URL
  fija** (un enlace web, no un archivo suelto), porque el envío se repetirá en el tiempo. El botón dirá
  **Programar**.
- **Una sola vez a fecha/hora**: usa en cambio el modo **📅 Una vez** (ver §5).

Ambos respetan la ventana horaria y el ritmo anti-baneo del sistema. Para gestionar los repetidos
ya creados ve a **📡 Actividad → Programados**: ahí aparece la lista y puedes **editar / pausar /
reanudar / borrar** los tuyos (también con buscador y paginación).

**Editar un repetido (✏️ Editar):** no hace falta borrarlo y volver a crearlo. Se abre el mismo
formulario con todo lo que ya tenía (texto, canales, listas, frecuencia, hora, días) y cambias solo lo
que quieras; el resto se conserva y el panel recalcula el **próximo envío**. Se aplican las mismas reglas
que al crear (texto obligatorio, al menos un canal, WhatsApp exige lista, imagen por URL `https://`,
semanal exige días). Editar **no reinicia el historial** del programado: sigue mostrando cuántas veces se
ha enviado y cuándo fue la última.

## 8. Detener, borrar y limpiar envíos

- **Borrar una difusión = detenerla.** Al borrar una fila del **Historial** (individual, con
  «🗑 Borrar seleccionados» o «🗑 Eliminar todas»), además de quitarla de la tabla se **detiene lo que
  quede pendiente por enviar** (se desencola). No revierte lo que ya se entregó.
- **📡 Actividad → Problemas** muestra la **Cola de envío** en vivo: cuántos mensajes están *en cola*,
  *enviándose ahora* y *atascados* (los que fallaron tras varios reintentos). Puedes **reintentar** los
  atascados o, en una emergencia, **🗑 Vaciar cola** (descarta lo que aún espera; no revierte lo ya
  enviado).
- **Envíos por partes** (📡 Actividad): los envíos grandes salen en grupos, de a uno, con pausas. Ahí
  ves el progreso y puedes **cancelar** o **borrar** los tuyos.

> Casi todas las tablas del panel tienen **buscador**, **paginación** y **eliminar todos**, para que
> manejar muchas filas sea fácil.

## 9. El envío AUTOMÁTICO (tocar con cuidado)

La sala de control **🏠 Inicio** muestra si el envío automático está **EN PAUSA** o **ACTIVO** (el
estado EN PAUSA se ve en **ámbar**; el rojo se reserva solo para fallos):

- **EN PAUSA** (estado normal): el canal solo se captura. Nada se difunde solo.
- **ACTIVO**: cada lista nueva del canal se envía SOLA a la **lista elegida por canal**. Tanto el
  interruptor **"Envíos automáticos activos"** como la **"Lista del envío automático"** (por canal)
  están en **🏠 Inicio**. El sistema no permite activarlo sin lista elegida.
- **La pausa y la activación afectan a TODOS los usuarios** (es un interruptor global). No lo cambies
  sin acordarlo con el administrador. Al activar, el panel te muestra exactamente a cuántas personas
  les llegará — léelo antes de confirmar.

## 10. Lo que NO debes tocar (a menos que seas tú el responsable)

- **Ajustes → 🔌 Conexiones** (cuenta de Telegram por bot o por *mi cuenta personal*, vinculación de
  WhatsApp, tokens): si se desconecta la cuenta, se caen la captura y los envíos de todos. Ojo con
  **Vincular WhatsApp**: pedir un código nuevo **cierra la sesión que está funcionando** (el panel te
  avisa antes). Solo hazlo si eres el responsable de re-vincular; si te toca, es guiado: pones el
  número, WhatsApp te da un código de 8 dígitos, lo escribes en **⋮ → Dispositivos vinculados →
  Vincular con número de teléfono** y el panel confirma solo.
- **Canal de Telegram** (Ajustes → 📥 Captura): cambiarlo redirige TODA la captura a otro canal.
- **Aumento de precio, imagen de la lista y textos a eliminar de cada lista** (Ajustes → 📥 Captura):
  afectan lo que ven los clientes de todos los usuarios.
- **Anti-baneo y ventanas horarias** (Ajustes → 📤 Ritmo y horarios): protegen las cuentas contra
  bloqueos de Telegram/WhatsApp.

## 11. Problemas comunes

| Síntoma | Qué mirar |
|---|---|
| "No llegan capturas nuevas" | ¿La captura está activa? (🏠 Inicio → switch **"Capturar listas del canal"**). ¿El canal publicó algo con texto? Los posts SOLO-imagen sin caption no se capturan. |
| "Mi envío no salió" | Ábrelo en **📡 Actividad → Historial**: cada fila muestra la razón del fallo. ¿Elegiste lista/destinatarios? ¿Era programado y aún no llega la hora (zona horaria del sistema)? |
| "A un contacto no le llegó" | Puede estar excluido — recuerda que se aplican las exclusiones de TODOS los usuarios, o el contacto bloqueó al remitente. En WhatsApp, si le fallaron varios envíos seguidos el sistema lo **auto-excluye**: el administrador lo ve en Ajustes → 🔌 Conexiones → «Auto-excluidos por fallos» y puede pulsar **Reincluir** solo en ese contacto. |
| "Hay envíos atascados" | 📡 Actividad → Problemas: **reintenta** los atascados; si algo quedó pegado, **Vaciar cola**. |
| "El panel me bloqueó el ingreso" | 5 intentos fallidos bloquean 5 minutos. Espera y reintenta, o usa el reseteo por correo. |
| "Sale 'EN PAUSA' (en ámbar) en 🏠 Inicio" | Es el estado del envío AUTOMÁTICO. Tus envíos manuales salen igual. |
| "Sale 📴 Sin conexión" | No hay red (o el servidor no responde). Ves lo último cargado; espera y reintenta — los envíos no se pierden. |
| "Me pide la contraseña otra vez" | Pasa al recargar o al actualizar a la versión nueva: la sesión vive solo mientras la app está abierta. |
| "No veo el botón ⬇ Instalar app" | Ya la tienes instalada (ahí se oculta), o tu navegador no lo soporta: usa **⋮ → Instalar aplicación**, y en iPhone **Compartir → Añadir a pantalla de inicio**. |

## 12. Buenas prácticas

- Antes de un envío grande, pruébalo contigo mismo o con la cuenta de prueba.
- Usa **listas de contactos nombradas** en vez de marcar contactos sueltos: reproducible y auditable.
- No hagas envíos masivos seguidos por fuera de la ventana horaria: el ritmo anti-baneo existe para
  proteger el número/cuenta de todos.
- Todo queda en la **auditoría** (Ajustes → 🛠️ Auditoría: quién hizo qué y cuándo) — opera como si tu
  nombre quedara en cada acción, porque queda.
