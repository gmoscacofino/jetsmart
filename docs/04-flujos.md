# 04 — Flujos del sistema

> Refleja la arquitectura serverless puro del TP4: sin VPC, sin RDS, sin bastion. Cognito Authorizer en API Gateway, Saga orquestada con Step Functions, analytics como data lake (S3 + Glue + Athena).

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
9. API Gateway (chatbot) → Cognito Authorizer valida el JWT
   Si es inválido o falta → 401 antes de invocar Lambda
   Si es válido → invoca chat-handler con los claims ya resueltos
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

En TP3, `chat-handler` validaba el JWT internamente con `python-jose` (~50 líneas + layer). En TP4 esa validación la hace API Gateway con un `aws_api_gateway_authorizer` tipo `COGNITO_USER_POOLS`. Los claims llegan a la Lambda ya verificados en `event.requestContext.authorizer.claims`. Excepción documentada: el API de `auth-callback` queda con `authorization = "NONE"` porque Cognito redirige sin Authorization header.

---

## Flujo 2 — Mensaje del chatbot

El chat es **sincrónico**: la Lambda responde en la misma invocación.

```
Usuario escribe "quiero volar de Buenos Aires a Mendoza el 15 de junio"
        ↓
Frontend hace POST a https://<api-gw>/api/chat
con Authorization: Bearer <id_token>
        ↓
API Gateway → Cognito Authorizer valida el JWT
(token inválido / faltante → 401, no se invoca Lambda)
        ↓
API Gateway invoca `chat-handler` con claims en
event.requestContext.authorizer.claims
        ↓
Lambda identifica al usuario por el `sub` del claim
Carga historial de la sesión desde DynamoDB
        ↓
Lambda construye prompt para Claude:
  [system prompt cargado desde S3 assets]
  [historial completo de la sesión]
  [mensaje nuevo]
        ↓
Lambda llama a la API de Anthropic (claude-sonnet-4-6)
con API key leída de Secrets Manager (cacheada en cold start)
        ↓
[bucle de tool use, hasta 5 rondas]
Si Claude pide tool → Lambda ejecuta:
  search_flights / list_flight_dates / get_reservation /
  list_user_reservations / list_saved_passengers / check_in /
  get_boarding_pass / create_claim / create_reservation
        ↓
Si create_reservation → Lambda llama a Step Functions
  StartExecution (no espera el resultado)
  Devuelve transaction_id al chat — la Saga corre async
        ↓
Lambda guarda intercambio en DynamoDB (sincrónico):
  { rol: "usuario",    mensaje: "..." }
  { rol: "asistente", mensaje: "..." }
        ↓
Lambda publica evento en SNS `events` (asincrónico — no espera):
  { event_type: "chat_message", user_id, ... }
        ↓
Lambda devuelve la respuesta al frontend
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
CollectPayment  (Lambda payment-collect-payment)
  Procesa el cobro (mock; en prod sería la pasarela de pagos)
        ↓
ConfirmBooking  (Lambda payment-confirm-booking)
  Update reserva PENDIENTE → CONFIRMADA en DynamoDB
  Publica evento en SNS `events`
        ↓
PostBookingActions  (estado Parallel)
  ├── BoardingPass   (Lambda boarding-pass)
  │       Genera boarding pass, sube a S3, pre-signed URL 15 min
  └── Notification   (Lambda notification)
          Envía notificación de éxito al usuario
        ↓
BookingConfirmed ✓
```

### Compensaciones automáticas

Cada paso de éxito tiene un `Catch` que dispara la rama de compensación. La Saga sólo ejecuta las compensaciones de pasos que efectivamente corrieron.

```
Si CollectPayment falla:
  → CancelBooking (Lambda payment-cancel-booking)
       Marca la reserva como CANCELADA
  → ReleaseFlight (Lambda payment-release-flight)
       Devuelve los asientos bloqueados en DynamoDB
  → NotifyBookingFailed (Lambda notification, modo error)
  → BookingDLQ (SDK integration directa: sqs:sendMessage)
       Persiste el contexto del fallo en booking-failed-dlq (14 días)
  → BookingFailed ✗

Si ConfirmBooking falla (raro: el cobro ya pasó):
  → RefundPayment (Lambda payment-refund-payment)
       Revierte el cobro
  → CancelBooking → ReleaseFlight → NotifyBookingFailed → BookingDLQ
```

