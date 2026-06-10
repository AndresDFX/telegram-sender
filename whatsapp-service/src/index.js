// Servicio WhatsApp portable (Baileys). Mantiene la conexión de WhatsApp Web (sesión
// en DynamoDB), expone el QR para vincular desde el panel, lista contactos y envía las
// listas reenviadas desde Telegram. Protegido con un bearer token compartido.
import makeWASocket, { Browsers, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys'
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
let lastError = null // último error de arranque (DynamoDB/red)
let lastPairError = null // último error de requestPairingCode (p.ej. "inténtalo más tarde")
let clearOnStart = false // limpiar la sesión en DynamoDB antes del próximo arranque
let reconnectTimer = null // única reconexión pendiente
let gen = 0 // generación del socket activo (ignora eventos de sockets viejos)
let chain = Promise.resolve() // mutex: serializa los (re)arranques en vez de descartarlos
const contacts = {} // jid -> nombre

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
  contacts[c.id] = c.name || c.notify || c.verifiedName || contacts[c.id] || ''
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
    const { state, saveCreds, loadContacts, saveContacts } = await useDynamoAuthState(TABLE, SESSION_ID)
    persistContactsFn = saveContacts
    // Carga los contactos persistidos (solo si el mapa está vacío: en reconexiones ya están en memoria).
    if (Object.keys(contacts).length === 0) {
      try {
        Object.assign(contacts, await loadContacts())
      } catch (e) {
        log.error({ err: String(e) }, 'cargar contactos falló')
      }
    }
    const { version } = await fetchLatestBaileysVersion()
    if (gen !== myGen) return // otro arranque ganó mientras esperábamos
    const s = makeWASocket({
      version,
      auth: state,
      logger: pino({ level: 'silent' }),
      printQRInTerminal: false,
      browser: Browsers.macOS('Desktop'),
      markOnlineOnConnect: false,
      // Re-sincroniza el historial en cada conexión para poblar los contactos (la
      // reconexión simple no los reenvía). Una vez persistidos, se cargan al instante.
      syncFullHistory: process.env.SYNC_FULL_HISTORY !== 'false',
    })
    sock = s
    lastError = null

    s.ev.on('creds.update', saveCreds)
    s.ev.on('contacts.upsert', (cs) => cs.forEach(recordContact))
    s.ev.on('contacts.update', (cs) => cs.forEach(recordContact))
    s.ev.on('messaging-history.set', ({ contacts: cs }) => (cs || []).forEach(recordContact))

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
          // Sesión inválida: se limpia en el próximo arranque (bajo la cadena) y se regenera QR.
          pairingCode = null
          clearOnStart = true
          log.warn('Sesión cerrada (loggedOut); limpiando y regenerando QR...')
        } else {
          log.warn({ code: lastClose }, 'Conexión cerrada; reconectando...')
        }
        scheduleReconnect()
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
      '<h2>Sender · servicio WhatsApp</h2>' +
        `<p>Servicio activo ✓ · ${connected ? 'WhatsApp conectado' : 'WhatsApp NO conectado (escanea el QR)'}</p>` +
        '<p>Endpoints: <code>/health</code> (público), <code>/status</code>, <code>/contacts</code>, <code>/send</code> (requieren token).</p>' +
        '<p>Configura este servicio (URL + token) desde el panel admin y escanea el QR desde ahí.</p>'
    )
)

app.get('/health', (req, res) => res.json({ ok: true }))

// Página de QR en vivo (mismo origen → sin CORS). El token va por query para abrirla
// directo en el navegador. El QR se auto-renueva y avisa cuando conecta.
app.get('/qr', (req, res) => {
  if ((req.query.token || '') !== TOKEN) return res.status(401).type('html').send('token inválido')
  res.type('html').send(
    '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<meta name="referrer" content="no-referrer">' + // evita filtrar el token por Referer
      '<title>Vincular WhatsApp · Sender</title>' +
      '<body style="font-family:system-ui;text-align:center;background:#0b1020;color:#e6ebff;padding:28px;margin:0">' +
      '<h2>Vincular WhatsApp · Sender</h2><div id="s">cargando…</div>' +
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

// Resuelve a quién enviar según el modo de targeting y las listas de distribución.
// mode: "all" | "only" (whitelist sobre list_ids) | "except" (blacklist sobre list_ids).
// Compara contra el jid completo y contra el número (id sin @dominio).
function resolverTargets(mode, list_ids, exclude) {
  const ex = new Set((exclude || []).map(String))
  const sel = new Set((list_ids || []).map(String))
  const enSeleccion = (id) => sel.has(id) || sel.has(id.split('@')[0])
  return Object.keys(contacts).filter((id) => {
    if (!id.endsWith('@s.whatsapp.net')) return false
    if (ex.has(id) || ex.has(id.split('@')[0])) return false
    if (mode === 'only') return enSeleccion(id)
    if (mode === 'except') return !enSeleccion(id)
    return true
  })
}

async function enviarLote(text, image_url, targets) {
  let sent = 0
  let failed = 0
  for (const jid of targets) {
    try {
      if (image_url) {
        await sock.sendMessage(jid, { image: { url: image_url } })
        if (text) await sock.sendMessage(jid, { text })
      } else {
        await sock.sendMessage(jid, { text })
      }
      sent++
      await new Promise((r) => setTimeout(r, SEND_DELAY_MS)) // anti-baneo
    } catch (e) {
      failed++
      log.error({ jid, err: String(e) }, 'fallo enviando')
    }
  }
  log.info({ sent, failed, total: targets.length }, 'lote WhatsApp enviado')
}

// Fire-and-forget: responde de inmediato y envía en segundo plano (el envío a muchos
// contactos con delay tarda minutos; el backend no debe esperar).
app.post('/send', auth, (req, res) => {
  if (!connected || !sock) return res.status(409).json({ error: 'whatsapp_no_conectado' })
  const { text = '', image_url = null, exclude = [], mode = 'all', list_ids = [] } = req.body || {}
  const targets = resolverTargets(mode, list_ids, exclude)
  res.status(202).json({ accepted: true, targets: targets.length, mode })
  enviarLote(text, image_url, targets).catch((e) => log.error({ err: String(e) }, 'enviarLote falló'))
})

app.listen(PORT, () => log.info(`whatsapp-service en :${PORT}`))
restart()
