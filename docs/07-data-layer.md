# 07 — Capa de datos: DynamoDB y RDS

---

## DynamoDB — Single Table Design

### Por qué Single Table Design

En DynamoDB, la práctica recomendada es usar una sola tabla para toda la aplicación. DynamoDB cobra por operación — si una pantalla necesita datos de tres tablas distintas, son tres lecturas. Con Single Table Design, todo lo que necesita una operación está en una sola query.

Esto requiere diseñar el esquema **en función de los access patterns**: primero se listan todas las consultas que va a hacer la aplicación, y de ahí se derivan las claves.

---

### Entidades y claves

**Tabla: `jetsmart`**

| Entidad | PK | SK | Descripción |
|---|---|---|---|
| Perfil de usuario | `USER#{userId}` | `#METADATA` | Email y última actividad, escrito en cada chat |
| Pasajero guardado | `USER#{userId}` | `PASSENGER#{nombre_normalizado}` | Auto-guardado al confirmar una reserva |
| Reserva de usuario | `USER#{userId}` | `RESERVATION#{reservationId}` | Booking completo creado por la Saga |
| Reclamo de usuario | `USER#{userId}` | `CLAIM#{claimId}` | Reclamo registrado por el chatbot |
| Mensaje de chat | `SESSION#{sessionId}` | `MSG#{ISO-timestamp}#{uuid8}` | Historial de conversación con TTL de 7 días |
| Vuelo disponible (mock) | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}` | Datos de vuelo con contador de asientos |
| Agregado diario (analytics) | `ANALYTICS#DAILY` | `DATE#{yyyy-mm-dd}` | Contadores de mensajes, búsquedas, compras, check-ins, reclamos e ingresos |
| Ruta top (analytics) | `ANALYTICS#ROUTES` | `ROUTE#{ruta}` | Conteo de búsquedas y compras por ruta (ej: `AEP-MDZ`) |

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
| AP17 | Actualizar agregados diarios (ADD atómico) | analytics_processor | UpdateItem | `ANALYTICS#DAILY` | `DATE#{hoy}` |
| AP18 | Actualizar top rutas (ADD atómico) | analytics_processor | UpdateItem | `ANALYTICS#ROUTES` | `ROUTE#{ruta}` |
| AP19 | Leer métricas del dashboard (últimos 30 días) | chat_handler (admin) | Query | `ANALYTICS#DAILY` | begins_with `DATE#`, Limit=30, DESC |
| AP20 | Leer top 5 rutas (dashboard) | chat_handler (admin) | Query | `ANALYTICS#ROUTES` | Limit=5, DESC |

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

**Reserva de usuario** (`USER#{userId}` / `RESERVATION#{reservationId}`)
```
reservation_id : "RES-XXXXXXXX"
status         : "PENDIENTE" → "CONFIRMADA" → "CHECK-IN" | "CANCELADA"
origin         : código IATA (ej: "AEP")
destination    : código IATA (ej: "MDZ")
flight_number  : número de vuelo (ej: "JA123")
flight_date    : "YYYY-MM-DD"
passenger_count: entero
tarifa         : "BASIC" | "LIGHT" | "SMART" | "FULL FLEX"
total          : Decimal — precio total en USD
transaction_id : "TX-XXXXXXXXXXXX" — agregado al confirmar
email          : email de contacto
phone          : teléfono de contacto
passenger_name : nombre completo del pasajero principal
created_at     : ISO-8601 timestamp
```

**Reclamo** (`USER#{userId}` / `CLAIM#{claimId}`)
```
claim_id       : "CLM-XXXXXXXX"
tipo           : "equipaje_perdido" | "equipaje_daniado" | "vuelo_demorado" | "vuelo_cancelado" | "reembolso" | "otro"
descripcion    : texto libre
reservation_id : opcional — reserva relacionada
status         : "RECIBIDO"
created_at     : ISO-8601 timestamp
```

**Vuelo disponible** (`FLIGHT#{origen}#{destino}` / `DATE#{fecha}`)
```
vuelo_numero          : "JA123"
precio                : Decimal — precio base por pasajero en USD
asientos_disponibles  : entero — decrementado atómicamente en cada reserva
hora_salida           : "HH:MM"
hora_llegada          : "HH:MM"
duracion              : "2h 10m"
aerolinea             : "JetSmart"
```

**Agregado diario** (`ANALYTICS#DAILY` / `DATE#{yyyy-mm-dd}`)
```
searches        : entero — mensajes de chat del día (ADD atómico)
flight_searches : entero — búsquedas de vuelo (tool search_flights) del día (ADD atómico)
purchases       : entero — compras confirmadas del día (ADD atómico)
checkins        : entero — check-ins realizados del día (ADD atómico)
claims          : entero — reclamos iniciados del día (ADD atómico)
revenue         : Decimal — ingresos del día (ADD atómico)
```

**Ruta top** (`ANALYTICS#ROUTES` / `ROUTE#{ruta}`)
```
route    : "AEP-MDZ"
count    : entero — total de interacciones (búsquedas + compras) (ADD atómico)
searches : entero — búsquedas de vuelo en esa ruta (ADD atómico)
purchases: entero — compras confirmadas en esa ruta (ADD atómico)
```

---

### Decremento atómico de asientos

`reserve_flight_handler` usa `ConditionExpression` para evitar sobreventa:

