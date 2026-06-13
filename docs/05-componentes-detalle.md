# 05 — Componentes en detalle

## Lambda — Funciones serverless

Lambda es el servicio de cómputo principal de este proyecto. Cada función Lambda:
- Se ejecuta en respuesta a un trigger (API Gateway, SQS, Cognito, SNS)
- Corre de 0 a N instancias en paralelo según la demanda
- Se cobra por invocación y por milisegundos de ejecución
- No requiere provisionar servidores ni administrar infraestructura

### Las 13 Lambdas del proyecto

| Nombre | Trigger | Función |
|---|---|---|
| `chat-handler` | API Gateway (todos los paths, **detrás de Cognito Authorizer**) | Punto de entrada principal: chat con tool use, historial, reservas del usuario, inicio de pago. Lee claims ya validados de `event.requestContext.authorizer.claims` (sin validación JWT manual) |
| `payment-reserve-flight` | Step Functions (estado ReserveFlight) | Verifica disponibilidad y bloquea asientos en DynamoDB (decremento atómico con ConditionExpression) |
| `payment-reserve-booking` | Step Functions (estado ReserveBooking) | Crea la reserva en DynamoDB con estado PENDIENTE |
| `payment-collect-payment` | Step Functions (estado CollectPayment) | Procesa el cobro (mock; en producción llama al gateway de pagos) |
| `payment-confirm-booking` | Step Functions (estado ConfirmBooking) | Actualiza la reserva a CONFIRMADA; publica evento para analytics |
| `payment-refund-payment` | Step Functions (compensación) | Revierte el cobro si ConfirmBooking falla |
| `payment-cancel-booking` | Step Functions (compensación) | Cancela la reserva si fue creada |
| `payment-release-flight` | Step Functions (compensación) | Libera los asientos bloqueados si ReserveFlight se ejecutó |
| `boarding-pass` | Step Functions (PostBookingActions, rama paralela) | Genera el boarding pass y lo persiste en DynamoDB |
| `notification` | Step Functions (PostBookingActions + error path) | Envía confirmación al usuario (éxito o fracaso del pago) |
| `analytics-processor` | SQS analytics-queue | Escribe eventos crudos en S3 como JSON Lines particionado por fecha (TP4: ya no escribe a RDS) |
| `auth-callback` | API Gateway GET /callback (bridge HTTPS del workaround) | Intercambia authorization code por tokens JWT y redirige al frontend |
| `cognito-trigger` | Cognito post-registration | Asigna grupo `users` al usuario nuevo |

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

SNS es un servicio de pub/sub: un publicador manda un mensaje al topic y todos los suscriptores lo reciben.

### El SNS topic del proyecto

| Topic | Publicado por | Suscriptores |
|---|---|---|
| `events` | chat-handler (mensajes de chat) y payment-confirm-booking (compras completadas) | analytics-queue (SQS) |

En la arquitectura original (TALO — Trigger-and-Lambda-Orchestration) había 5 topics encadenando los pasos del flujo de pago. Esa responsabilidad la asumió **Step Functions**: el state machine orquesta los pasos directamente, invocando cada Lambda en el orden definido en la ASL. SNS queda únicamente para fan-out de eventos de analytics.

### Fan-out con SNS

El topic `events` recibe eventos de dos fuentes:
- Mensajes de chat (publicados por `chat-handler`)
- Compras completadas (publicadas por `payment-confirm-booking`)

Todos llegan a la misma `analytics-queue` → `analytics-processor` → **S3 analytics** (JSON Lines particionado). Si en el futuro se quiere agregar otro consumidor (por ejemplo, un servicio de marketing que dispara emails), basta con suscribirlo al topic — sin tocar el código de los publicadores.

---

## SQS (Simple Queue Service)

SQS es una cola de mensajes. El productor pone mensajes en la cola y el consumidor los lee cuando puede.

### Las queues del proyecto

| Queue | Fuente | Propósito |
|---|---|---|
| `analytics-queue` | SNS `events` | Trigger de `analytics-processor` para escribir en S3 (data lake) |
| `analytics-dlq` | `analytics-queue` (mensajes fallidos) | Retención de eventos de analytics que fallaron 3 veces |
| `booking-failed-dlq` | Step Functions (estado BookingDLQ) | Retención de reservas fallidas para investigación (14 días) |

