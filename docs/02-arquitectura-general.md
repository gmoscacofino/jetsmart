# 02 — Arquitectura general

## Qué estamos construyendo

Un chatbot conversacional que replica la experiencia de la web de JetSmart, funcionando como canal end-to-end para reservar vuelos, hacer check-in, consultar el estado de vuelos, gestionar reservas y hacer reclamos.

El chatbot usa inteligencia artificial (Claude de Anthropic) para entender lenguaje natural. Los datos de vuelos son simulados (mock data) ya que la API real de JetSmart no es pública.

---

## Decisiones de arquitectura

### Serverless en lugar de contenedores

La arquitectura usa Lambda en lugar de ECS (contenedores Docker). Las razones:

- **Costo**: Lambda cobra por invocación y por milisegundo de ejecución. Un chatbot con tráfico irregular paga solo cuando hay requests. ECS cobra por el tiempo que los contenedores están corriendo, haya tráfico o no.
- **Escala automática**: Lambda escala automáticamente con el tráfico sin configuración adicional.
- **Sin infraestructura**: No hay que administrar instancias EC2 ni clusters.

### Step Functions con patrón Saga para reservas

El flujo de reserva y pago es una **transacción distribuida** — involucra múltiples pasos (reservar asiento, crear booking, cobrar, confirmar) que deben ser atómicos: si falla cualquier paso intermedio, todo lo anterior debe deshacerse.

Step Functions orquesta esta lógica con el **patrón Saga**: cada paso tiene una compensación automática que se ejecuta si algo falla después.

```
Flujo exitoso:
  ReserveFlight → ReserveBooking → CollectPayment → ConfirmBooking
                                                           ↓
                                                  PostBookingActions (paralelo)
                                                  ├── Notification
                                                  └── BoardingPass

Si ConfirmBooking falla:
  RefundPayment → CancelBooking → ReleaseFlight → NotifyBookingFailed → DLQ
```

**Por qué Step Functions en lugar de SNS→SQS:**
- La Saga requiere orquestación con estado — saber qué pasos se ejecutaron para hacer el rollback correcto.
- Step Functions mantiene ese estado automáticamente y reintenta pasos fallidos con backoff exponencial.
- Con SNS→SQS habría que implementar el tracking de estado manualmente en DynamoDB.

### Decremento atómico de asientos

`payment-reserve-flight` usa `ConditionExpression="asientos_disponibles >= :min"` en DynamoDB. Si dos usuarios intentan reservar el último asiento simultáneamente, solo uno recibe un response exitoso — el otro recibe `ConditionalCheckFailedException`, que Step Functions propaga como error de disponibilidad y ejecuta el rollback.

### Chat sincrónico, Saga asincrónica

El chat **debe** ser sincrónico: el usuario envía un mensaje y espera la respuesta inmediata. Al confirmar la compra, `chat-handler` llama a Step Functions con `startExecution` (no espera el resultado) y retorna un transaction ID inmediatamente. La Saga corre en background.

### Mock data en lugar de API real de JetSmart

En producción, el backend se conectaría a la API interna de JetSmart para obtener disponibilidad de vuelos. Esa API no es pública. En este TP, los datos de vuelos (rutas, precios, fechas disponibles) se cargan como mock data en DynamoDB con el esquema `PK=FLIGHT#{origen}#{destino}` / `SK=DATE#{fecha}`.

### DynamoDB y RDS — por qué los dos

**DynamoDB** — para datos del chatbot en tiempo real:
- Historial de conversaciones (lecturas y escrituras muy frecuentes)
- Datos mock de vuelos (consultas rápidas por clave exacta)
- Reservas y reclamos de usuarios

**RDS (PostgreSQL)** — para analytics del administrador:
- Eventos de negocio (mensajes de chat, compras) que llegan via SNS→SQS
- Dashboard del admin con métricas y totales

DynamoDB es muy rápido para buscar un dato puntual por clave. RDS es mejor para agregar datos con SQL (COUNT, SUM, GROUP BY).

### Lambda analytics en VPC, resto de Lambdas fuera

`analytics-processor` es la única Lambda dentro de la VPC porque necesita acceso directo a RDS en subnet privada. También usa VPC Endpoints para acceder a DynamoDB y Secrets Manager sin salir a internet.

El resto de Lambdas no están en la VPC — así pueden llamar a la API de Anthropic (internet) directamente y sin pasar por el NAT Gateway.

