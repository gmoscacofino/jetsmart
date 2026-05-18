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
| `chat-handler` | API Gateway (todos los paths) | Punto de entrada principal: chat con tool use, historial, reservas del usuario, métricas admin, inicio de pago |
| `payment-reserve-flight` | Step Functions (estado ReserveFlight) | Verifica disponibilidad y bloquea asientos en DynamoDB (decremento atómico con ConditionExpression) |
| `payment-reserve-booking` | Step Functions (estado ReserveBooking) | Crea la reserva en DynamoDB con estado PENDIENTE |
| `payment-collect-payment` | Step Functions (estado CollectPayment) | Procesa el cobro (mock; en producción llama al gateway de pagos) |
| `payment-confirm-booking` | Step Functions (estado ConfirmBooking) | Actualiza la reserva a CONFIRMADA; publica evento para analytics |
| `payment-refund-payment` | Step Functions (compensación) | Revierte el cobro si ConfirmBooking falla |
| `payment-cancel-booking` | Step Functions (compensación) | Cancela la reserva si fue creada |
| `payment-release-flight` | Step Functions (compensación) | Libera los asientos bloqueados si ReserveFlight se ejecutó |
| `boarding-pass` | Step Functions (PostBookingActions, rama paralela) | Genera el boarding pass y lo sube a S3 como pre-signed URL |
| `notification` | Step Functions (PostBookingActions + error path) | Envía confirmación al usuario (éxito o fracaso del pago) |
| `analytics-processor` | SQS analytics-queue | Escribe eventos en RDS PostgreSQL (en VPC) |
| `auth-callback` | API Gateway GET /callback | Intercambia authorization code por tokens JWT |
| `cognito-trigger` | Cognito post-registration | Asigna grupo `users` y crea perfil en DynamoDB |

### Tool use en chat-handler

`chat-handler` no llama al LLM una sola vez — implementa un **bucle de tool use** de hasta 5 rondas. Claude puede pausar su respuesta y pedir que la Lambda ejecute funciones reales para obtener datos antes de responder:

- `search_flights` — consulta disponibilidad de vuelos en DynamoDB (simula el PSS real de JetSmart)
- `get_reservation` — consulta el estado de una reserva del usuario

En producción, estas funciones llamarían a la API interna de JetSmart en lugar de DynamoDB. La interfaz hacia Claude es idéntica en ambos casos.

