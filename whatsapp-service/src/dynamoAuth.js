// Estado de autenticación de Baileys persistido en DynamoDB (un item por clave),
// para que la sesión de WhatsApp sobreviva reinicios/spin-down sin re-escanear el QR.
import { DynamoDBClient, GetItemCommand, PutItemCommand, ScanCommand, BatchWriteItemCommand } from '@aws-sdk/client-dynamodb'
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
  // M22/B11: escribe/borra en LOTES de 25 (límite de BatchWriteItem) con reintento de
  // UnprocessedItems (backoff lineal). Reemplaza los PutItem/DeleteItem sueltos en serie, que ante
  // un fallo parcial dejaban la sesión inconsistente (unas claves escritas y otras no). Devuelve
  // cuántos requests quedaron sin procesar tras los reintentos (0 = todo OK).
  const batchWrite = async (requests) => {
    let pendientes = 0
    for (let i = 0; i < requests.length; i += 25) {
      let lote = requests.slice(i, i + 25)
      for (let intento = 0; intento < 4 && lote.length; intento++) {
        const r = await ddb.send(new BatchWriteItemCommand({ RequestItems: { [table]: lote } }))
        const un = (r.UnprocessedItems && r.UnprocessedItems[table]) || []
        if (!un.length) { lote = []; break }
        lote = un
        await new Promise((res) => setTimeout(res, 100 * (intento + 1))) // backoff lineal entre reintentos
      }
      pendientes += lote.length
    }
    return pendientes
  }

  // Borra TODOS los items de esta sesión (creds + keys). Se usa al cerrar sesión
  // (loggedOut) para poder re-vincular sin borrar nada a mano en DynamoDB.
  const clearAll = async () => {
    const prefix = `${sessionId}::`
    const requests = []
    let startKey
    do {
      const r = await ddb.send(new ScanCommand({
        TableName: table,
        ProjectionExpression: 'id',
        FilterExpression: 'begins_with(id, :p)',
        ExpressionAttributeValues: { ':p': { S: prefix } },
        ExclusiveStartKey: startKey,
      }))
      for (const it of r.Items || []) requests.push({ DeleteRequest: { Key: { id: it.id } } })
      startKey = r.LastEvaluatedKey
    } while (startKey)
    // B11: si quedan ítems sin borrar tras los reintentos, LANZA: el caller (doStart) no debe dar
    // por consumido el clearOnStart con un borrado a medias (re-vincular sobre creds inconsistentes).
    const pendientes = await batchWrite(requests)
    if (pendientes) throw new Error(`clearAll: ${pendientes} ítems de sesión no se pudieron borrar`)
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
          // M22: persiste las claves de sesión en LOTE con reintento (no PutItem/DeleteItem sueltos
          // en serie, que ante un fallo parcial dejaban unas escritas y otras no → sesión corrupta).
          const requests = []
          for (const category in data) {
            for (const id in data[category]) {
              const value = data[category][id]
              const id_ = { S: pk(`${category}-${id}`) }
              requests.push(value
                ? { PutRequest: { Item: { id: id_, value: { S: JSON.stringify(value, BufferJSON.replacer) } } } }
                : { DeleteRequest: { Key: { id: id_ } } })
            }
          }
          const pendientes = await batchWrite(requests)
          if (pendientes) throw new Error(`keys.set: ${pendientes} claves de sesión no se persistieron`)
        },
      },
    },
    saveCreds: () => write('creds', creds),
  }
}