### Pipeline de analytics: SNS → SQS → Lambda

Los eventos del chat (mensajes, compras) se publican en un SNS topic. SQS suscribe ese topic — actúa como buffer que suaviza los picos de tráfico. La Lambda `analytics-processor` consume la cola de a 10 mensajes por invocación, escribe en RDS y actualiza agregados en DynamoDB.

Si RDS falla, el mensaje vuelve a SQS para reintento. Después de 3 intentos va al DLQ (analytics-dlq).

---

## Mapa de componentes

| # | Componente | Categoría | Rol |
|---|---|---|---|
| 1 | VPC | Red | Red privada que contiene los recursos |
| 2 | Internet Gateway | Red | Entrada/salida de internet |
| 3 | Subnets (6) | Red | 2 públicas + 2 privadas cómputo + 2 privadas datos |
| 4 | Route Tables | Red | Reglas de enrutamiento por subnet |
| 5 | NAT Gateway | Red | Salida a internet desde subnets privadas (solo analytics Lambda) |
| 6 | Security Groups | Red | Control de tráfico entre componentes |
| 7 | VPC Endpoints (×3) | Red | Secrets Manager + SQS + CloudWatch Logs (Interface) — acceso privado desde VPC sin NAT |
| 8 | EC2 Bastion | Red | Acceso a RDS via SSM port-forwarding (sin SSH, sin puerto 22) |
| 9 | S3 — frontend | Storage | Archivos estáticos del sitio web (HTML/CSS/JS) |
| 10 | S3 — assets | Storage | Boarding passes generados |
| 11 | Cognito User Pool | Auth | Registro y login de usuarios con Hosted UI |
| 12 | Cognito Groups | Auth | `users` (chatbot) y `admins` (dashboard) |
| 13 | API Gateway (chat) | Cómputo | Endpoint HTTPS `/api/*` → invoca chat-handler |
| 14 | API Gateway (auth) | Cómputo | Endpoint HTTPS `/callback` → invoca auth-callback |
| 15 | Lambda — chat-handler | Cómputo | Chat con tool use, historial, auth JWT, inicio de reserva |
| 16 | Lambda — payment-reserve-flight | Cómputo | Paso 1 Saga: bloquea asientos (decremento atómico) |
| 17 | Lambda — payment-reserve-booking | Cómputo | Paso 2 Saga: crea reserva en DynamoDB estado PENDIENTE |
| 18 | Lambda — payment-collect | Cómputo | Paso 3 Saga: procesa el cobro |
| 19 | Lambda — payment-confirm | Cómputo | Paso 4 Saga: confirma reserva; publica evento para analytics |
| 20 | Lambda — payment-refund | Cómputo | Compensación: revierte el cobro |
| 21 | Lambda — payment-cancel | Cómputo | Compensación: cancela la reserva |
| 22 | Lambda — payment-release-flight | Cómputo | Compensación: libera los asientos bloqueados |
| 23 | Lambda — boarding-pass | Cómputo | PostBookingActions: genera boarding pass en DynamoDB |
| 24 | Lambda — notification | Cómputo | PostBookingActions + error path: notifica al usuario |
| 25 | Lambda — analytics-processor | Cómputo | Consume SQS, escribe en RDS via proxy (en VPC) |
| 25b | RDS Proxy | Cómputo | Pool de conexiones entre analytics-processor y RDS; lee credenciales de Secrets Manager |
| 26 | Lambda — auth-callback | Cómputo | Intercambia authorization code por tokens JWT |
| 27 | Lambda — cognito-trigger | Cómputo | Post-registro: asigna grupo `users` al usuario nuevo |
| 28 | Step Functions | Orquestación | State machine del patrón Saga (reserva y pago) |
| 29 | SNS — events | Mensajería | Recibe eventos del chat (mensajes, compras) → fan-out a SQS analytics |
| 30 | SNS — notifications | Mensajería | Notificaciones al usuario (booking confirmado / fallido) |
| 31 | SQS — analytics | Mensajería | Buffer de eventos hacia analytics-processor |
| 32 | SQS — analytics-dlq | Mensajería | DLQ: eventos que fallaron 3 veces |
| 33 | SQS — booking-failed-dlq | Mensajería | DLQ: flujos Saga que no pudieron completarse |
| 34 | DynamoDB | Base de datos | Single Table Design: sesiones, reservas, vuelos mock, analytics agregados |
| 35 | RDS PostgreSQL | Base de datos | Log detallado de eventos para el dashboard admin |
| 36 | Secrets Manager | Seguridad | API key Anthropic + credenciales RDS |
| 37 | Lambda Layer — anthropic | Cómputo | SDK de Anthropic compilado para Python 3.12; usado por chat-handler |
| 37b | Lambda Layer — psycopg2 | Cómputo | Driver PostgreSQL compilado para Python 3.12; usado por analytics-processor |
| 38 | IAM — LabRole | Seguridad | Rol preexistente de AWS Academy — compartido por todas las Lambdas y el RDS Proxy |
| 39 | CloudWatch (13 log groups) | Observabilidad | Logs de todas las Lambdas con retención de 30 días, creados con `for_each` |

