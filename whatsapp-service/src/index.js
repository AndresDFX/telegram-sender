// Servicio WhatsApp portable (Baileys). Mantiene la conexión de WhatsApp Web (sesión
// en DynamoDB), expone el QR para vincular desde el panel, lista contactos y envía las
// listas reenviadas desde Telegram. Protegido con un bearer token compartido.
import makeWASocket, { Browsers, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys'
import { DynamoDBClient, UpdateItemCommand } from '@aws-sdk/client-dynamodb'
import express from 'express'
import pino from 'pino'
import qrcode from 'qrcode'
import { useDynamoAuthState } from './dynamoAuth.js'

const PORT = process.env.PORT || 8080
const TOKEN = process.env.WHATSAPP_TOKEN || ''
const TABLE = process.env.WHATSAPP_AUTH_TABLE || 'telegram-sync-dev-whatsapp-auth'
const SESSION_ID = process.env.WHATSAPP_SESSION_ID || 'default'
const SEND_DELAY_MS = Number(process.env.SEND_DELAY_MS || 2000)

let sock = null
let connected = false
let currentQR = null
let me = null
let pairNumber = null // número para vincular por código (en vez de QR)
let pairingCode = null // código de 8 dígitos generado
let lastClose = null // último statusCode de cierre (diagnóstico)
let loggedOut = false // sesión cerrada por WhatsApp; requiere re-vincular (/reset)
let replaced = false // 440: otro host tomó la sesión; este cedió (usar /reconnect para retomar)
let lastError = null // último error de arranque (DynamoDB/red)
let lastPairError = null // último error de requestPairingCode (p.ej. "inténtalo más tarde")
let clearOnStart = false // limpiar la sesión en DynamoDB antes del próximo arranque
let reconnectTimer = null // única reconexión pendiente
let gen = 0 // generación del socket activo (ignora eventos de sockets viejos)
let chain = Promise.resolve() // mutex: serializa los (re)arranques en vez de descartarlos
const contacts = {} // jid -> nombre
// Opt-out anti-baneo: jid -> nº de fallos de envío consecutivos. Al alcanzar BLOQUEO_UMBRAL el
// contacto se auto-excluye de los envíos (deja de reintentarse). Se reinicia a 0 al enviar OK.
const failures = {}
const BLOQUEO_UMBRAL = Number(process.env.BLOQUEO_UMBRAL || 3)
let persistFailuresFn = null
let failuresSaveTimer = null

const log = pino({ level: 'info' })

// Mapea códigos de cierre de Baileys a un mensaje legible para el usuario.
function closeMsg(code) {
  if (code == null) return null
  const m = {
    401: 'sesión cerrada (loggedOut)',
    403: 'bloqueado por WhatsApp ("inténtalo más tarde")',
    408: 'timeout: no se completó la vinculación a tiempo',
    428: 'conexión cerrada',
    440: 'sesión reemplazada (vinculada en otro lugar)',
    515: 'requiere reinicio (normal justo tras vincular)',
  }
  return m[code] || `código ${code}`
}

let persistContactsFn = null // saveContacts del arranque actual
let contactsSaveTimer = null

function recordContact(c) {
  if (!c || !c.id) return
  if (c.name) {
    contacts[c.id] = c.name // nombre de tu agenda: máxima prioridad, gana siempre
  } else if (!contacts[c.id]) {
    contacts[c.id] = c.notify || c.verifiedName || '' // solo si no hay nada, no degradar
  }
  scheduleSaveContacts()
}

// Guarda los contactos en DynamoDB con debounce (los eventos llegan en ráfagas).
function scheduleSaveContacts() {
  if (contactsSaveTimer || !persistContactsFn) return
  contactsSaveTimer = setTimeout(async () => {
    contactsSaveTimer = null
    try {
      await persistContactsFn({ ...contacts })
    } catch (e) {
      log.error({ err: String(e) }, 'persistir contactos falló')
    }
  }, 4000)
}

// Guarda el conteo de fallos (opt-out) en DynamoDB con debounce.
function scheduleSaveFailures() {
  if (failuresSaveTimer || !persistFailuresFn) return
  failuresSaveTimer = setTimeout(async () => {
    failuresSaveTimer = null
    try {
      await persistFailuresFn({ ...failures })
    } catch (e) {
      log.error({ err: String(e) }, 'persistir fallos falló')
    }
  }, 4000)
}

// (Re)arranca el socket de forma SERIALIZADA: si ya hay un arranque en curso, este se
// encola y espera (no se descarta como antes). Así /pair y las reconexiones nunca quedan
// en no-op silencioso. Devuelve la promesa de la cadena para poder await-earla.
function restart() {
  chain = chain.then(doStart).catch((e) => log.error({ err: String(e) }, 'arranque falló'))
  return chain
}

function scheduleReconnect() {
  if (reconnectTimer) return
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    restart()
  }, 3000)
}

