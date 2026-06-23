# 02 — Arquitectura general

## Qué estamos construyendo

Un chatbot conversacional que replica la experiencia de la web de JetSmart, funcionando como canal end-to-end para reservar vuelos, hacer check-in, consultar el estado de vuelos, gestionar reservas y hacer reclamos.

El chatbot usa inteligencia artificial (Claude de Anthropic) para entender lenguaje natural. La tabla DynamoDB `business` cumple el rol de PSS (Passenger Service System) de la aerolínea — es la fuente única de verdad de vuelos, reservas, pasajeros y reclamos que consumirían también el sitio web, la app móvil y el call center. Para el TP se pre-carga un dataset de demo con `seed.py`.

---

## Cambios respecto al TP3 (post-feedback)

| Decisión TP3 | Decisión TP4 | Razón |
|---|---|---|
| Lambda `analytics-processor` escribe a RDS PostgreSQL via RDS Proxy | `business-analytics-emitter` emite a Kinesis Data Firehose → S3 en JSON Lines particionado (sin Lambda de transformación) | El equipo de business analytics consume Athena con cliente SQL — patrón data lake estándar |
| Bastion EC2 en subnet pública para acceso a RDS | Eliminado | Sin RDS, no hay caso de uso |
| VPC con subnets públicas/privadas/datos, NAT Gateway, VPC Endpoints | **VPC `10.0.0.0/16`** con subnets públicas (ALB + NAT), privadas-fargate y privadas-lambda, 1 NAT Gateway y VPC Endpoints | El core del chatbot pasó a contenedores Fargate (post-feedback Faustino): el cómputo en contenedor vive DENTRO de la VPC, en subnets privadas |
| Validación JWT manual con `python-jose` dentro de `chat-handler` | **Validación JWT in-app** en el contenedor Fargate (`server.py` valida la firma contra el JWKS del Cognito User Pool) | El chatbot ya no es Lambda detrás de API Gateway: es un servicio FastAPI detrás de un ALB, y valida el token dentro del contenedor antes de invocar la lógica |
| `auth-callback` Lambda como bridge HTTPS | **Idéntica** (workaround invariable) | Frontend está en S3 HTTP; Cognito requiere redirect HTTPS — la Lambda detrás de API GW es el único bridge HTTPS posible |

### Cambios introducidos en TP4 (demostración final, post-presentación)

| Cambio | Razón |
|---|---|
| **DynamoDB partida en dos tablas single-design**: `jetsmart-prod-conversations` (chat) + `jetsmart-prod-business` (PSS-like) | Bounded contexts: separa estado efímero del chatbot del dominio de negocio. Failure isolation entre el canal y el core. Retention policies independientes (TTL en chat, persistencia en negocio). Prepara la arquitectura para sumar otros canales que compartirían la business table. |
| **Reservas migran a esquema PNR-céntrico** (record locator de 6 chars uppercase, à la Navitaire/Amadeus) con sub-items `SEGMENT#`, `PAX#`, `BP#` | Refleja cómo modelaría las reservas un PSS real. Habilita queries útiles ("quién está en este vuelo") via el GSI ReservationsByFlight. |
| **Implementada derivación a humano**: nueva tool `escalate_to_human` en `chat_handler` → SQS `human-handoff` → Lambda `human_handoff_processor` (mock call center) | Completar la feature de TP1 que no se había implementado. SQS desacopla el chatbot del sistema del call center: si el call center está caído, el pedido queda esperando. |
| **Implementadas notificaciones proactivas (event-driven)**: trigger por **DynamoDB Stream** sobre `business` → Lambda `stream-emitter` (detecta la transición a CANCELADO) → publica `flight_cancelled` al SNS central `events` → Lambda `proactive_notifications` (suscripción SNS→Lambda directo con filter policy) → fan-out de emails vía SNS `notifications` | Completa la feature de TP1. Ops cambia `estado_vuelo=CANCELADO` en el master row del vuelo (consola DynamoDB o dashboard interno) y el Stream propaga. Ver justificación #28. |
| **Boarding pass async vía SNS→Lambda directo**: el Saga ya no invoca la Lambda de boarding pass directamente — el estado terminal de éxito publica `booking_confirmed` al SNS central `events` y la Lambda `boarding_pass_async` (suscripción con filter policy `booking_confirmed`) lo consume | Desacopla el path sync del Saga del trabajo de generación del PDF. Fire-and-forget: la reserva ya quedó confirmada antes del fan-out. |
| **Backbone de eventos: UN solo SNS topic `events`** con fan-out por filter policy (SNS→Lambda directo salvo `human-handoff`) | La única cola funcional es `human-handoff` (protege un downstream no elástico: el call center). Las demás suscripciones van SNS→Lambda directo con alarma de Lambda Errors; el resto de DLQs (`booking-failed-dlq`, `refund-failures-dlq`) son sinks de revisión manual escritos por los Catch de las Step Functions. |

