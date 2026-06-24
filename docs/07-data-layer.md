# 07 — Capa de datos: DynamoDB (dos tablas, bounded contexts) + Data Lake (S3 + Athena)

> **Cambio TP4:** la única tabla DynamoDB del TP3 se separó en **dos tablas single-design**, una por bounded context. El esquema de reservas migró a un patrón **PNR-céntrico** (record locator de 6 chars, à la Navitaire/Amadeus), con 2 GSIs en la tabla de negocio.

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
- **Backup window independiente**: ambas tienen PITR (35 días), pero podrían divergir.
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
| **Token PII (TP4 final)** | `SESSION#{session_id}` | `TOKEN#<placeholder>` | 24h | Mapping reversible token→valor real para tokenización de PII hacia Anthropic. Ver justificación #27. |

---

### Tabla 2 — `jetsmart-prod-business` (PSS-like)

Estado persistente del dominio. PK/SK, **2 GSIs**, **DynamoDB Stream habilitado** (`NEW_AND_OLD_IMAGES`), sin TTL.

| Entidad | PK | SK | Descripción |
|---|---|---|---|
| **Vuelo — master row** | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}#FLIGHT#{vuelo_numero}` | Schedule + status + precio. Disparador del Stream cuando `estado_vuelo` pasa a CANCELADO. |
| **Vuelo — inventario de asientos (TP4)** | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}#FLIGHT#{vuelo_numero}#SEAT#{row}{letter}` | Ítem individual por asiento (120 por vuelo). Reserva atómica via ConditionExpression. Soporta soft-hold via `held_by` + `hold_expires_at`. |
| **PNR canónico (TP4)** | `PNR#{pnr}` | `#METADATA` | Record locator (6 chars, ej `JS7K2P`) con `user_id`, `status`, `total`, `tarifa`, `pasajeros` |
| **PNR segment (TP4)** | `PNR#{pnr}` | `SEGMENT#{seq}#{vuelo}#{fecha}` | Leg del PNR (incluye `gsi2pk` para "quién está en vuelo X") |
| **PNR passenger (TP4)** | `PNR#{pnr}` | `PAX#{seq}` | Pasajero del PNR. Persiste `fecha_nacimiento` + `sexo`. |
| **PNR boarding pass (TP4)** | `PNR#{pnr}` | `BP#{seq}` | Referencia al BP en S3 (`s3_key`, `bp_url`, `issued_at`) |
| **PNR extras (TP4 final)** | `PNR#{pnr}` | `EXTRA#{nn:02d}` | Cada extra contratado como ítem individual (`extra_type`, `amount`). Habilita auditoría por PNR. |
| **User reservation pointer (TP4)** | `USER#{user_id}` | `RESERVATION#{pnr}` | Thin pointer denormalizado para "mis reservas" (vocabulario en español) |
| **Passenger CRM (TP4)** | `PASSENGER#{dni}` | `#PROFILE` | Frequent flyer canónico (full_name, email, phone, total_bookings) |
| **Passenger booking history (TP4)** | `PASSENGER#{dni}` | `PNR#{pnr}` | Back-ref histórico |
| **Claim canónico (TP4)** | `CLAIM#{claim_id}` | `#METADATA` | Reclamo (movido desde USER#) |
| **User claim pointer (TP4)** | `USER#{user_id}` | `CLAIM#{claim_id}` | Thin pointer "mis reclamos" |

> **DynamoDB Stream:** habilitado con `stream_view_type = NEW_AND_OLD_IMAGES`. Consumido por la Lambda `stream-emitter` con `filter_criteria` (solo master rows `FLIGHT#` con `estado_vuelo`). Detecta la transición `estado_vuelo → CANCELADO` (OldImage ≠ CANCELADO, NewImage = CANCELADO) y publica `flight_cancelled` al topic SNS central `events` con `MessageAttribute event_type`. Patrón CDC. Ver justificación #28.

### GSIs de la business table (2)

| GSI | HK (`gsi*pk`) | RK (`gsi*sk`) | Projection | Caller |
|---|---|---|---|---|
| **`ReservationsByFlight`** | `FLIGHT#{vuelo}#{fecha}` | `PNR#{pnr}` | INCLUDE (user_id, email, passenger_name, status) | `proactive_notifications` / `refund` — "qué pasajeros tengo en el vuelo cancelado" |
| **`FlightsByDate`** | `FLIGHTDATE#{fecha}` | `vuelo_numero` | INCLUDE (estado_vuelo, vuelo_numero, fecha, hora_salida) | `weather-poller` — "qué vuelos activos hay en esta fecha" (hora_salida → forecast por hora de salida) |
> **`ReservationsByFlight`** habilita el fan-out de cancelaciones: sin él, encontrar "todos los PNRs en el vuelo JA203 del 2026-06-20" requiere Scan O(n) sobre la tabla entera. Con el GSI es una sola Query O(log n) — el atributo `gsi2pk` se estampa en cada SEGMENT# al crear el booking.
>
> **`FlightsByDate`** reemplaza el Scan que hacía el `weather-poller` para listar vuelos activos. Es **sparse** (solo el master row `FLIGHT#` estampa `gsi_flights_pk`; los 120 `SEAT#` por vuelo no), así que el índice contiene únicamente vuelos. **Particionado por fecha** (`FLIGHTDATE#{fecha}`): cada día es su propia partición → sin hot partition. El poller hace una Query por fecha de la ventana [hoy, hoy+48h] (2-3 queries) con `FilterExpression` por `estado_vuelo IN (EN_HORARIO, DEMORADO)`, en vez de escanear la tabla entera.

> **Histórico:** TP4 inicial tenía dos GSIs adicionales que se eliminaron en TP4 final:
> - `FlightByNumber`: lo usaba `scripts/cancel_flight.py`. Al volcar el trigger a DynamoDB Streams quedó sin consumidor en runtime.
> - `ReservationsByPassenger`: pensado para un canal de call center que buscara PNRs por DNI/email. El canal nunca se implementó. El ítem `PAX#01#EMAILALIAS` que solo existía para alimentar este GSI también se eliminó.
>
> Ambos se sacaron para no replicar WCU a índices ociosos. El nombre lógico `gsi2pk` se mantiene tal cual para no requerir reescritura de ítems.

---

### Por qué PNR-céntrico (no `USER#/RESERVATION#`)

El TP3 modelaba la reserva como sub-item del usuario: `USER#{userId}/RESERVATION#{id}`. Funcionaba para "mis reservas" pero rompía el paradigma de un PSS real:

- Una reserva tiene **múltiples segmentos** (ida + vuelta + escala) y **múltiples pasajeros** (ej. una familia). El modelo TP3 no podía expresarlos sin denormalización.
- El **record locator (PNR)** es la clave canónica en la industria — los agentes lo usan para conversar, las APIs lo aceptan, el papel del boarding pass lo lleva. No es propiedad del usuario, es la entidad central.
- Para responder "quién está en este vuelo" había que escanear todos los `RESERVATION#` items o mantener una proyección manual.

El modelo TP4 invierte la jerarquía: **el PNR es la entidad canónica**, y `USER#{userId}/RESERVATION#{pnr}` queda como **thin pointer denormalizado** sólo para optimizar el query "mis reservas". El PNR contiene todos sus sub-items (segments, pax, BP) bajo el mismo PK, y se accede por PNR o vía GSI.

---

### Access patterns — todas las operaciones de la aplicación

**Tabla conversations:**

| # | Operación | Lambda | DynamoDB op | PK | SK / condición |
|---|---|---|---|---|---|
| AP1 | Actualizar perfil (email, last_seen) | chat_handler | UpdateItem | `USER#{userId}` | `#METADATA` |
| AP2 | Leer historial de chat (últimos 40 msgs) | chat_handler | Query | `SESSION#{sessionId}` | begins_with `MSG#`, Limit=40, DESC |
| AP3 | Guardar mensaje en sesión | chat_handler | PutItem | `SESSION#{sessionId}` | `MSG#{ts}#{uuid}` |
| AP4 | Persistir mapping token PII → valor real | chat_handler (tokenize) | PutItem + TTL 24h | `SESSION#{sessionId}` | `TOKEN#<token>` |
| AP5 | Resolver token PII a valor real | chat_handler (detokenize) | GetItem | `SESSION#{sessionId}` | `TOKEN#<token>` |
| AP6 | Guardar handoff ticket + thin pointer | chat_handler (escalate_to_human) | 2× PutItem | `SESSION#{sid}` y `USER#{userId}` | `HANDOFF#...` |

**Tabla business — operaciones de vuelo y asientos:**

| # | Operación | Lambda | DynamoDB op | PK | SK / condición |
|---|---|---|---|---|---|
| AP7 | Listar fechas con vuelos en una ruta | chat_handler (`list_flight_dates`) | Query + filtro Python para excluir SEAT# | `FLIGHT#{origen}#{destino}` | begins_with `DATE#` |
| AP8 | Buscar vuelos de una ruta en fecha + COUNT real de asientos libres | chat_handler (`search_flights`) | Query (master rows) + Query `Select=COUNT` por SEAT# libre | `FLIGHT#{origen}#{destino}` | begins_with `DATE#{fecha}#`, filter `attribute_not_exists(reserved_by)` |
| AP9 | Listar categorías de asientos libres | chat_handler (`list_available_seats`) | Query + `FilterExpression: attribute_not_exists(reserved_by)` | `FLIGHT#{origen}#{destino}` | begins_with `DATE#{fecha}#FLIGHT#{vuelo}#SEAT#` |
| AP10 | Get master row del vuelo | payment_processor (`reserve_flight`) | GetItem | `FLIGHT#{origen}#{destino}` | `DATE#{fecha}#FLIGHT#{vuelo}` |
| AP11 | Reservar seat específico (atómico) | payment_processor (`reserve_flight`) | UpdateItem + `ConditionExpression: attribute_not_exists(reserved_by) AND (attribute_not_exists(held_by) OR held_by = :user OR hold_expires_at <= :now)` | `FLIGHT#{o}#{d}` | `DATE#{f}#FLIGHT#{v}#SEAT#{seat_id}` |
| AP12 | Hold temporal de seat (10 min TTL) | chat_handler (`hold_seat`) | UpdateItem + ConditionExpression | `FLIGHT#{o}#{d}` | `...#SEAT#{seat_id}` |
| AP13 | Verificar estado del hold propio | chat_handler (`check_hold_status`) | GetItem | `FLIGHT#{o}#{d}` | `...#SEAT#{seat_id}` |
| AP14 | Liberar hold (cambio de seat) | chat_handler (`release_hold`) | UpdateItem `REMOVE held_by, hold_expires_at` + ConditionExpression `held_by = :user` | `FLIGHT#{o}#{d}` | `...#SEAT#{seat_id}` |
| AP15 | Liberar seat reservado (compensación Saga) | payment_processor (`release_flight`) | UpdateItem `REMOVE reserved_by, reserved_at` + ConditionExpression `reserved_by = :owned_pnr` | `FLIGHT#{o}#{d}` | `...#SEAT#{seat_id}` |
| AP16 | Detectar cancelación de vuelo (Stream) → publicar `flight_cancelled` al SNS `events` | stream-emitter | DynamoDB Stream event | — | filter `eventName=MODIFY AND NewImage.estado_vuelo=CANCELADO` |

**Tabla business — operaciones de reserva (PNR-céntrico):**

| # | Operación | Lambda | DynamoDB op | PK | SK / condición |
|---|---|---|---|---|---|
| AP17 | Crear PNR canónico (atomic, idempotente) | payment_processor (`reserve_booking`) | PutItem + `ConditionExpression: attribute_not_exists(PK)` | `PNR#{pnr}` | `#METADATA` |
| AP18 | Crear SEGMENT del PNR (con gsi2pk para "quién está en vuelo X") | payment_processor (`reserve_booking`) | PutItem | `PNR#{pnr}` | `SEGMENT#{seq}#{vuelo}#{fecha}` |
| AP19 | Crear PAX del PNR (`fecha_nacimiento`, `sexo`, `seat`, etc.) | payment_processor (`reserve_booking`) | PutItem | `PNR#{pnr}` | `PAX#{seq}` |
| AP20 | Persistir extras del PNR (1 ítem por extra) | payment_processor (`reserve_booking`) | PutItem por extra | `PNR#{pnr}` | `EXTRA#{nn:02d}` |
| AP21 | Crear thin pointer "mis reservas" | payment_processor (`reserve_booking`) | PutItem | `USER#{userId}` | `RESERVATION#{pnr}` |
| AP22 | Upsert pasajero CRM + back-ref | payment_processor (`reserve_booking`) | UpdateItem + PutItem | `PASSENGER#{key}` | `#PROFILE` y `PNR#{pnr}` |
| AP23 | Confirmar reserva (canónico + thin pointer) | payment_processor (`confirm_booking`) | 2× UpdateItem + `ConditionExpression: status = PENDIENTE AND user_id = :uid` | `PNR#{pnr}` y `USER#{userId}` | `#METADATA` y `RESERVATION#{pnr}` |
| AP24 | Cancelar reserva (compensación) | payment_processor (`cancel_booking`) | 2× UpdateItem | `PNR#{pnr}` y `USER#{userId}` | `#METADATA` y `RESERVATION#{pnr}` |
| AP25 | Crear BP del PNR | boarding_pass_async | PutItem | `PNR#{pnr}` | `BP#{seq}` |

**Tabla business — operaciones de consulta del chat:**

| # | Operación | Lambda | DynamoDB op | PK | SK / condición |
|---|---|---|---|---|---|
| AP26 | Listar reservas del usuario | chat_handler (`list_user_reservations`) | Query | `USER#{userId}` | begins_with `RESERVATION#`, Limit=20, DESC |
| AP27 | Obtener una reserva (thin pointer) | chat_handler (`get_reservation`) | GetItem | `USER#{userId}` | `RESERVATION#{pnr}` |
| AP28 | Hacer check-in (status) | chat_handler (`check_in`) | 2× UpdateItem | `USER#{userId}` y `PNR#{pnr}` | `RESERVATION#{pnr}` y `#METADATA` |
| AP29 | Obtener boarding pass | chat_handler (`get_boarding_pass`) | GetItem | `PNR#{pnr}` | `BP#{seq}` |
| AP30 | Listar pasajeros guardados del user | chat_handler (`list_saved_passengers`) | Query del thin pointer + group by passenger_name | `USER#{userId}` | begins_with `RESERVATION#` |
| AP31 | Crear reclamo | chat_handler (`create_claim`) | 2× PutItem | `CLAIM#{claim_id}` y `USER#{userId}` | `#METADATA` y `CLAIM#{claim_id}` |

**Tabla business — operaciones de notificación proactiva:**

| # | Operación | Lambda | DynamoDB op | PK / Index | SK / condición |
|---|---|---|---|---|---|
| AP32 | Encontrar PNRs afectados por vuelo cancelado | proactive_notifications | **GSI Query** sobre `ReservationsByFlight` | `gsi2pk = FLIGHT#{vuelo}#{fecha}` | begins_with `PNR#` |
| AP33 | Marcar PNR como AFFECTED_BY_CANCELLATION | proactive_notifications | UpdateItem | `PNR#{pnr}` | `#METADATA` |
| AP36 | Listar vuelos activos de una fecha | weather-poller | **GSI Query** sobre `FlightsByDate` (1 por fecha de la ventana 48h) + filter `estado_vuelo IN (EN_HORARIO, DEMORADO)` | `gsi_flights_pk = FLIGHTDATE#{fecha}` | — |
| AP37 | Cancelar vuelo por clima (idempotente) | weather-poller | UpdateItem condicional | `FLIGHT#{org}#{dst}` | `estado_vuelo <> CANCELADO` |

> **Nota:** los access patterns de call center "buscar PNR por DNI/email" (AP34/AP35 en versiones previas del doc) requerían el GSI `ReservationsByPassenger` que se eliminó en TP4 final (sin consumidor en runtime). Si en producción se agregara un canal de call center, se recreará el GSI o se implementará vía Scan + filter para volúmenes bajos.

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

**PNR canónico** (`PNR#{pnr}` / `#METADATA`)
```
pnr            : "JS7K2P"
user_id        : sub del JWT
status         : "PENDIENTE" → "CONFIRMADA" → "CHECK-IN" | "CANCELADA" | "AFFECTED_BY_CANCELLATION"
total          : Decimal — calculado server-side via pricing.compute_total
pasajeros      : entero
tarifa         : "BASIC" | "LIGHT" | "SMART" | "FULL FLEX"
email_contacto : email del JWT
telefono       : teléfono de contacto
payment_id     : UUID del Saga (para idempotencia y trazabilidad)
transaction_id : "TX-..." — se setea al confirmar
created_at     : ISO-8601
```
> Atomicidad: `PutItem` con `ConditionExpression: attribute_not_exists(PK)` previene colisión de PNR. Verificación de ownership (`user_id`, `payment_id`) antes de tratar como idempotente.

**PNR segment** (`PNR#{pnr}` / `SEGMENT#{seq}#{vuelo}#{fecha}`)
```
seq            : 1 (1 segmento por reserva en TP4; multi-segmento queda como roadmap)
origen         : código IATA
destino        : código IATA
fecha          : "YYYY-MM-DD"
vuelo_numero   : "JA123"
cabin          : "ECONOMY"
fare_class     : "BASIC" | "LIGHT" | "SMART" | "FULL FLEX"
status         : "PENDIENTE" → "CONFIRMADA"
gsi2pk         : "FLIGHT#{vuelo}#{fecha}"  ← clave del GSI ReservationsByFlight
gsi2sk         : "PNR#{pnr}"
user_id        : sub del JWT (proyectado en el GSI)
email          : email de contacto (proyectado)
passenger_name : nombre del pax (proyectado)
```
> El GSI `ReservationsByFlight` indexa estos items para responder "quién está en este vuelo" en O(log n). Es el habilitador de las notificaciones proactivas (AP32).

**PNR boarding pass** (`PNR#{pnr}` / `BP#{seq}`)
```
seq        : 1
s3_key     : "PNR#JS7K2P/SEG#01/BP_001.txt"
bp_url     : presigned S3 URL (no se expone al chat, solo a SES en futuro)
issued_at  : ISO-8601
```
> Generado asincrónicamente por `boarding_pass_async` (Lambda triggered por SQS). Fire-and-forget — si falla, el PNR confirmado sigue siendo válido.

**CRM Passenger** (`PASSENGER#{key}` / `#PROFILE`)
```
passenger_name    : nombre completo
email             : email de contacto (último usado)
phone             : teléfono (último usado)
last_booking      : ISO-8601 — última reserva donde apareció este pasajero
reservation_count : entero (ADD atómico, incrementa por reserva)
```
> `key` = `dni` si está disponible, sino `_passenger_key(passenger_name)` (slug). Upsert idempotente.

**CRM Passenger booking history** (`PASSENGER#{key}` / `PNR#{pnr}`)
```
pnr : "JS7K2P"
```
> Back-ref minimalista: permite query `PASSENGER#{key} begins_with PNR#` para "todas las reservas de este pasajero".

**Handoff ticket** (`SESSION#{sid}` / `HANDOFF#{ts}#{handoff_id}`)
```
handoff_id : "HO-XXXXXXXX"
session_id : referencia a la sesión
user_id    : sub del JWT
reason     : motivo libre
urgency    : "low" | "medium" | "high"
status     : "QUEUED" → "ACK"
created_at : ISO-8601
ttl        : epoch — 30 días
```

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

El PNR se genera con SHA-256 del `payment_id` en el chat-handler (`app/chat-handler/chat_core.py`) ANTES de iniciar el Saga (ver justificación #22), lo que permite que cada paso del Saga sea idempotente y que la compensación libere exactamente el seat de este PNR.

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

**Reserva confirmada — thin pointer en `USER#`** (el ítem canónico vive en `PNR#JS7K2P/#METADATA`):
```json
{
  "PK": "USER#us-east-1:a1b2c3d4-e5f6-...",
  "SK": "RESERVATION#JS7K2P",
  "pnr": "JS7K2P",
  "status": "CONFIRMADA",
  "origen": "AEP",
  "destino": "MDZ",
  "vuelo_numero": "JA203",
  "fecha": "2026-06-22",
  "pasajeros": 1,
  "tarifa": "SMART",
  "total": "120.00",
  "nombre_pasajero": "Juan Pérez",
  "telefono": "+5491112345678",
  "email": "juan@example.com",
  "seat": "12C",
  "created_at": "2026-06-17T14:35:00.123456+00:00"
}
```

**Vuelo disponible — master row:**
```json
{
  "PK": "FLIGHT#AEP#MDZ",
  "SK": "DATE#2026-06-22#FLIGHT#JA203",
  "vuelo_numero": "JA203",
  "fecha": "2026-06-22",
  "origen": "AEP",
  "destino": "MDZ",
  "precio": "59.00",
  "hora_salida": "17:00",
  "hora_llegada": "18:15",
  "duracion": "1h 15m",
  "estado_vuelo": "EN_HORARIO",
  "puerta": "12"
}
```

**Asiento libre:**
```json
{
  "PK": "FLIGHT#AEP#MDZ",
  "SK": "DATE#2026-06-22#FLIGHT#JA203#SEAT#12C",
  "seat_id": "12C",
  "row": 12,
  "letter": "C",
  "seat_type": "estandar",
  "vuelo_numero": "JA203",
  "fecha": "2026-06-22"
}
```

**Asiento con hold temporal:**
```json
{
  "PK": "FLIGHT#AEP#MDZ",
  "SK": "DATE#2026-06-22#FLIGHT#JA203#SEAT#1A",
  "seat_id": "1A",
  "seat_type": "primera_fila",
  "held_by": "USER#us-east-1:a1b2c3d4-...",
  "hold_expires_at": 1750183200
}
```

**Asiento reservado (con PNR confirmado):**
```json
{
  "PK": "FLIGHT#AEP#MDZ",
  "SK": "DATE#2026-06-22#FLIGHT#JA203#SEAT#12C",
  "seat_id": "12C",
  "seat_type": "estandar",
  "reserved_by": "PNR#JS7K2P",
  "reserved_at": "2026-06-17T14:35:00.123456+00:00"
}
```

**Token PII (conversations):**
```json
{
  "PK": "SESSION#sess-abc123",
  "SK": "TOKEN#<EMAIL_a7b3c2f1d4>",
  "token": "<EMAIL_a7b3c2f1d4>",
  "kind": "EMAIL",
  "value": "juan@example.com",
  "created_at": "2026-06-17T14:30:00+00:00",
  "ttl": 1750249800
}
```

**Extra del PNR:**
```json
{
  "PK": "PNR#JS7K2P",
  "SK": "EXTRA#01",
  "pnr": "JS7K2P",
  "extra_type": "mascota",
  "amount": "35.00",
  "created_at": "2026-06-17T14:35:00+00:00"
}
```

---

### Configuración de las tablas

**Tabla `conversations`:**

| Parámetro | Valor | Razón |
|---|---|---|
| Billing mode | PAY_PER_REQUEST | Tráfico irregular — no pagar por capacidad ociosa |
| TTL attribute | `ttl` | Mensajes 7d, perfiles 30d, handoff 30d, tokens PII 24h — limpieza automática |
| Encriptación at-rest | AWS managed (`aws/dynamodb`) | KMS implícito, sin costo |
| GSIs | Ninguno | Todos los access patterns resueltos con PK+SK |
| Stream | Deshabilitado | Nadie escucha cambios en conversations |

**Tabla `business`:**

| Parámetro | Valor | Razón |
|---|---|---|
| Billing mode | PAY_PER_REQUEST | Tráfico irregular — no pagar por capacidad ociosa |
| TTL attribute | No tiene | Datos del PSS son persistentes (sin TTL); las reservas no expiran |
| Encriptación at-rest | AWS managed (`aws/dynamodb`) | KMS implícito, sin costo |
| GSIs | 2 (ReservationsByFlight, FlightsByDate) | Resuelven AP32 (fan-out de cancelaciones) y AP36 (vuelos activos del weather-poller) sin Scan |
| **Stream** | **Habilitado, `NEW_AND_OLD_IMAGES`** | Consumido por la Lambda `stream-emitter` con filter_criteria. Permite comparar transiciones (no re-cancelaciones) y publicar `flight_cancelled` al SNS `events`. |
| Point-in-time recovery | Habilitado | Recuperación granular hasta 35 días atrás |

---

## Data Lake — S3 + Glue + Athena (analytics histórico)

> **Cambio respecto al TP3:** este capa reemplaza al **RDS PostgreSQL** (`eventos_chat` table) y al **RDS Proxy** del TP3. La razón está en `docs/02-arquitectura-general.md` ("Decisiones de arquitectura"): el patrón correcto para business analytics es un data lake serverless, no un OLTP postgres.

### Rol dentro de la arquitectura

Los eventos del chatbot se acumulan en S3 para consumo offline del equipo de business analytics:

```
chat-handler (Fargate) / confirm_booking_handler / cancel_booking_handler / ...
        ↓ SNS publish (event_type, user_id, event_ts)  — eventos semánticos del chat → interaction_events
    SNS events topic                                business_analytics_emitter (CDC del Stream)
        ↓ suscripción                                       ↓ PutRecord
    Kinesis Data Firehose (delivery streams: interaction / reservation / flight / claim)
        ↓ batch NATIVO por tamaño/tiempo (5 MB / 60 s), sin Lambda de transformación
        ↓ JSON Lines gzip
    s3://...-analytics/lake/<entidad>/dt=YYYY-MM-DD/hh=HH/<uuid>.gz
        ↓ partition projection (sin crawler)
    Glue Data Catalog (database: jetsmart_prod_analytics, tablas: reservation/flight/claim/interaction_events)
        ↓
    Athena Workgroup
        ↓ JDBC
    Equipo Business Analytics (DBeaver / DataGrip)
```

---

### Esquema de las tablas del Glue Data Catalog (partition projection, sin crawler)

El data lake **no** usa una tabla `events` genérica con `payload` struct. Son **4 tablas tipadas**, una por entidad, definidas estáticamente en el Glue Data Catalog (`reservation_events` / `flight_events` / `claim_events` / `interaction_events`) con **partition projection** sobre `dt`/`hh` — **no hay Glue Crawler**. Cada columna es un campo de primer nivel del JSON Lines (no un struct anidado), lo que evita `json_extract_scalar` en las queries. Las particiones `dt` y `hh` aplican a las cuatro tablas.

**`reservation_events`** — CDC de PNRs (emitido por `business-analytics-emitter`):

| Columna | Tipo Athena |
|---|---|
| `event_id` | string |
| `pnr` | string |
| `event_type` | string |
| `old_status` | string |
| `new_status` | string |
| `total` | double |
| `pax_count` | int |
| `user_id` | string |
| `vuelo` | string |
| `fecha` | string |
| `event_ts` | string |

**`flight_events`** — CDC de cambios de estado de vuelo (emitido por `business-analytics-emitter`):

| Columna | Tipo Athena |
|---|---|
| `event_id` | string |
| `vuelo` | string |
| `origen` | string |
| `destino` | string |
| `fecha` | string |
| `hora_salida` | string |
| `old_estado` | string |
| `new_estado` | string |
| `event_ts` | string |

**`claim_events`** — CDC de reclamos (emitido por `business-analytics-emitter`):

| Columna | Tipo Athena |
|---|---|
| `event_id` | string |
| `claim_id` | string |
| `event_type` | string |
| `old_status` | string |
| `new_status` | string |
| `tipo` | string |
| `pnr` | string |
| `user_id` | string |
| `event_ts` | string |

**`interaction_events`** — eventos semánticos del chat (suscripción SNS del topic central):

| Columna | Tipo Athena |
|---|---|
| `event_type` | string |
| `user_id` | string |
| `event_ts` | string |

> El SerDe (`JsonSerDe`) usa `ignore.malformed.json=true`. En `interaction_events` además se mapea `mapping.event_ts=timestamp` (el campo del payload llega como `timestamp` y se expone como columna `event_ts`).

**Particiones (las 4 tablas):**

| Columna | Tipo | Origen |
|---|---|---|
| `dt` | string (partition) | `YYYY-MM-DD` — del path S3 `lake/<tabla>/dt=.../hh=.../` |
| `hh` | string (partition) | `HH` — del path S3 |

> **Por qué partition projection:** Athena calcula las particiones a partir de la `storage.location.template` (no necesita un crawler ni `MSCK REPAIR`). Cuando la query filtra por `dt` o `hh` hace *partition pruning* automático — una query sobre `dt = '2026-06-13'` lee solo los archivos de ese día, no full-scan del bucket. Esto reduce el costo de Athena drásticamente.

---

### Fuentes y destino de cada tabla

No hay un campo `payload` genérico: cada evento se escribe ya tipado en la tabla que le corresponde. Hay dos fuentes de ingesta (ver `firehose.tf`):

| Tabla del lake | Fuente | Cómo llega |
|---|---|---|
| `reservation_events` | `business-analytics-emitter` (CDC del Stream de `business`) | `PutRecord` al Firehose de reservations |
| `flight_events` | `business-analytics-emitter` (CDC del Stream de `business`) | `PutRecord` al Firehose de flight |
| `claim_events` | `business-analytics-emitter` (CDC del Stream de `business`) | `PutRecord` al Firehose de claim |
| `interaction_events` | Suscripción SNS del topic central `events` | Firehose suscrito al SNS (eventos semánticos del chat) |

> Las tres tablas de CDC (`reservation` / `flight` / `claim`) se alimentan de la misma Lambda emitter que lee el DynamoDB Stream de `business` y rutea cada cambio al delivery stream correspondiente. `interaction_events` es la única alimentada directamente desde SNS.

---

### Ejemplo de archivos en S3

Firehose escribe JSON Lines gzip, un objeto `.gz` por buffer (5 MB / 60 s), bajo `lake/<tabla>/dt=YYYY-MM-DD/hh=HH/`. Cada línea es un registro plano y tipado (sin `payload` anidado).

`s3://jetsmart-prod-123456789012-analytics/lake/reservation_events/dt=2026-06-13/hh=14/abc-def-123.gz` (descomprimido):

```json
{"event_id":"evt-9f2a","pnr":"JS7K2P","event_type":"booking_confirmed","old_status":"PENDIENTE","new_status":"CONFIRMADA","total":120.0,"pax_count":1,"user_id":"us-east-1:a1b2-...","vuelo":"JA203","fecha":"2026-06-22","event_ts":"2026-06-13T14:36:01Z"}
```

`s3://...-analytics/lake/flight_events/dt=2026-06-13/hh=14/...gz`:

```json
{"event_id":"evt-3c1b","vuelo":"JA203","origen":"AEP","destino":"MDZ","fecha":"2026-06-22","hora_salida":"17:00","old_estado":"EN_HORARIO","new_estado":"CANCELADO","event_ts":"2026-06-13T14:40:00Z"}
```

`s3://...-analytics/lake/interaction_events/dt=2026-06-13/hh=14/...gz`:

```json
{"event_type":"busqueda_vuelo","user_id":"us-east-1:a1b2-...","event_ts":"2026-06-13T14:35:10Z"}
```

---

### Queries de ejemplo desde Athena

Cada query apunta a la tabla tipada correspondiente (`jetsmart_prod_analytics.<tabla>`), no a una tabla `events`. Las columnas son de primer nivel — no hace falta `json_extract_scalar`.

```sql
-- Interacciones del chat por tipo, últimos 7 días
SELECT event_type, COUNT(*) AS cantidad
FROM jetsmart_prod_analytics.interaction_events
WHERE dt >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY event_type
ORDER BY cantidad DESC;

-- Revenue de reservas confirmadas del último mes (columna total tipada como double)
SELECT
  SUM(total) AS revenue_usd,
  COUNT(*)   AS reservas
FROM jetsmart_prod_analytics.reservation_events
WHERE event_type = 'booking_confirmed'
  AND dt >= date_format(current_date - interval '30' day, '%Y-%m-%d');

-- Vuelos cancelados por ruta, último mes
SELECT
  origen, destino, COUNT(*) AS cancelaciones
FROM jetsmart_prod_analytics.flight_events
WHERE new_estado = 'CANCELADO'
  AND dt >= date_format(current_date - interval '30' day, '%Y-%m-%d')
GROUP BY origen, destino
ORDER BY cancelaciones DESC
LIMIT 10;

-- Reclamos por tipo, último mes
SELECT tipo, COUNT(*) AS cantidad
FROM jetsmart_prod_analytics.claim_events
WHERE dt >= date_format(current_date - interval '30' day, '%Y-%m-%d')
GROUP BY tipo
ORDER BY cantidad DESC;
```

---

### Configuración

| Parámetro | Valor | Razón |
|---|---|---|
| Formato | JSON Lines gzip (`.gz`) | Firehose escribe JSON Lines comprimido; Athena lo lee nativo |
| Particionamiento | `dt=YYYY-MM-DD/hh=HH` | Partition pruning automático (partition projection, sin crawler) |
| Encriptación at rest | AES-256 (SSE-S3) | Sin costo adicional |
| Lifecycle | Glacier después de 90 días | Costo mínimo para retención histórica |
| Ingesta | Kinesis Data Firehose (batch nativo 5 MB / 60 s) | Sin Lambda de transformación intermedia |
| Athena Workgroup | `jetsmart-prod-analytics` | Aislamiento de results y cost tracking del equipo |
| Result location | `s3://...-analytics/athena-results/` | Resultados expiran a los 14 días |
| Acceso | LabRole (Firehose + emitter) + cliente SQL externo | IAM least-privilege |