async function doStart() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  // Teardown del socket anterior: quitamos sus listeners ANTES de cerrarlo para que su
  // 'close' no dispare otra reconexión (la causa de la tormenta de sockets/códigos).
  if (sock) {
    try {
      sock.ev.removeAllListeners()
      sock.end(undefined)
    } catch (e) {}
    sock = null
  }
  const myGen = ++gen
  loggedOut = false // arranque fresco: limpiamos los flags
  replaced = false

  // Limpieza de sesión bajo la cadena (serializada), ANTES de leer creds: así nunca se
  // borran credenciales que un socket nuevo acaba de escribir.
  if (clearOnStart) {
    clearOnStart = false
    try {
      const prev = await useDynamoAuthState(TABLE, SESSION_ID)
      await prev.clearAll()
    } catch (e) {
      log.error({ err: String(e) }, 'clearAll falló')
    }
    if (gen !== myGen) return // reemplazado mientras limpiábamos
  }

  try {
    const { state, saveCreds, loadContacts, saveContacts, loadFailures, saveFailures } = await useDynamoAuthState(TABLE, SESSION_ID)
    persistContactsFn = saveContacts
    persistFailuresFn = saveFailures
    // Carga y FUSIONA los contactos persistidos en cada arranque (también en /reconnect):
    // así un host que retoma la sesión obtiene los contactos guardados por otro host.
    try {
      Object.assign(contacts, await loadContacts())
    } catch (e) {
      log.error({ err: String(e) }, 'cargar contactos falló')
    }
    try {
      Object.assign(failures, await loadFailures())
    } catch (e) {
      log.error({ err: String(e) }, 'cargar fallos falló')
    }
    // fetchLatestBaileysVersion es una llamada de red SIN timeout; si se cuelga, bloquearía
    // toda la cadena de arranques. La acotamos y caemos a la versión por defecto de Baileys.
    let version
    try {
      const r = await Promise.race([
        fetchLatestBaileysVersion(),
        new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 8000)),
      ])
      version = r.version
    } catch (e) {
      log.warn({ err: String(e) }, 'fetchLatestBaileysVersion falló/timeout; uso versión por defecto')
      version = undefined
    }
    if (gen !== myGen) return // otro arranque ganó mientras esperábamos
    const sockConfig = {
      auth: state,
      logger: pino({ level: 'silent' }),
      printQRInTerminal: false,
      browser: Browsers.macOS('Desktop'),
      markOnlineOnConnect: false,
      // Sincroniza el historial al vincular para poblar los contactos (la reconexión no
      // los reenvía). Una vez persistidos, se cargan al instante de DynamoDB.
      syncFullHistory: process.env.SYNC_FULL_HISTORY !== 'false',
    }
    if (version) sockConfig.version = version // si no, Baileys usa su versión por defecto
    const s = makeWASocket(sockConfig)
    sock = s
    lastError = null

    s.ev.on('creds.update', saveCreds)
    s.ev.on('contacts.upsert', (cs) => cs.forEach(recordContact))
    s.ev.on('contacts.update', (cs) => cs.forEach(recordContact))
    s.ev.on('messaging-history.set', ({ contacts: cs }) => (cs || []).forEach(recordContact))
    // (B) Captura el pushName de mensajes entrantes: rellena nombres con la actividad,
    // sin pisar el nombre de agenda ya guardado.
    s.ev.on('messages.upsert', ({ messages }) => {
      for (const m of messages || []) {
        const jid = m.key?.remoteJid
        if (jid && jid.endsWith('@s.whatsapp.net') && !m.key.fromMe && m.pushName && !contacts[jid]) {
          contacts[jid] = m.pushName
          scheduleSaveContacts()
        }
      }
    })

    // Vinculación por código (8 dígitos): una sola vez por socket si hay número y no está registrado.
    if (pairNumber && !s.authState.creds.registered) {
      setTimeout(async () => {
        if (gen !== myGen) return // socket reemplazado: no pidas código en uno viejo
        try {
          const code = await s.requestPairingCode(pairNumber)
          if (gen !== myGen) return // reemplazado tras el await: no publiques un código muerto
          pairingCode = code
          lastPairError = null
          log.info({ pairingCode }, 'Código de emparejamiento generado')
        } catch (e) {
          lastPairError = String(e?.message || e)
          log.error({ err: lastPairError }, 'requestPairingCode falló')
        }
      }, 3000)
    }

    s.ev.on('connection.update', async (u) => {
      if (gen !== myGen) return // ignora eventos de sockets de generaciones anteriores
      const { connection, lastDisconnect, qr } = u
      if (qr) {
        if (pairNumber) return // en modo emparejamiento no publicamos QR (no mezclar flujos)
        const data = await qrcode.toDataURL(qr)
        if (gen !== myGen) return // recheck tras el await
        currentQR = data
        log.info('Nuevo QR disponible para vincular')
      }
      if (connection === 'open') {
        connected = true
        currentQR = null
        pairingCode = null
        pairNumber = null
        lastClose = null
        lastError = null
        lastPairError = null
        me = s.user
        log.info({ me: me?.id }, 'WhatsApp conectado')
        // Si hay pocos contactos, fuerza la re-sincronización del address-book (app-state)
        // para poblar contactos + nombres SIN re-vincular. Una vez persistidos, no se repite.
        if (Object.keys(contacts).length < 50) {
          try {
            log.info('Pocos contactos; re-sincronizando app-state...')
            await s.resyncAppState(['critical_unblock_low', 'critical_block', 'regular_high', 'regular_low', 'regular'], true)
            log.info({ contactos: Object.keys(contacts).length }, 'app-state re-sincronizado')
          } catch (e) {
            log.error({ err: String(e) }, 'resyncAppState falló')
          }
        }
      }
      if (connection === 'close') {
        connected = false
        lastClose = lastDisconnect?.error?.output?.statusCode ?? null
        if (gen !== myGen) return
        // En emparejamiento NO reconectamos: invalidaría el código entregado al usuario.
        if (pairNumber) {
          log.warn({ code: lastClose }, 'Cierre durante emparejamiento; reintenta manualmente.')
          return
        }
        if (lastClose === DisconnectReason.loggedOut) {
          // Sesión inválida. NO auto-borramos ni reconectamos: si otro host (re)vinculó,
          // borrar aquí destruiría la sesión nueva. El usuario re-vincula con POST /reset
          // (o el script -Reset), que es un borrado explícito y seguro.
          loggedOut = true
          pairingCode = null
          log.warn('Sesión cerrada (loggedOut). Re-vincula con /reset o el script -Reset.')
        } else if (lastClose === DisconnectReason.connectionReplaced) {
          // 440: otro host tomó la misma sesión. CEDEMOS (no reconectar) para evitar la
          // "guerra de 440" entre local y Render. El host activo es el último que conectó.
          replaced = true
          log.warn('Conexión reemplazada (440) por otro host; cedo (no reconecto). Usa /reconnect para retomar.')
        } else {
          log.warn({ code: lastClose }, 'Conexión cerrada; reconectando...')
          scheduleReconnect()
        }
      }
    })
  } catch (e) {
    // Fallo de DynamoDB/red al arrancar: no dejar el servicio mudo, reintentar.
    lastError = String(e?.message || e)
    log.error({ err: lastError }, 'arranque falló; reintentando...')
    scheduleReconnect()
  }
}

