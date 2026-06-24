# 05 — Componentes en detalle

## ECS Fargate — Cómputo en contenedor (el core)

> **Cambio de arquitectura (TP4, post-defensa 17/06):** el core del chatbot dejó de ser una Lambda y pasó a ser un **servicio FastAPI nativo en ECS Fargate**, dentro de la VPC. Es la respuesta al feedback de la defensa: el cómputo del chatbot ahora vive en subnets privadas, no en Lambdas sueltas. Ver `docs/03-networking.md`.

Dos workloads en contenedor corren en Fargate (`ecs.tf`), ambos en subnets privadas con `assign_public_ip=false`:

| Servicio | Entrada | Auto Scaling | CPU/Mem |
|---|---|---|---|
| `chat-handler` | ALB internet-facing (HTTP:80) → target group puerto 8000 | 2 → 6 tasks (target tracking CPU 60%) | 256 / 512 |
| `weather-poller` | Sin ALB — solo egress por NAT a la clima API | desired_count = 1 (fijo) | 256 / 512 |

### `chat-handler` — servicio FastAPI nativo

El punto de entrada principal del chatbot. **No es una Lambda**: es un contenedor Docker (imagen en ECR, pulleada por Fargate) que corre FastAPI nativo. Código en `app/chat-handler/`:

- `server.py` — el router FastAPI. Por cada request: valida el JWT, rutea método/path a la función de negocio, traduce el `(status, payload)` a respuesta HTTP.
- `chat_core.py` — la lógica de negocio: bucle de tool use de Anthropic, acceso a DynamoDB, tokenización PII, inicio del Saga de pago.

Rutas expuestas:

| Método + path | Auth | Función |
|---|---|---|
| `POST /api/chat` | JWT | Chat con tool use, historial, contexto del usuario |
| `GET /api/reservations` | JWT | Reservas del usuario autenticado |
| `POST /api/payment` | JWT | Inicia el Saga de pago (StartExecution de Step Functions) |
| `GET /health` | sin auth | Health check del ALB target group |

**Topología:** 2 tasks Multi-AZ (1 por subnet privada / AZ) detrás del ALB, con Auto Scaling 2→6 por target tracking de CPU al 60%. El ALB es HTTP:80 (Academy no habilita ACM/HTTPS; en producción real iría listener 443 con ACM).

#### Validación de JWT in-app (no Cognito Authorizer)

`chat-handler` valida el **JWT de Cognito dentro del contenedor** (`server.py`), no en el perímetro:

- Descarga el JWKS del User Pool (cacheado en memoria, refresca ante `kid` desconocido por rotación de claves).
- Verifica firma **RS256**, `issuer` y `exp`; valida `aud` si hay `COGNITO_CLIENT_ID` seteado.
- Pasa los claims (la identidad del usuario, `sub`) a `chat_core` como contexto.
- Si el token es inválido → `401` antes de ejecutar lógica de negocio. El preflight `OPTIONS` sale sin auth (CORS).

> **Evolución del manejo de auth:** TP3 validaba el JWT manualmente con `python-jose` dentro de la Lambda. El diseño serverless intermedio (rechazado) proponía delegar la validación a un **Cognito Authorizer** en API Gateway. La arquitectura final **no usa Cognito Authorizer**: el contenedor valida el JWT in-app contra el JWKS (RS256). Conceptualmente es lo mismo que hacía la Lambda en TP3, ahora en un servicio web nativo.

### Tool use en chat-handler

`chat-handler` no llama al LLM una sola vez — implementa un **bucle de tool use** de hasta 5 rondas (`MAX_TOOL_ROUNDS = 5`). Claude puede pausar su respuesta y pedir que el servicio ejecute funciones reales para obtener datos antes de responder:

- `search_flights` — consulta disponibilidad de vuelos en la tabla `business` (el PSS de la aerolínea)
- `get_reservation` — consulta el estado de una reserva del usuario

La tabla `business` es la fuente única de verdad: la consultan tanto el chatbot como (en una arquitectura completa) la web, la app móvil y el call center.