```python
table.update_item(
    Key={"PK": f"FLIGHT#{origen}#{destino}", "SK": f"DATE#{fecha}"},
    UpdateExpression="ADD asientos_disponibles :dec",
    ConditionExpression="asientos_disponibles >= :min",
    ExpressionAttributeValues={":dec": -pasajeros, ":min": pasajeros},
)
```

Si dos usuarios intentan reservar el último asiento simultáneamente, DynamoDB ejecuta solo uno. El otro recibe `ConditionalCheckFailedException`, que Step Functions propaga como error y dispara las compensaciones (rollback automático vía Saga).

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

**Reserva confirmada:**
```json
{
  "PK": "USER#us-east-1:a1b2c3d4-e5f6-...",
  "SK": "RESERVATION#RES-D6F11672",
  "reservation_id": "RES-D6F11672",
  "status": "CONFIRMADA",
  "origin": "AEP",
  "destination": "MDZ",
  "flight_number": "JA101",
  "flight_date": "2026-06-20",
  "passenger_count": 1,
  "tarifa": "SMART",
  "total": "120",
  "transaction_id": "TX-A1B2C3D4E5F6",
  "email": "usuario@email.com",
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

**Agregado diario:**
```json
{
  "PK": "ANALYTICS#DAILY",
  "SK": "DATE#2026-05-15",
  "searches": 142,
  "purchases": 8,
  "revenue": "960.00"
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

## RDS PostgreSQL — Log de eventos para analytics

### Rol dentro de la arquitectura

RDS almacena el **log detallado** de cada evento para el dashboard del administrador. El flujo es:

```
chat_handler / confirm_booking_handler
        ↓ SNS publish (event_type, user_id, payload)
    SNS events topic
        ↓ fan-out
    SQS analytics (buffer, batch_size=10)
        ↓ trigger
analytics_processor Lambda (dentro de VPC)
        ↓
    eventos_chat (RDS)    +    ANALYTICS#DAILY / ANALYTICS#ROUTES (DynamoDB)
```

El dashboard del admin **lee desde DynamoDB** (agregados pre-computados), no desde RDS. RDS sirve para consultas ad-hoc futuras sobre el log completo.

---

### Schema real (de `analytics_processor.py`)

```sql
CREATE TABLE IF NOT EXISTS eventos_chat (
    id          BIGSERIAL    PRIMARY KEY,
    tipo_evento VARCHAR(50)  NOT NULL,
    usuario_id  VARCHAR(100) NOT NULL,
    timestamp   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    datos       JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eventos_tipo      ON eventos_chat (tipo_evento);
CREATE INDEX IF NOT EXISTS idx_eventos_usuario   ON eventos_chat (usuario_id);
CREATE INDEX IF NOT EXISTS idx_eventos_timestamp ON eventos_chat (timestamp);
```

El schema se aplica automáticamente en cada `terraform apply` mediante `aws_lambda_invocation` que invoca `analytics_processor` con `{"migrate": true}`.

---

### Tipos de eventos y estructura de `datos`

| tipo_evento | Quién publica | Estructura de `datos` |
|---|---|---|
| `chat_message` | `chat_handler` — cada turno de conversación | `{ "session_id": "...", "message_length": 42 }` |
| `purchase_complete` | `payment_processor` (ConfirmBooking, Saga paso 4) | `{ "amount": 120.0 }` |
| `busqueda_vuelo` | `chat_handler` — al ejecutar tool `search_flights` | `{ "origen": "AEP", "destino": "MDZ", "fecha": "2026-06-20", "pasajeros": 1, "ruta": "AEP-MDZ" }` |
| `checkin_realizado` | `chat_handler` — al ejecutar tool `check_in` | `{ "reservation_id": "RES-XXXX", "flight_number": "JA101", "origin": "AEP", "destination": "MDZ" }` |
| `reclamo_iniciado` | `chat_handler` — al ejecutar tool `create_claim` | `{ "claim_id": "CLM-XXXX", "tipo": "equipaje_perdido", "reservation_id": "RES-XXXX" }` |

El campo `datos` viene del campo `payload` del evento SNS. Los campos de alto nivel (`user_id`, `timestamp`) se mapean a columnas directas; el resto va a `datos` como JSONB.

---

### Ejemplo de registro en RDS

```sql
SELECT * FROM eventos_chat WHERE tipo_evento = 'purchase_complete' LIMIT 1;

 id | tipo_evento       | usuario_id                          | timestamp            | datos           | created_at
----+-------------------+-------------------------------------+----------------------+-----------------+------------------
  1 | purchase_complete | us-east-1:a1b2c3d4-e5f6-...        | 2026-05-15 14:35:00Z | {"amount": 120} | 2026-05-15T14:35Z
```

---

### Configuración de RDS

| Parámetro | Valor | Razón |
|---|---|---|
| Engine | PostgreSQL 15 | Soporte nativo JSONB, funciones analíticas |
| Instance class | `db.t3.micro` | Suficiente para el TP, costo mínimo en Academy |
| Storage | 20 GB gp2 | Mínimo suficiente |
| Multi-AZ | Deshabilitado | Reduce costo en Academy (no hay SLA real) |
| Subnet | Privada (datos) | Sin acceso directo desde internet |
| Acceso | Solo desde analytics_processor (VPC) + Bastion (SSM) | Security group restrictivo |
| Encriptación at rest | Habilitado (AWS managed key) | Sin costo adicional |
| Credenciales | Secrets Manager (`jetsmart/rds/credentials`) | Nunca hardcodeadas en código |