const app = express()
app.use(express.json({ limit: '6mb' }))

function auth(req, res, next) {
  if (!TOKEN || req.get('authorization') !== `Bearer ${TOKEN}`) {
    return res.status(401).json({ error: 'unauthorized' })
  }
  next()
}

// Raíz informativa: evita el "Cannot GET /" y confirma que el servicio está vivo.
app.get('/', (req, res) =>
  res
    .type('html')
    .send(
      '<h2>Replica · servicio WhatsApp</h2>' +
        `<p>Servicio activo ✓ · ${connected ? 'WhatsApp conectado' : 'WhatsApp NO conectado (escanea el QR)'}</p>` +
        '<p>Endpoints: <code>/health</code> (público), <code>/status</code>, <code>/contacts</code>, <code>/send</code> (requieren token).</p>' +
        '<p>Configura este servicio (URL + token) desde el panel admin y escanea el QR desde ahí.</p>'
    )
)

app.get('/health', (req, res) => res.json({ ok: true }))

// Favicon: logo fan-out de Replica (SVG inline). Sirve la raíz, /qr y cualquier página del servicio.
const FAVICON =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><defs><linearGradient id="f" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#FD531E"/><stop offset="1" stop-color="#FD9E76"/></linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#f)"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 24c5 0 5.5-9 11.5-9"/><path d="M21 24h11.5"/><path d="M21 24c5 0 5.5 9 11.5 9"/></g><circle cx="15" cy="24" r="4.2" fill="#fff"/><circle cx="33.5" cy="15" r="3" fill="#fff"/><circle cx="34.5" cy="24" r="3" fill="#fff"/><circle cx="33.5" cy="33" r="3" fill="#fff"/></svg>'
app.get(['/favicon.ico', '/favicon.svg'], (req, res) =>
  res.set('Content-Type', 'image/svg+xml').set('Cache-Control', 'public, max-age=604800').send(FAVICON)
)

