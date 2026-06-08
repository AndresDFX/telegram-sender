# **☁️ Arquitectura Serverless AWS: Telegram → Telegram (Sincronización 1:1 en Tiempo Real)**

**Objetivo:** Lograr una sincronización exacta orientada a eventos (1:1). Cada vez que el canal fuente de Telegram publica una lista, AWS la intercepta, aplica un markup del 15%, y la distribuye inmediatamente por **mensaje directo** a cada uno de los clientes.

**Estrategia:** Arquitectura 100% gratuita basada en Webhooks que reacciona en tiempo real, respetando estrictamente los límites de lectura y escritura de la API de Telegram para envíos masivos.

## **⚖️ Los Límites de Telegram (Entrada vs. Salida)**

Para que la recepción y el envío ocurran la misma cantidad de veces sin errores ni bloqueos temporales, debemos balancear estos límites oficiales:

1. **Recepción (Ingesta mediante Webhooks):** Telegram enviará un evento HTTP POST (Webhook) a tu servidor (AWS) **instantáneamente** cada vez que haya un mensaje nuevo en el canal fuente. No hay límite práctico de recepción diaria, pero tu servidor debe responder con un HTTP 200 OK rápido para que Telegram no asuma que falló.  
2. **Envío (Broadcasting a Usuarios Privados):** Límite estricto de **30 mensajes por segundo** a nivel global del Bot para mensajes directos a cuentas individuales.

## **📐 Arquitectura Orientada a Eventos (1:1)**

El sistema no funciona con cronogramas ni colas pesadas. Está "dormido" y solo despierta cuando Telegram le avisa que hay una nueva lista, ejecutando la distribución directa.

  \[ EVENTO: NUEVA LISTA EN CANAL FUENTE \]  
         │ (Ingreso inmediato vía Webhook)  
         ▼  
  Amazon API Gateway (Endpoint Webhook)  
         │  
         ▼  
  AWS Lambda (Parser & Broadcaster)   
         │ 1\. Responde rápido "HTTP 200" a Telegram para cerrar conexión.  
         │ 2\. Extrae el texto del payload, busca precios y aplica \+15%.  
         │ 3\. Consulta la base de datos de clientes.  
         ▼  
  Amazon DynamoDB (Tabla: "SubscriptoresTelegram")  
  Retorna \~1000 IDs de chat individuales  
         │  
         ▼ 4\. Bucle de envío seguro (Broadcasting)  
  Telegram Bot API ──► Mensaje Directo a Cliente 1  
                   ──► Mensaje Directo a Cliente 2  
                   ──► Mensaje Directo a Cliente 1000

## **🧮 Estrategia de Envío y Manejo de Límites**

Para entregar la lista procesada al chat privado de 1,000 clientes simultáneamente, la Lambda debe programarse para evitar el error anti-spam 429 Too Many Requests.

* **Táctica en Lambda:** Bucle iterativo con un retraso (time.sleep(0.05)) entre cada solicitud de envío.  
* **Rendimiento Seguro:** Enviar 1 mensaje cada 0.05 segundos equivale a **20 mensajes por segundo**, lo cual nos deja un margen seguro por debajo del límite duro de 30 msgs/seg de Telegram.  
* **Resultado:** La lista es detectada por AWS y, transcurridos aproximadamente **50 segundos**, la totalidad de los 1,000 clientes ya la han recibido en su chat privado.

## **⏱️ Código Seguro en AWS Lambda**

import time  
import requests  
import json

