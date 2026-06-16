# 05 — Componentes en detalle

## Lambda — Funciones serverless

Lambda es el servicio de cómputo principal de este proyecto. Cada función Lambda:
- Se ejecuta en respuesta a un trigger (API Gateway, SQS, Cognito, SNS)
- Corre de 0 a N instancias en paralelo según la demanda
- Se cobra por invocación y por milisegundos de ejecución
- No requiere provisionar servidores ni administrar infraestructura

### Las 16 Lambdas del proyecto

| Nombre | Trigger | Función |
|---|---|---|
| `chat-handler` | API Gateway (todos los paths, **detrás de Cognito Authorizer**) | Punto de entrada principal: chat con tool use, historial, reservas del usuario, inicio de pago. Lee claims ya validados de `event.requestContext.authorizer.claims` (sin validación JWT manual) |
| `payment-reserve-flight` | Step Functions (estado ReserveFlight) | Verifica disponibilidad y bloquea asientos en DynamoDB (decremento atómico con ConditionExpression) |
| `payment-reserve-booking` | Step Functions (estado ReserveBooking) | Crea la reserva en DynamoDB con estado PENDIENTE |
| `payment-collect` | Step Functions (estado CollectPayment) | Procesa el cobro (mock; en producción llama al gateway de pagos) |
| `payment-confirm` | Step Functions (estado ConfirmBooking) | Actualiza la reserva a CONFIRMADA; publica evento para analytics |
| `payment-refund` | Step Functions (compensación) | Revierte el cobro si ConfirmBooking falla |
| `payment-cancel` | Step Functions (compensación) | Cancela la reserva si fue creada |
| `payment-release-flight` | Step Functions (compensación) | Libera los asientos bloqueados si ReserveFlight se ejecutó |
| `boarding-pass-async` | SQS `boarding-pass-generation` (publicado por Step Functions PostBookingActions) | Consume mensajes encolados, genera el boarding pass, lo sube a S3 `boarding-passes` y graba `bp_url` en el PNR. Fire-and-forget: si falla, no afecta la reserva confirmada |
| `notification` | Step Functions (PostBookingActions + error path) | Envía confirmación al usuario (éxito o fracaso del pago) vía SNS `notifications` |
| `analytics-processor` | SQS `analytics` | Escribe eventos crudos en S3 `analytics` como JSON Lines particionado por fecha (TP4: ya no escribe a RDS) |
| `human-handoff-processor` | SQS `human-handoff` (publicado por chat-handler cuando el LLM invoca la tool `escalate_to_human`) | Simula el POST al sistema del call center y actualiza el ticket HANDOFF# en `conversations` a status=ACK |
| `proactive-notifications` | SQS `proactive-notifications` (suscrita a SNS `flight-events` publicado por ops) | Ante cancelación de vuelo, hace Query a GSI2 para encontrar todos los PNRs afectados y publica un email por usuario |
| `auth-callback` | API Gateway GET /callback (bridge HTTPS del workaround) | Intercambia authorization code por tokens JWT y redirige al frontend |
| `cognito-trigger` | Cognito post-registration | Asigna grupo `users` al usuario nuevo |
| `backup-dynamodb` | EventBridge cron diario 03:00 UTC | Dispara `dynamodb:ExportTableToPointInTime` sobre `business`; el export queda en S3 `backups`. Mecanismo complementario a PITR (35d continuos), cubre retención AFIP de 10 años |

### Tool use en chat-handler

`chat-handler` no llama al LLM una sola vez — implementa un **bucle de tool use** de hasta 5 rondas. Claude puede pausar su respuesta y pedir que la Lambda ejecute funciones reales para obtener datos antes de responder:

- `search_flights` — consulta disponibilidad de vuelos en DynamoDB (simula el PSS real de JetSmart)
- `get_reservation` — consulta el estado de una reserva del usuario

En producción, estas funciones llamarían a la API interna de JetSmart en lugar de DynamoDB. La interfaz hacia Claude es idéntica en ambos casos.

