# 07 — Capa de datos: DynamoDB (dos tablas, bounded contexts) + Data Lake (S3 + Athena)

> **Cambio TP4:** la única tabla DynamoDB del TP3 se separó en **dos tablas single-design**, una por bounded context. El esquema de reservas migró a un patrón **PNR-céntrico** (record locator de 6 chars, à la Navitaire/Amadeus), con 3 GSIs en la tabla de negocio.

---

## DynamoDB — Dos tablas (Conversations + Business / PSS-like)

### Por qué dos tablas y no una

El TP3 usaba single-table design para todo el sistema. Al introducir las features de TP4 (derivación a humano, notificaciones proactivas), quedó claro que el dominio tenía **dos contextos lógicos** distintos:

| Contexto | Característica | Datos |
|---|---|---|
| **Conversations** | Estado efímero del canal chatbot | Sesiones, mensajes, perfil chat-scoped, escalaciones a humano |
| **Business (PSS-like)** | Estado persistente del dominio de aerolínea | Vuelos, reservas (PNRs), pasajeros (CRM), reclamos, boarding passes |

Cada contexto tiene **retention, scaling y propiedad** distintas. La tabla de conversations es propiedad del canal "chatbot" — si mañana ese chatbot se reemplaza por otro stack, los datos son portables. La tabla de business es propiedad de la aerolínea — la comparten todos los canales (chatbot, web, app, IVR, call center).

Cada tabla **sí** mantiene single-table design dentro de su contexto — distintas entidades comparten PK/SK con prefijos.

### Beneficios concretos del split

- **Failure isolation**: si la tabla de chat se satura/throttle, no afecta las reservas. Y viceversa.
- **Retention independiente**: TTL agresivo en conversations (7d msgs, 30d handoffs) vs persistencia en business.
- **Backup window independiente**: ambas tienen PITR + export diario, pero podrían divergir.
- **Cost attribution**: cuánto cuesta operar el canal chatbot vs el core de negocio.
- **Encryption key independiente**: hoy ambas usan SSE-S3, pero podrían usar KMS distintas (compliance segmentation).
- **Reemplazabilidad del chatbot**: la conversation table es completamente desechable si se reemplaza el canal.

---

### Tabla 1 — `jetsmart-prod-conversations`

Estado efímero del chatbot. PK/SK, **sin GSIs**, TTL en todos los items.

| Entidad | PK | SK | TTL | Descripción |
|---|---|---|---|---|
| Mensaje de chat | `SESSION#{session_id}` | `MSG#{ts}#{uuid8}` | 7d | Historial conversacional (igual que TP3) |
| Perfil chat-scoped | `USER#{user_id}` | `#METADATA` | 30d | Email + last_seen — fuente para el LLM context |
| **Handoff ticket (TP4)** | `SESSION#{session_id}` | `HANDOFF#{ts}#{handoff_id}` | 30d | Ticket de derivación a humano (status=QUEUED → ACK) |
| **Handoff pointer (TP4)** | `USER#{user_id}` | `HANDOFF#{handoff_id}` | 30d | Thin pointer para "mis tickets de soporte" |

---

### Tabla 2 — `jetsmart-prod-business` (PSS-like)

Estado persistente del dominio. PK/SK, **3 GSIs**, sin TTL.