// Página de QR en vivo (mismo origen → sin CORS). El token va por query para abrirla
// directo en el navegador. El QR se auto-renueva y avisa cuando conecta.
app.get('/qr', (req, res) => {
  if ((req.query.token || '') !== TOKEN) return res.status(401).type('html').send('token inválido')
  res.type('html').send(
    '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<meta name="referrer" content="no-referrer">' + // evita filtrar el token por Referer
      '<title>Vincular WhatsApp · Replica</title>' +
      '<link rel="icon" href="/favicon.ico">' +
      '<body style="font-family:system-ui;text-align:center;background:#0b1020;color:#e6ebff;padding:28px;margin:0">' +
      '<h2>Vincular WhatsApp · Replica</h2><div id="s">cargando…</div>' +
      '<img id="q" style="width:300px;height:300px;margin:18px;background:#fff;border-radius:10px;padding:10px;object-fit:contain"/>' +
      '<div id="hint" style="color:#8b96b8">WhatsApp → Dispositivos vinculados → Vincular un dispositivo</div>' +
      '<script>const tok=new URLSearchParams(location.search).get("token");' +
      'async function tick(){try{const r=await fetch("/status",{headers:{Authorization:"Bearer "+tok}});const s=await r.json();' +
      'const S=document.getElementById("s"),Q=document.getElementById("q"),H=document.getElementById("hint");' +
      'if(s.connected){S.textContent="✅ Conectado"+(s.me?(" como "+s.me.id):"");Q.style.display="none";H.textContent="";return;}' +
      'if(s.pairingCode){Q.style.display="none";S.innerHTML="Código: <b style=\\"font-size:22px;letter-spacing:3px\\">"+s.pairingCode+"</b>";H.textContent="WhatsApp → Dispositivos vinculados → Vincular con número de teléfono.";}' +
      'else if(s.qr){Q.style.display="inline";Q.src=s.qr;S.textContent="Escanea el QR (se renueva solo):";}' +
      'else{S.textContent="esperando…";Q.style.display="none";}' +
      'if(s.lastPairError){H.textContent="WhatsApp dijo: "+s.lastPairError;}else if(!s.connected&&s.lastCloseMsg){H.textContent="Último cierre: "+s.lastCloseMsg;}' +
      '}catch(e){}setTimeout(tick,3000);}tick();</script>'
  )
})

