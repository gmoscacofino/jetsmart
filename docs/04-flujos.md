# 04 — Flujos del sistema

> Refleja la arquitectura desplegada: el core del chatbot (chat-handler) corre como servicio FastAPI en ECS Fargate (subnets privadas de la VPC) detrás de un ALB internet-facing; las Lambdas de negocio también corren en la VPC (subnets `private-lambda`), salvo auth-callback/cognito-trigger que son regionales; sin RDS, sin bastion. El JWT de Cognito se valida in-app en el contenedor (no Cognito Authorizer), Saga orquestada con Step Functions, analytics como data lake (S3 + Glue + Athena).

## Flujo 1 — Autenticación (login con Cognito)

### Por qué se necesita un workaround con Lambda

Cuando el usuario inicia sesión en Cognito, el servicio le devuelve un `code` temporal a una URL que vos configuraste. Para convertir ese `code` en tokens reales (Access Token, ID Token), hace falta ejecutar código — hacer un POST al token endpoint de Cognito.

S3 no puede ejecutar código y sólo sirve HTTP estático. Cognito sólo redirige a URLs HTTPS. La Lambda `auth-callback` detrás de API Gateway es el bridge HTTPS que cierra el gap.

### El flujo paso a paso

```
1. Usuario abre el frontend en S3 (HTTP)
           ↓
2. Click en "Iniciar sesión"
   Frontend redirige a la Cognito Hosted UI
           ↓
3. Usuario ingresa email + contraseña
   Cognito verifica credenciales y genera un `code` de un solo uso
           ↓
4. Cognito redirige al usuario a:
   API Gateway (auth)  →  GET /callback?code=abc123
   (authorization = "NONE" porque Cognito no manda Authorization header)
           ↓
5. API Gateway invoca la Lambda `auth-callback`
   Lambda hace POST al token endpoint de Cognito con el code
   Cognito responde con: Access Token + ID Token + Refresh Token
           ↓
6. Lambda hace 302 al frontend en S3:
   https://jetsmart-frontend.s3.../index.html#id_token=xyz
           ↓
7. El JavaScript del frontend lee los tokens del fragment
   Los guarda en localStorage
           ↓
8. A partir de ahora, cada request al backend incluye el ID Token
   en el header: Authorization: Bearer <token>
           ↓
9. ALB → chat-handler (Fargate) valida el JWT in-app
   server.py verifica firma RS256 contra el JWKS del User Pool, issuer y exp
   Si es inválido o falta → 401 antes de ejecutar la lógica
   Si es válido → pasa los claims (sub, etc.) a chat_core
```

### Cognito trigger — post-registro

Cuando un usuario se registra (no en login), Cognito invoca automáticamente la Lambda `cognito-trigger` que asigna al usuario al grupo `users` del User Pool.

```
Usuario completa el registro en la Hosted UI
        ↓
Cognito trigger invoca Lambda `cognito-trigger`
        ↓
Lambda llama a Cognito AdminAddUserToGroup → grupo `users`
```

### Cambio respecto al TP3

En TP3, `chat-handler` validaba el JWT internamente con `python-jose` corriendo en Lambda. En la arquitectura desplegada el chat-handler corre como servicio FastAPI en Fargate detrás del ALB y la validación sigue siendo in-app: `server.py` verifica la firma RS256 contra el JWKS del User Pool (más issuer y exp) y pasa los claims a la lógica. No hay Cognito Authorizer: el API Gateway del chatbot fue reemplazado por el ALB. El único API Gateway que queda es el de `auth-callback`, con `authorization = "NONE"` porque Cognito redirige sin Authorization header.

---

## Flujo 2 — Mensaje del chatbot

El chat es **sincrónico**: el chat-handler responde en el mismo request HTTP.

