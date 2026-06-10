// Estado de autenticación de Baileys persistido en DynamoDB (un item por clave),
// para que la sesión de WhatsApp sobreviva reinicios/spin-down sin re-escanear el QR.
import { DynamoDBClient, GetItemCommand, PutItemCommand, DeleteItemCommand } from '@aws-sdk/client-dynamodb'
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

  const creds = (await read('creds')) || initAuthCreds()

  return {
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