app.get('/status', auth, (req, res) =>
  res.json({
    connected,
    me: me ? { id: me.id, name: me.name } : null,
    mode: pairNumber ? 'pairing' : 'qr',
    qr: currentQR,
    pairingCode,
    loggedOut,
    replaced,
    lastClose,
    lastCloseMsg: closeMsg(lastClose),
    lastError,
    lastPairError,
    contacts: Object.keys(contacts).length,
  })
)

// Vincular por código de 8 dígitos (alternativa al QR). Recibe el número con código de
// país (solo dígitos). Reinicia el socket pidiendo el código y lo devuelve.
app.post('/pair', auth, async (req, res) => {
  if (connected) return res.status(409).json({ error: 'ya_conectado' })
  const { number } = req.body || {}
  const limpio = String(number || '').replace(/[^0-9]/g, '')
  if (limpio.length < 8) return res.status(400).json({ error: 'numero_invalido' })
  pairNumber = limpio
  pairingCode = null
  lastPairError = null
  currentQR = null
  lastClose = null
  clearOnStart = true // empezar limpio: evita quedar atascado con una sesión registrada inválida
  await restart() // serializado: garantiza un socket nuevo que pide el código
  for (let i = 0; i < 20 && !pairingCode && !lastPairError; i++) await new Promise((r) => setTimeout(r, 1000))
  if (pairingCode) return res.json({ pairingCode, number: pairNumber })
  // Falló: limpiar el estado de emparejamiento y volver a modo QR para no quedar bloqueado.
  const detalle = lastPairError || 'no se generó el código a tiempo'
  pairNumber = null
  pairingCode = null
  restart() // vuelve a modo QR (sin await)
  return res.status(504).json({ error: 'sin_codigo', detalle })
})

// Fuerza la re-sincronización del address-book (contactos + nombres) sobre la sesión actual.
app.post('/sync', auth, async (req, res) => {
  if (!connected || !sock) return res.status(409).json({ error: 'whatsapp_no_conectado' })
  try {
    await sock.resyncAppState(['critical_unblock_low', 'critical_block', 'regular_high', 'regular_low', 'regular'], true)
    res.json({ ok: true, contacts: Object.keys(contacts).length })
  } catch (e) {
    res.status(500).json({ error: 'sync_fallo', detalle: String(e?.message || e) })
  }
})

// Reconecta releyendo las credenciales de DynamoDB SIN borrarlas. Útil para que un host
// (p.ej. Render) tome una sesión recién (re)vinculada por otro host, o salga de loggedOut.
app.post('/reconnect', auth, async (req, res) => {
  pairNumber = null
  pairingCode = null
  await restart()
  res.json({ ok: true })
})

// Reinicia desde cero: borra la sesión guardada y regenera QR. Útil si quedó en mal estado.
app.post('/reset', auth, async (req, res) => {
  pairNumber = null
  pairingCode = null
  currentQR = null
  lastClose = null
  lastError = null
  lastPairError = null
  clearOnStart = true
  await restart()
  res.json({ ok: true })
})

app.get('/contacts', auth, (req, res) => {
  const list = Object.entries(contacts)
    .filter(([id]) => id.endsWith('@s.whatsapp.net'))
    .map(([id, name]) => ({ id, name: name || id.split('@')[0] }))
  res.json({ contacts: list })
})