```
Usuario escribe "quiero volar de Buenos Aires a Mendoza el 15 de junio"
        ↓
Frontend hace POST a http://<alb-dns>/api/chat
con Authorization: Bearer <id_token>
        ↓
ALB → chat-handler (Fargate) valida el JWT in-app
(server.py: firma RS256 contra el JWKS, issuer, exp;
 token inválido / faltante → 401, no se ejecuta la lógica)
        ↓
chat-handler identifica al usuario por el `sub` del claim
Carga historial de la sesión desde DynamoDB
        ↓
chat-handler construye prompt para Claude:
  [system prompt cargado desde S3 assets]
  [historial completo de la sesión]
  [mensaje nuevo]
        ↓
chat-handler llama a la API de Anthropic (claude-haiku-4-5-20251001)
con API key leída de Secrets Manager (cacheada en el proceso)
        ↓
[bucle de tool use, hasta 5 rondas — MAX_TOOL_ROUNDS = 5]
Si Claude pide tool → chat-handler ejecuta una de las 10:
  search_flights / list_flight_dates / get_reservation /
  list_user_reservations / list_saved_passengers / check_in /
  get_boarding_pass / create_claim / create_reservation /
  escalate_to_human
        ↓
Si create_reservation → chat-handler llama a Step Functions
  StartExecution (no espera el resultado)
  Devuelve transaction_id al chat — la Saga corre async
        ↓
chat-handler guarda intercambio en DynamoDB (sincrónico):
  { rol: "usuario",    mensaje: "..." }
  { rol: "asistente", mensaje: "..." }
        ↓
chat-handler publica evento en SNS `events` (asincrónico — no espera):
  { event_type: "chat_message", user_id, ... }
        ↓
chat-handler devuelve la respuesta al frontend
        ↓
Frontend renderiza el mensaje
```

**Por qué DynamoDB sincrónico y SNS asincrónico:** si el historial se escribiera async, el próximo mensaje podría llegar antes de persistir el actual y el LLM no lo vería en el contexto. DynamoDB tarda ~5ms, no frena al usuario. SNS es para analytics — no es urgente y no debe acoplar la latencia del chat al pipeline de eventos.

---

## Flujo 3 — Reserva y pago (Saga con Step Functions)

El flujo de pago es una transacción distribuida — varios pasos que deben ser atómicos. Step Functions lo orquesta con el patrón Saga: cada paso tiene una compensación que se ejecuta automáticamente ante fallos.

```
Usuario confirma "comprar" en el chat
        ↓
chat-handler invoca tool `create_reservation`
        ↓
chat-handler llama a Step Functions StartExecution
con el input: { user_id, flight, passengers, fare, ... }
Devuelve transaction_id al chat (no espera resultado)
        ↓
═══════════════════════════════════════════════════════════
Step Functions State Machine — Saga (async)
═══════════════════════════════════════════════════════════

ReserveFlight   (Lambda payment-reserve-flight)
  Decremento atómico de asientos en DynamoDB
  ConditionExpression: asientos_disponibles >= :pasajeros
  Si la condición falla → error → compensación
        ↓
ReserveBooking  (Lambda payment-reserve-booking)
  Crea la reserva en DynamoDB con estado PENDIENTE
        ↓
CollectPayment  (Lambda payment-collect)
  Procesa el cobro (mock; en prod sería la pasarela de pagos)
        ↓
ConfirmBooking  (Lambda payment-confirm)
  Update reserva PENDIENTE → CONFIRMADA en DynamoDB
        ↓
PublishBookingConfirmed  (SDK integration sns:publish)
  Publica el evento `booking_confirmed` al SNS central `events`
  (event_type como MessageAttribute). Best-effort: si el publish
  falla, la reserva ya quedó confirmada igual.
  El fan-out post-booking (notification + boarding-pass + analytics)
  lo hacen las SUSCRIPCIONES con filter_policy, NO un Parallel interno
  ni una cola.
        ↓
BookingConfirmed ✓
```

> **Cambio TP4:** el post-procesado (notificación + boarding pass) salió del path sync del Saga. El estado terminal de éxito sólo publica `booking_confirmed` al topic `events`; las Lambdas suscriptas (`notification`, `boarding_pass_async`) reaccionan por filtro. El boarding pass es fire-and-forget y no afecta la reserva ya confirmada. El usuario consulta el BP con `get_boarding_pass` y, si todavía no se generó, recibe "tu boarding pass se está generando, intentá en unos segundos".

### Compensaciones automáticas

Cada paso de éxito tiene un `Catch` que dispara la rama de compensación. La Saga sólo ejecuta las compensaciones de pasos que efectivamente corrieron.

