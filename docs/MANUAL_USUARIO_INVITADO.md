# Manual — Usar Replica con el mismo canal del administrador

`Para: un usuario NUEVO del panel · No requiere conocimientos técnicos · Actualizado: 2026-07-08`

Este manual explica cómo una segunda persona usa la plataforma **Replica** aprovechando el mismo
canal de origen que ya tiene configurado el administrador: ver las listas que se capturan del canal,
enviarlas a sus contactos y programar envíos — sin tocar nada de la configuración técnica.

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

1. El administrador entra a **Ajustes y estado → Acceso → Usuarios del panel** y crea tu usuario
   (nombre, correo y contraseña temporal, rol **Usuario**).
2. Te comparte la URL del panel y la contraseña temporal **por un canal seguro** (nunca por chat plano).
3. En tu primer ingreso: **Ajustes y estado → Acceso → Cambiar contraseña**. Usa una de 8+ caracteres.
4. Si la olvidas: «¿Olvidaste tu contraseña?» en el login te envía un código al correo registrado.

> Con el rol *Usuario* puedes operar todo el flujo de envíos. Lo único reservado al administrador es
> gestionar usuarios/roles del panel.

## 3. Qué compartes con el administrador y qué es solo tuyo

| Compartido (una sola configuración para todos) | Personal (solo tuyo) |
|---|---|
| **Canal de origen** y su captura automática | **Tus patrones de exclusión** por nombre (ej. `FAM`) |
| Historial de **Envíos** y listas capturadas | **Tus contactos excluidos** por número/id |
| **Listas guardadas** de destinatarios (TG/WA) | Tus **excepciones** a los patrones |
| Aumento de precio (markup), pie de mensaje, imagen | Tu contraseña y tu correo |
| Conexiones (bot, cuenta Telegram, WhatsApp) | |
| Interruptores de **captura** y **envío automático** | |
| Ventanas horarias y ritmo anti-baneo | |

Dos consecuencias importantes:

- **No necesitas configurar el canal**: al entrar ya ves las mismas capturas que el administrador.
- Las **exclusiones son personales pero se suman**: al enviar, la plataforma excluye la **unión** de
  las exclusiones de todos los usuarios. Si tú excluyes a alguien, tampoco recibirá los envíos que
  haga el administrador (y viceversa). Coordina antes de excluir contactos compartidos.

## 4. Ver lo que llega del canal

- **Envíos → Historial**: cada lista capturada aparece como fila con estado
  **📥 Capturada (no enviada)**. Clic en la fila para ver el **texto completo** (ya con el aumento
  de precio aplicado).
- Si una fila dice *"📷 La publicación original incluye una imagen (este texto es su caption)"*, el
  contenido real de ese post estaba en la foto del canal — ábrelo en Telegram para verla.
- Si el administrador dejó el **preview a Mensajes Guardados** activo, las capturas también llegan a
  los Mensajes Guardados de la cuenta de Telegram conectada.

## 5. Enviar una lista a TUS contactos (flujo típico)

1. Abre **Envíos → Componer**.
2. Pega el texto (puedes copiarlo desde el detalle de una captura) y añade imagen si quieres
   (archivo o URL).
3. Marca el/los **canales** (Telegram y/o WhatsApp).
4. En **Enviar a**, elige una **lista guardada** o busca y marca contactos concretos.
   ⚠️ Evita el modo "todos": el pie de página y las guardias existen porque enviar a toda la agenda
   por error es el mayor riesgo de la plataforma.
5. **Enviar**: sale de inmediato (aunque el sistema esté "EN PAUSA": la pausa solo frena lo
   automático). **Programar (opcional)**: elige fecha/hora — se interpreta en la zona horaria
   configurada del sistema (Colombia), no en la de tu navegador.
6. Sigue el progreso en **Envíos → Historial** (barras por canal, errores visibles por fila).

Notas:
- El envío manual respeta las exclusiones (las tuyas + las de los demás usuarios).
- El texto manual va **tal cual** lo escribes (sin markup automático); las capturas ya lo traen aplicado.
- WhatsApp manual exige elegir lista (no permite "toda la agenda").

## 6. Crear tu propia lista de destinatarios

1. **Fuentes y listas → Telegram** (o WhatsApp): busca contactos, márcalos y **Guardar como lista**
   con un nombre claro (ej. `Clientes Pedro`).
2. Esa lista queda disponible para todos los usuarios en Componer y Programados.
3. ⚠️ **No renombres ni borres listas que no creaste**: si una lista está elegida como destino del
   envío automático y desaparece, el sistema deja de difundir y lo marca como error en el job.

## 7. Programar envíos recurrentes

**Envíos → Programados → Programar un mensaje**: texto, canales, lista destino y frecuencia
(una vez / diario / semanal). Respetan la ventana horaria y el ritmo anti-baneo del sistema.
Puedes pausar/reanudar/borrar los tuyos desde la tabla.

## 8. El envío AUTOMÁTICO (tocar con cuidado)

La barra superior del panel muestra si el envío automático está **EN PAUSA** o **ACTIVO**:

- **EN PAUSA** (estado normal): el canal solo se captura. Nada se difunde solo.
- **ACTIVO**: cada lista nueva del canal se envía SOLA a la **lista elegida por canal** en
  *Ajustes → Envío → Lista del envío automático*. El sistema no permite activarlo sin lista elegida.
- **La pausa y la activación afectan a TODOS los usuarios** (es un interruptor global). No lo cambies
  sin acordarlo con el administrador. Al activar, el panel te muestra exactamente a cuántas personas
  les llegará — léelo antes de confirmar.

## 9. Lo que NO debes tocar (a menos que seas tú el responsable)

- **Ajustes → Telegram / WhatsApp** (conexiones, tokens, vinculación por QR): si se desconecta la
  cuenta, se caen la captura y los envíos de todos.
- **Fuente del canal** (Fuentes y listas → Fuente): cambiarla redirige TODA la captura a otro canal.
- **Aumento de precio, pie de mensaje y patrones de limpieza**: afectan lo que ven los clientes de
  todos los usuarios.
- **Anti-baneo y ventanas horarias**: protegen las cuentas contra bloqueos de Telegram/WhatsApp.

## 10. Problemas comunes

| Síntoma | Qué mirar |
|---|---|
| "No llegan capturas nuevas" | ¿La captura está activa? (Ajustes → Envío → Recopilación automática). ¿El canal publicó algo con texto? Los posts SOLO-imagen sin caption no se capturan. |
| "Mi envío no salió" | Ábrelo en Envíos: cada fila muestra la razón del fallo. ¿Elegiste lista/destinatarios? ¿Era programado y aún no llega la hora (zona horaria del sistema)? |
| "A un contacto no le llegó" | Puede estar excluido — recuerda que se aplican las exclusiones de TODOS los usuarios, o el contacto bloqueó al remitente. |
| "El panel me bloqueó el ingreso" | 5 intentos fallidos bloquean 5 minutos. Espera y reintenta, o usa el reseteo por correo. |
| "Sale 'EN PAUSA' arriba" | Es el estado del envío AUTOMÁTICO. Tus envíos manuales salen igual. |

## 11. Buenas prácticas

- Antes de un envío grande, pruébalo contigo mismo o con la cuenta de prueba.
- Usa **listas nombradas** en vez de marcar contactos sueltos: reproducible y auditable.
- No hagas envíos masivos seguidos por fuera de la ventana horaria: el ritmo anti-baneo existe para
  proteger el número/cuenta de todos.
- Todo queda en la **auditoría** (quién hizo qué y cuándo) — opera como si tu nombre quedara en cada
  acción, porque queda.