Ver explicación completa en [01 — Cómo funciona un chatbot](./01-como-funciona-chatbot.md#tool-use-cómo-el-chatbot-consulta-datos-reales).

### Runtime y configuración

Todas las Lambdas usan **Python 3.12**. El timeout configurable es de 30 segundos por defecto (variable `lambda_timeout`).

### Lambda en VPC vs. fuera de VPC

| Lambda | En VPC | Razón |
|---|---|---|
| `analytics-processor` | Sí (subnets privadas) | Necesita acceso a RDS en subnet privada |
| Todas las demás | No | Acceden a DynamoDB/SNS/SQS via endpoints públicos de AWS; chat-handler necesita llamar a Anthropic (internet) sin NAT |

---

## API Gateway

API Gateway es el punto de entrada HTTP del sistema. Lambda no tiene URL propia — API Gateway recibe las requests HTTPS del navegador y las traduce en invocaciones de Lambda.

### Dos instancias de API Gateway

**1. API principal (chatbot)**
- Maneja: `POST /api/chat`, `GET /api/reservations`, `POST /api/payment`, `GET /api/admin/metrics`
- Usa un recurso `{proxy+}` que captura todos los paths y los enruta a la Lambda correspondiente según el path en el handler

**2. API de auth (callback)**
- Maneja: `GET /callback`
- Invoca exclusivamente la Lambda `auth-callback`
- Es el redirect URI registrado en el Cognito App Client

### Por qué API Gateway y no una URL de Lambda

Lambda Function URLs (feature más nueva de AWS) podrían reemplazar API Gateway para casos simples. Se eligió API Gateway porque:
- Permite centralizar la autorización
- Más familiar y documentado para el contexto académico
- Permite agregar rate limiting y WAF en producción

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

Todos llegan a la misma `analytics-queue` → `analytics-processor` → RDS. Si en el futuro se quiere agregar otro consumidor (por ejemplo, un servicio de marketing que dispara emails), basta con suscribirlo al topic — sin tocar el código de los publicadores.

---

## SQS (Simple Queue Service)

SQS es una cola de mensajes. El productor pone mensajes en la cola y el consumidor los lee cuando puede.

### Las queues del proyecto

| Queue | Fuente | Propósito |
|---|---|---|
| `analytics-queue` | SNS `events` | Trigger de `analytics-processor` para escribir en RDS |
| `analytics-dlq` | `analytics-queue` (mensajes fallidos) | Retención de eventos de analytics que fallaron |
| `booking-failed-dlq` | Step Functions (estado BookingDLQ) | Retención de reservas fallidas para investigación (14 días) |

El flujo de pago ya no usa colas SQS entre sus pasos — Step Functions orquesta directamente cada Lambda de pago y maneja retries y compensaciones.

### Por qué SQS para analytics y no invocación directa

Para analytics, el volumen puede ser alto (un evento por cada mensaje de chat). SQS desacopla la escritura en RDS:

```
Sin SQS:
chat-handler → analytics-processor Lambda → RDS
(si RDS está lento, chat-handler espera → el usuario espera)

Con SQS:
chat-handler → SNS → SQS → analytics-processor → RDS
(chat-handler termina inmediatamente; analytics se procesa después)
```

La escritura en RDS puede tardar decenas de milisegundos. Sacarla del path sincrónico del chat mejora la latencia percibida por el usuario.

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

- **Sin VPC**: la Lambda chat-handler no está en la VPC. DynamoDB es accesible por endpoint público de AWS, sin necesidad de NAT ni VPC.
- **Latencia baja**: operaciones de GetItem/PutItem en < 5ms — no frena al usuario.
- **Escala automática**: on-demand billing, sin capacidad que administrar.

---

## RDS PostgreSQL

Base de datos relacional para analytics. Vive en subnets privadas dentro de la VPC.

Ver el schema completo en [07 — Capa de datos](./07-data-layer.md).

### Por qué en la VPC (y por qué eso afecta a Lambda)

RDS no tiene un endpoint público accesible desde internet — solo es accesible desde dentro de la VPC. Por eso la Lambda `analytics-processor` debe correr dentro de la VPC con el security group correcto.

### RDS Proxy

Entre `analytics-processor` y RDS hay un **RDS Proxy** (`aws_db_proxy.main`). El proxy mantiene un pool de conexiones persistentes contra RDS — cada invocación de Lambda reutiliza una conexión existente en lugar de abrir una nueva. Esto es crítico porque Lambda puede tener decenas de instancias simultáneas: sin proxy, RDS recibiría decenas de conexiones nuevas por segundo y agotaría su límite de conexiones (`max_connections`).

El proxy lee las credenciales de RDS directamente desde Secrets Manager y autentica a Lambda sin exponer la contraseña. La Lambda solo conoce el endpoint del proxy, no el de RDS.

```
Lambda analytics-processor
    → RDS Proxy (pool de conexiones) → RDS PostgreSQL
       (lee credenciales de Secrets Manager)
```

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
| `admins` | Equipo / profesores | Dashboard de analytics |

El grupo se incluye en el ID Token del usuario. La Lambda de chat y el frontend lo leen para decidir qué mostrar y qué permitir.

---

## Secrets Manager

Guarda dos secretos:

| Secreto | Contenido |
|---|---|
| `jetsmart/anthropic-api-key` | La API key de Anthropic |
| `jetsmart/rds-credentials` | Host, puerto, usuario y contraseña de RDS |

La Lambda chat-handler lee la API key de Anthropic al arrancar. La Lambda analytics-processor lee las credenciales de RDS cada vez que procesa un mensaje (o las cachea en el contexto de ejecución).

Los secretos están encriptados con AWS managed KMS keys.

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

Dos buckets con propósitos distintos:

### `jetsmart-frontend`
- Archivos estáticos del sitio web (HTML, CSS, JS)
- Static website hosting habilitado
- Público (accesible desde internet)
- Sin versionado (los archivos se sobreescriben en cada deploy)

### `jetsmart-assets`
- Boarding passes generados por Lambda
- Backups de DynamoDB
- **System prompt de Claude** (`config/system_prompt.txt`) — guardado en S3 para evitar el límite de 4 KB de variables de entorno de Lambda. La Lambda lo descarga en el cold start.
- Privado — acceso a boarding passes via pre-signed URLs temporales (15 min)
- Lifecycle: boarding passes expiran en 90 días; backups migran a STANDARD_IA a los 30 días
- Encriptación AES-256 habilitada por defecto

## Lambda Layers

Dos layers compilados para Python 3.12 en Linux x86_64:

| Layer | Contenido | Usada por |
|---|---|---|
| `jetsmart-prod-anthropic` | SDK `anthropic` + dependencias HTTP | `chat-handler` |
| `jetsmart-prod-psycopg2` | Driver PostgreSQL `psycopg2-binary` | `analytics-processor` |

Los layers se construyen localmente con `scripts/build-layers.sh` antes de correr `terraform apply`. El script usa `--platform manylinux2014_x86_64` para garantizar compatibilidad con el runtime de Lambda.