```
Si CollectPayment falla:
  → CancelBooking (Lambda payment-cancel)
       Marca la reserva como CANCELADA
  → ReleaseFlight (Lambda payment-release-flight)
       Devuelve los asientos bloqueados en DynamoDB
  → PublishBookingFailed (SDK integration sns:publish)
       Publica `booking_failed` a `events`; la Lambda notification
       reacciona por filtro (booking_failed) y avisa al usuario
  → BookingDLQ (SDK integration directa: sqs:sendMessage)
       Persiste el contexto del fallo en booking-failed-dlq (14 días)
  → BookingFailed ✗

Si ConfirmBooking falla (raro: el cobro ya pasó):
  → RefundPayment (Lambda payment-refund)
       Revierte el cobro
  → CancelBooking → ReleaseFlight → PublishBookingFailed → BookingDLQ
```

### Por qué Step Functions y no SNS→SQS encadenadas (TALO del TP3)

El TP3 implementaba este flujo con cadenas de `payment-validate-queue → Lambda → SNS → payment-reserve-queue → ...`, cada Lambda sabiendo a qué topic publicar el siguiente paso. La orquestación quedaba distribuida y el rollback había que codificarlo manualmente.

Step Functions centraliza la orquestación en la ASL. Las Lambdas sólo hacen su trabajo y devuelven estado. `Catch` declarativo dispara compensaciones, retries con backoff exponencial son configuración, no código.

### Decremento atómico de asientos

`payment-reserve-flight` usa `ConditionExpression="asientos_disponibles >= :pasajeros"`. Si dos usuarios reservan el último asiento simultáneamente, sólo uno gana — el otro recibe `ConditionalCheckFailedException`, Step Functions lo cataloga como error de disponibilidad y dispara la rama de compensación.

---

## Flujo 4 — Boarding pass (async via SNS → Lambda — TP4)

TP4: el boarding pass se desacopló del path sync del Saga. Ahora corre como un fire-and-forget event-driven: la Saga sólo publica el hecho `booking_confirmed` y la Lambda reacciona por suscripción con filtro.

```
Step Functions — estado terminal de éxito "PublishBookingConfirmed"
  SDK integration arn:aws:states:::sns:publish → SNS `events`
  (event_type=booking_confirmed como MessageAttribute)
        ↓ suscripción SNS→Lambda DIRECTO (filter_policy event_type=booking_confirmed)
Lambda boarding_pass_async
        ↓
Lambda parsea el estado, genera el contenido del boarding pass (texto)
        ↓
Lambda sube el archivo al bucket S3 jetsmart-prod-<account-id>-boarding-passes:
  ruta dentro del bucket: {user_id}/{pnr}.txt
  SSE-S3, public access block activo
        ↓
Lambda genera pre-signed URL (válida 15 min, sólo para este objeto)
        ↓
Lambda hace PutItem en business table:
  PNR#{pnr}/BP#01  con  s3_key + bp_url + issued_at
        ↓
[Async] Cuando el usuario consulta `get_boarding_pass` en el chat,
chat-handler hace GetItem PNR#/BP#01:
  - Si existe → devuelve la pre-signed URL
  - Si todavía no existe → devuelve "tu boarding pass se está generando, intentá en unos segundos"
```

### Por qué async

Si la generación del BP fallara, antes detenía toda la confirmación de la reserva (el usuario podía no ver su PNR confirmado por un error post-pago). Con esta arquitectura:

- La reserva queda confirmada de inmediato; el BP no es bloqueante.
- Es fire-and-forget: NO hay cola `boarding-pass-generation` ni DLQ. La durabilidad la dan el retry de SNS→Lambda y una alarma de Lambda Errors en CloudWatch (ver `messaging.tf`). Si la generación falla, el usuario igualmente puede reintentar consultando su BP más tarde; el BP es recuperable, no es plata.
- Demostramos el patrón de **decoupling event-driven** (Saga publica el hecho, la Lambda reacciona por suscripción con filtro), que es lo que se vería en una arquitectura productiva.

El bucket es privado — la pre-signed URL es el único modo de descarga.

---

## Flujo 7 — Derivación a humano (TP4)

Permite al usuario hablar con un agente humano cuando el chatbot no puede resolver su problema. Detrás de escena, decopla el chatbot del sistema del call center.