### Bounded contexts: Conversations vs PSS Business

Las dos tablas DynamoDB representan dos **bounded contexts** distintos del DDD:

- **Conversations** = estado efímero del chatbot. Sesiones, mensajes, perfil chat-scoped, escalaciones a humano. TTL de días. Si se borra, no se pierde negocio. Es propiedad del canal "chatbot".
- **Business (PSS-like)** = estado persistente del negocio. Vuelos, reservas (PNRs), pasajeros (CRM), reclamos, boarding passes. Sin TTL. Es propiedad de la aerolínea, compartido por todos los canales (chatbot, web, app, IVR, call center).

La separación habilita decisiones independientes de:
- Retention policy y backup window.
- Encryption key (KMS distinta por contexto, si quisiéramos).
- Scaling y cost attribution (cuánto cuesta operar el chatbot vs. el core de negocio).
- Reemplazabilidad del chatbot (si mañana migrás el chatbot a otro stack, la data conversacional es portable independientemente del PSS).

---

## Decisiones de arquitectura

### VPC para el cómputo en contenedor (Fargate) y las Lambdas de negocio

El core del chatbot (`chat-handler` y `weather-poller`) corre en **contenedores Fargate** — recursos con identidad de red persistente que justifican una VPC. Por eso existe una **VPC `10.0.0.0/16`** con:

- **subnets públicas** → ALB internet-facing + NAT Gateway,
- **subnets privadas-fargate** → tasks Fargate (`assign_public_ip=false`),
- **subnets privadas-lambda** → las 9 Lambdas de negocio (`vpc_config`),
- **1 NAT Gateway** (egress de las tasks hacia internet, p.ej. la API del clima),
- **2 gateway endpoints** (S3, DynamoDB) + **8 interface endpoints** (sns, sqs, secretsmanager, states, ecr.api, ecr.dkr, logs, kinesis-firehose) para alcanzar servicios AWS sin salir por NAT.

**Las 9 Lambdas de negocio corren en la VPC** (subnets `private-lambda`, `vpc_config`) y alcanzan DynamoDB/SNS/SQS/Step Functions/S3/Secrets por los **VPC endpoints**. Solo `auth-callback` y `cognito-trigger` quedan regionales (sin VPC). Así, todo el cómputo del chatbot —Fargate y Lambdas— vive dentro de la VPC.

Trade-off: a cambio del NAT Gateway y los endpoints que hay que mantener, ganamos aislamiento de red real para el core del chatbot (subnets privadas, sin IP pública). El tracing distribuido se mantiene vía **X-Ray**.

### Step Functions con patrón Saga para reservas

El flujo de reserva y pago es una **transacción distribuida** — múltiples pasos (reservar asiento, crear booking, cobrar, confirmar) que deben ser atómicos: si falla cualquier paso intermedio, todo lo anterior debe deshacerse.

Step Functions orquesta esta lógica con el **patrón Saga**: cada paso tiene una compensación automática que se ejecuta si algo falla después.

