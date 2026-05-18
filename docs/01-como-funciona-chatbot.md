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

**Backend** — el código que corre en un servidor, invisible para el usuario. En este proyecto, el backend son funciones Lambda: se activan cuando llega un request, procesan el mensaje y devuelven una respuesta.

---

## ¿Qué es una API?

Cuando el backend necesita hablar con otro sistema — por ejemplo, pedirle a una IA que genere una respuesta — lo hace a través de una **API**.

Una API es un canal de comunicación entre sistemas. El backend manda un pedido (HTTP request), el otro sistema lo procesa y devuelve una respuesta (JSON).

```
Tu Lambda (chat-handler)          API de Anthropic
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

Cada vez que el usuario manda un mensaje, la Lambda construye un "paquete" con tres partes y se lo manda al LLM:

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

El LLM no tiene memoria entre llamadas. Cada vez que lo llamás, es como la primera vez. Por eso la Lambda siempre le manda **todo el historial** junto con el mensaje nuevo. Ese historial se guarda en DynamoDB.

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
Frontend (S3) hace POST al API Gateway
+ incluye el Access Token de Cognito en el header
        ↓
API Gateway invoca la Lambda chat-handler
        ↓
Lambda verifica que el token sea válido (Cognito)
        ↓
Lambda carga el historial de esta conversación desde DynamoDB
        ↓
Lambda construye el prompt:
  [system prompt] + [historial] + [mensaje nuevo]
        ↓
Lambda llama a la API de Anthropic
(usando la key guardada en Secrets Manager)
        ↓
Anthropic devuelve la respuesta del chatbot
        ↓
Lambda guarda el intercambio nuevo en DynamoDB (sincrónico)
        ↓
Lambda publica evento en SNS → SQS → analytics-processor → RDS (asincrónico)
        ↓
Lambda devuelve la respuesta al frontend
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
  Lambda ejecuta la herramienta → obtiene datos reales de DynamoDB
  Claude recibe los datos → "Sí, el vuelo JA-201 sale a las 08:00, precio $85 USD"
```

### Las herramientas disponibles

El chatbot tiene declaradas dos herramientas:

| Herramienta | Cuándo la usa Claude | Datos que devuelve |
|---|---|---|
| `search_flights` | El usuario pregunta por disponibilidad, precios o itinerarios | Número de vuelo, horarios, precio por pasajero, asientos disponibles |
| `get_reservation` | El usuario pregunta por el estado de una reserva | Estado, origen, destino, fecha, total |

### El bucle de tool use

La Lambda implementa un bucle de hasta 5 rondas:

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

Al llamar a `messages.create()`, se pasa la lista de herramientas disponibles. Claude devuelve un bloque de tipo `tool_use` con el nombre de la herramienta y los parámetros que eligió. La Lambda ejecuta la herramienta y devuelve el resultado en un bloque `tool_result`. Claude entonces genera la respuesta final en lenguaje natural.

```python
# La Lambda le declara las herramientas a Claude:
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

Claude decide cuándo y cómo llamarlas según la pregunta del usuario — la Lambda no le dice explícitamente "usá esta herramienta".

### Integración con la API real de JetSmart

En este TP, `_execute_tool` consulta DynamoDB para obtener los datos de vuelos. DynamoDB actúa como una simulación del **PSS (Passenger Service System)**: el sistema de reservas central que usan las aerolíneas reales.

En producción, la misma función llamaría a la API interna de JetSmart:

```python
# Implementación en este TP (DynamoDB simula el PSS):
def _execute_tool(name, inputs, user_id):
    if name == "search_flights":
        resp = table.get_item(
            Key={"PK": f"FLIGHT#{origen}#{destino}", "SK": f"DATE#{fecha}"}
        )
        return json.dumps(resp.get("Item", {}))

# Implementación en producción (API interna de JetSmart):
def _execute_tool(name, inputs, user_id):
    if name == "search_flights":
        resp = requests.get(
            "https://api-interna.jetsmart.com/availability",
            params={"origin": origen, "destination": destino, "date": fecha},
            headers={"Authorization": f"Bearer {JETSMART_INTERNAL_TOKEN}"},
        )
        return json.dumps(resp.json())
```

La interfaz hacia Claude es exactamente igual en ambos casos. La única diferencia es que en producción `_execute_tool` hace una llamada HTTP al PSS real en lugar de leer DynamoDB.

```
Todos los canales de JetSmart consumen el mismo PSS:

  App móvil  ──→ API Gateway → chatbot Lambda → _execute_tool → PSS (API interna)
  Sitio web  ──→                                                     ↑
  Agencias   ──→────────────────────────────────────────────────────→│
                                                          (fuente única de verdad)
```

### El flujo completo con tool use

```
Usuario: "¿hay vuelos de AEP a SCL el 20 de junio para 2 personas?"
        ↓
Frontend hace POST /api/chat
        ↓
Lambda chat-handler recibe el mensaje
        ↓
Lambda llama a Claude con TOOLS declaradas
        ↓
Claude responde: stop_reason = "tool_use"
  → quiere ejecutar search_flights(origen=AEP, destino=SCL, fecha=2026-06-20, pasajeros=2)
        ↓
Lambda ejecuta _execute_tool("search_flights", {...})
  → consulta DynamoDB (o en producción: API interna JetSmart)
  → obtiene: vuelo JA-201, salida 08:00, precio $85, asientos disponibles: 142
        ↓
Lambda devuelve el resultado a Claude como tool_result
        ↓
Claude responde: stop_reason = "end_turn"
  → genera texto: "Sí, hay un vuelo disponible. El JA-201 sale a las 08:00..."
        ↓
Lambda guarda el intercambio en DynamoDB
Lambda publica evento en SNS → analytics
Lambda devuelve la respuesta al frontend
```

---

## Por qué la API key nunca va en el frontend

Si el frontend llamara a Anthropic directamente, la API key quedaría visible en el código JavaScript del navegador — cualquier persona podría robarla y usarla a nuestro costo.

```
✗ INCORRECTO: Frontend → Anthropic (key expuesta en el browser)
✓ CORRECTO:   Frontend → API Gateway → Lambda → Anthropic (key en Secrets Manager)
```

La API key **nunca sale de la Lambda**.

---

## La diferencia con un chatbot de script fijo

Hay dos tipos de chatbots:

| Tipo | Cómo funciona | Limitación |
|---|---|---|
| Script fijo | Sigue pasos predefinidos: "¿Cuál es tu origen?" → espera respuesta → "¿Cuál es tu destino?" | El usuario tiene que responder exactamente lo esperado |
| Conversacional (LLM) | El usuario escribe en lenguaje natural, la IA entiende el contexto | Más flexible, más natural |

Nuestro chatbot es **conversacional**: el usuario puede escribir "quiero volar de BUE a MZA el 15 de junio para 2 personas" en un solo mensaje y el LLM entiende todo.