El flujo de pago ya no usa colas SQS entre sus pasos — Step Functions orquesta directamente cada Lambda de pago y maneja retries y compensaciones.

### Por qué SQS para analytics y no invocación directa

Para analytics, el volumen puede ser alto (un evento por cada mensaje de chat). SQS desacopla la escritura a S3:

```
Sin SQS:
chat-handler → analytics-processor Lambda → S3
(si S3 está lento, chat-handler espera → el usuario espera)

Con SQS:
chat-handler → SNS → SQS → analytics-processor → S3
(chat-handler termina inmediatamente; analytics se procesa después)
```

Aunque S3 PutObject es rápido (~50ms), sacarlo del path sincrónico del chat mejora la latencia y permite reintentos automáticos con DLQ si algo falla.

### Long polling

`analytics-queue` está configurada con `receive_wait_time_seconds = 20` (long polling). En lugar de consultar la cola constantemente, Lambda espera hasta 20 segundos a que llegue un mensaje. Reduce el número de requests vacíos y el costo.

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

Cuando el pago es exitoso, `boarding-pass` y `notification` se ejecutan en paralelo (estado `Parallel` en ASL). Step Functions espera a que ambas terminen antes de avanzar a `BookingConfirmed`. Esto reduce el tiempo total de la acción post-pago sin código adicional.

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
| `/aws/lambda/jetsmart-prod-payment-collect-payment` | payment-collect-payment |
| `/aws/lambda/jetsmart-prod-payment-confirm-booking` | payment-confirm-booking |
| `/aws/lambda/jetsmart-prod-payment-refund-payment` | payment-refund-payment |
| `/aws/lambda/jetsmart-prod-payment-cancel-booking` | payment-cancel-booking |
| `/aws/lambda/jetsmart-prod-payment-release-flight` | payment-release-flight |
| `/aws/lambda/jetsmart-prod-boarding-pass` | boarding-pass |
| `/aws/lambda/jetsmart-prod-notification` | notification |
| `/aws/lambda/jetsmart-prod-analytics-processor` | analytics-processor |
| `/aws/lambda/jetsmart-prod-auth-callback` | auth-callback |
| `/aws/lambda/jetsmart-prod-cognito-trigger` | cognito-trigger |
| `/aws/states/jetsmart-prod-booking` | Step Functions state machine |

Retención configurada en 30 días.

---

## S3

Tres buckets con propósitos distintos:

### `jetsmart-prod-<account-id>-frontend`
- Archivos estáticos del sitio web (HTML, CSS, JS)
- Static website hosting habilitado
- Público (accesible desde internet)
- Sin versionado (los archivos se sobreescriben en cada deploy)

### `jetsmart-prod-<account-id>-assets`
- Boarding passes generados por Lambda
- **System prompt de Claude** (`config/system_prompt.txt`) — guardado en S3 para evitar el límite de 4 KB de variables de entorno de Lambda. La Lambda lo descarga en el cold start.
- Privado — acceso a boarding passes via pre-signed URLs temporales (15 min)
- Lifecycle: boarding passes expiran en 90 días; backups migran a STANDARD_IA a los 30 días
- Encriptación AES-256 habilitada por defecto

### `jetsmart-prod-<account-id>-analytics`
- Eventos crudos del chatbot en formato JSON Lines particionado: `events/dt=YYYY-MM-DD/hh=HH/<uuid>.jsonl`
- Resultados de queries Athena en `athena-results/`
- Privado con `public_access_block` activo en todas las dimensiones
- Lifecycle: archivar particiones a Glacier después de 90 días; expirar resultados Athena a los 14 días
- Encriptación AES-256

## Lambda Layers

Un único layer compilado para Python 3.12 en Linux x86_64:

| Layer | Contenido | Usada por |
|---|---|---|
| `jetsmart-prod-anthropic` | SDK `anthropic` + dependencias HTTP | `chat-handler` |

> El layer `jetsmart-prod-psycopg2` del TP3 (driver PostgreSQL) se eliminó junto con RDS. La validación JWT manual con `python-jose` también desapareció — la hace ahora el Cognito Authorizer.

El layer se construye localmente con `scripts/build-layers.sh` antes de correr `terraform apply`. El script usa `--platform manylinux2014_x86_64` para garantizar compatibilidad con el runtime de Lambda.