| Entidad | PK | SK | Descripción |
|---|---|---|---|
| Vuelo (inventario) | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}#FLIGHT#{vuelo_numero}` | Schedule + status + asientos |
| **PNR canónico (TP4)** | `PNR#{pnr}` | `#METADATA` | Record locator (6 chars, ej `JS7K2P`) con `user_id`, `status`, `total` |
| **PNR segment (TP4)** | `PNR#{pnr}` | `SEGMENT#{seq}#{vuelo}#{fecha}` | Leg del PNR (incluye `gsi2pk` para "quién está en vuelo X") |
| **PNR passenger (TP4)** | `PNR#{pnr}` | `PAX#{seq}` | Pasajero del PNR (incluye `gsi3pk` para buscar por DNI) |
| **PNR boarding pass (TP4)** | `PNR#{pnr}` | `BP#{seq}` | Referencia al BP en S3 (`s3_key`, `bp_url`, `issued_at`) |
| **User reservation pointer (TP4)** | `USER#{user_id}` | `RESERVATION#{pnr}` | Thin pointer denormalizado para "mis reservas" |
| **Passenger CRM (TP4)** | `PASSENGER#{dni}` | `#PROFILE` | Frequent flyer canónico (full_name, email, phone, total_bookings) |
| **Passenger booking history (TP4)** | `PASSENGER#{dni}` | `PNR#{pnr}` | Back-ref histórico |
| **Claim canónico (TP4)** | `CLAIM#{claim_id}` | `#METADATA` | Reclamo (movido desde USER#) |
| **User claim pointer (TP4)** | `USER#{user_id}` | `CLAIM#{claim_id}` | Thin pointer "mis reclamos" |

### GSIs de la business table (3)

| GSI | HK (`gsi*pk`) | RK (`gsi*sk`) | Projection | Caller |
|---|---|---|---|---|
| **GSI1 `FlightByNumber`** | `vuelo_numero` | `fecha` | INCLUDE (estado_vuelo, puerta, demora, horario_real, origen, destino) | chat_handler para status, `cancel_flight.py` para localizar el FLIGHT a marcar |
| **GSI2 `ReservationsByFlight`** | `FLIGHT#{vuelo}#{fecha}` | `PNR#{pnr}` | INCLUDE (user_id, email, passenger_name, status) | `proactive_notifications` — "qué pasajeros tengo en el vuelo cancelado" |
| **GSI3 `ReservationsByPassenger`** | `DNI#{dni}` o `EMAIL#{email}` | `PNR#{pnr}` | KEYS_ONLY | call center / chatbot — buscar PNR por DNI/email del pasajero |

> **El GSI clave de TP4 es GSI2.** Sin él, encontrar "todos los PNRs en el vuelo JA203 del 2026-06-20" requiere Scan O(n) sobre la tabla entera. Con GSI2 es una sola Query O(log n) — el atributo `gsi2pk` se estampa en cada SEGMENT# al crear el booking.

---

### Por qué PNR-céntrico (no `USER#/RESERVATION#`)

El TP3 modelaba la reserva como sub-item del usuario: `USER#{userId}/RESERVATION#{id}`. Funcionaba para "mis reservas" pero rompía el paradigma de un PSS real:

- Una reserva tiene **múltiples segmentos** (ida + vuelta + escala) y **múltiples pasajeros** (ej. una familia). El modelo TP3 no podía expresarlos sin denormalización.
- El **record locator (PNR)** es la clave canónica en la industria — los agentes lo usan para conversar, las APIs lo aceptan, el papel del boarding pass lo lleva. No es propiedad del usuario, es la entidad central.
- Para responder "quién está en este vuelo" había que escanear todos los `RESERVATION#` items o mantener una proyección manual.

El modelo TP4 invierte la jerarquía: **el PNR es la entidad canónica**, y `USER#{userId}/RESERVATION#{pnr}` queda como **thin pointer denormalizado** sólo para optimizar el query "mis reservas". El PNR contiene todos sus sub-items (segments, pax, BP) bajo el mismo PK, y se accede por PNR o vía GSI.

---

### Access patterns — todas las operaciones de la aplicación

| # | Operación | Lambda | DynamoDB op | PK | SK / condición |
|---|---|---|---|---|---|
| AP1 | Actualizar perfil (email, last_seen) | chat_handler | UpdateItem | `USER#{userId}` | `#METADATA` |
| AP2 | Listar pasajeros guardados | chat_handler | Query | `USER#{userId}` | begins_with `PASSENGER#` |
| AP3 | Auto-guardar pasajero al reservar | payment_processor (ReserveBooking) | UpdateItem | `USER#{userId}` | `PASSENGER#{key}` |
| AP4 | Leer historial de chat (últimos 40 msgs) | chat_handler | Query | `SESSION#{sessionId}` | begins_with `MSG#`, Limit=40, DESC |
| AP5 | Guardar mensaje en sesión | chat_handler | PutItem | `SESSION#{sessionId}` | `MSG#{ts}#{uuid}` |
| AP6 | Obtener una reserva por ID | chat_handler | GetItem | `USER#{userId}` | `RESERVATION#{reservationId}` |
| AP7 | Listar todas las reservas del usuario | chat_handler | Query | `USER#{userId}` | begins_with `RESERVATION#`, Limit=20, DESC |
| AP8 | Hacer check-in (update de status) | chat_handler | UpdateItem | `USER#{userId}` | `RESERVATION#{reservationId}` |
| AP9 | Obtener vuelos de una ruta en todas las fechas | chat_handler | Query | `FLIGHT#{origen}#{destino}` | begins_with `DATE#` |
| AP10 | Obtener vuelo de una ruta en fecha específica | chat_handler / payment_processor | GetItem | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}` |
| AP11 | Guardar reclamo | chat_handler | PutItem | `USER#{userId}` | `CLAIM#{claimId}` |
| AP12 | Crear reserva en estado PENDIENTE | payment_processor (ReserveBooking) | PutItem | `USER#{userId}` | `RESERVATION#{reservationId}` |
| AP13 | Decrementar asientos (atómico) | payment_processor (ReserveFlight) | UpdateItem + ConditionExpression | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}` |
| AP14 | Confirmar reserva → CONFIRMADA | payment_processor (ConfirmBooking) | UpdateItem | `USER#{userId}` | `RESERVATION#{reservationId}` |
| AP15 | Cancelar reserva → CANCELADA | payment_processor (CancelBooking) | UpdateItem | `USER#{userId}` | `RESERVATION#{reservationId}` |
| AP16 | Liberar asientos (rollback) | payment_processor (ReleaseFlight) | UpdateItem | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}` |

---

### Detalle de atributos por entidad

**Perfil de usuario** (`USER#{userId}` / `#METADATA`)
```
email     : email del JWT de Cognito
last_seen : ISO-8601 — actualizado en cada mensaje de chat
```