```
Usuario escribe: "quiero hablar con un humano"
        ↓
chat_handler invoca el modelo Claude
        ↓
Claude evalúa el intent y decide invocar la tool `escalate_to_human`
con parámetros: { reason, urgency }
        ↓
chat_handler:
  1. Genera handoff_id (HO-XXXXXXXX)
  2. Pone Item en conversations table:
       SESSION#{session_id} / HANDOFF#{ts}#{handoff_id}  (status=QUEUED, TTL 30d)
     Y thin pointer:
       USER#{user_id} / HANDOFF#{handoff_id}             (status=QUEUED, TTL 30d)
  3. Envía mensaje a SQS human-handoff:
       { handoff_id, session_id, user_id, reason, urgency, created_at }
  4. Publica evento "handoff_escalated" a SNS events (analytics)
  5. Responde al usuario: "Tu pedido fue derivado al equipo de soporte humano
     (ticket HO-XXXXXXXX). Te van a contactar al email registrado según prioridad."
        ↓ trigger event_source_mapping (batch_size=5)
Lambda human_handoff_processor
        ↓
  1. MOCK POST https://mock.callcenter.internal/tickets
     (en producción: HTTP real al CRM del call center)
  2. Recibe call_center_ticket (CC-XXXXXXXX) del sistema externo
  3. UpdateItem HANDOFF# en conversations:
       status=ACK, call_center_ticket=CC-XXXX, acked_at=now
  4. Publica email vía SNS notifications:
       Subject: "Tu solicitud de soporte fue derivada — HO-XXXX"
       Body:    ticket id, prioridad, motivo, "un agente te va a contactar"
```

### Por qué SQS y no llamada directa al call center

- **Decopla disponibilidad:** si el call center está caído, el chatbot sigue respondiendo. El pedido queda esperando en la cola.
- **Reintentos automáticos con DLQ:** si la Lambda `human_handoff_processor` falla, SQS reintenta hasta 3 veces. Después cae a `human-handoff-dlq` (retención 14d) y dispara alarma CloudWatch.
- **Trazabilidad:** todos los handoffs quedan registrados en conversations table con TTL de 30d para auditoría.

### Mock del call center

En este TP el "POST al call center" se loguea en CloudWatch como `MOCK POST https://mock.callcenter.internal/tickets`. En producción sería una llamada HTTP real a un sistema externo (Salesforce Service Cloud, Genesys, Zendesk Talk, etc.).

---

## Flujo 8 — Notificaciones proactivas (TP4, event-driven)

Cuando un vuelo se cancela en el sistema de operaciones, todos los pasajeros afectados reciben automáticamente una notificación por email. Habilita el caso de uso "tormenta cancela vuelo → 5000 pasajeros enterados antes de llegar al aeropuerto".

> **Trigger en TP4 final:** **DynamoDB Stream + Lambda `stream-emitter`**. Ops (o el weather-poller en Fargate) cambia el `estado_vuelo` del master row a `CANCELADO` desde la consola DynamoDB (o el dashboard interno que conectaría en producción) y el flujo se dispara automáticamente. Ver justificación #28.

```
[Disparador en producción — sistema de ops o consola DynamoDB]
   ops → UpdateItem master row FLIGHT#AEP#MDZ / DATE#2026-06-20#FLIGHT#JA203
         SET estado_vuelo = "CANCELADO", cancellation_reason = "...", cancellation_at = "..."
        ↓
DynamoDB Stream (NEW_AND_OLD_IMAGES) de la tabla business
        ↓ event_source_mapping con filter_criteria (eventName=MODIFY, NewImage.estado_vuelo=CANCELADO)
Lambda stream-emitter
   1. Filtra master rows FLIGHT# (descarta SEAT#, PNR#, etc.)
   2. Detecta transición real (OldImage.estado_vuelo != CANCELADO)
   3. Publica al SNS central `events`:
      { event_type: "flight_cancelled", vuelo_numero, fecha, reason }
        ↓ suscripción SNS→Lambda DIRECTO (filter_policy event_type=flight_cancelled)
Lambda proactive_notifications
        ↓
  1. Parsea body (SNS-wrapped: extrae .Message del envelope)
  2. Query GSI2 ReservationsByFlight con HK=FLIGHT#JA203#2026-06-20
     → devuelve TODOS los items SEGMENT# del vuelo (1 query, no scan)
     → cada uno tiene user_id, email, passenger_name, pnr
  3. Para cada PNR:
     UpdateItem PNR#{pnr}/#METADATA  set status=AFFECTED_BY_CANCELLATION
                                          cancellation_reason
                                          cancellation_notified_at
  4. Dedup emails únicos (un user con varios PNRs en el mismo vuelo no recibe duplicados)
  5. Para cada email único:
     SNS publish a notifications:
       Subject: "Cancelación de vuelo JA203 — 2026-06-20"
       Body:    PNR, motivo, instrucciones de reprogramación
  6. Publica evento "flight_cancellation_notified" a SNS events (analytics)
```