// Opt-out: contactos auto-excluidos por fallos repetidos de envío (>= BLOQUEO_UMBRAL).
app.get('/blocked', auth, (req, res) => {
  const blocked = Object.keys(failures)
    .filter((id) => (failures[id] || 0) >= BLOQUEO_UMBRAL)
    .map((id) => ({ id, name: contacts[id] || id.split('@')[0], fallos: failures[id] }))
  res.json({ umbral: BLOQUEO_UMBRAL, total: blocked.length, con_fallos: Object.keys(failures).length, blocked })
})

// Reinicia el conteo de fallos (re-incluye a los auto-excluidos).
app.post('/blocked/clear', auth, (req, res) => {
  for (const k of Object.keys(failures)) delete failures[k]
  scheduleSaveFailures()
  res.json({ ok: true })
})

// Resuelve a quién enviar según el modo de targeting y las listas de distribución.
// mode: "all" | "only" (whitelist sobre list_ids) | "except" (blacklist sobre list_ids).
// Compara contra el jid completo y contra el número (id sin @dominio).
function resolverTargets(mode, list_ids, exclude, exclude_patterns) {
  const ex = new Set((exclude || []).map(String))
  const sel = new Set((list_ids || []).map(String))
  // Patrones de auto-exclusión por NOMBRE (p. ej. "FAM"): substring sin distinguir mayúsculas.
  const pats = (exclude_patterns || []).map((p) => String(p).trim().toLowerCase()).filter(Boolean)
  const enSeleccion = (id) => sel.has(id) || sel.has(id.split('@')[0])
  const coincidePatron = (id) => {
    if (!pats.length) return false
    const nombre = String(contacts[id] || '').toLowerCase()
    return pats.some((p) => nombre.includes(p))
  }
  // .sort() -> orden ESTABLE: el conteo y los slices [offset,limit) del envío fraccionado
  // se mantienen coherentes entre llamadas aunque cambie el orden de inserción.
  return Object.keys(contacts)
    .filter((id) => {
      if (!id.endsWith('@s.whatsapp.net')) return false
      if (ex.has(id) || ex.has(id.split('@')[0])) return false
      if ((failures[id] || 0) >= BLOQUEO_UMBRAL) return false // opt-out: auto-excluido por fallos
      if (coincidePatron(id)) return false // auto-excluido por patrón de nombre
      if (mode === 'only') return enSeleccion(id)
      if (mode === 'except') return !enSeleccion(id)
      return true
    })
    .sort()
}

// Delay ALEATORIO en [lo, hi] ms entre mensajes (evita patrones predecibles / anti-baneo).
function delayAleatorio(lo, hi) {
  let a = Number(lo), b = Number(hi)
  if (!Number.isFinite(a)) a = SEND_DELAY_MS
  if (!Number.isFinite(b)) b = SEND_DELAY_MS
  if (b < a) { const t = a; a = b; b = t }
  if (b <= 0) return 0
  if (a < 0) a = 0
  return Math.floor(a + Math.random() * (b - a))
}

// Reporte de progreso del job (estado) al store de broadcasts en DynamoDB. Best-effort:
// nunca debe romper el envío. El nombre de la tabla y el id llegan en el payload de /send.
const ddb = new DynamoDBClient({ region: process.env.AWS_REGION || 'us-east-1' })
// Tabla de estados: preferimos la del entorno; si llega en el payload solo la aceptamos si
// tiene el sufijo esperado (no escribir a una tabla arbitraria dirigida desde fuera).
function bcTable(fromPayload) {
  if (process.env.BROADCASTS_TABLE) return process.env.BROADCASTS_TABLE
  if (typeof fromPayload === 'string' && /-broadcasts$/.test(fromPayload)) return fromPayload
  return null
}
async function bcSetTotal(table, id, total) {
  if (!table || !id) return
  try {
    await ddb.send(new UpdateItemCommand({
      TableName: table, Key: { id: { S: id } },
      UpdateExpression: 'SET wa_total = :t, wa_started = :b',
      ConditionExpression: 'attribute_exists(id)', // no crear items fantasma
      ExpressionAttributeValues: { ':t': { N: String(total) }, ':b': { BOOL: true } },
    }))
  } catch (e) { if (e.name !== 'ConditionalCheckFailedException') log.error({ err: String(e) }, 'bcSetTotal falló') }
}
async function bcIncr(table, id, sent, failed) {
  if (!table || !id || (!sent && !failed)) return
  try {
    await ddb.send(new UpdateItemCommand({
      TableName: table, Key: { id: { S: id } },
      UpdateExpression: 'ADD wa_sent :s, wa_failed :f',
      ConditionExpression: 'attribute_exists(id)',
      ExpressionAttributeValues: { ':s': { N: String(sent) }, ':f': { N: String(failed) } },
    }))
  } catch (e) { if (e.name !== 'ConditionalCheckFailedException') log.error({ err: String(e) }, 'bcIncr falló') }
}