### Por qué Step Functions y no SNS→SQS encadenadas (TALO del TP3)

El TP3 implementaba este flujo con cadenas de `payment-validate-queue → Lambda → SNS → payment-reserve-queue → ...`, cada Lambda sabiendo a qué topic publicar el siguiente paso. La orquestación quedaba distribuida y el rollback había que codificarlo manualmente.

Step Functions centraliza la orquestación en la ASL. Las Lambdas sólo hacen su trabajo y devuelven estado. `Catch` declarativo dispara compensaciones, retries con backoff exponencial son configuración, no código.

### Decremento atómico de asientos

`payment-reserve-flight` usa `ConditionExpression="asientos_disponibles >= :pasajeros"`. Si dos usuarios reservan el último asiento simultáneamente, sólo uno gana — el otro recibe `ConditionalCheckFailedException`, Step Functions lo cataloga como error de disponibilidad y dispara la rama de compensación.

---

## Flujo 4 — Boarding pass

Corre como una rama del estado `Parallel` `PostBookingActions` dentro de la Saga.

```
Step Functions invoca Lambda `boarding-pass` con el contexto de la reserva
        ↓
Lambda lee la reserva y los pasajeros desde DynamoDB
        ↓
Lambda genera el contenido del boarding pass (texto)
        ↓
Lambda sube el archivo al bucket S3 `jetsmart-prod-<account-id>-assets`:
  ruta: boarding-passes/{reserva_id}/{pasajero_id}.txt
  SSE-S3, public access block activo
        ↓
Lambda genera pre-signed URL (válida 15 min, sólo para este objeto)
        ↓
Lambda devuelve la URL a Step Functions → entra en el resultado de PostBookingActions
        ↓
Cuando el usuario consulta `get_boarding_pass` en el chat,
chat-handler devuelve la URL pre-firmada
```

El bucket es privado — la pre-signed URL es el único modo de descarga.

---

## Flujo 5 — Analytics (data lake S3 + Glue + Athena)

Los eventos del chatbot se procesan offline. La capa OLTP (DynamoDB) no se toca para analytics; los eventos viajan por SNS → SQS → Lambda y se materializan como objetos JSON Lines particionados en S3.

```
Publicadores:
  chat-handler           → SNS `events`  (mensajes de chat)
  payment-confirm-booking → SNS `events` (compras completadas)
  payment-cancel-booking  → SNS `events` (cancelaciones)
        ↓
SNS topic `events`
        ↓ fan-out (sólo 1 sub hoy: analytics)
SQS `analytics-queue`   (long polling 20s, DLQ tras 3 intentos)
        ↓ trigger batch_size=10
Lambda `analytics-processor`
        ↓ put_object
S3 `jetsmart-prod-<account-id>-analytics`:
  events/dt=YYYY-MM-DD/hh=HH/<uuid>.jsonl
        ↓
(en paralelo, cada hora)
Glue Crawler `events-crawler`
  Infiere schema (event_type, user_id, timestamp, payload, ingested_at)
  Descubre nuevas particiones dt= / hh=
        ↓
Glue Data Catalog (database: jetsmart_prod_analytics, table: events)
        ↓ JDBC
Athena Workgroup `jetsmart-prod-analytics`
        ↓ SQL
Equipo de business analytics (DBeaver / DataGrip)
```

### Eventos publicados

| event_type | Publicador | Cuándo |
|---|---|---|
| `chat_message` | chat-handler | Cada mensaje procesado del chatbot |
| `booking_confirmed` | payment-confirm-booking | Reserva confirmada exitosamente |
| `booking_cancelled` | payment-cancel-booking | Saga compensa una reserva |

### Por qué SQS entre SNS y Lambda

SQS amortigua picos y permite reintentos con DLQ. Si la Lambda o S3 fallaran, sin SQS los mensajes se perderían (SNS→Lambda directo no garantiza retención). Sacar S3 PutObject del path sincrónico del chat además mejora la latencia del usuario aunque sea ~50ms.

### Por qué data lake y no RDS (cambio del TP3)