**Pasajero guardado** (`USER#{userId}` / `PASSENGER#{nombre_normalizado}`)
```
passenger_name    : nombre completo del pasajero
email             : email de contacto
phone             : teléfono de contacto
last_booking      : ISO-8601 — última reserva en que usó este pasajero
reservation_count : entero — cantidad de reservas (ADD atómico, incrementa en cada booking)
```
El `nombre_normalizado` es el nombre en minúsculas con guiones bajos, truncado a 40 chars (ej: `juan_perez`). Si el mismo pasajero viaja de nuevo, el `UpdateItem` hace upsert e incrementa `reservation_count`.

**Mensaje de chat** (`SESSION#{sessionId}` / `MSG#{ts}#{uuid8}`)
```
role          : "user" | "assistant"
content       : string (texto) | JSON string (tool use rounds)
content_type  : "text" | "tool"
user_id       : sub del JWT — valida que la sesión pertenece al usuario
ttl           : epoch seconds — 7 días desde la escritura (limpieza automática)
```

**Token PII** (`SESSION#{sessionId}` / `TOKEN#<token>`)
```
token      : "<EMAIL_a7b3c2f1d4>" | "<DNI_..>" | "<DATE_..>" | "<PHONE_..>" | "<SEXO_..>"
kind       : "EMAIL" | "DNI" | "DATE" | "PHONE" | "SEXO"
value      : valor PII real
created_at : ISO-8601
ttl        : epoch seconds — 24h
```
> Mapping reversible entre placeholders y valores reales para la tokenización de PII antes de mandar a la API de Anthropic. Tokens determinísticos por sesión (HMAC) — mismo dato en la misma sesión siempre da el mismo token. Ver justificación #27.