```
Flujo exitoso:
  ReserveFlight → ReserveBooking → CollectPayment → ConfirmBooking
                                                           ↓
                                          PublishBookingConfirmed (sns:publish a events)
                                                           ↓
                                                   BookingConfirmed
   (el fan-out — notification + boarding-pass + analytics — lo hacen
    las suscripciones al topic `events` filtradas por event_type)

Si ConfirmBooking falla:
  RefundPayment → CancelBooking → ReleaseFlight
                → PublishBookingFailed (sns:publish a events) → BookingDLQ → BookingFailed
```

**Por qué Step Functions en lugar de SNS→SQS:** la Saga requiere orquestación con estado — saber qué pasos se ejecutaron para hacer el rollback correcto. Step Functions mantiene ese estado, reintenta con backoff exponencial y permite compensaciones declarativas. Con SNS→SQS habría que implementar el tracking de estado manualmente en DynamoDB.

### Decremento atómico de asientos

`payment-reserve-flight` usa `ConditionExpression="asientos_disponibles >= :min"` en DynamoDB. Si dos usuarios intentan reservar el último asiento simultáneamente, sólo uno recibe un response exitoso — el otro recibe `ConditionalCheckFailedException`, que Step Functions propaga como error de disponibilidad y ejecuta el rollback.

### Chat sincrónico, Saga asincrónica

El chat **debe** ser sincrónico: el usuario manda un mensaje y espera la respuesta inmediata. Al confirmar la compra, `chat-handler` llama a Step Functions con `startExecution` (no espera el resultado) y retorna un transaction ID inmediatamente. La Saga corre en background.

### DynamoDB `business` como PSS

La tabla `business` no es un mock que se reemplazaría por otra cosa — es el PSS. El chatbot, la web, la app y el call center son canales sobre la misma tabla. El esquema PNR-céntrico (`PK=PNR#...`, sub-items `SEGMENT#`, `PAX#`, `BP#`, GSIs por número de vuelo / por reserva / por pasajero) es el patrón estándar de un PSS comercial (estilo Navitaire / Amadeus). Para que el chatbot tenga algo que mostrar, `seed.py` precarga rutas, fechas, precios e inventario al hacer `terraform apply`.

### DynamoDB para tiempo real, S3+Athena para analytics

**DynamoDB** — datos del chatbot en tiempo real:
- Historial de conversaciones (lecturas y escrituras muy frecuentes)
- Datos mock de vuelos (consultas rápidas por clave exacta)
- Reservas y reclamos de usuarios

