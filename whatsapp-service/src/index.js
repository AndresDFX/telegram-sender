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
let starting = false // lock: evita arrancar sockets en paralelo
let reconnectTimer = null // única reconexión pendiente
let gen = 0 // generación del socket activo (ignora eventos de sockets viejos)
const contacts = {} // jid -> nombre

const log = pino({ level: 'info' })

function recordContact(c) {
  if (!c || !c.id) return
  contacts[c.id] = c.name || c.notify || c.verifiedName || contacts[c.id] || ''
}

function scheduleReconnect() {
  if (reconnectTimer) return
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    startSock().catch((e) => log.error({ err: String(e) }, 'reconexión falló'))
  }, 3000)
}

async function startSock() {
  if (starting) return // un solo arranque a la vez (evita sockets en paralelo)
  starting = true
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
  try {
    const { state, saveCreds } = await useDynamoAuthState(TABLE, SESSION_ID)
    const { version } = await fetchLatestBaileysVersion()
    const s = makeWASocket({
      version,
      auth: state,
      logger: pino({ level: 'silent' }),
      printQRInTerminal: false,
      browser: Browsers.macOS('Desktop'),
      markOnlineOnConnect: false,
    })
    sock = s

    s.ev.on('creds.update', saveCreds)
    s.ev.on('contacts.upsert', (cs) => cs.forEach(recordContact))
    s.ev.on('contacts.update', (cs) => cs.forEach(recordContact))
    s.ev.on('messaging-history.set', ({ contacts: cs }) => (cs || []).forEach(recordContact))

    // Vinculación por código (8 dígitos), una sola vez por socket si hay número y no está registrado.
    if (pairNumber && !s.authState.creds.registered) {
      setTimeout(async () => {
        if (gen !== myGen) return // socket reemplazado: no pidas código en uno viejo
        try {
          pairingCode = await s.requestPairingCode(pairNumber)
          log.info({ pairingCode }, 'Código de emparejamiento generado')
        } catch (e) {
          log.error({ err: String(e) }, 'requestPairingCode falló')
        }
      }, 3000)
    }

    s.ev.on('connection.update', async (u) => {
      if (gen !== myGen) return // ignora eventos de sockets de generaciones anteriores
      const { connection, lastDisconnect, qr } = u
      if (qr) {
        currentQR = await qrcode.toDataURL(qr)
        log.info('Nuevo QR disponible para vincular')
      }
      if (connection === 'open') {
        connected = true
        currentQR = null
        pairingCode = null
        pairNumber = null
        lastClose = null
        me = s.user
        log.info({ me: me?.id }, 'WhatsApp conectado')
      }
      if (connection === 'close') {
        connected = false
        const code = lastDisconnect?.error?.output?.statusCode
        lastClose = code ?? null
        const registered = !!s.authState?.creds?.registered
        if (code === DisconnectReason.loggedOut) {
          pairingCode = null
          log.warn('Sesión cerrada (loggedOut).')
        } else if (registered) {
          log.warn({ code }, 'Sesión registrada cerrada; reconectando...')
          scheduleReconnect()
        } else if (pairNumber) {
          // Vinculando por código: NO reconectar (evita invalidar el código y la tormenta).
          log.warn({ code }, 'Cierre durante emparejamiento; esperando reintento manual.')
        } else {
          // Modo QR sin registrar: refrescar el QR una sola vez.
          log.warn({ code }, 'Cierre en modo QR; renovando QR...')
          scheduleReconnect()
        }
      }
    })
  } finally {
    starting = false
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
      '<title>Vincular WhatsApp · Sender</title>' +
      '<body style="font-family:system-ui;text-align:center;background:#0b1020;color:#e6ebff;padding:28px;margin:0">' +
      '<h2>Vincular WhatsApp · Sender</h2><div id="s">cargando…</div>' +
      '<img id="q" style="width:300px;height:300px;margin:18px;background:#fff;border-radius:10px;padding:10px;object-fit:contain"/>' +
      '<p style="color:#8b96b8">WhatsApp → Dispositivos vinculados → Vincular un dispositivo</p>' +
      '<script>const tok=new URLSearchParams(location.search).get("token");' +
      'async function tick(){try{const r=await fetch("/status",{headers:{Authorization:"Bearer "+tok}});const s=await r.json();' +
      'if(s.connected){document.getElementById("s").textContent="✅ Conectado"+(s.me?(" como "+s.me.id):"");document.getElementById("q").style.display="none";return;}' +
      'if(s.qr){document.getElementById("q").src=s.qr;document.getElementById("s").textContent="Escanea el QR (se renueva solo):";}' +
      'else{document.getElementById("s").textContent="esperando QR…";}}catch(e){}setTimeout(tick,3000);}tick();</script>'
  )
})

app.get('/status', auth, (req, res) =>
  res.json({
    connected,
    me: me ? { id: me.id, name: me.name } : null,
    qr: currentQR,
    pairingCode,
    lastClose,
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
  await startSock() // reinicia limpio (un solo socket) y pide el código
  for (let i = 0; i < 15 && !pairingCode; i++) await new Promise((r) => setTimeout(r, 1000))
  if (!pairingCode) return res.status(504).json({ error: 'sin_codigo', detalle: 'no se generó el código a tiempo' })
  res.json({ pairingCode, number: pairNumber })
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
startSock().catch((e) => log.error(e))