async function enviarLote(text, image_url, targets, track) {
  const { table, id, bcTotal, delayMin, delayMax } = track || {}
  // En envío fraccionado, el total del JOB lo fija el llamador (bcTotal), no el del slice.
  await bcSetTotal(table, id, bcTotal != null ? bcTotal : targets.length)
  let sent = 0, failed = 0, sentDelta = 0, failedDelta = 0
  for (const jid of targets) {
    try {
      if (image_url) {
        await sock.sendMessage(jid, { image: { url: image_url } })
        if (text) await sock.sendMessage(jid, { text })
      } else {
        await sock.sendMessage(jid, { text })
      }
      sent++; sentDelta++
      if (failures[jid]) { delete failures[jid]; scheduleSaveFailures() } // envío OK -> limpia fallos
      await new Promise((r) => setTimeout(r, delayAleatorio(delayMin, delayMax))) // anti-baneo (jitter)
    } catch (e) {
      failed++; failedDelta++
      failures[jid] = (failures[jid] || 0) + 1; scheduleSaveFailures() // opt-out: cuenta el fallo
      log.error({ jid, err: String(e) }, 'fallo enviando')
    }
    if (sentDelta + failedDelta >= 20) { // progreso parcial cada ~20
      await bcIncr(table, id, sentDelta, failedDelta); sentDelta = 0; failedDelta = 0
    }
  }
  await bcIncr(table, id, sentDelta, failedDelta) // resto final
  log.info({ sent, failed, total: targets.length }, 'lote WhatsApp enviado')
}

// Fire-and-forget: responde de inmediato y envía en segundo plano (el envío a muchos
// contactos con delay tarda minutos; el backend no debe esperar).
//
// Soporta envío FRACCIONADO: el dispatcher resuelve el set completo aquí y pide solo el
// slice [offset, offset+limit). count_only devuelve cuántos resolvería (para planificar).
app.post('/send', auth, (req, res) => {
  const {
    text = '', image_url = null, exclude = [], mode = 'all', list_ids = [],
    broadcast_id = null, broadcasts_table = null,
    count_only = false, offset = null, limit = null, bc_total = null,
    delay_min_ms = null, delay_max_ms = null, exclude_patterns = [],
  } = req.body || {}
  const all = resolverTargets(mode, list_ids, exclude, exclude_patterns) // orden estable
  if (count_only) return res.json({ count: all.length, mode })
  if (!connected || !sock) return res.status(409).json({ error: 'whatsapp_no_conectado' })
  const off = Number(offset) || 0
  const slice = offset != null || limit != null
    ? all.slice(off, limit != null ? off + Number(limit) : undefined)
    : all
  res.status(202).json({ accepted: true, targets: slice.length, total: all.length, mode })
  enviarLote(text, image_url, slice, {
    table: bcTable(broadcasts_table),
    id: broadcast_id,
    bcTotal: bc_total != null ? Number(bc_total) : null,
    delayMin: delay_min_ms != null ? Number(delay_min_ms) : null,
    delayMax: delay_max_ms != null ? Number(delay_max_ms) : null,
  }).catch((e) => log.error({ err: String(e) }, 'enviarLote falló'))
})

app.listen(PORT, () => log.info(`whatsapp-service en :${PORT}`))
restart()