def lambda\_handler(event, context):  
    \# 1\. Parsear evento de entrada (Webhook de Telegram)  
    body \= json.loads(event.get('body', '{}'))  
      
    \# Ignorar si no es un mensaje del canal fuente  
    if 'channel\_post' not in body:  
        return {"statusCode": 200, "body": "OK"}  
          
    mensaje\_original \= body\['channel\_post'\]\['text'\]  
      
    \# 2\. Lógica de Negocio (+15%)  
    mensaje\_final \= aplicar\_markup(mensaje\_original)  
      
    \# 3\. Distribución respetando límites de Telegram  
    clientes \= obtener\_usuarios\_dynamodb() \# Lista de IDs (Ej. 1000 usuarios)  
      
    for chat\_id in clientes:  
        enviar\_telegram(chat\_id, mensaje\_final)  
          
        \# ⚠️ DELAY ANTIBAN CRÍTICO: 0.05s para respetar los 30 msg/seg  
        time.sleep(0.05)   
          
    \# El Webhook termina exitosamente tras \~50 segundos  
    return {"statusCode": 200, "body": "Sincronización 1:1 Completada en mensajes directos"}

## **⚙️ Análisis de Costos Elásticos (AWS Free Tier)**

Incluso si la frecuencia de sincronización sube drásticamente (ej. 30 listas publicadas al día), el esquema "Pago por Uso" (Serverless) se mantiene gratis:

Basado en **30 ejecuciones diarias** (recepción y envío directo 1:1 a mil usuarios):

| Componente AWS / API | Consumo Mensual (30 eventos/día) | Límite Free Tier AWS | Costo |
| :---- | :---- | :---- | :---- |
| **API Gateway** | \~900 peticiones | 1,000,000 peticiones | **$0.00** |
| **AWS Lambda** | 900 invocaciones (\~50 seg c/u) | 400,000 GB-segundos | **$0.00** |
| **AWS DynamoDB** | Lecturas de 1000 items / día | 25 GB, 25 WCU/RCU | **$0.00** |
| **Telegram API** | Recepción y envíos ilimitados | **ILIMITADO** | **$0.00** |
| **TOTAL MENSUAL** |  |  | **$0.00 USD** |

**💡 Conclusión:** La estrategia de envío por mensajes directos en tiempo real es perfectamente viable en Telegram. AWS absorberá el evento del Webhook de manera instantánea, procesará la lista y la repartirá uno a uno de forma metódica, manteniéndose siempre dentro de la capa gratuita sin importar el volumen de listas que proceses.

Correcciones viabilidad

**1\. Lambda tiene un timeout máximo de 15 minutos** Con 1000 usuarios × 0.05s \= \~50 segundos, en este caso específico está bien. Pero si escala a 2000–3000 usuarios, rompe el límite. Habría que replantear con SQS \+ Lambda en paralelo.

**2\. El código no maneja errores por usuario** Si un usuario bloqueó el bot, Telegram devuelve un error 403\. Tal como está, el `for` loop se rompe o ignora silenciosamente esos casos. Necesita un `try/except` por cada envío.

**3\. DynamoDB scan implícito** `obtener_usuarios_dynamodb()` sugiere un scan completo de la tabla en cada ejecución. Eso es costoso a escala y lento. Lo correcto sería usar una Query con índice o paginar con `LastEvaluatedKey`.

**4\. El análisis de costos asume Lambda con 128 MB** 900 invocaciones × 50 seg × 128 MB \= \~5,625 GB-seg/mes. Eso sí cabe en Free Tier (400,000 GB-seg), pero si subes la memoria (recomendable para I/O intensivo), el cálculo cambia.

**5\. No hay reintentos ni dead-letter queue** Si Lambda falla a mitad del envío, no hay forma de saber qué usuarios ya recibieron el mensaje y cuáles no. Un SQS DLQ resolvería esto elegantemente.

---

**🔧 Mejora sugerida para escalar**

Webhook → API Gateway → Lambda (solo parsea \+ encola)

                              ↓

                           SQS Queue (1 mensaje por usuario)

                              ↓

                    Lambda Worker (procesa en lotes, reintentos automáticos)

                              ↓

                         Telegram API

Esto desacopla la recepción del envío, sobrevive errores parciales y escala sin tocar el timeout de Lambda.