---

## Diagrama de arquitectura

```
INTERNET
   │
   ├── Browser → S3 jetsmart-frontend (HTML/CSS/JS — static website hosting)
   ├── Browser → Cognito Hosted UI (login / registro)
   ├── Browser → API Gateway /callback → Lambda auth-callback → redirige con #token=...
   └── Browser → API Gateway /api/* → Lambda chat-handler ⟺ Anthropic API (HTTPS, internet)
                                             │               (claude-haiku-4-5, bucle tool use)
                       ┌─────────────────────┴──────────────────────────────┐
                       │ tool: create_reservation                            │ evento: chat_message
                       ↓                                                     ↓
              ┌─────────────────────────────────┐                    SNS events
              │  Step Functions — Saga          │                         │
              │                                 │                         ↓
              │  ReserveFlight (Lambda)         │                  SQS analytics
              │       ↓ (ok) / → CancelBooking │                         │
              │  ReserveBooking (Lambda)        │                         ↓
              │       ↓ (ok) / → CancelBooking │          Lambda analytics-processor
              │  CollectPayment (Lambda)        │          (en VPC — subnet privada)
              │       ↓ (ok) / → CancelBooking │               │          │
              │  ConfirmBooking (Lambda)        │               ↓          ↓
              │       ↓ (ok) / → RefundPayment │        RDS PostgreSQL  DynamoDB
              │                                 │        (eventos_chat)  (agregados)
              │  PostBookingActions (paralelo): │
              │  ┌────────────┬──────────────┐  │
              │  Notification  BoardingPass   │  │
              │  (Lambda)      (Lambda)       │  │
              │  └────────────┴──────────────┘  │
              │       ↓                          │
              │  BookingConfirmed (Succeed)      │
              │                                  │
              │  Compensaciones:                 │
              │  RefundPayment → CancelBooking   │
              │  → ReleaseFlight                 │
              │  → NotifyBookingFailed           │
              │  → SQS booking-failed-dlq        │
              └─────────────────────────────────┘

DENTRO DE LA VPC:
  analytics-processor Lambda ←──→ RDS Proxy ←──→ RDS PostgreSQL (subnet privada datos)
                            ←──→ SQS (VPC Interface Endpoint)
                            ←──→ Secrets Manager (VPC Interface Endpoint)
                            ←──→ CloudWatch Logs (VPC Interface Endpoint)
  EC2 Bastion ←──→ SSM port-forwarding — acceso operativo a RDS (sin puerto 22)

FUERA DE LA VPC (servicios managed — siempre accesibles):
  S3 · Cognito · API Gateway · Step Functions · SNS · SQS · DynamoDB · Secrets Manager · CloudWatch

INTERNET EXTERNO:
  Anthropic API (claude-haiku-4-5-20251001) — llamada directa desde chat-handler Lambda
```

---

## Funcionalidades del chatbot

| Funcionalidad | Cómo funciona |
|---|---|
| Búsqueda de vuelos | Tool use → `list_flight_dates` + `search_flights` → consulta DynamoDB |
| Reserva completa | Tool use → `create_reservation` → Step Functions Saga |
| Check-in | Tool use → `check_in` → UpdateItem en DynamoDB |
| Boarding pass | Tool use → `get_boarding_pass` → consulta DynamoDB |
| Mis reservas | Tool use → `list_user_reservations` → Query DynamoDB por usuario |
| Reclamos | Tool use → `create_claim` → PutItem en DynamoDB |
| Dashboard admin | GET /api/admin/metrics → consulta DynamoDB aggregates (requiere grupo `admins`) |