### Por qué GSI2 ReservationsByFlight

Sin este índice, encontrar "qué pasajeros están en el vuelo X del día Y" requiere un Scan de toda la tabla business — O(n) lineal sobre todas las reservas históricas. Con GSI2, una sola Query devuelve el resultado en O(log n) — el atributo `gsi2pk = FLIGHT#{vuelo}#{fecha}` se estampa en cada item SEGMENT# en el momento del booking.

**Es el GSI clave del TP4** — habilita el caso de uso "ops cancela un vuelo, el sistema notifica automáticamente a todos los afectados".

### Por qué el topic central `events` como punto de fan-out

- **SNS `events`**: el mismo `flight_cancelled` que escucha `proactive_notifications` lo escucha también `refund_trigger` (dispara la Refund Saga). Sumar otro suscriptor (dashboard ops, etc.) es agregar una suscripción con filtro, sin tocar al publisher (`stream-emitter`).
- **SNS→Lambda directo, sin SQS**: el downstream es elástico (Lambda escala) y el resultado es recuperable desde DynamoDB → no se justifica una cola amortiguadora. La durabilidad la dan el retry de SNS→Lambda y una alarma de Lambda Errors en CloudWatch. NO existe la cola `proactive-notifications` ni su DLQ.

### Por qué offline en el demo y no en vivo

El demo en vivo se concentra en el flujo end-to-end del chatbot (login → search → reserva → check-in → BP async → derivación a humano). Disparar la cancelación en vivo agregaría riesgo (depender de CLI + creds + email subscription) y no demuestra nada distinto a lo que muestran los CloudWatch logs de una corrida previa. La cancelación se ejecuta antes de la presentación; durante la defensa se muestra el diagrama, los logs persistidos, y la respuesta de la pregunta "¿cómo se dispararía en producción?".

---

## Flujo 5 — Analytics (data lake S3 + Glue + Athena)

Los eventos se procesan offline. La capa OLTP (DynamoDB) no se consulta para analytics; los datos se materializan como JSON Lines gzip particionados en S3. Hay **dos fuentes de ingesta** que alimentan **4 Kinesis Data Firehose tipados**, uno por entidad. Firehose batchea nativo (no hay Lambda de procesamiento intermedia): cada delivery stream acumula 5 MB o 60 s, comprime en GZIP y escribe a S3.

```
FUENTE 1 — CDC transaccional (cambios en business)
  Tabla DynamoDB `business`
        ↓ DynamoDB Stream (NEW_AND_OLD_IMAGES)
  event_source_mapping (filter_criteria: INSERT/MODIFY de PK PNR# / FLIGHT# con estado_vuelo / CLAIM#)
        ↓ batch_size=50, ReportBatchItemFailures
  Lambda `business-analytics-emitter`
    Clasifica la entidad por el prefijo del PK (PNR# / FLIGHT# / CLAIM#)
    Arma el record tipado y hace PutRecord al Firehose correspondiente:
        ↓                      ↓                      ↓
  Firehose                Firehose               Firehose
  reservation_events      flight_events          claim_events

FUENTE 2 — Eventos de comportamiento (chat)
  SNS topic `events`  (chat_message, busqueda_vuelo, checkin_realizado, handoff_requested, …)
        ↓ suscripción protocol=firehose, raw_message_delivery=true
        ↓ filter_policy: event_type anything-but [booking_confirmed, booking_failed, flight_cancelled]
          (los transaccionales ya entran por el CDC → sin doble conteo)
  Firehose interaction_events

  ── Los 4 Firehose escriben a S3 (buffer 5 MB / 60 s, GZIP) ──
        ↓
  S3 `jetsmart-prod-<account-id>-analytics`:
    lake/<entidad>/dt=YYYY-MM-DD/hh=HH/*.gz
    (entidad ∈ reservation_events | flight_events | claim_events | interaction_events)
    Records fallidos → lake-errors/<entidad>/...
        ↓
  Glue Data Catalog  (database: jetsmart_prod_analytics)
    4 tablas EXTERNAL tipadas ESTÁTICAS, definidas en Terraform (analytics.tf)
    partition projection dt (date) / hh (integer 0–23) → SIN crawler
        ↓ JDBC
  Athena Workgroup `jetsmart-prod-analytics`
        ↓ SQL: jetsmart_prod_analytics.<tabla>
  Equipo de business analytics (DBeaver / DataGrip)
```

