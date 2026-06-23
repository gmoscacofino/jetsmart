# 01 — Cómo funciona un chatbot

## El concepto base

Un chatbot es un programa que recibe texto y devuelve texto. La complejidad no está en el concepto, sino en **dónde vive ese programa** y **cómo decide qué responder**.

```
[Usuario escribe algo]
        ↓
[Algún sistema lo procesa]
        ↓
[El sistema devuelve una respuesta]
        ↓
[Usuario ve la respuesta]
```

---

## Frontend y backend

Toda aplicación web tiene dos partes:

**Frontend** — lo que el usuario ve. En este caso, la página del chatbot: el cuadro de texto, los mensajes, el diseño. Son archivos estáticos (HTML, CSS, JavaScript) que el navegador descarga y muestra. En este proyecto viven en S3 (static website hosting).

**Backend** — el código que corre en un servidor, invisible para el usuario. En este proyecto, el core del chatbot (`chat-handler`) es un servicio FastAPI que corre en **ECS Fargate**, dentro de la VPC: recibe el request HTTP, procesa el mensaje y devuelve una respuesta. Alrededor hay funciones Lambda —la mayoría también dentro de la VPC, en subnets `private-lambda`— para tareas asincrónicas (saga de pago, notificaciones, analytics, etc.).

---

## ¿Qué es una API?

Cuando el backend necesita hablar con otro sistema — por ejemplo, pedirle a una IA que genere una respuesta — lo hace a través de una **API**.

Una API es un canal de comunicación entre sistemas. El backend manda un pedido (HTTP request), el otro sistema lo procesa y devuelve una respuesta (JSON).

```
Tu servicio (chat-handler)        API de Anthropic
          │                               │
          │──── "El usuario preguntó X" ──→│
          │                               │  (procesa)
          │←─── "La respuesta es Y" ───── │
```

---

## El ingrediente clave: el LLM

Un chatbot conversacional usa un **LLM** (Large Language Model) — el modelo de inteligencia artificial que entiende lenguaje natural. Claude (Anthropic), GPT (OpenAI) son ejemplos.

El LLM no vive en tu servidor. Lo usás llamando a su API, igual que usás Google Maps sin construir los mapas vos mismo.

> Nosotros no construimos la IA. Construimos la **aplicación** que usa la IA.

---

## Cómo el backend habla con el LLM

Cada vez que el usuario manda un mensaje, el chat-handler construye un "paquete" con tres partes y se lo manda al LLM:

```
┌─────────────────────────────────────────────────────┐
│ 1. SYSTEM PROMPT (instrucciones fijas)              │
│    "Sos el asistente de JetSmart. Ayudás a         │
│     reservar vuelos. Las rutas disponibles son..."  │
│                                                     │
│ 2. HISTORIAL DE CONVERSACIÓN                        │
│    [todo lo que se dijo antes en este chat]         │
│                                                     │
│ 3. MENSAJE NUEVO DEL USUARIO                        │
│    "quiero volar a Mendoza el 15 de junio"          │
└─────────────────────────────────────────────────────┘
                        ↓
              LLM genera una respuesta
```

### El system prompt

Es el texto de instrucciones que le da identidad al chatbot. Acá se define:
- Qué es (el asistente de JetSmart)
- Qué puede hacer (reservar vuelos, hacer check-in, etc.)
- Qué datos conoce (rutas disponibles, precios mock, reglas de equipaje)
- Cómo debe comportarse

### Por qué el LLM no "recuerda" solo

El LLM no tiene memoria entre llamadas. Cada vez que lo llamás, es como la primera vez. Por eso el chat-handler le manda **los últimos 40 mensajes** (`MAX_HISTORY = 40`) junto con el mensaje nuevo. Ese historial se guarda en DynamoDB y se rescata con un Query limitado.

```
DynamoDB — historial de una sesión:
[
  { rol: "usuario",    mensaje: "quiero volar a Mendoza" },
  { rol: "asistente", mensaje: "¿Para qué fecha?" },
  { rol: "usuario",    mensaje: "el 15 de junio" },
  { rol: "asistente", mensaje: "¿Cuántos pasajeros?" },
  ...
]
```

---

## El flujo completo de un mensaje

```
Usuario escribe "quiero volar a Mendoza"
        ↓
Frontend (S3) hace POST al ALB (POST /api/chat)
+ incluye el Access Token de Cognito en el header
        ↓
ALB enruta el request al chat-handler (Fargate)
        ↓
chat-handler valida el token in-app (firma RS256 contra el JWKS de Cognito)
        ↓
chat-handler carga el historial de esta conversación desde DynamoDB
        ↓
chat-handler construye el prompt:
  [system prompt] + [historial] + [mensaje nuevo]
        ↓
chat-handler llama a la API de Anthropic
(usando la key guardada en Secrets Manager)
        ↓
Anthropic devuelve la respuesta del chatbot
        ↓
chat-handler guarda el intercambio nuevo en DynamoDB (sincrónico)
        ↓
chat-handler publica evento en SNS → SQS → analytics → S3 data lake (asincrónico)
        ↓
chat-handler devuelve la respuesta al frontend
        ↓
Frontend muestra el mensaje al usuario
```

