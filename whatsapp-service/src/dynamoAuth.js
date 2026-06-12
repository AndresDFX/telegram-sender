// Estado de autenticación de Baileys persistido en DynamoDB (un item por clave),
// para que la sesión de WhatsApp sobreviva reinicios/spin-down sin re-escanear el QR.
import { DynamoDBClient, GetItemCommand, PutItemCommand, DeleteItemCommand, ScanCommand } from '@aws-sdk/client-dynamodb'
import { initAuthCreds, BufferJSON, proto } from '@whiskeysockets/baileys'

export async function useDynamoAuthState(table, sessionId, region) {
  const ddb = new DynamoDBClient({ region: region || process.env.AWS_REGION || 'us-east-1' })
  const pk = (k) => `${sessionId}::${k}`

  const read = async (k) => {
    const r = await ddb.send(new GetItemCommand({ TableName: table, Key: { id: { S: pk(k) } } }))
    if (!r.Item || !r.Item.value) return null
    return JSON.parse(r.Item.value.S, BufferJSON.reviver)
  }
  const write = async (k, v) => {
    await ddb.send(new PutItemCommand({
      TableName: table,
      Item: { id: { S: pk(k) }, value: { S: JSON.stringify(v, BufferJSON.replacer) } },
    }))
  }
  const remove = async (k) => {
    await ddb.send(new DeleteItemCommand({ TableName: table, Key: { id: { S: pk(k) } } }))
  }

  // Borra TODOS los items de esta sesión (creds + keys). Se usa al cerrar sesión
  // (loggedOut) para poder re-vincular sin borrar nada a mano en DynamoDB.
  const clearAll = async () => {
    const prefix = `${sessionId}::`
    let startKey
    do {
      const r = await ddb.send(new ScanCommand({
        TableName: table,
        ProjectionExpression: 'id',
        FilterExpression: 'begins_with(id, :p)',
        ExpressionAttributeValues: { ':p': { S: prefix } },
        ExclusiveStartKey: startKey,
      }))
      for (const it of r.Items || []) {
        await ddb.send(new DeleteItemCommand({ TableName: table, Key: { id: it.id } }))
      }
      startKey = r.LastEvaluatedKey
    } while (startKey)
  }

  const creds = (await read('creds')) || initAuthCreds()

  // Contactos persistidos (jid -> nombre): sobreviven reinicios/spin-down y migración de
  // host. Se guardan bajo el mismo prefijo de sesión, así clearAll() también los borra.
  const loadContacts = async () => (await read('__contacts__')) || {}
  const saveContacts = async (map) => write('__contacts__', map)

  // Conteo de fallos de envío por jid (opt-out anti-baneo): jids que fallan repetidamente se
  // auto-excluyen de los envíos. Persistido bajo el prefijo de sesión (clearAll también lo borra).
  const loadFailures = async () => (await read('__failures__')) || {}
  const saveFailures = async (map) => write('__failures__', map)

  return {
    clearAll,
    loadContacts,
    saveContacts,
    loadFailures,
    saveFailures,
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {}
          await Promise.all(ids.map(async (id) => {
            let value = await read(`${type}-${id}`)
            if (type === 'app-state-sync-key' && value) {
              value = proto.Message.AppStateSyncKeyData.fromObject(value)
            }
            data[id] = value
          }))
          return data
        },
        set: async (data) => {
          const tasks = []
          for (const category in data) {
            for (const id in data[category]) {
              const value = data[category][id]
              const key = `${category}-${id}`
              tasks.push(value ? write(key, value) : remove(key))
            }
          }
          await Promise.all(tasks)
        },
      },
    },
    saveCreds: () => write('creds', creds),
  }
}