OLTP postgres es la herramienta equivocada para analítica histórica: cargás el primario con queries pesadas, no escala por costo, y requiere VPC + RDS Proxy + bastion para acceso del equipo. S3 + Athena es serverless, escala a TBs, cobra ~5 USD/TB escaneado, y se consulta con cualquier cliente JDBC sin tocar la red privada (que ya no existe).

### Frescura

El crawler corre cada 1 hora. Para refresh inmediato durante la demo:
```bash
aws glue start-crawler --name jetsmart-prod-events-crawler
```

---

## Flujo 6 — Backups

DynamoDB es el único datastore persistente del sistema. La estrategia tiene dos capas complementarias:

| Capa | Mecanismo | Cobertura | Cuándo se usa |
|---|---|---|---|
| Continuo (PITR) | `point_in_time_recovery` en la tabla | Últimos 35 días, restauración a cualquier segundo | Recuperación operacional: borrado accidental, corrupción reciente |
| Archivo (Export) | EventBridge cron diario → Lambda → Export a S3 | 1 año, retención de archivo | Pérdida catastrófica de la tabla, PITR deshabilitado, análisis histórico |

### Export diario automatizado a S3

```
EventBridge Rule  cron(0 3 * * ? *)        (03:00 UTC = 00:00 ART, hora valle)
        ↓ invoke
Lambda backup-dynamodb
        ↓ dynamodb:ExportTableToPointInTime
        ↓ (async — el export corre en background, no bloquea la Lambda)
DynamoDB service ejecuta el export consumiendo PITR
        ↓ put_object
S3 bucket jetsmart-prod-<account-id>-backups
  dynamodb/YYYY-MM-DD/AWSDynamoDB/<export-id>/data/*.json.gz
        ↓ lifecycle
0–90 días:   STANDARD
90–365 días: GLACIER (acceso poco frecuente, restore 3-5h)
365 días:    expira
```

La Lambda **no espera** el resultado del export — `ExportTableToPointInTime` devuelve un `exportArn` inmediatamente y DynamoDB hace el trabajo en background (puede tardar varios minutos según el tamaño de la tabla). El estado del export se consulta con `describe_export` si fuera necesario, pero para el flujo diario es fire-and-forget: si falla, la Lambda loguea el error en CloudWatch y al día siguiente reintenta el cron.

### Permisos: bucket policy explícita

El bucket de backups tiene una **bucket policy** que permite a `dynamodb.amazonaws.com` hacer `s3:PutObject` y `s3:AbortMultipartUpload`, con `Condition: aws:SourceAccount = <account propio>` para mitigar el confused deputy. Sin esa policy, el export falla con AccessDenied al intentar escribir el archivo.

### Por qué bucket dedicado y no prefix en `assets/`

- **Lifecycle independiente** — los backups quieren retención larga (365 días + archivo en Glacier); los boarding passes y backups del system prompt quieren expiración corta. Mezclar prefijos en un solo bucket complica el razonamiento de costos y la trazabilidad.
- **Bucket policy específica para DynamoDB** — escribir desde un service principal externo (DynamoDB) requiere policy bucket-level. Tenerla en `assets` ampliaría el blast radius de esa policy a otros prefijos que no la necesitan.
- **Trazabilidad** — `jetsmart-prod-<account>-backups` es nombre auto-explicativo. En auditoría / Cost Explorer separa cleanly del resto.

### Por qué Glacier a los 90 días y no antes

PITR cubre los últimos 35 días. Los exports diarios duplican esa cobertura hasta el día 35, pero a partir de ahí son la única protección. Glacier a partir del día 90 deja una ventana de ~2 meses de **restore inmediato post-PITR** antes de pasar al tier frío.

### Resto del sistema

- **S3 buckets** — los objetos `.jsonl` de analytics son inmutables por diseño (append-only por partición). El bucket frontend se reconstruye desde el repo en cada deploy.
- **Lambdas / Step Functions / API GW / Cognito** — código y configuración viven en Terraform en el repo. La fuente de verdad es git; ante pérdida total, `terraform apply` reconstruye toda la infraestructura.
- **Secrets Manager** — la única secret (`anthropic-api-key`) se rota manualmente; backup = la key original en el password manager del equipo.

> El flujo de backup de RDS del TP3 desapareció junto con RDS.