La escritura en DynamoDB (historial) es **sincrónica** — se hace antes de devolver la respuesta porque el siguiente mensaje la necesita. La escritura en SNS (analytics) es **asincrónica** — no hace esperar al usuario.

---

## Tool use: cómo el chatbot consulta datos reales

### El problema sin tool use

Si le preguntás al chatbot "¿hay vuelos de Buenos Aires a Santiago el 20 de junio?", el LLM por sí solo tiene un problema: no tiene acceso a la base de datos de JetSmart. Va a responder con lo que aprendió durante su entrenamiento, que puede estar desactualizado o ser incorrecto para una aerolínea específica.

```
Sin tool use:
  Usuario: "¿hay vuelos BUE→SCL el 20 de junio?"
  Claude: "JetSmart no opera esa ruta" ← respuesta incorrecta, basada en entrenamiento
```

### La solución: tool use (function calling)

**Tool use** es un mecanismo por el cual el LLM puede pausar su respuesta, pedir que se ejecute una función externa, y recibir los resultados antes de continuar.

En lugar de que el LLM adivine, le damos herramientas que puede invocar:

```
Con tool use:
  Usuario: "¿hay vuelos BUE→SCL el 20 de junio?"
  Claude: "Necesito consultar disponibilidad" → invoca search_flights(origen=AEP, destino=SCL, fecha=2026-06-20)
  chat-handler ejecuta la herramienta → obtiene datos reales de DynamoDB
  Claude recibe los datos → "Sí, el vuelo JA-201 sale a las 08:00, precio $85 USD"
```

### Las herramientas disponibles

El chatbot tiene declaradas diez herramientas, agrupadas por familia:

**Búsqueda y consulta (read-only sobre el PSS)**

| Herramienta | Cuándo la usa Claude | Datos que devuelve |
|---|---|---|
| `list_flight_dates` | El usuario quiere saber cuándo puede volar de A a B sin fecha fija | Fechas con asientos, número de vuelo, hora de salida, precio desde |
| `search_flights` | El usuario pregunta por un vuelo concreto en una fecha | Número de vuelo, horarios, precio por pasajero, asientos disponibles |
| `get_reservation` | El usuario pregunta por el estado de una reserva por PNR | Estado, origen, destino, fecha, pasajeros, total |
| `list_user_reservations` | El usuario pide "mis reservas" | Hasta 20 reservas del usuario autenticado |
| `list_saved_passengers` | El usuario quiere reusar un pasajero de un viaje anterior | Nombres derivados de reservas pasadas |

**Operaciones transaccionales**

| Herramienta | Cuándo la usa Claude | Efecto |
|---|---|---|
| `create_reservation` | El usuario confirmó explícitamente todos los detalles de compra | Arranca el Step Functions de pago (Saga); devuelve `payment_id` |
| `check_in` | El usuario pide check-in y el vuelo es en ≤24 h | Cambia el status de la reserva a `CHECK-IN` |
| `get_boarding_pass` | El usuario ya hizo check-in y pide el BP | URL del BP si está listo, o "procesando" si la generación asincrónica no terminó |

**Soporte**

| Herramienta | Cuándo la usa Claude | Efecto |
|---|---|---|
| `create_claim` | El usuario reporta equipaje, demoras, cancelaciones, reembolsos | Crea claim con `CLM-xxxx`; emite evento SNS |
| `escalate_to_human` | El usuario lo pide explícitamente, hay frustración alta, o el caso queda fuera del alcance del bot | Encola handoff en SQS con `ticket_id`; el call center lo retoma con contexto |

### El bucle de tool use

El chat-handler implementa un bucle de hasta 5 rondas:

```
┌─────────────────────────────────────────────────────────────────┐
│  for _ in range(MAX_TOOL_ROUNDS):                               │
│                                                                 │
│    resp = claude.messages.create(messages=..., tools=TOOLS)     │
│                                                                 │
│    if resp.stop_reason != "tool_use":                           │
│        break  ← Claude respondió con texto, terminamos         │
│                                                                 │
│    # Claude quiere usar herramientas                            │
│    for tc in resp.content donde tc.type == "tool_use":          │
│        resultado = _execute_tool(tc.name, tc.input)             │
│        tool_results.append(resultado)                           │
│                                                                 │
│    # Agregar al historial: respuesta de Claude + resultados     │
│    messages.append({"role": "assistant", "content": resp})      │
│    messages.append({"role": "user",      "content": results})   │
│                                                                 │
│  texto_final = primer bloque de texto en resp.content           │
└─────────────────────────────────────────────────────────────────┘
```

