// Servicio WhatsApp portable (Baileys). Mantiene la conexión de WhatsApp Web (sesión
// en DynamoDB), expone el QR para vincular desde el panel, lista contactos y envía las
// listas reenviadas desde Telegram. Protegido con un bearer token compartido.
import makeWASocket, { DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys'
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
const contacts = {} // jid -> nombre

const log = pino({ level: 'info' })

function recordContact(c) {
  if (!c || !c.id) return
  contacts[c.id] = c.name || c.notify || c.verifiedName || contacts[c.id] || ''
}

async function startSock() {
  const { state, saveCreds } = await useDynamoAuthState(TABLE, SESSION_ID)
  const { version } = await fetchLatestBaileysVersion()
  sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    printQRInTerminal: false,
    browser: ['TelegramSender', 'Chrome', '1.0'],
    markOnlineOnConnect: false,
  })

  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('contacts.upsert', (cs) => cs.forEach(recordContact))
  sock.ev.on('contacts.update', (cs) => cs.forEach(recordContact))
  sock.ev.on('messaging-history.set', ({ contacts: cs }) => (cs || []).forEach(recordContact))

  sock.ev.on('connection.update', async (u) => {
    const { connection, lastDisconnect, qr } = u
    if (qr) {
      currentQR = await qrcode.toDataURL(qr)
      log.info('Nuevo QR disponible para vincular')
    }
    if (connection === 'open') {
      connected = true
      currentQR = null
      me = sock.user
      log.info({ me: me?.id }, 'WhatsApp conectado')
    }
    if (connection === 'close') {
      connected = false
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        log.warn('Sesión cerrada (loggedOut). Reinicia para re-escanear el QR.')
      } else {
        log.warn({ code }, 'Conexión cerrada; reconectando...')
        setTimeout(() => startSock().catch((e) => log.error(e)), 3000)
      }
    }
  })
}

const app = express()
app.use(express.json({ limit: '6mb' }))

function auth(req, res, next) {
  if (!TOKEN || req.get('authorization') !== `Bearer ${TOKEN}`) {
    return res.status(401).json({ error: 'unauthorized' })
  }
  next()
}

app.get('/health', (req, res) => res.json({ ok: true }))

app.get('/status', auth, (req, res) =>
  res.json({ connected, me: me ? { id: me.id, name: me.name } : null, qr: currentQR, contacts: Object.keys(contacts).length })
)

app.get('/contacts', auth, (req, res) => {
  const list = Object.entries(contacts)
    .filter(([id]) => id.endsWith('@s.whatsapp.net'))
    .map(([id, name]) => ({ id, name: name || id.split('@')[0] }))
  res.json({ contacts: list })
})

async function enviarLote(text, image_url, exclude) {
  const ex = new Set((exclude || []).map(String))
  const targets = Object.keys(contacts).filter(
    (id) => id.endsWith('@s.whatsapp.net') && !ex.has(id) && !ex.has(id.split('@')[0])
  )
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
  const { text = '', image_url = null, exclude = [] } = req.body || {}
  const ex = new Set((exclude || []).map(String))
  const total = Object.keys(contacts).filter(
    (id) => id.endsWith('@s.whatsapp.net') && !ex.has(id) && !ex.has(id.split('@')[0])
  ).length
  res.status(202).json({ accepted: true, targets: total })
  enviarLote(text, image_url, exclude).catch((e) => log.error({ err: String(e) }, 'enviarLote falló'))
})

app.listen(PORT, () => log.info(`whatsapp-service en :${PORT}`))
startSock().catch((e) => log.error(e))