Ver explicación completa en [01 — Cómo funciona un chatbot](./01-como-funciona-chatbot.md#tool-use-cómo-el-chatbot-consulta-datos-reales).

### `weather-poller` — task Fargate de operaciones

Task Fargate `desired_count=1` (sin ALB), también en subnet privada. Loop continuo: por cada vuelo activo de la ventana de 48h (Query a la GSI `FlightsByDate`), consulta el **pronóstico de WeatherAPI.com** (`GET /forecast.json?q=iata:<IATA>&dt=<fecha>&hour=<hora_salida>`) para el aeropuerto de origen a la hora de salida del vuelo. Si el viento (`wind_kph > 90`) o la visibilidad (`vis_km*1000 < 550 m`) superan los umbrales, escribe la transición a `estado_vuelo=CANCELADO` en el master row del vuelo en `business`. Sale por NAT a WeatherAPI. A partir de ahí el flujo proactivo se dispara por DynamoDB Stream (ver `stream-emitter` más abajo).

> La cancelación es **idempotente** (`UpdateItem` con `ConditionExpression estado_vuelo <> CANCELADO`). No usa pronóstico fuera del horizonte del plan free de WeatherAPI (3 días), que cubre de sobra la ventana de 48h.

---

## Lambda — Funciones serverless

Lambda es el servicio de cómputo de los flujos asíncronos y de orquestación. Cada función Lambda:
- Se ejecuta en respuesta a un trigger (SQS, Cognito, SNS, Step Functions, DynamoDB Stream)
- Corre de 0 a N instancias en paralelo según la demanda
- Se cobra por invocación y por milisegundos de ejecución
- No requiere provisionar servidores ni administrar infraestructura

> El core del chatbot (`chat-handler`) **ya no es una Lambda** — es el servicio Fargate documentado arriba. Las Lambdas de abajo cubren el Saga de pago/refund, las notificaciones y los consumers de stream.

### Las Lambdas del proyecto

> Nota TP4 (event-driven proactive notifications): el flujo de cancelaciones se dispara por DynamoDB Stream. La Lambda `stream-emitter` consume el stream de `business` y publica al SNS central (`flight_cancelled`) cuando detecta una transición a `estado_vuelo=CANCELADO` en un master row FLIGHT#.

| Nombre | Trigger | Función |
|---|---|---|
| `payment-reserve-flight` | Step Functions (estado ReserveFlight) | Verifica disponibilidad y bloquea asientos en DynamoDB (decremento atómico con ConditionExpression) |
| `payment-reserve-booking` | Step Functions (estado ReserveBooking) | Crea la reserva en DynamoDB con estado PENDIENTE |
| `payment-collect` | Step Functions (estado CollectPayment) | Procesa el cobro (mock; en producción llama al gateway de pagos) |
| `payment-confirm` | Step Functions (estado ConfirmBooking) | Actualiza la reserva a CONFIRMADA; publica evento para analytics |
| `payment-refund` | Step Functions (compensación) | Revierte el cobro si ConfirmBooking falla |
| `payment-cancel` | Step Functions (compensación) | Cancela la reserva si fue creada |
| `payment-release-flight` | Step Functions (compensación) | Libera los asientos bloqueados si ReserveFlight se ejecutó |
| `boarding-pass-async` | SNS `events` (filtro `event_type=booking_confirmed`), publicado por la Saga al confirmar | Genera el boarding pass, lo sube a S3 `boarding-passes` y graba `bp_url` en el PNR. Fire-and-forget: si falla, no afecta la reserva confirmada |
| `notification` | SNS `events` (filtro `event_type` ∈ {`booking_confirmed`, `booking_failed`}), publicado por la Saga | Envía confirmación al usuario (éxito o fracaso del pago) vía SNS `notifications` |
| `business-analytics-emitter` | **DynamoDB Stream** de `business` (filtro PNR#/FLIGHT#/CLAIM#) | CDC hacia el data lake: clasifica la entidad, deriva la transición Old→New, redacta PII y hace `PutRecord` al Firehose correspondiente. Reemplaza al `analytics-processor` del diseño viejo — Firehose batchea nativo, sin Lambda de transformación |
| `human-handoff-processor` | SQS `human-handoff` (alimentada por el SNS central, filtro `handoff_requested`, que publica `chat-handler`) | Simula el POST al sistema del call center y actualiza el ticket HANDOFF# en `conversations` a status=ACK |
| `proactive-notifications` | SNS `events` (filtro `event_type=flight_cancelled`, SNS→Lambda directo) | Ante cancelación de vuelo, hace Query al GSI ReservationsByFlight para encontrar todos los PNRs afectados y publica un email por usuario |
| `stream-emitter` | **DynamoDB Stream** de `business` con filter_criteria (eventName=MODIFY, NewImage.estado_vuelo=CANCELADO) | Detecta transición a CANCELADO en master row FLIGHT#, publica `flight_cancelled` al SNS central. Patrón CDC: el evento se deriva del cambio comprometido, evita el dual-write del poller |
| `refund-trigger` | SNS `events` (filtro `event_type=flight_cancelled`, SNS→Lambda directo) | Arranca la refund Saga (StartExecution con name=flight_id para idempotencia) |
| `auth-callback` | API Gateway GET /callback (bridge HTTPS del workaround) | Intercambia authorization code por tokens JWT y redirige al frontend |
| `cognito-trigger` | Cognito post-registration | Asigna grupo `users` al usuario nuevo |

### Runtime y configuración

Todas las Lambdas usan **Python 3.12**. El timeout configurable es de 30 segundos por defecto (variable `lambda_timeout`), con algunas excepciones explícitas en código: `business-analytics-emitter` usa 60s para tolerar batches grandes. (El `chat-handler` ya no es Lambda — corre en Fargate sin límite de timeout de Lambda.)

### Las Lambdas de negocio corren en la VPC

Las **9 Lambdas de negocio** (payment Saga, refund, notification, stream-emitter, business-analytics-emitter, human-handoff, proactive-notifications, boarding-pass-async, refund-trigger) se configuran con `vpc_config` apuntando a las subnets **`private-lambda`** — están **dentro de la VPC**, igual que Fargate. Alcanzan los servicios AWS por los **VPC endpoints** (DynamoDB/S3 por Gateway Endpoint; SNS/SQS/Secrets/Step Functions/Firehose por Interface Endpoint), sin salir por NAT. Esto completa la respuesta al feedback de Faustino: ya no hay Lambdas sueltas fuera de la VPC.

`auth-callback` y `cognito-trigger` quedan **fuera de la VPC** — solo tocan Cognito/DynamoDB regional.

---

## API Gateway

Queda **una sola instancia de API Gateway**: la del flujo de auth (callback/logout). El chatbot **ya no entra por API Gateway** — entra por el **ALB** que enruta a Fargate (ver sección ECS Fargate). El API Gateway del chatbot del diseño viejo fue reemplazado por el ALB.

### API de auth (callback) — bridge del workaround Cognito

`jetsmart-prod-auth-api` (`modules/auth`):
- Maneja: `GET /callback` y `GET /logout`.
- Invoca exclusivamente la Lambda `auth-callback`.
- `/callback` es el redirect URI registrado en el Cognito App Client.
- `authorization = "NONE"` porque Cognito redirige con `?code=...` en query string (sin Authorization header). Es un bridge HTTPS: Cognito exige HTTPS para callback/logout, pero el frontend está en S3 HTTP — la Lambda hace el 302 final al frontend. Documentado en `teoria/notas-de-clase/workaround-cognito.md`.
- Throttling: 5 req/s sostenido, 10 burst (más conservador que el chatbot porque el flujo de auth es raro por usuario).

### Por qué el chatbot entra por ALB y no por API Gateway

El core del chatbot pasó a ser un servicio web nativo en Fargate (contenedor de larga vida), no funciones invocadas por evento. Un **ALB** es el balanceador natural para ese modelo: distribuye tráfico HTTP a las tasks por IP (`target_type = "ip"`, awsvpc), corre health checks contra `/health`, e integra con el Auto Scaling del servicio. API Gateway tiene sentido para Lambda invocada por evento — no para un pool de contenedores detrás de un load balancer.

La validación del JWT, que en el diseño serverless intermedio se pensó delegar a un Cognito Authorizer de API Gateway, ahora la hace el contenedor in-app (ver "Validación de JWT in-app" arriba).

---

## SNS (Simple Notification Service)

SNS es un servicio de pub/sub: un publicador manda un mensaje al topic y todos los suscriptores lo reciben. Permite fan-out (un evento → muchos consumidores) sin que el publicador conozca a los consumidores.

### Los SNS topics del proyecto

**Dos topics**, cada uno con un dominio claro (`messaging.tf`).

| Topic | Publicado por | Suscriptores |
|---|---|---|
| `events` | `chat-handler` (mensajes de chat), Step Functions Saga (`booking_confirmed` / `booking_failed`) y `stream-emitter` (`flight_cancelled`) | Firehose `interaction_events` (→ data lake), SQS `human-handoff` (`handoff_requested`), y Lambdas suscritas por filtro (`notification`, `boarding-pass-async`, `proactive-notifications`, `refund-trigger`) |
| `notifications` | Lambdas (`notification`, `human-handoff-processor`, `proactive-notifications`, etc.) y CloudWatch Alarms | Endpoints email suscritos manualmente con `aws sns subscribe` (el topic acepta también SMS u otros protocolos sin cambios de código si se quisiera sumarlos) |

### Por qué dos topics y no uno solo

Cada topic representa un **dominio** distinto y tiene consumidores diferentes:

- `events` → backbone de eventos de dominio (chat, transacciones de booking, cancelaciones de vuelo). Es el bus central: cada subscriber filtra por `event_type` (filter policy) y recibe solo lo que le importa. Las cancelaciones llegan acá como `event_type=flight_cancelled`, publicadas por la Lambda `stream-emitter` triggered por DynamoDB Stream: el `weather-poller` (Fargate) modifica el ítem en la tabla y el resto del flujo se dispara automáticamente.
- `notifications` → comunicación saliente al usuario y al equipo de operaciones (alarmas).

La separación es por **dirección y propósito**, no por tipo de evento de dominio: `events` es el bus interno de dominio; `notifications` es el canal de salida (email/alarmas). Por eso el fan-out de dominio se resuelve con un único bus + filter policies, no multiplicando topics.

### Por qué SNS y no Step Functions

En la arquitectura original del TP3 (TALO — Trigger-and-Lambda-Orchestration) había topics SNS encadenando los pasos del flujo de pago, generando una orquestación distribuida. Esa responsabilidad la asumió **Step Functions** en TP4: el state machine invoca cada Lambda en orden y SNS quedó únicamente para los tres roles de arriba (analytics, notifications, ingest de operaciones).

### Fan-out con SNS — caso de uso

`notifications` recibe eventos de **CloudWatch Alarms** (`business-analytics-emitter-errors` y las DLQ depth alarms) y de Lambdas que confirman acciones al usuario. Cualquier endpoint suscrito (email del equipo de ops, eventualmente Slack) recibe todo. Sumar un consumer nuevo (Telegram, PagerDuty) es una sola subscripción — no requiere tocar las Lambdas ni las alarms.

---

## SQS (Simple Queue Service)

SQS es una cola de mensajes. El productor pone mensajes en la cola y el consumidor los lee cuando puede. Patrón fundamental para desacoplar productores rápidos de consumidores que pueden ser lentos o fallar.

### Las queues del proyecto

Una sola cola funcional (`human-handoff`) con su DLQ, más dos DLQ standalone escritas por las Step Functions. La única cola con consumidor es `human-handoff`; el resto del fan-out es SNS→Lambda directo (ver SNS). **4 recursos SQS en total** (`messaging.tf`).

| Queue principal | Productor | Consumidor | DLQ |
|---|---|---|---|
| `human-handoff` | SNS `events` (filtro `handoff_requested`, publicado por `chat-handler` cuando el LLM invoca tool `escalate_to_human`) | `human-handoff-processor` → call center mock | `human-handoff-dlq` |

| DLQ standalone | Fuente | Propósito |
|---|---|---|
| `booking-failed-dlq` | Step Functions estado `BookingDLQ` (SDK integration) | Retención de 14 días de reservas fallidas para investigación manual |
| `refund-failures-dlq` | Refund Saga, estado `RefundPNRFailed` (SDK integration) | Retención de 14 días de PNRs cuyo reembolso falló — revisión manual (es plata) |

### Configuración común

Todas las queues principales usan:
- `message_retention_seconds = 86400` (1 día — corto porque la DLQ retiene 14 días si la cola principal falla)
- `visibility_timeout_seconds = 360` — 12× el `lambda_timeout` default (30s) que usan los consumers de SQS, en línea con la recomendación de AWS de ≥6× el timeout para evitar duplicados durante retries
- `receive_wait_time_seconds = 20` — **long polling**: el consumer espera hasta 20s a que llegue un mensaje en vez de pollear constantemente. Reduce requests vacíos y costo
- `redrive_policy` con `maxReceiveCount = 3` — tras 3 fallas el mensaje pasa a la DLQ

Todas las DLQs usan `message_retention_seconds = 1209600` (14 días) para dar tiempo a investigar.

### Por qué SQS y no invocación directa

La cola `human-handoff` desacopla el único punto donde **el productor no debe esperar a un downstream que no es elástico**:

```
Sin SQS:
chat-handler → human-handoff-processor → call center
(si el call center está lento, el usuario del chat espera)

Con SQS:
chat-handler → SNS → SQS → human-handoff-processor → call center
(chat-handler termina inmediato; la derivación se procesa después y, si el
 call center está caído, el pedido espera en la cola sin perderse)
```

El resto de los flujos asíncronos **no usa SQS**: son SNS→Lambda directo (`boarding-pass-async`, `notification`, `proactive-notifications`, `refund-trigger`). El downstream es Lambda (elástico) o el resultado es re-derivable desde DynamoDB, así que una cola amortiguadora no aporta — la durabilidad la dan el retry de SNS y una alarma de Lambda Errors. La única cola se justifica por el call center, que es el único downstream no elástico. El analytics histórico tampoco usa SQS: SNS `events` entrega directo a un Firehose (`interaction_events`) que batchea al data lake.

### Por qué el flujo de pago NO usa SQS entre pasos

Step Functions orquesta las Lambdas de pago directamente y maneja retries + compensaciones en la ASL. SQS entre pasos sumaría latencia sin aportar — el flujo es sincrónico desde la perspectiva del usuario (espera la confirmación del pago).

El post-procesado tampoco va a SQS: al confirmar, la Saga publica `booking_confirmed` al SNS `events` y `boarding-pass-async` (suscripto por filtro) genera el BP en background. Es best-effort y no debe bloquear `BookingConfirmed` — desacoplado por SNS, no por una cola.

---

## Step Functions

Step Functions es el orquestador del flujo de pago. Define una máquina de estados (state machine) en ASL (Amazon States Language) que coordina las Lambdas de pago en secuencia, con manejo de errores y transacciones compensatorias (patrón Saga).

### El patrón Saga

Un pago involucra múltiples pasos que deben ejecutarse todos o ninguno. Si el paso 3 falla, los pasos 1 y 2 deben deshacerse. Ese es el problema que resuelve el patrón Saga.

```
Flujo exitoso:
  ReserveFlight → ReserveBooking → CollectPayment → ConfirmBooking
                                                          ↓
                                          PublishBookingConfirmed (sns:publish → events)
                                                          ↓
                                                  BookingConfirmed ✓
  (el fan-out post-booking —notification + boarding-pass + analytics— lo hacen
   las suscripciones al topic `events` por filtro, no un estado del state machine)

Flujo de error (compensaciones):
  Si cualquier paso falla →
  RefundPayment → CancelBooking → ReleaseFlight → PublishBookingFailed (sns:publish → events) → BookingDLQ → BookingFailed ✗
```

Cada compensación deshace el paso correspondiente:
- `ReleaseFlight` devuelve los asientos bloqueados por `ReserveFlight`
- `CancelBooking` marca como CANCELADA la reserva creada por `ReserveBooking`
- `RefundPayment` revierte el cobro hecho por `CollectPayment`

### Por qué Step Functions y no encadenamiento de SNS/SQS

El enfoque anterior (TALO: SNS→SQS→Lambda→SNS→...) requería que cada Lambda supiera a qué SNS topic publicar el resultado. La lógica de orquestación quedaba distribuida entre todas las funciones.

Con Step Functions, esa lógica vive en un único lugar: el state machine. Las Lambdas solo hacen su trabajo y devuelven el estado actualizado.

```
TALO (antes):
  payment-validate → publica SNS → payment-reserve lee SQS → publica SNS → ...
  (orquestación distribuida entre todas las Lambdas)

Step Functions (ahora):
  State machine invoca reserve-flight → recibe resultado → invoca reserve-booking → ...
  (orquestación centralizada en la ASL)
```

La compensación automática ante errores es la ventaja más importante: en TALO, implementar rollback requería código complejo en cada Lambda. Con Step Functions, se define en la ASL con `Catch` y el estado de compensación correspondiente.

### PublishBookingConfirmed: el post-procesado se hace por evento, no con un Parallel

Cuando el pago es exitoso, tras `ConfirmBooking` el state machine ejecuta un único estado `PublishBookingConfirmed` que publica `booking_confirmed` al SNS `events` con la integración SDK nativa (`arn:aws:states:::sns:publish`; el `event_type` va como `MessageAttribute`). Después avanza a `BookingConfirmed` (`Succeed`).

El fan-out post-booking **no vive dentro del state machine** (no hay estado `Parallel` ni una cola): lo resuelven las suscripciones al topic `events` por filter policy —`notification` (filtro `booking_confirmed`/`booking_failed`), `boarding-pass-async` (filtro `booking_confirmed`) y el Firehose de analytics—. Esto desacopla la orquestación del Saga del envío de notificaciones y la generación del BP.

`PublishBookingConfirmed` es best-effort: un `Catch` con `Next: BookingConfirmed` envuelve el publish, así que si el publish falla la reserva queda confirmada igual.

Cambio respecto al TP3: antes el boarding pass se generaba dentro del path sincrónico del Saga (Lambda directa). Si la generación fallaba, el Saga compensaba toda la reserva — un BP roto cancelaba el vuelo. En TP4 se sacó del path crítico: la reserva queda confirmada, se publica el evento y el BP es eventualmente consistente vía suscripción SNS→Lambda.

### BookingDLQ: SDK integration

El estado `BookingDLQ` no invoca una Lambda — escribe directamente en SQS usando la integración SDK nativa de Step Functions (`Resource: "arn:aws:states:::sqs:sendMessage"`). Es más eficiente y evita una Lambda cuya única función sería hacer `sqs.send_message()`.

---

## DynamoDB

Base de datos NoSQL administrada. Estructura de tablas para este proyecto:

Ver el diseño completo en [07 — Capa de datos](./07-data-layer.md).

### Por qué DynamoDB para el chat

- **Acceso interno por VPC endpoint**: alcanzable por el Gateway Endpoint de DynamoDB desde la VPC (tráfico interno, sin salir por NAT).
- **Latencia baja**: operaciones de GetItem/PutItem en < 5ms — no frena al usuario.
- **Escala automática**: on-demand billing, sin capacidad que administrar.

---

## Data Lake: S3 + Glue + Athena

La capa de analytics histórico **reemplaza al RDS PostgreSQL del TP3** con un patrón data lake estándar.

### S3 — almacenamiento crudo

Bucket `jetsmart-prod-<account-id>-analytics` con encriptación SSE-S3 y bloqueo de acceso público. Estructura de keys particionada Hive-style para que Athena haga *partition pruning* en sus queries:

```
s3://jetsmart-prod-<account-id>-analytics/
└── lake/
    └── reservation_events/        ← una carpeta por tabla tipada
        └── dt=2026-06-13/
            └── hh=14/
                └── <uuid>.gz      ← objeto GZIP batcheado por Firehose (5 MB / 60 s)
```

Una carpeta `lake/<entidad>/` por cada delivery stream (reservation_events, flight_events, claim_events, interaction_events). Cada `.gz` contiene JSON Lines (una línea por evento). Lifecycle policy: archivar a Glacier después de 90 días, expirar los resultados de Athena después de 14 días.

### Glue Data Catalog — tablas tipadas estáticas

`aws_glue_catalog_database.analytics` es el catálogo de metadata. **No hay Glue Crawler**: el schema no se descubre, está declarado en Terraform. La database contiene **4 tablas tipadas estáticas** (`aws_glue_catalog_table.lake`, una por entidad), cada una con sus columnas de primer nivel y **partition projection** sobre `dt`/`hh` (las particiones se proyectan en consulta, sin registrarlas):

| Tabla | Columnas (primer nivel) |
|---|---|
| `reservation_events` | `event_id`, `pnr`, `event_type`, `old_status`, `new_status`, `total` (double), `pax_count` (int), `user_id`, `vuelo`, `fecha`, `event_ts` |
| `flight_events` | `event_id`, `vuelo`, `origen`, `destino`, `fecha`, `hora_salida`, `old_estado`, `new_estado`, `event_ts` |
| `claim_events` | `event_id`, `claim_id`, `event_type`, `old_status`, `new_status`, `tipo`, `pnr`, `user_id`, `event_ts` |
| `interaction_events` | `event_type`, `user_id`, `event_ts` |

La ingesta la hacen los **4 Kinesis Data Firehose** (ver `firehose.tf`): `reservation_events` / `flight_events` / `claim_events` los alimenta la Lambda `business-analytics-emitter` por CDC (`PutRecord`); `interaction_events` lo alimenta la suscripción SNS del topic central. Como las tablas son estáticas y las particiones se proyectan, los datos nuevos quedan consultables apenas Firehose los escribe (buffer 60 s) — sin crawler ni descubrimiento de schema.

El Glue Catalog y el bucket S3 de analytics los maneja el LabRole de Academy.

### Athena — consultas SQL

`aws_athena_workgroup.analytics` es el workgroup del equipo de business analytics. Sus resultados se escriben a `s3://...-analytics/athena-results/` (encriptados con SSE-S3).

El equipo se conecta con cliente SQL externo (DBeaver / DataGrip) usando el driver JDBC de Athena. Pueden tirar queries en Presto/Trino SQL:

```sql
SELECT event_type, COUNT(*)
FROM jetsmart_prod_analytics.reservation_events
WHERE dt >= '2026-06-01'
GROUP BY 1;
```

Sin VPC, sin RDS Proxy, sin pool de conexiones — Athena es serverless puro, paga por TB escaneado (~5 USD/TB).

---

## Cognito

### User Pool

El User Pool es el directorio de usuarios. Gestiona:
- Registro (email + contraseña)
- Login
- Recuperación de contraseña
- Tokens JWT (Access Token, ID Token, Refresh Token)

Usa la **Cognito Hosted UI** — una página de login que AWS genera automáticamente. No hay que construir una pantalla de login propia.

### Grupos de Cognito

| Grupo | Quién | Qué accede |
|---|---|---|
| `users` | Cualquier usuario registrado | El chatbot, sus reservas, check-in |

> En TP3 había un grupo `admins` para un dashboard de analytics. En TP4 ese dashboard se eliminó — el equipo de business analytics consume Athena directamente con cliente SQL (mejor patrón). El grupo `users` es el único activo.

El grupo se incluye en el ID Token del usuario. El servicio `chat-handler` (Fargate) lo lee desde los claims del JWT validado in-app si lo necesita.

---

## Secrets Manager

Guarda un único secreto:

| Secreto | Contenido |
|---|---|
| `jetsmart-prod/anthropic-api-key` | La API key de Anthropic |

> El secreto `jetsmart-prod/rds-credentials` del TP3 se eliminó junto con RDS.

El servicio `chat-handler` (Fargate) lee la API key de Anthropic al iniciar el contenedor (`chat_core.py`, init eager en el import) y la cachea en memoria del proceso.

El secreto está encriptado con AWS managed KMS keys.

---

## CloudWatch

Recibe los logs de las Lambdas (un log group por Lambda) y de los servicios Fargate (driver `awslogs`):

| Log group | Workload |
|---|---|
| `/ecs/jetsmart-prod-chat-handler` | chat-handler (Fargate) |
| `/ecs/jetsmart-prod-weather-poller` | weather-poller (Fargate) |
| `/aws/lambda/jetsmart-prod-payment-reserve-flight` | payment-reserve-flight |
| `/aws/lambda/jetsmart-prod-payment-reserve-booking` | payment-reserve-booking |
| `/aws/lambda/jetsmart-prod-payment-collect` | payment-collect |
| `/aws/lambda/jetsmart-prod-payment-confirm` | payment-confirm |
| `/aws/lambda/jetsmart-prod-payment-refund` | payment-refund |
| `/aws/lambda/jetsmart-prod-payment-cancel` | payment-cancel |
| `/aws/lambda/jetsmart-prod-payment-release-flight` | payment-release-flight |
| `/aws/lambda/jetsmart-prod-boarding-pass-async` | boarding-pass-async |
| `/aws/lambda/jetsmart-prod-notification` | notification |
| `/aws/lambda/jetsmart-prod-business-analytics-emitter` | business-analytics-emitter |
| `/aws/lambda/jetsmart-prod-stream-emitter` | stream-emitter |
| `/aws/lambda/jetsmart-prod-refund-trigger` | refund-trigger |
| `/aws/lambda/jetsmart-prod-human-handoff-processor` | human-handoff-processor |
| `/aws/lambda/jetsmart-prod-proactive-notifications` | proactive-notifications |
| `/aws/lambda/jetsmart-prod-auth-callback` | auth-callback |
| `/aws/lambda/jetsmart-prod-cognito-trigger` | cognito-trigger |
| `/aws/states/jetsmart-prod-booking-workflow` | Step Functions state machine |

Retención configurada en 30 días para todos los log groups.

### Alarms

Alarmas conectadas al SNS topic `notifications`. Una de Lambda Errors sobre el emitter de analytics, tres de profundidad de DLQ (una por cada DLQ real), y una de Lambda Errors por cada consumer SNS→Lambda directo sin DLQ (`notification`, `boarding-pass-async`, `proactive-notifications`, `refund-trigger`):

| Alarma | Métrica | Por qué importa |
|---|---|---|
| `business-analytics-emitter-errors` | `AWS/Lambda Errors > 0` sobre `business-analytics-emitter` | Si falla, no emitimos eventos CDC al data lake |
| `human-handoff-dlq-messages-visible` | `AWS/SQS ApproximateNumberOfMessagesVisible > 0` | Hay derivaciones a humano que no se procesaron |
| `booking-failed-dlq-messages-visible` | idem | Hay reservas fallidas pendientes de revisión manual |
| `refund-failures-dlq-messages-visible` | idem | Hay PNRs sin reembolsar (path de plata no recuperable) |
| `<consumer>-errors` (×4) | `AWS/Lambda Errors > 0` sobre cada consumer SNS→Lambda directo | Visibilidad del fallo sin DLQ: `notification`, `boarding-pass-async`, `proactive-notifications`, `refund-trigger` |

---

## S3

Tres buckets con propósitos distintos:

### `jetsmart-prod-<account-id>-frontend`
- Archivos estáticos del sitio web (HTML, CSS, JS)
- Static website hosting habilitado
- Público (accesible desde internet) con `aws_s3_bucket_policy.frontend` que permite `s3:GetObject` a `Principal: "*"`
- Sin versionado (los archivos se sobreescriben en cada deploy)
- Sin encriptación adicional (contenido público)

### `jetsmart-prod-<account-id>-boarding-passes`
- Boarding passes generados por la Lambda `boarding-pass-async` — un archivo de texto por PNR
- Acceso desde el chatbot vía **presigned URLs temporales** (15 min de validez)
- Privado con `public_access_block` activo en las cuatro dimensiones
- **Versionado ON** — protege contra `DELETE` accidental. Los BP son write-once por PNR, no hay overwrites en operación normal
- Lifecycle:
  - Versión current: expira a los **90 días**
  - Versiones non-current: expiran a los **30 días** (cleanup de huérfanas tras delete accidental)
- Encriptación SSE-S3 (AES-256)

> Renombrado desde `assets` en TP4. Cuando el system prompt se movió a Lambda Layer (ver sección "Lambda Layers"), el bucket pasó a contener únicamente boarding passes y se renombró para reflejarlo.

### `jetsmart-prod-<account-id>-analytics`
- Eventos del data lake en JSON Lines (GZIP) particionado Hive-style: `lake/<entidad>/dt=YYYY-MM-DD/hh=HH/<uuid>.gz`
- Resultados de queries Athena en `athena-results/`
- Privado con `public_access_block` activo
- Lifecycle: archivar particiones a Glacier después de 90 días; expirar resultados Athena a los 14 días
- Encriptación SSE-S3

## Dependencias del chat-handler: imagen Docker (no Lambda Layers)

Como `chat-handler` ahora es un **contenedor Fargate**, sus dependencias y su system prompt van **dentro de la imagen Docker** (`app/chat-handler/Dockerfile`), no en Lambda Layers:

- **SDK `anthropic` + FastAPI/uvicorn + PyJWT/requests** — se instalan con `pip install -r requirements.txt` durante el `docker build`.
- **System prompt** — se hornea en la imagen con `COPY terraform/infra/templates/system_prompt.tpl /opt/system_prompt.txt`. `chat_core.py` lo lee de `SYSTEM_PROMPT_PATH = /opt/system_prompt.txt` al iniciar.

La imagen se publica en ECR y Fargate la pullea. Versionado por tag de imagen (`var.image_tag`): cada build es un artefacto inmutable, rollback = re-deploy del tag anterior.

> **Vestigios del diseño viejo:** `layers.tf` todavía define dos `aws_lambda_layer_version` (`anthropic` y `system-prompt`) del diseño serverless. **Ya no los consume nadie** — el chat-handler era la única Lambda que los montaba y dejó de ser Lambda. Son candidatos a borrar. El layer `psycopg2` del TP3 (driver PostgreSQL) ya se eliminó junto con RDS. La validación JWT manual con `python-jose` se mantiene conceptualmente, pero ahora corre **in-app en el contenedor** (PyJWT contra el JWKS, RS256) — no en un Cognito Authorizer.