Claude puede invocar múltiples herramientas en una sola ronda. En la ronda siguiente, recibe todos los resultados y decide si necesita más datos o si ya puede responder.

### Cómo funciona en la API de Anthropic

Al llamar a `messages.create()`, se pasa la lista de herramientas disponibles. Claude devuelve un bloque de tipo `tool_use` con el nombre de la herramienta y los parámetros que eligió. El chat-handler ejecuta la herramienta y devuelve el resultado en un bloque `tool_result`. Claude entonces genera la respuesta final en lenguaje natural.

```python
# El chat-handler le declara las herramientas a Claude:
TOOLS = [
    {
        "name": "search_flights",
        "description": "Busca vuelos disponibles entre dos ciudades...",
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":    {"type": "string", "description": "Código IATA (ej: AEP, SCL)"},
                "destino":   {"type": "string"},
                "fecha":     {"type": "string", "description": "Formato YYYY-MM-DD"},
                "pasajeros": {"type": "integer"},
            },
            "required": ["origen", "destino", "fecha"],
        },
    },
    ...
]
```

Claude decide cuándo y cómo llamarlas según la pregunta del usuario — el chat-handler no le dice explícitamente "usá esta herramienta".

### La tabla `business` ES el PSS

En esta arquitectura la tabla DynamoDB `business` no simula al PSS (Passenger Service System) — **es** el PSS de la aerolínea. El chatbot es uno de los canales que lo consumen; los otros canales (web, app móvil, agencias, IVR, call center) consultan la misma fuente.

```python
def _execute_tool(name, inputs, user_id):
    if name == "search_flights":
        resp = biz_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk":     f"FLIGHT#{origen}#{destino}",
                ":prefix": f"DATE#{fecha}#",
            },
        )
        return json.dumps(resp.get("Items", []))
```

No hay "modo demo" ni "modo prod" — el chat-handler hace `Query` a la business table igual hoy que en una hipotética operación real. El TP carga un dataset de vuelos cuando se hace `terraform apply` (ver `seed.py`), pero el esquema (`PK=FLIGHT#{origen}#{destino}` / `SK=DATE#{fecha}#FLIGHT#{vuelo}` + GSIs por número de vuelo, por reserva y por pasajero) es el que tendría un PSS de verdad.

```
Todos los canales de la aerolínea consultan el mismo PSS:

  App móvil  ──→                                                  ┌─→ DynamoDB
  Sitio web  ──→  API Gateway / API canal ──→ Lambda / servicio ──┤    business
  Chatbot    ──→  (este TP modela el del chatbot)                 │    (PSS)
  Agencias   ──→                                                  └─→
                                                       (fuente única de verdad)
```

### El flujo completo con tool use

```
Usuario: "¿hay vuelos de AEP a SCL el 20 de junio para 2 personas?"
        ↓
Frontend hace POST /api/chat (vía ALB)
        ↓
chat-handler (Fargate) recibe el mensaje
        ↓
chat-handler llama a Claude con TOOLS declaradas
        ↓
Claude responde: stop_reason = "tool_use"
  → quiere ejecutar search_flights(origen=AEP, destino=SCL, fecha=2026-06-20, pasajeros=2)
        ↓
chat-handler ejecuta _execute_tool("search_flights", {...})
  → consulta DynamoDB business (el PSS)
  → obtiene: vuelo JA-201, salida 08:00, precio $85, asientos disponibles: 142
        ↓
chat-handler devuelve el resultado a Claude como tool_result
        ↓
Claude responde: stop_reason = "end_turn"
  → genera texto: "Sí, hay un vuelo disponible. El JA-201 sale a las 08:00..."
        ↓
chat-handler guarda el intercambio en DynamoDB
chat-handler publica evento en SNS → analytics
chat-handler devuelve la respuesta al frontend
```

---

## Por qué la API key nunca va en el frontend

Si el frontend llamara a Anthropic directamente, la API key quedaría visible en el código JavaScript del navegador — cualquier persona podría robarla y usarla a nuestro costo.

```
✗ INCORRECTO: Frontend → Anthropic (key expuesta en el browser)
✓ CORRECTO:   Frontend → ALB → chat-handler (Fargate) → Anthropic (key en Secrets Manager)
```

La API key **nunca sale del chat-handler**.

---

## La diferencia con un chatbot de script fijo

Hay dos tipos de chatbots:

| Tipo | Cómo funciona | Limitación |
|---|---|---|
| Script fijo | Sigue pasos predefinidos: "¿Cuál es tu origen?" → espera respuesta → "¿Cuál es tu destino?" | El usuario tiene que responder exactamente lo esperado |
| Conversacional (LLM) | El usuario escribe en lenguaje natural, la IA entiende el contexto | Más flexible, más natural |

Nuestro chatbot es **conversacional**: el usuario puede escribir "quiero volar de BUE a MZA el 15 de junio para 2 personas" en un solo mensaje y el LLM entiende todo.