**Thin pointer Reserva de usuario** (`USER#{userId}` / `RESERVATION#{pnr}`)
```
pnr             : PNR de 6 chars (charset PSS sin 0/1/I/O, ej: "JS7K2P")
status          : "PENDIENTE" → "CONFIRMADA" → "CHECK-IN" | "CANCELADA"
origen          : código IATA (ej: "AEP")
destino         : código IATA (ej: "MDZ")
vuelo_numero    : número de vuelo (ej: "JA123")
fecha           : "YYYY-MM-DD"
pasajeros       : entero
tarifa          : "BASIC" | "LIGHT" | "SMART" | "FULL FLEX"
total           : Decimal — precio total en USD (calculado server-side)
nombre_pasajero : nombre completo del pasajero principal
telefono        : teléfono de contacto
email           : email del JWT
seat            : ID de asiento asignado (ej: "12A")
created_at      : ISO-8601 timestamp
```
> El thin pointer convive con el ítem canónico `PNR#{pnr}/#METADATA` y sus dependientes (`SEGMENT#`, `PAX#`, `BP#`, `EXTRA#`) — ver el modelo PNR-céntrico de la sección anterior. El handler de API mapea `pnr → reservation_id` para exponerlo en el JSON de respuesta. Vocabulario unificado a español (TP4 — ver justificación #24).

**Extras del PNR** (`PNR#{pnr}` / `EXTRA#{nn:02d}`)
```
extra_type : "mascota" | "asiento_estandar" | "asiento_salida_rapida" |
             "asiento_salida_emergencia" | "asiento_primera_fila" |
             "flexismart" | "tarjeta_embarque" | "embarque_prioritario" |
             "equipaje_mano" | "equipaje_bodega"
amount     : Decimal — monto cobrado (0 si está incluido en la tarifa)
created_at : ISO-8601
```
> Los extras se persisten 1 ítem por extra contratado. Habilita auditoría por PNR ("qué llevó cada pasajero") y queries del estilo "cuántas mascotas viajaron este mes". Los nombres están en `lambda/pricing.py:EXTRAS_FIJOS`.

**Pasajero del PNR** (`PNR#{pnr}` / `PAX#{seq}`)
```
seq              : entero — 1 = pasajero principal
full_name        : nombre + apellido
dni              : 7-8 dígitos numéricos sin puntos (validado server-side)
email            : email de contacto (del JWT)
phone            : teléfono de contacto
seat             : ID de asiento asignado (ej "12A")
fecha_nacimiento : "YYYY-MM-DD" — recolectado en PASO 5d para coherencia PSS/TSA
sexo             : "Masculino" | "Femenino" | "Otro" — PASO 5c
gsi3pk           : "DNI#{dni}" — para buscar PNR por DNI
gsi3sk           : "PNR#{pnr}"
```
> Validación server-side en `chat_handler._validate_passenger_input`: rechaza la reserva con error explícito si el formato no cumple. Como tokenizamos PII antes de mandar a Anthropic, Claude solo ve placeholders — la validación de formato es responsabilidad del server (justificación #27).

**Reclamo** (`USER#{userId}` / `CLAIM#{claimId}`)
```
claim_id       : "CLM-XXXXXXXX"
tipo           : "equipaje_perdido" | "equipaje_daniado" | "vuelo_demorado" | "vuelo_cancelado" | "reembolso" | "otro"
descripcion    : texto libre
reservation_id : opcional — reserva relacionada
status         : "RECIBIDO"
created_at     : ISO-8601 timestamp
```

**Vuelo disponible — master row** (`FLIGHT#{origen}#{destino}` / `DATE#{fecha}#FLIGHT#{vuelo}`)
```
vuelo_numero          : "JA123"
precio                : Decimal — precio base por pasajero en USD
hora_salida           : "HH:MM"
hora_llegada          : "HH:MM"
duracion              : "2h 10m"
estado_vuelo          : "EN_HORARIO" | "DEMORADO" | "CANCELADO"
puerta                : "12"
```
> El SK incluye `#FLIGHT#{vuelo}` para soportar múltiples frecuencias (mañana/tarde) en la misma ruta+fecha. Los ítems SEAT# (abajo) son lexicográficamente posteriores al master row con `begins_with(SK, "DATE#{fecha}#FLIGHT#{vuelo}#SEAT#")`.

**Inventario de asientos** (`FLIGHT#{origen}#{destino}` / `DATE#{fecha}#FLIGHT#{vuelo}#SEAT#{row}{letter}`)
```
seat_id         : "12A"
row             : entero 1..20
letter          : "A" | "B" | "C" | "D" | "E" | "F"
seat_type       : "estandar" | "salida_rapida" | "salida_emergencia" | "primera_fila"
vuelo_numero    : "JA123"
fecha           : "YYYY-MM-DD"
reserved_by     : (ausente si libre) | "PNR#{pnr}" (si reservado)
reserved_at     : ISO-8601 (sólo si reservado)
held_by         : (ausente si no holdeado) | "USER#{sub}" (soft-hold)
hold_expires_at : epoch UTC seconds (sólo si holdeado)
```
> 120 ítems SEAT# por vuelo (20 filas × 6 letras A-F). Reserva atómica vía `UpdateItem` con `ConditionExpression: attribute_not_exists(reserved_by)`. Liberación con `ConditionExpression: reserved_by = :owned_pnr` para evitar liberar seats de otros PNRs. Categorías: fila 1 = primera_fila, filas 6-10 = salida_rapida, filas 14-15 = salida_emergencia, resto = estandar.
>
> **Soft-hold (TP4):** atributos `held_by` y `hold_expires_at` opcionales para el patrón de reserva temporal (10 min) mientras el usuario completa el flujo de compra. Permite que dos users no peleen por el mismo asiento entre PASO 3 (elección) y PASO 6 (confirmación). Ver justificación #26.

> En TP3 había dos entidades adicionales (`ANALYTICS#DAILY` y `ANALYTICS#ROUTES`) que el dashboard admin leía. En TP4 fueron eliminadas — el equipo de business analytics consume los mismos datos vía Athena sobre S3, no via DynamoDB.

---

### Reserva atómica de asientos (seat map real, TP4)

`reserve_flight_handler` reserva un asiento ESPECÍFICO con `ConditionExpression`:

```python
table.update_item(
    Key={"PK": f"FLIGHT#{origen}#{destino}", "SK": f"DATE#{fecha}#FLIGHT#{vuelo}#SEAT#{seat_id}"},
    UpdateExpression="SET reserved_by = :pnr, reserved_at = :now",
    ConditionExpression="attribute_exists(PK) AND attribute_not_exists(reserved_by)",
    ExpressionAttributeValues={":pnr": f"PNR#{pnr}", ":now": iso_now},
)
```

Si dos usuarios intentan reservar el mismo asiento, DynamoDB ejecuta sólo uno. El otro recibe `ConditionalCheckFailedException` y Step Functions dispara la compensación. La liberación (compensación) usa `ConditionExpression: reserved_by = :owned_pnr` para evitar liberar asientos de otros PNRs.

El PNR se genera con SHA-256 del `payment_id` en `chat_handler.py` ANTES de iniciar el Saga (ver justificación #22), lo que permite que cada paso del Saga sea idempotente y que la compensación libere exactamente el seat de este PNR.

---

### Ejemplos de ítems reales

**Mensaje de usuario:**
```json
{
  "PK": "SESSION#sess-abc123",
  "SK": "MSG#2026-05-15T14:30:00.000000+00:00#a1b2c3d4",
  "role": "user",
  "content": "quiero volar de Buenos Aires a Mendoza el 20 de junio",
  "content_type": "text",
  "user_id": "us-east-1:a1b2c3d4-e5f6-...",
  "ttl": 1748387400
}
```

**Reserva confirmada** (thin pointer en `USER#`; el ítem canónico vive en `PNR#JS7K2P/#METADATA`):
```json
{
  "PK": "USER#us-east-1:a1b2c3d4-e5f6-...",
  "SK": "RESERVATION#JS7K2P",
  "pnr": "JS7K2P",
  "status": "CONFIRMADA",
  "origin": "AEP",
  "destination": "MDZ",
  "flight_number": "JA101",
  "flight_date": "2026-06-20",
  "passenger_count": 1,
  "tarifa": "SMART",
  "total": "120",
  "passenger_name": "Juan Pérez",
  "created_at": "2026-05-15T14:35:00.123456+00:00"
}
```

**Vuelo disponible:**
```json
{
  "PK": "FLIGHT#AEP#MDZ",
  "SK": "DATE#2026-06-20",
  "vuelo_numero": "JA101",
  "precio": "120",
  "asientos_disponibles": 47,
  "hora_salida": "08:00",
  "hora_llegada": "10:10",
  "duracion": "2h 10m",
  "aerolinea": "JetSmart"
}
```

---

### Configuración de la tabla

| Parámetro | Valor | Razón |
|---|---|---|
| Billing mode | PAY_PER_REQUEST (on-demand) | Tráfico irregular — no pagar por capacidad ociosa |
| TTL attribute | `ttl` | Mensajes de chat se eliminan solos a los 7 días |
| Encriptación | AWS managed key (SSE) | Habilitado por defecto, sin costo adicional |
| GSI | Ninguno | Todos los access patterns resueltos con PK+SK |

---

## Data Lake — S3 + Glue + Athena (analytics histórico)

> **Cambio respecto al TP3:** este capa reemplaza al **RDS PostgreSQL** (`eventos_chat` table) y al **RDS Proxy** del TP3. La razón está en `docs/02-arquitectura-general.md` ("Decisiones de arquitectura"): el patrón correcto para business analytics es un data lake serverless, no un OLTP postgres.

### Rol dentro de la arquitectura

Los eventos del chatbot se acumulan en S3 para consumo offline del equipo de business analytics:

```
chat_handler / confirm_booking_handler / cancel_booking_handler / ...
        ↓ SNS publish (event_type, user_id, timestamp, payload)
    SNS events topic
        ↓ fan-out
    SQS analytics (buffer, batch_size=10, DLQ)
        ↓ trigger
analytics_processor Lambda (regional, sin VPC)
        ↓ put_object (JSON Lines)
    s3://...-analytics/events/dt=YYYY-MM-DD/hh=HH/<uuid>.jsonl
        ↓ cron(0 * * * ? *) (cada hora)
    Glue Crawler
        ↓ descubre schema + particiones
    Glue Data Catalog (database: jetsmart_prod_analytics, table: events)
        ↓
    Athena Workgroup
        ↓ JDBC
    Equipo Business Analytics (DBeaver / DataGrip)
```

---

### Esquema descubierto por el Glue Crawler

El crawler inspecciona los `.jsonl` y crea la tabla `events` con columnas inferidas:

| Columna | Tipo Athena | Origen |
|---|---|---|
| `event_type` | string | Campo `event_type` del mensaje SNS |
| `user_id` | string | Campo `user_id` |
| `timestamp` | string (ISO-8601) | Campo `timestamp` |
| `payload` | struct (anidado) | Campo `payload` (estructura variable según tipo de evento) |
| `ingested_at` | string (ISO-8601) | Agregado por `analytics-processor` en el momento de la escritura |
| `dt` | string (partition) | `YYYY-MM-DD` — particionada del path S3 |
| `hh` | string (partition) | `HH` — particionada del path S3 |

> **Por qué particiones Hive-style:** Athena hace *partition pruning* automático cuando la query filtra por `dt` o `hh`. Una query que escanea solo `dt = '2026-06-13'` lee únicamente los archivos de ese día — no hace full-scan del bucket. Esto reduce el costo de Athena drásticamente.

---

### Tipos de eventos y estructura de `payload`

| event_type | Quién publica | Estructura de `payload` |
|---|---|---|
| `chat_message` | `chat_handler` — cada turno de conversación | `{ "session_id": "...", "message_length": 42 }` |
| `purchase_complete` | `payment_processor` (ConfirmBooking, Saga paso 4) | `{ "amount": 120.0 }` |
| `busqueda_vuelo` | `chat_handler` — al ejecutar tool `search_flights` | `{ "origen": "AEP", "destino": "MDZ", "fecha": "2026-06-20", "pasajeros": 1, "ruta": "AEP-MDZ" }` |
| `checkin_realizado` | `chat_handler` — al ejecutar tool `check_in` | `{ "reservation_id": "RES-XXXX", "flight_number": "JA101", "origin": "AEP", "destination": "MDZ" }` |
| `reclamo_iniciado` | `chat_handler` — al ejecutar tool `create_claim` | `{ "claim_id": "CLM-XXXX", "tipo": "equipaje_perdido", "reservation_id": "RES-XXXX" }` |

---

### Ejemplo de archivo en S3

`s3://jetsmart-prod-123456789012-analytics/events/dt=2026-06-13/hh=14/abc-def-123.jsonl`:

```json
{"event_type":"chat_message","user_id":"us-east-1:a1b2-...","timestamp":"2026-06-13T14:35:00Z","payload":{"session_id":"sess-1","message_length":42},"ingested_at":"2026-06-13T14:35:02Z"}
{"event_type":"busqueda_vuelo","user_id":"us-east-1:a1b2-...","timestamp":"2026-06-13T14:35:10Z","payload":{"origen":"AEP","destino":"MDZ","fecha":"2026-06-20","pasajeros":1,"ruta":"AEP-MDZ"},"ingested_at":"2026-06-13T14:35:11Z"}
{"event_type":"purchase_complete","user_id":"us-east-1:a1b2-...","timestamp":"2026-06-13T14:36:01Z","payload":{"amount":120.0},"ingested_at":"2026-06-13T14:36:02Z"}
```

---

### Queries de ejemplo desde Athena

```sql
-- Eventos por tipo, últimos 7 días
SELECT event_type, COUNT(*) AS cantidad
FROM jetsmart_prod_analytics.events
WHERE dt >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY event_type
ORDER BY cantidad DESC;

-- Compras totales del último mes
SELECT
  SUM(CAST(json_extract_scalar(payload, '$.amount') AS DOUBLE)) AS revenue_usd,
  COUNT(*) AS purchases
FROM jetsmart_prod_analytics.events
WHERE event_type = 'purchase_complete'
  AND dt >= date_format(current_date - interval '30' day, '%Y-%m-%d');

-- Búsquedas más frecuentes por ruta
SELECT
  json_extract_scalar(payload, '$.ruta') AS ruta,
  COUNT(*) AS busquedas
FROM jetsmart_prod_analytics.events
WHERE event_type = 'busqueda_vuelo'
  AND dt >= date_format(current_date - interval '30' day, '%Y-%m-%d')
GROUP BY 1
ORDER BY busquedas DESC
LIMIT 10;
```

---

### Configuración

| Parámetro | Valor | Razón |
|---|---|---|
| Formato | JSON Lines (`.jsonl`) | Simple, sin layers extras; Athena lo lee nativo |
| Particionamiento | `dt=YYYY-MM-DD/hh=HH` | Partition pruning automático |
| Encriptación at rest | AES-256 (SSE-S3) | Sin costo adicional |
| Lifecycle | Glacier después de 90 días | Costo mínimo para retención histórica |
| Crawler schedule | `cron(0 * * * ? *)` | Cada hora — balance entre frescura y costo |
| Athena Workgroup | `jetsmart-prod-analytics` | Aislamiento de results y cost tracking del equipo |
| Result location | `s3://...-analytics/athena-results/` | Resultados expiran a los 14 días |
| Acceso | LabRole (Lambda) + LabRole (Crawler) + cliente SQL externo | IAM least-privilege |