Ver explicación completa en [01 — Cómo funciona un chatbot](./01-como-funciona-chatbot.md#tool-use-cómo-el-chatbot-consulta-datos-reales).

### Runtime y configuración

Todas las Lambdas usan **Python 3.12**. El timeout configurable es de 30 segundos por defecto (variable `lambda_timeout`).

### Todas las Lambdas regionales (sin VPC)

En el TP4 **ninguna Lambda usa VPC**. La justificación está en `docs/03-networking.md`: sin recursos persistentes (RDS, EC2), la VPC era over-engineering. Las Lambdas acceden a DynamoDB, SNS, SQS, Step Functions, S3, Secrets Manager directamente por endpoints regionales de AWS — el tráfico va por la red interna de AWS, encriptado con TLS.

Ganancia concreta: cold start ~200ms en lugar de 500ms-2s.

---

## API Gateway

API Gateway es el punto de entrada HTTP del sistema. Lambda no tiene URL propia — API Gateway recibe las requests HTTPS del navegador y las traduce en invocaciones de Lambda.

### Dos instancias de API Gateway

**1. API principal (chatbot) — protegida por Cognito Authorizer**
- Maneja: `POST /api/chat`, `GET /api/reservations`, `POST /api/payment`.
- Usa un recurso `{proxy+}` que captura todos los paths y los enruta a `chat-handler`.
- Método `ANY /{proxy+}` con `authorization = "COGNITO_USER_POOLS"` y un `aws_api_gateway_authorizer` que valida el JWT contra el User Pool.
- Método `OPTIONS /{proxy+}` con `authorization = "NONE"` y `MOCK` integration para servir el preflight CORS sin invocar Lambda.
- Método `ANY /` con `authorization = "NONE"` para exponer `/health` sin auth.

**2. API de auth (callback) — bridge del workaround Cognito**
- Maneja: `GET /callback`.
- Invoca exclusivamente la Lambda `auth-callback`.
- Es el redirect URI registrado en el Cognito App Client.
- `authorization = "NONE"` porque Cognito redirige con `?code=...` en query string (sin Authorization header). Está documentado en `teoria/notas-de-clase/workaround-cognito.md`.

### Cognito Authorizer — cambio respecto al TP3

En el TP3, la Lambda `chat-handler` descargaba el JWKS de Cognito, parseaba el JWT manualmente con `python-jose` y validaba firma/issuer/token_use por cuenta propia. Eran ~50 líneas de código aplicativo manejando un problema de seguridad estándar.

En el TP4, API Gateway hace toda esa validación con un recurso de Terraform:

```hcl
resource "aws_api_gateway_authorizer" "cognito" {
  name            = "${var.name_prefix}-cognito-authorizer"
  type            = "COGNITO_USER_POOLS"
  rest_api_id     = aws_api_gateway_rest_api.chatbot.id
  provider_arns   = [var.cognito_user_pool_arn]
  identity_source = "method.request.header.Authorization"
}
```

Los claims llegan a la Lambda ya validados en `event.requestContext.authorizer.claims`. Si el token es inválido, API GW devuelve `401` antes de invocar Lambda — sin gastar invocación ni cold start. El layer `python-jose` también desapareció.

### Por qué API Gateway y no una URL de Lambda

Lambda Function URLs son más simples pero no soportan Cognito Authorizer nativo (tendría que volver a validar manualmente). API Gateway permite:
- Cognito Authorizer plug-and-play.
- Throttling configurable (10 req/s sostenido, 20 burst).
- CORS preflight con MOCK integration.
- Stages independientes (dev / staging / prod).

---

## SNS (Simple Notification Service)

SNS es un servicio de pub/sub: un publicador manda un mensaje al topic y todos los suscriptores lo reciben. Permite fan-out (un evento → muchos consumidores) sin que el publicador conozca a los consumidores.

### Los SNS topics del proyecto

**Tres topics**, cada uno con un dominio claro (`messaging.tf`).

| Topic | Publicado por | Suscriptores |
|---|---|---|
| `events` | `chat-handler` (mensajes de chat) y `payment-confirm` (compras completadas) | SQS `analytics` |
| `notifications` | Lambdas (`notification`, `human-handoff-processor`, `proactive-notifications`, etc.) y CloudWatch Alarms | Endpoints email/SMS suscritos manualmente con `aws sns subscribe` |
| `flight-events` | Script ops `scripts/cancel_flight.py` cuando un vuelo cambia de estado (cancelado, demorado, gate change) | SQS `proactive-notifications` |

### Por qué tres topics y no uno solo

Cada topic representa un **dominio de eventos** distinto y tiene consumidores diferentes:

- `events` → analytics interno (data lake)
- `notifications` → comunicación saliente al usuario y al equipo de operaciones (alarmas)
- `flight-events` → ingest desde el sistema externo de operaciones de JetSmart

Unificar todo en un solo topic acoplaría dominios sin necesidad y haría que cada consumer tuviera que filtrar mensajes por `event_type` — antipatrón. Topics separados dejan que cada consumer se suscriba solo a lo que le importa.

### Por qué SNS y no Step Functions

En la arquitectura original del TP3 (TALO — Trigger-and-Lambda-Orchestration) había topics SNS encadenando los pasos del flujo de pago, generando una orquestación distribuida. Esa responsabilidad la asumió **Step Functions** en TP4: el state machine invoca cada Lambda en orden y SNS quedó únicamente para los tres roles de arriba (analytics, notifications, ingest de operaciones).

### Fan-out con SNS — caso de uso

`notifications` recibe eventos de **CloudWatch Alarms** (`analytics-processor-errors` y 3 DLQ depth alarms) y de Lambdas que confirman acciones al usuario. Cualquier endpoint suscrito (email del equipo de ops, eventualmente Slack) recibe todo. Sumar un consumer nuevo (Telegram, PagerDuty) es una sola subscripción — no requiere tocar las Lambdas ni las alarms.

---

## SQS (Simple Queue Service)

SQS es una cola de mensajes. El productor pone mensajes en la cola y el consumidor los lee cuando puede. Patrón fundamental para desacoplar productores rápidos de consumidores que pueden ser lentos o fallar.

### Las queues del proyecto

Cuatro flujos asíncronos, cada uno con su DLQ — más una DLQ standalone para reservas fallidas. **9 recursos SQS en total** (`messaging.tf`).

| Queue principal | Productor | Consumidor | DLQ |
|---|---|---|---|
| `analytics` | SNS `events` (chat-handler + payment-confirm) | `analytics-processor` → S3 data lake | `analytics-dlq` |
| `human-handoff` | `chat-handler` cuando el LLM invoca tool `escalate_to_human` | `human-handoff-processor` → call center mock | `human-handoff-dlq` |
| `proactive-notifications` | SNS `flight-events` (script ops cancela vuelo) | `proactive-notifications` → Query GSI2 + fan-out emails | `proactive-notifications-dlq` |
| `boarding-pass-generation` | Step Functions `PostBookingActions` (vía `arn:aws:states:::sqs:sendMessage`) | `boarding-pass-async` → genera BP + sube a S3 | `boarding-pass-generation-dlq` |

| DLQ standalone | Fuente | Propósito |
|---|---|---|
| `booking-failed-dlq` | Step Functions estado `BookingDLQ` (SDK integration) | Retención de 14 días de reservas fallidas para investigación manual |

### Configuración común

Todas las queues principales usan:
- `message_retention_seconds = 86400` (1 día — corto porque la DLQ retiene 14 días si la cola principal falla)
- `visibility_timeout_seconds = 360` — 6× el timeout de Lambda (60s), recomendación oficial AWS para evitar duplicados durante retries
- `receive_wait_time_seconds = 20` — **long polling**: el consumer espera hasta 20s a que llegue un mensaje en vez de pollear constantemente. Reduce requests vacíos y costo
- `redrive_policy` con `maxReceiveCount = 3` — tras 3 fallas el mensaje pasa a la DLQ

Todas las DLQs usan `message_retention_seconds = 1209600` (14 días) para dar tiempo a investigar.

### Por qué SQS y no invocación directa

Cada cola desacopla un punto donde **el productor no debe esperar al consumidor**:

```
Sin SQS:
chat-handler → analytics-processor → S3
(si S3 está lento, el usuario del chat espera)

Con SQS:
chat-handler → SNS → SQS → analytics-processor → S3
(chat-handler termina inmediato; analytics corre después)
```

Mismo principio para human-handoff (chat no debe esperar al call center), boarding-pass (la reserva ya está confirmada, el BP puede llegar 5s después), proactive-notifications (cancelar un vuelo no debe esperar a que se envíen N emails).

### Por qué el flujo de pago NO usa SQS entre pasos

Step Functions orquesta las Lambdas de pago directamente y maneja retries + compensaciones en la ASL. SQS entre pasos sumaría latencia sin aportar — el flujo es sincrónico desde la perspectiva del usuario (espera la confirmación del pago).

La excepción es el paso post-pago `boarding-pass-generation`: ahí sí se publica a SQS porque la generación del BP es best-effort y no debe bloquear `BookingConfirmed`.

---

## Step Functions

Step Functions es el orquestador del flujo de pago. Define una máquina de estados (state machine) en ASL (Amazon States Language) que coordina las Lambdas de pago en secuencia, con manejo de errores y transacciones compensatorias (patrón Saga).

### El patrón Saga

Un pago involucra múltiples pasos que deben ejecutarse todos o ninguno. Si el paso 3 falla, los pasos 1 y 2 deben deshacerse. Ese es el problema que resuelve el patrón Saga.

```
Flujo exitoso:
  ReserveFlight → ReserveBooking → CollectPayment → ConfirmBooking
                                                          ↓
                                                  PostBookingActions (paralelo)
                                                  ├── Notification
                                                  └── BoardingPass
                                                          ↓
                                                  BookingConfirmed ✓

Flujo de error (compensaciones):
  Si cualquier paso falla →
  RefundPayment → CancelBooking → ReleaseFlight → NotifyBookingFailed → BookingDLQ → BookingFailed ✗
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

### PostBookingActions: estado Parallel

Cuando el pago es exitoso, el estado `Parallel` corre dos ramas en simultáneo:

- **Rama A — `NotifyBookingConfirmed`** invoca la Lambda `notification` directamente (síncrono).
- **Rama B — `EnqueueBoardingPass`** publica el state a la cola SQS `boarding-pass-generation` usando la integración SDK nativa de Step Functions (`arn:aws:states:::sqs:sendMessage`). La Lambda `boarding-pass-async` consume después y genera el BP en background.

Step Functions espera a que ambas ramas terminen antes de avanzar a `BookingConfirmed`, pero un `Catch` envuelve todo el `Parallel` con `Next: BookingConfirmed` para que la reserva quede confirmada aún si una rama falla — el BP fallido se puede regenerar después desde la DLQ.

Cambio respecto al TP3: antes el boarding pass se generaba dentro del path sincrónico del Saga (Lambda directa, no SQS). Si la generación fallaba, el Saga compensaba toda la reserva — un BP roto cancelaba el vuelo. En TP4 se sacó del path crítico: la reserva queda confirmada y el BP es eventualmente consistente.

### BookingDLQ: SDK integration

El estado `BookingDLQ` no invoca una Lambda — escribe directamente en SQS usando la integración SDK nativa de Step Functions (`Resource: "arn:aws:states:::sqs:sendMessage"`). Es más eficiente y evita una Lambda cuya única función sería hacer `sqs.send_message()`.

---

## DynamoDB

Base de datos NoSQL administrada. Estructura de tablas para este proyecto:

Ver el diseño completo en [07 — Capa de datos](./07-data-layer.md).

### Por qué DynamoDB para el chat

- **Sin VPC**: ninguna Lambda usa VPC. DynamoDB es accesible por endpoint regional de AWS.
- **Latencia baja**: operaciones de GetItem/PutItem en < 5ms — no frena al usuario.
- **Escala automática**: on-demand billing, sin capacidad que administrar.

---

## Data Lake: S3 + Glue + Athena

La capa de analytics histórico **reemplaza al RDS PostgreSQL del TP3** con un patrón data lake estándar.

### S3 — almacenamiento crudo

Bucket `jetsmart-prod-<account-id>-analytics` con encriptación SSE-S3 y bloqueo de acceso público. Estructura de keys particionada Hive-style para que Athena haga *partition pruning* en sus queries:

```
s3://jetsmart-prod-<account-id>-analytics/
└── events/
    └── dt=2026-06-13/
        └── hh=14/
            └── <uuid>.jsonl    ← uno por invocación de analytics-processor
```

Cada `.jsonl` contiene una línea JSON por evento. Lifecycle policy: archivar a Glacier después de 90 días, expirar los resultados de Athena después de 14 días.

### Glue Data Catalog y Crawler

`aws_glue_catalog_database.analytics` es el catálogo de metadata. **Glue Crawler** (`aws_glue_crawler.events`) corre cada hora, descubre el schema de los `.jsonl` y crea/actualiza la tabla `events` con las columnas inferidas (event_type, user_id, timestamp, payload, ingested_at) y las particiones `dt`, `hh`.

El crawler usa el LabRole de Academy, que tiene permisos sobre el bucket S3 de analytics y sobre el Glue Catalog.

### Athena — consultas SQL

`aws_athena_workgroup.analytics` es el workgroup del equipo de business analytics. Sus resultados se escriben a `s3://...-analytics/athena-results/` (encriptados con SSE-S3).

El equipo se conecta con cliente SQL externo (DBeaver / DataGrip) usando el driver JDBC de Athena. Pueden tirar queries en Presto/Trino SQL:

```sql
SELECT event_type, COUNT(*)
FROM jetsmart_prod_analytics.events
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

El grupo se incluye en el ID Token del usuario. La Lambda chat-handler lo lee desde los claims si lo necesita.

---

## Secrets Manager

Guarda un único secreto:

| Secreto | Contenido |
|---|---|
| `jetsmart-prod/anthropic-api-key` | La API key de Anthropic |

> El secreto `jetsmart-prod/rds-credentials` del TP3 se eliminó junto con RDS.

La Lambda `chat-handler` lee la API key de Anthropic en el cold start y la cachea en el contexto de ejecución.

El secreto está encriptado con AWS managed KMS keys.

---

## CloudWatch

Recibe los logs de todas las Lambdas. Hay un log group por Lambda, creados con `for_each` en Terraform:

| Log group | Lambda |
|---|---|
| `/aws/lambda/jetsmart-prod-chat-handler` | chat-handler |
| `/aws/lambda/jetsmart-prod-payment-reserve-flight` | payment-reserve-flight |
| `/aws/lambda/jetsmart-prod-payment-reserve-booking` | payment-reserve-booking |
| `/aws/lambda/jetsmart-prod-payment-collect` | payment-collect |
| `/aws/lambda/jetsmart-prod-payment-confirm` | payment-confirm |
| `/aws/lambda/jetsmart-prod-payment-refund` | payment-refund |
| `/aws/lambda/jetsmart-prod-payment-cancel` | payment-cancel |
| `/aws/lambda/jetsmart-prod-payment-release-flight` | payment-release-flight |
| `/aws/lambda/jetsmart-prod-boarding-pass-async` | boarding-pass-async |
| `/aws/lambda/jetsmart-prod-notification` | notification |
| `/aws/lambda/jetsmart-prod-analytics-processor` | analytics-processor |
| `/aws/lambda/jetsmart-prod-human-handoff-processor` | human-handoff-processor |
| `/aws/lambda/jetsmart-prod-proactive-notifications` | proactive-notifications |
| `/aws/lambda/jetsmart-prod-auth-callback` | auth-callback |
| `/aws/lambda/jetsmart-prod-cognito-trigger` | cognito-trigger |
| `/aws/lambda/jetsmart-prod-backup-dynamodb` | backup-dynamodb |
| `/aws/states/jetsmart-prod-booking` | Step Functions state machine |

Retención configurada en 30 días para todos los log groups.

### Alarms

Cuatro alarmas conectadas al SNS topic `notifications`:

| Alarma | Métrica | Por qué importa |
|---|---|---|
| `analytics-processor-errors` | `AWS/Lambda Errors > 0` sobre `analytics-processor` | Si falla, no escribimos eventos al data lake |
| `human-handoff-dlq-messages-visible` | `AWS/SQS ApproximateNumberOfMessagesVisible > 0` | Hay derivaciones a humano que no se procesaron |
| `proactive-notifications-dlq-messages-visible` | idem | Hay notificaciones proactivas que no se enviaron |
| `boarding-pass-generation-dlq-messages-visible` | idem | Hay boarding passes pendientes de generar |

---

## S3

Cuatro buckets con propósitos distintos:

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
- Eventos crudos del chatbot en formato JSON Lines particionado Hive-style: `events/dt=YYYY-MM-DD/hh=HH/<uuid>.jsonl`
- Resultados de queries Athena en `athena-results/`
- Privado con `public_access_block` activo
- Lifecycle: archivar particiones a Glacier después de 90 días; expirar resultados Athena a los 14 días
- Encriptación SSE-S3

### `jetsmart-prod-<account-id>-backups`
- Exports diarios de la tabla DynamoDB `business` generados por la Lambda `backup-dynamodb`
- Estructura: `dynamodb/business/YYYY-MM-DD/AWSDynamoDB/<export-id>/data/*.json.gz`
- Privado con `public_access_block` activo
- **Versionado ON** — defensa contra `DELETE`/`PUT` accidental sobre exports
- Lifecycle progresivo (alineado a retención AFIP RG 1415, 10 años):

| Edad | Storage class |
|---|---|
| 0–29 días | `STANDARD` |
| 30–89 días | `STANDARD_IA` (acceso esporádico, retrieval inmediato) |
| 90–364 días | `GLACIER` (retrieval 3–5 h) |
| 365–3649 días | `DEEP_ARCHIVE` (retrieval 12 h, costo mínimo) |
| 3650 días | expira |

- Lifecycle de versiones non-current: transición a `GLACIER` a los 30 días, expiran a los 90
- Cleanup automático de delete markers huérfanos con `expired_object_delete_marker = true`
- Bucket policy permite a `dynamodb.amazonaws.com` hacer `PutObject` (export funciona)
- Encriptación SSE-S3

## Lambda Layers

**Dos layers** compilados para Python 3.12 en Linux x86_64 (`layers.tf`):

| Layer | Contenido | Montado en | Usada por |
|---|---|---|---|
| `jetsmart-prod-anthropic` | SDK `anthropic` + dependencias HTTP | `/opt/python/` | `chat-handler` |
| `jetsmart-prod-system-prompt` | El system prompt del chatbot (texto) | `/opt/system_prompt.txt` | `chat-handler` |

### Por qué el system prompt en layer y no en S3 (cambio respecto al TP3)

En el TP3 el system prompt vivía en S3 (`config/system_prompt.txt`) y la Lambda lo descargaba en el cold start con `s3:GetObject`. En TP4 se movió a Lambda Layer. Justificación:

- **Cero penalty de cold start** — leer un filesystem local de Lambda es ~1ms; un `GetObject` sobre la red regional son ~30-50ms en el cold start.
- **Versionado inmutable nativo** — cada `PublishLayerVersion` devuelve un ARN nuevo. Rollback es un click cambiando el ARN atachado a la Lambda. En S3 había que copiar archivos a una key alternativa.
- **Sin permisos IAM extra** — eliminamos el `s3:GetObject` de la policy del LabRole para esta key específica. Superficie de permisos más chica.

### Por qué dos layers separados y no uno solo

Distintos ciclos de vida:

- El SDK de `anthropic` cambia cuando Anthropic publica una versión nueva (mensual aprox).
- El system prompt cambia cuando el equipo de Producto tunea el comportamiento del bot (a veces varias veces por semana).

Mezclarlos contaminaría el versionado: cada ajuste de una línea del prompt obligaría a republicar el SDK también. Separarlos permite versionarlos independientemente y rotar el prompt sin tocar el SDK.

### Build pipeline

Ambos layers se construyen con `scripts/build-layers.sh` antes de `terraform apply`:
- `anthropic` se compila con `pip --platform manylinux2014_x86_64 --target` para garantizar compatibilidad con el runtime de Lambda
- `system-prompt` se zipea desde `config/system_prompt.txt`

> El layer `jetsmart-prod-psycopg2` del TP3 (driver PostgreSQL) se eliminó junto con RDS. La validación JWT manual con `python-jose` también desapareció — ahora la hace el Cognito Authorizer en el perímetro del API Gateway.