### Las 4 tablas tipadas (Glue Data Catalog)

Cada entidad tiene su propio esquema declarado en Terraform; no hay una tabla `events` genérica con un blob `payload`.

| Tabla | Columnas |
|---|---|
| `reservation_events` | event_id, pnr, event_type, old_status, new_status, total (double), pax_count (int), user_id, vuelo, fecha, event_ts |
| `flight_events` | event_id, vuelo, origen, destino, fecha, hora_salida, old_estado, new_estado, event_ts |
| `claim_events` | event_id, claim_id, event_type, old_status, new_status, tipo, pnr, user_id, event_ts |
| `interaction_events` | event_type, user_id, event_ts |

### Por qué Firehose y no SQS → Lambda (cambio del diseño viejo)

El diseño anterior ponía `SNS → SQS analytics-queue → Lambda analytics-processor → put_object`. Firehose lo reemplaza: hace el buffering por tamaño/tiempo, la compresión GZIP y el particionado dt/hh de forma nativa y administrada — sin código propio que mantener, sin cola, sin Lambda de escritura. El CDC desde el stream de `business` además captura los cambios transaccionales directo de la fuente de verdad (no depende de que cada publicador recuerde emitir el evento), y separar 4 streams tipados evita el `payload` JSON opaco: cada entidad queda consultable con columnas reales.

### Por qué data lake y no RDS (cambio del TP3)

OLTP postgres es la herramienta equivocada para analítica histórica: cargás el primario con queries pesadas, no escala por costo, y requiere VPC + RDS Proxy + bastion para acceso del equipo. S3 + Athena es serverless, escala a TBs, cobra ~5 USD/TB escaneado, y se consulta con cualquier cliente JDBC sin tocar la red privada.

### Frescura — sin crawler

Las tablas son **estáticas** (definidas en Terraform) y las particiones se resuelven en consulta con **partition projection** (rango `dt` desde 2026-01-01 hasta NOW, `hh` de 00 a 23). No hay descubrimiento de schema ni `start-crawler`: apenas Firehose escribe el objeto (buffer máx. 60 s), los datos quedan consultables. Athena proyecta la partición a partir de los predicados `dt`/`hh` de la query.

### Query de ejemplo (Athena)

```sql
-- Reservas confirmadas por día en la última semana
SELECT dt,
       count(*)         AS reservas,
       sum(total)       AS facturado,
       sum(pax_count)   AS pasajeros
FROM   jetsmart_prod_analytics.reservation_events
WHERE  dt >= date_format(current_date - interval '7' day, '%Y-%m-%d')
  AND  new_status = 'CONFIRMADA'
GROUP BY dt
ORDER BY dt;
```

Filtrar siempre por `dt` (y `hh` si aplica) acota las particiones escaneadas y, con ello, el costo por TB de Athena.

---

## Flujo 6 — Backups (PITR continuo)

DynamoDB es el único datastore persistente del sistema. La protección de datos se apoya en **PITR (point-in-time recovery)**, habilitado en ambas tablas.

| Capa | Mecanismo | Cobertura | Cuándo se usa |
|---|---|---|---|
| Continuo (PITR) | `point_in_time_recovery` en la tabla | Últimos 35 días, restauración a cualquier segundo | Recuperación operacional: borrado accidental, corrupción reciente |

### PITR — recuperación continua

`point_in_time_recovery` está activo en las tablas `conversations` y `business` (ver `database.tf`). DynamoDB mantiene backups continuos automáticos de los últimos 35 días: ante un borrado accidental o una corrupción reciente, se restaura la tabla a cualquier segundo dentro de esa ventana sin intervención manual ni infraestructura adicional.

### Resto del sistema

- **S3 buckets** — los objetos `.gz` de analytics son inmutables por diseño (append-only por partición). El bucket frontend se reconstruye desde el repo en cada deploy.
- **Lambdas / Step Functions / API GW / Cognito** — código y configuración viven en Terraform en el repo. La fuente de verdad es git; ante pérdida total, `terraform apply` reconstruye toda la infraestructura.
- **Secrets Manager** — la única secret (`anthropic-api-key`) se rota manualmente; backup = la key original en el password manager del equipo.

> El flujo de backup de RDS del TP3 desapareció junto con RDS.