**S3 + Athena** — analytics histórico para el equipo de business analytics:
- Dos fuentes alimentan el data lake: el CDC de la tabla `business` (DynamoDB Stream → `business-analytics-emitter`, que clasifica por entidad PNR#/FLIGHT#/CLAIM#) y los eventos de comportamiento del chat (SNS `events` → suscripción Firehose)
- **4 Kinesis Data Firehose delivery streams** (`reservation_events`, `flight_events`, `claim_events`, `interaction_events`) batchean de forma nativa (buffer 5 MB / 60 s, GZIP, sin Lambda de transformación) y escriben a S3 `lake/<entidad>/dt=YYYY-MM-DD/hh=HH/*.gz`
- **4 tablas Glue tipadas estáticas** (declaradas en Terraform) con **partition projection** sobre `dt`/`hh` — sin Glue Crawler ni descubrimiento de schema; los datos quedan consultables apenas Firehose los escribe (buffer 60 s)
- El equipo consume Athena vía cliente SQL externo (DBeaver / DataGrip con driver JDBC), consultando `jetsmart_prod_analytics.<tabla>`

**Por qué no RDS:** OLTP postgres es la herramienta equivocada para analítica. Carga el primario con queries pesadas, no escala por costo, y el patrón estándar 2026 para business analytics es data lake. Athena cobra ~5 USD/TB escaneado; con el volumen de eventos del chatbot el costo es despreciable.

**Por qué no QuickSight:** AWS Academy no lo habilita con LabRole. El equipo de analytics queda con cliente SQL (DBeaver/DataGrip) — funcional pero más manual.

### Validación del JWT in-app (en el contenedor Fargate)

El chatbot entra por un **ALB internet-facing (HTTP:80)** que enruta al servicio Fargate `chat-handler`. No hay API Gateway ni Cognito Authorizer en este path. El propio contenedor valida el **JWT de Cognito in-app**: `server.py` verifica la firma del token contra el **JWKS** del User Pool (issuer, expiración) y pasa los claims a la lógica. Las rutas (`POST /api/chat`, `GET /api/reservations`, `POST /api/payment`) requieren token; `GET /health` no (lo usa el health check del target group).

**Por qué in-app y no Cognito Authorizer:** el chatbot ya no es Lambda detrás de API Gateway — es un servicio FastAPI nativo detrás de un ALB. Academy no habilita ACM, así que no hay listener HTTPS ni auth Cognito nativa del ALB; la validación vive en el contenedor. En producción real iría ACM + listener 443 + auth Cognito en el ALB.

**Único API Gateway que queda:** el de `auth-callback` (`jetsmart-prod-auth-api`), bridge OAuth del Cognito Hosted UI, con `authorization = "NONE"` porque es Cognito quien redirige a `/callback` con el `code` en query string — no manda Authorization header. Es parte del **workaround documentado en** `teoria/notas-de-clase/workaround-cognito.md`.

### Workaround del redirect HTTPS

El frontend está en S3 HTTP. Cognito sólo redirige a URLs HTTPS. La Lambda `auth-callback` detrás de API Gateway (HTTPS estable) es el **bridge** que:

1. Recibe `?code=...` desde Cognito.
2. Intercambia el code por tokens contra el token endpoint de Cognito.
3. Hace un `302` al frontend con `#id_token=...`.

El frontend lee el token del fragment y lo guarda en localStorage. Después, todas las requests al chatbot llevan `Authorization: Bearer <token>` que el servicio Fargate valida in-app (contra el JWKS del User Pool) detrás del ALB.

### Pipeline de analytics: CDC + comportamiento → Firehose → S3

Dos fuentes alimentan el data lake. **(1) CDC de negocio:** el DynamoDB Stream de `business` dispara la Lambda `business-analytics-emitter`, que clasifica la entidad (PNR#/FLIGHT#/CLAIM#) y hace `PutRecord` al Firehose correspondiente (`reservation_events` / `flight_events` / `claim_events`). **(2) Comportamiento:** el SNS central `events` tiene una suscripción Firehose (filter `anything-but` los `event_type` transaccionales) hacia el delivery stream `interaction_events`.

Los **4 delivery streams** de **Kinesis Data Firehose** batchean de forma nativa (buffer 5 MB / 60 s, GZIP, sin Lambda de transformación) y entregan JSON Lines particionado en S3 (`lake/<entidad>/dt=YYYY-MM-DD/hh=HH/*.gz`).

Las **4 tablas Glue son estáticas y tipadas** (declaradas en Terraform): el schema no se descubre con un crawler, está declarado. Las particiones se proyectan en consulta (**partition projection** sobre `dt`/`hh`), así que los datos nuevos quedan consultables apenas Firehose los escribe. El buffering de Firehose absorbe los picos; los reintentos de entrega a S3 los maneja el propio Firehose.

---

## Mapa de componentes

| # | Componente | Categoría | Rol |
|---|---|---|---|
| 1 | S3 — frontend | Storage / Edge | Archivos estáticos del sitio web (HTML/CSS/JS). HTTP. |
| 2 | S3 — boarding-passes | Storage | Boarding passes generados por la Lambda `boarding-pass-async`. (Renombrado desde `assets` en TP4: el system prompt se movió a una Lambda Layer.) |
| 3 | S3 — analytics | Storage / Data lake | Eventos crudos en JSON Lines, particionado por `dt=YYYY-MM-DD/hh=HH`. |
| 4 | Cognito User Pool | Auth | Registro y login con Hosted UI. |
| 5 | Cognito Groups | Auth | `users` (chatbot). |
| 6 | ALB — chatbot | Cómputo / Edge | Application Load Balancer internet-facing (HTTP:80). Enruta `/api/*` al servicio Fargate chat-handler; health check sobre `/health`. **No hay Cognito Authorizer — el JWT se valida in-app.** |
| 7 | API Gateway — auth | Cómputo / Edge | `jetsmart-prod-auth-api`. Único API Gateway. Endpoint HTTPS `/callback` (+`/logout`) → invoca auth-callback (bridge del workaround). |
| 8 | Fargate — chat-handler | Cómputo | Servicio FastAPI nativo en ECS Fargate (2 tasks Multi-AZ, Auto Scaling 2→6 por CPU 60%), en subnets privadas detrás del ALB. Chat con tool use, historial, inicio de reserva. Valida el JWT de Cognito in-app (JWKS) y usa los claims. |
| 8b | Fargate — weather-poller | Cómputo | Task Fargate (desired 1) en subnets privadas, egress por NAT. Detecta condiciones climáticas y cancela vuelos afectados. |
| 9 | Lambda — payment-reserve-flight | Cómputo | Paso 1 Saga: bloquea asientos (decremento atómico DynamoDB). |
| 10 | Lambda — payment-reserve-booking | Cómputo | Paso 2 Saga: crea reserva PENDIENTE. |
| 11 | Lambda — payment-collect | Cómputo | Paso 3 Saga: procesa el cobro. |
| 12 | Lambda — payment-confirm | Cómputo | Paso 4 Saga: confirma reserva; publica evento. |
| 13 | Lambda — payment-refund | Cómputo | Compensación: revierte el cobro. |
| 14 | Lambda — payment-cancel | Cómputo | Compensación: cancela la reserva. |
| 15 | Lambda — payment-release-flight | Cómputo | Compensación: libera los asientos. |
| 16 | Lambda — boarding-pass-async | Cómputo | TP4: suscripción SNS→Lambda directo al topic `events` (filter `booking_confirmed`); genera BP en S3 y graba bp_url en PNR. |
| 17 | Lambda — notification | Cómputo | Suscripción SNS→Lambda directo al topic `events` (filter `booking_confirmed`, `booking_failed`): notifica al usuario. |
| 18 | Lambda — business-analytics-emitter | Cómputo | Emite eventos de negocio a Kinesis Data Firehose (PutRecord); Firehose batchea nativo y escribe S3 JSON Lines (sin Lambda de transformación). |
| 19 | Lambda — auth-callback | Cómputo | Bridge HTTPS del workaround. Intercambia code por tokens. |
| 20 | Lambda — cognito-trigger | Cómputo | Post-registro: asigna grupo `users`. |
| 20c | **Lambda — human-handoff-processor (TP4)** | Cómputo | Consume SQS human-handoff, mock POST al call center, actualiza ticket HANDOFF# y notifica al usuario por email. |
| 20d | **Lambda — proactive-notifications (TP4)** | Cómputo | Suscripción SNS→Lambda directo al topic `events` (filter `flight_cancelled`); Query a GSI ReservationsByFlight, fan-out de emails vía SNS notifications. |
| 21 | Step Functions | Orquestación | State machine del patrón Saga. TP4: el estado terminal de éxito publica `booking_confirmed` al SNS central `events` en lugar de un Parallel interno; el fan-out post-booking lo hacen las suscripciones con filtro. |
| 22 | SNS — events | Mensajería | Topic central (backbone). Publishers: chat-handler, Step Functions (booking_confirmed/booking_failed), stream-emitter (flight_cancelled). Fan-out por filter policy (`event_type`): SNS→Lambda directo (notification, boarding_pass_async, proactive_notifications, refund_trigger), SQS human-handoff (handoff_requested) y Firehose interaction_events (comportamiento, anything-but transaccionales). |
| 23 | SNS — notifications | Mensajería | Notificaciones al usuario (booking confirmado / fallido / handoff ack / cancelación de vuelo). |
| 23b | **Lambda — stream-emitter (TP4)** | Cómputo | Consume el DynamoDB Stream de `business`. Detecta la transición a estado_vuelo=CANCELADO en master rows FLIGHT# (OldImage≠CANCELADO, NewImage=CANCELADO) y publica `flight_cancelled` al SNS central `events`. También emite el CDC de negocio al data lake. |
| 25 | SQS — refund-failures-dlq | Mensajería | DLQ: ejecuciones de refund que no pudieron completarse. |
| 26 | SQS — booking-failed-dlq | Mensajería | DLQ: flujos Saga que no pudieron completarse. |
| 26a | **SQS — human-handoff + DLQ (TP4)** | Mensajería | Única cola funcional. Suscrita al SNS `events` (filter `handoff_requested`) → Lambda human-handoff-processor (call center mock). DLQ retiene 14d para reintento manual. |
| 27a | **DynamoDB — conversations (TP4)** | Base de datos | Single Table Design: sesiones, mensajes, perfil chat-scoped, handoffs. TTL en todos los items. |
| 27b | **DynamoDB — business (TP4, PSS-like)** | Base de datos | Single Table Design PNR-céntrico: FLIGHT#, PNR#/SEGMENT#/PAX#/BP#/EXTRA#, PASSENGER#, CLAIM#. 1 GSI (ReservationsByFlight). Stream habilitado (NEW_AND_OLD_IMAGES) consumido por stream-emitter. |
| 28 | Glue Data Catalog (4 tablas estáticas + partition projection, sin crawler) | Catálogo | 4 tablas tipadas declaradas en Terraform (`reservation_events`, `flight_events`, `claim_events`, `interaction_events`) sobre `lake/<entidad>/`. Schema estático (no descubierto); particiones `dt`/`hh` por partition projection. |
| 30 | Athena Workgroup | Consultas | Endpoint SQL para el equipo de business analytics. |
| 31 | Secrets Manager | Seguridad | API key Anthropic. |
| 32 | Lambda Layer — anthropic | Cómputo | SDK de Anthropic compilado para Python 3.12. |
| 33 | IAM — LabRole | Seguridad | Rol preexistente de AWS Academy — compartido por todas las Lambdas. |
| 34 | CloudWatch (17 log groups + 4 alarms) | Observabilidad | Logs de las 16 Lambdas + 1 log group del state machine de Step Functions, todos con retención 30d. Alarms: business-analytics-emitter errors (fallo de PutRecord a Firehose) + DLQ depth alarms + alarmas de error de los consumers SNS→Lambda directo. |

---

## Diagrama de arquitectura

```
                            INTERNET
                                │
                ┌───────────────┼────────────────────────────────────────┐
                │               │                                        │
   Browser → S3 frontend     Browser → Cognito Hosted UI (HTTPS)         │
   (HTTP estático)               │ (login / registro)                    │
                                 │                                       │
                                 ↓ redirect ?code=...                    │
                          API Gateway /callback (HTTPS — workaround)     │
                                 │                                       │
                                 ↓                                       │
                          Lambda auth-callback ──→ Cognito token endpoint│
                                 │                                       │
                                 ↓ 302 con #id_token=...                 │
                          Browser guarda token en localStorage           │
                                 │                                       │
                                 ↓ Authorization: Bearer <token>         │
                          ALB internet-facing (HTTP:80) /api/*           │
                                 │                                       │
                                 ↓ forward al target group               │
                          Fargate chat-handler (subnets privadas)        │
                                 ↓ valida JWT in-app (JWKS Cognito)       │
                                 ↓ (sin token válido → 401)               │
                                 ├─→ DynamoDB (sesiones / vuelos / reservas)
                                 ├─→ Secrets Manager (Anthropic API key)
                                 ├─→ Anthropic API (HTTPS externo)       │
                                 └─→ Step Functions (Saga reserva)       │
                                          ├─→ payment-* Lambdas          │
                                          └─→ PublishBookingConfirmed → SNS events
                                                   │ (fan-out por filter policy)
                                                   ├─→ λ notification
                                                   └─→ λ boarding-pass-async

                          Pipeline analytics (data lake):
                          DynamoDB Stream (business)            SNS events
                                  │ CDC                             │ comportamiento
                                  ↓                                 ↓ (anything-but transaccionales)
                          λ business-analytics-emitter      suscripción Firehose
                          (clasifica PNR#/FLIGHT#/CLAIM#)            │
                                  │ PutRecord                       │
                                  ↓                                 ↓
                          ┌───────────────── 4 Kinesis Data Firehose ─────────────────┐
                          │ reservation_events  flight_events  claim_events  interaction_events
                          └────────────────────────────┬───────────────────────────────┘
                                                        ↓ batch nativo (5 MB / 60 s) + GZIP
                                              S3 jetsmart-analytics/lake/
                                                  <entidad>/dt=YYYY-MM-DD/hh=HH/*.gz
                                                              │
                                                              ↓ (sin crawler)
                                  Glue Data Catalog: 4 tablas estáticas tipadas
                                  + partition projection (dt/hh) — schema en Terraform
                                                              │
                                                              ↓ JDBC
                                                          Athena Workgroup
                                                              │
                                                              ↓ SQL (jetsmart_prod_analytics.<tabla>)
                                              Equipo Business Analytics
                                              (DBeaver / DataGrip)
```

**VPC para el cómputo en contenedor.** El core del chatbot (Fargate: chat-handler + weather-poller) corre en subnets privadas dentro de la VPC `10.0.0.0/16`, y las 9 Lambdas de negocio también (subnets `private-lambda`); solo auth-callback y cognito-trigger quedan regionales. La seguridad la garantizan: aislamiento de red (subnets privadas, SGs, NAT, VPC endpoints), IAM (LabRole con least-privilege por servicio), Cognito (autenticación), validación JWT in-app en el contenedor (autorización), encriptación en tránsito (TLS de servicios AWS) y en reposo (S3 SSE-S3, DynamoDB SSE).

---

## Funcionalidades del chatbot

| Funcionalidad | Cómo funciona |
|---|---|
| Búsqueda de vuelos | Tool use → `list_flight_dates` + `search_flights` → Query business table |
| Reserva completa | Tool use → `create_reservation` → Step Functions Saga → crea items PNR-céntricos en business + thin pointer en USER# |
| Check-in | Tool use → `check_in` → UpdateItem PNR + thin pointer |
| Boarding pass | Tool use → `get_boarding_pass` → lee item PNR/BP#01. Si no existe → mensaje "generándose" (BP es async) |
| Mis reservas | Tool use → `list_user_reservations` → Query thin pointers USER#/RESERVATION#{pnr} |
| Reclamos | Tool use → `create_claim` → PutItem CLAIM# canónico + thin pointer en USER# |
| Pasajeros guardados | Tool use → `list_saved_passengers` → Query reservas y agrupar por nombre |
| **Derivación a humano (TP4)** | Tool use → `escalate_to_human` → PutItem HANDOFF# en conversations + send a SQS → Lambda mock call center |
| **Notificación proactiva (TP4, event-driven)** | UpdateItem master row `estado_vuelo=CANCELADO` → DynamoDB Stream → Lambda `stream-emitter` (detecta la transición) → publica `flight_cancelled` al SNS central `events` → Lambda `proactive_notifications` (SNS→Lambda directo, filter `flight_cancelled`) → Query GSI ReservationsByFlight → SNS notifications a cada pasajero afectado |
| Análisis del negocio (offline) | Eventos → S3 → Athena → equipo de business analytics consulta vía SQL |
