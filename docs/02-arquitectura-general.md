# 02 — Arquitectura general

## Qué estamos construyendo

Un chatbot conversacional que replica la experiencia de la web de JetSmart, funcionando como canal end-to-end para reservar vuelos, hacer check-in, consultar el estado de vuelos, gestionar reservas y hacer reclamos.

El chatbot usa inteligencia artificial (Claude de Anthropic) para entender lenguaje natural. La tabla DynamoDB `business` cumple el rol de PSS (Passenger Service System) de la aerolínea — es la fuente única de verdad de vuelos, reservas, pasajeros y reclamos que consumirían también el sitio web, la app móvil y el call center. Para el TP se pre-carga un dataset de demo con `seed.py`.

---

## Cambios respecto al TP3 (post-feedback)

| Decisión TP3 | Decisión TP4 | Razón |
|---|---|---|
| Lambda `analytics-processor` escribe a RDS PostgreSQL via RDS Proxy | Escribe a S3 en JSON Lines particionado | El equipo de business analytics consume Athena con cliente SQL — patrón data lake estándar |
| Bastion EC2 en subnet pública para acceso a RDS | Eliminado | Sin RDS, no hay caso de uso |
| VPC con subnets públicas/privadas/datos, NAT Gateway, VPC Endpoints | Eliminada | Sin RDS ni EC2 no hay recursos persistentes que aislar — todas las Lambdas son regionales |
| Validación JWT manual con `python-jose` dentro de `chat-handler` | Cognito Authorizer en API Gateway | Mueve la validación al perímetro, libera código aplicativo, rechaza requests inválidas antes de invocar Lambda |
| `auth-callback` Lambda como bridge HTTPS | **Idéntica** (workaround invariable) | Frontend está en S3 HTTP; Cognito requiere redirect HTTPS — la Lambda detrás de API GW es el único bridge HTTPS posible |

### Cambios introducidos en TP4 (demostración final, post-presentación)

| Cambio | Razón |
|---|---|
| **DynamoDB partida en dos tablas single-design**: `jetsmart-prod-conversations` (chat) + `jetsmart-prod-business` (PSS-like) | Bounded contexts: separa estado efímero del chatbot del dominio de negocio. Failure isolation entre el canal y el core. Retention policies independientes (TTL en chat, persistencia en negocio). Prepara la arquitectura para sumar otros canales que compartirían la business table. |
| **Reservas migran a esquema PNR-céntrico** (record locator de 6 chars uppercase, à la Navitaire/Amadeus) con sub-items `SEGMENT#`, `PAX#`, `BP#` | Refleja cómo modelaría las reservas un PSS real. Habilita queries útiles ("quién está en este vuelo") via GSI2. |
| **Implementada derivación a humano**: nueva tool `escalate_to_human` en `chat_handler` → SQS `human-handoff` → Lambda `human_handoff_processor` (mock call center) | Completar la feature de TP1 que no se había implementado. SQS desacopla el chatbot del sistema del call center: si el call center está caído, el pedido queda esperando. |
| **Implementadas notificaciones proactivas (event-driven)**: trigger por **DynamoDB Stream** sobre `business` → Lambda `flight_cancellation_detector` → SNS `flight-events` → SQS `proactive-notifications` → Lambda → fan-out de emails vía SNS `notifications` | Completa la feature de TP1. Ops cambia `estado_vuelo=CANCELADO` en el master row del vuelo (consola DynamoDB o dashboard interno) y el Stream propaga. Ver justificación #28. |
| **Boarding pass async vía SQS**: el Saga ya no invoca la Lambda de boarding pass directamente — publica un mensaje a SQS `boarding-pass-generation` y la nueva Lambda `boarding_pass_async` la consume | Desacopla el path sync del Saga del trabajo de generación del PDF. Si el BP falla queda en DLQ sin afectar la reserva ya confirmada. |
| **3 nuevas SQS + DLQs** y **1 nuevo SNS topic** (`flight-events`) | Patrón consistente con el SQS de analytics: cada cola funcional tiene su DLQ con retención de 14 días y CloudWatch alarm. |
| **CloudTrail multi-region** con sink S3 dedicado, log file validation y lifecycle 90 días | Capa de auditoría de gobernanza: traza todas las API calls del management plane (IAM, cambios de config de Lambda/SNS/SQS/DynamoDB). Sin CloudWatch Logs (restricción Academy) y sin Glue/Athena (el JSON classifier default no parsea bien la estructura wrapped de CloudTrail — en producción iría con custom classifier o un SIEM). Consulta ad-hoc vía `aws s3 cp` + `jq`. Compensa la pérdida de VPC Flow Logs. |

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

### Serverless puro, sin VPC

Para tener una VPC con sentido se necesitan recursos con identidad de red persistente (EC2, RDS, ElastiCache, contenedores). Cuando la arquitectura es 100% Lambda + servicios managed regionales (DynamoDB, SNS, SQS, Step Functions, S3, Cognito), la VPC sólo agrega complejidad sin beneficio.

Trade-off: perdemos la visibilidad de **VPC Flow Logs** a nivel de red. Ganamos: cold start mínimo, sin endpoints que mantener, sin SGs, sin route tables, sin NAT Gateway. La auditoría se mantiene vía **CloudTrail** (multi-region, todas las API calls del management plane sinkeadas a S3 — consulta ad-hoc vía CLI) y **X-Ray** (tracing distribuido).

### Step Functions con patrón Saga para reservas

El flujo de reserva y pago es una **transacción distribuida** — múltiples pasos (reservar asiento, crear booking, cobrar, confirmar) que deben ser atómicos: si falla cualquier paso intermedio, todo lo anterior debe deshacerse.

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
- Eventos de negocio (mensajes de chat, compras, check-ins, reclamos) que llegan via SNS→SQS
- `analytics-processor` Lambda los escribe como JSON Lines particionado por `dt=YYYY-MM-DD/hh=HH`
- Glue Crawler descubre el schema automáticamente (corre cada hora)
- El equipo consume Athena vía cliente SQL externo (DBeaver / DataGrip con driver JDBC)

**Por qué no RDS:** OLTP postgres es la herramienta equivocada para analítica. Carga el primario con queries pesadas, no escala por costo, y el patrón estándar 2026 para business analytics es data lake. Athena cobra ~5 USD/TB escaneado; con el volumen de eventos del chatbot el costo es despreciable.

**Por qué no QuickSight:** AWS Academy no lo habilita con LabRole. El equipo de analytics queda con cliente SQL (DBeaver/DataGrip) — funcional pero más manual.

### Cognito Authorizer en API Gateway

`chat-handler` ya **no** valida JWT internamente. El API Gateway lo hace con un **Cognito Authorizer** (recurso `aws_api_gateway_authorizer` tipo `COGNITO_USER_POOLS`). Si el token es inválido o falta, API GW devuelve `401` antes de invocar Lambda.

**Ganancia:** menos código aplicativo, validación canónica de AWS, rechazo en el perímetro sin gastar invocación de Lambda.

**Excepción documentada:** el API GW de `auth-callback` queda con `authorization = "NONE"` porque es Cognito quien redirige a ese endpoint con el `code` en query string — no manda Authorization header. Es parte del **workaround documentado en** `teoria/notas-de-clase/workaround-cognito.md`.

### Workaround del redirect HTTPS

El frontend está en S3 HTTP. Cognito sólo redirige a URLs HTTPS. La Lambda `auth-callback` detrás de API Gateway (HTTPS estable) es el **bridge** que:

1. Recibe `?code=...` desde Cognito.
2. Intercambia el code por tokens contra el token endpoint de Cognito.
3. Hace un `302` al frontend con `#id_token=...`.

El frontend lee el token del fragment y lo guarda en localStorage. Después, todas las requests al chatbot llevan `Authorization: Bearer <token>` que API Gateway valida con el Cognito Authorizer.

### Pipeline de analytics: SNS → SQS → Lambda → S3

Los eventos del chat (mensajes, compras, check-ins, reclamos) se publican en un SNS topic. SQS suscribe ese topic — actúa como buffer que suaviza picos de tráfico. La Lambda `analytics-processor` consume la cola de a 10 mensajes por invocación y los escribe como JSON Lines en S3.

Si S3 falla (caso muy raro), el mensaje vuelve a SQS para reintento. Después de 3 intentos va a la DLQ `analytics-dlq`.

---

## Mapa de componentes

| # | Componente | Categoría | Rol |
|---|---|---|---|
| 1 | S3 — frontend | Storage / Edge | Archivos estáticos del sitio web (HTML/CSS/JS). HTTP. |
| 2 | S3 — boarding-passes | Storage | Boarding passes generados por la Lambda `boarding-pass-async`. (Renombrado desde `assets` en TP4: el system prompt se movió a una Lambda Layer.) |
| 3 | S3 — analytics | Storage / Data lake | Eventos crudos en JSON Lines, particionado por `dt=YYYY-MM-DD/hh=HH`. |
| 3b | S3 — backups | Storage / Backup | Exports diarios de DynamoDB (Hive-path `dynamodb/YYYY-MM-DD/...`). Lifecycle: 90d STANDARD → GLACIER → expira a 365d. |
| 4 | Cognito User Pool | Auth | Registro y login con Hosted UI. |
| 5 | Cognito Groups | Auth | `users` (chatbot). |
| 6 | API Gateway — chatbot | Cómputo / Edge | Endpoint HTTPS `/api/*` → invoca chat-handler. **Protegido por Cognito Authorizer.** |
| 7 | API Gateway — auth | Cómputo / Edge | Endpoint HTTPS `/callback` → invoca auth-callback (bridge del workaround). |
| 8 | Lambda — chat-handler | Cómputo | Chat con tool use, historial, inicio de reserva. Lee claims del Cognito Authorizer. |
| 9 | Lambda — payment-reserve-flight | Cómputo | Paso 1 Saga: bloquea asientos (decremento atómico DynamoDB). |
| 10 | Lambda — payment-reserve-booking | Cómputo | Paso 2 Saga: crea reserva PENDIENTE. |
| 11 | Lambda — payment-collect | Cómputo | Paso 3 Saga: procesa el cobro. |
| 12 | Lambda — payment-confirm | Cómputo | Paso 4 Saga: confirma reserva; publica evento. |
| 13 | Lambda — payment-refund | Cómputo | Compensación: revierte el cobro. |
| 14 | Lambda — payment-cancel | Cómputo | Compensación: cancela la reserva. |
| 15 | Lambda — payment-release-flight | Cómputo | Compensación: libera los asientos. |
| 16 | Lambda — boarding-pass-async | Cómputo | TP4: consume SQS boarding-pass-generation, genera BP en S3 y graba bp_url en PNR. |
| 17 | Lambda — notification | Cómputo | PostBookingActions + error path: notifica al usuario. |
| 18 | Lambda — analytics-processor | Cómputo | Consume SQS, escribe S3 JSON Lines. |
| 19 | Lambda — auth-callback | Cómputo | Bridge HTTPS del workaround. Intercambia code por tokens. |
| 20 | Lambda — cognito-trigger | Cómputo | Post-registro: asigna grupo `users`. |
| 20b | Lambda — backup-dynamodb | Cómputo | Disparada por EventBridge cron diario. Exporta sólo la tabla `business` al bucket de backups (la `conversations` es efímera por TTL y queda cubierta por PITR). |
| 20c | **Lambda — human-handoff-processor (TP4)** | Cómputo | Consume SQS human-handoff, mock POST al call center, actualiza ticket HANDOFF# y notifica al usuario por email. |
| 20d | **Lambda — proactive-notifications (TP4)** | Cómputo | Consume SQS proactive-notifications, Query a GSI2 ReservationsByFlight, fan-out de emails vía SNS notifications. |
| 21 | Step Functions | Orquestación | State machine del patrón Saga. TP4: PostBookingActions Branch B ahora publica a SQS en lugar de invocar Lambda directo. |
| 21b | EventBridge Rule — backup-dynamodb-daily | Orquestación / Scheduling | Cron `0 3 * * ? *` (03:00 UTC). Único trigger basado en tiempo del sistema. |
| 22 | SNS — events | Mensajería | Eventos del chat (mensajes, compras, handoffs) → fan-out a SQS analytics. |
| 23 | SNS — notifications | Mensajería | Notificaciones al usuario (booking confirmado / fallido / handoff ack / cancelación de vuelo). |
| 23b | **SNS — flight-events (TP4)** | Mensajería | Publicado por `flight_cancellation_detector` (Lambda triggered por DynamoDB Stream) cuando un master row pasa a estado_vuelo=CANCELADO. → SQS proactive-notifications. |
| 23c | **Lambda — flight-cancellation-detector (TP4)** | Cómputo | Consume DynamoDB Stream de `business`. Filter criteria server-side (eventName=MODIFY, NewImage.estado_vuelo=CANCELADO). Filtra master rows FLIGHT# + valida transición real (no re-publica). Publica al SNS flight-events. |
| 24 | SQS — analytics | Mensajería | Buffer de eventos hacia analytics-processor. |
| 25 | SQS — analytics-dlq | Mensajería | DLQ: eventos que fallaron 3 veces de escritura S3. |
| 26 | SQS — booking-failed-dlq | Mensajería | DLQ: flujos Saga que no pudieron completarse. |
| 26a | **SQS — human-handoff + DLQ (TP4)** | Mensajería | Chat → call center mock. DLQ retiene 14d para reintento manual. |
| 26b | **SQS — proactive-notifications + DLQ (TP4)** | Mensajería | SNS flight-events → fan-out de emails. DLQ con CloudWatch alarm. |
| 26c | **SQS — boarding-pass-generation + DLQ (TP4)** | Mensajería | Saga → boarding pass async. DLQ con alarm. |
| 27a | **DynamoDB — conversations (TP4)** | Base de datos | Single Table Design: sesiones, mensajes, perfil chat-scoped, handoffs. TTL en todos los items. |
| 27b | **DynamoDB — business (TP4, PSS-like)** | Base de datos | Single Table Design PNR-céntrico: FLIGHT#, PNR#/SEGMENT#/PAX#/BP#, PASSENGER#, CLAIM#. 2 GSIs (ReservationsByFlight, ReservationsByPassenger). Stream habilitado (NEW_AND_OLD_IMAGES) para flight_cancellation_detector. |
| 28 | Glue Catalog Database | Catálogo | Schema descubierto del bucket de eventos. |
| 29 | Glue Crawler | Catálogo | Corre cada hora, descubre nuevas particiones y campos. |
| 30 | Athena Workgroup | Consultas | Endpoint SQL para el equipo de business analytics. |
| 31 | Secrets Manager | Seguridad | API key Anthropic. |
| 32 | Lambda Layer — anthropic | Cómputo | SDK de Anthropic compilado para Python 3.12. |
| 33 | IAM — LabRole | Seguridad | Rol preexistente de AWS Academy — compartido por todas las Lambdas. |
| 34 | CloudWatch (17 log groups + 4 alarms) | Observabilidad | Logs de las 16 Lambdas + 1 log group del state machine de Step Functions, todos con retención 30d. Alarms: analytics-processor errors + 3 DLQ depth alarms (human-handoff, proactive-notifications, boarding-pass-generation). |

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
                          API Gateway /api/* (HTTPS)                     │
                                 │                                       │
                                 ↓ Cognito Authorizer valida JWT         │
                                 ↓ (sin token válido → 401, no invoca)   │
                          Lambda chat-handler                            │
                                 ├─→ DynamoDB (sesiones / vuelos / reservas)
                                 ├─→ Secrets Manager (Anthropic API key)
                                 ├─→ Anthropic API (HTTPS externo)       │
                                 └─→ Step Functions (Saga reserva)       │
                                          ├─→ payment-* Lambdas          │
                                          ├─→ SQS → boarding-pass-async  │
                                          └─→ notification               │

                          Pipeline analytics:
                          chat-handler          → SNS events ─┐
                          payment-confirm       → SNS events ─┤
                          proactive-notifications → SNS events ─┘
                                                              │
                                                              ↓ fan-out
                                                          SQS analytics (+ DLQ)
                                                              │
                                                              ↓ batch 10
                                                       Lambda analytics-processor
                                                              │
                                                              ↓ put_object
                                              S3 jetsmart-analytics/events/
                                                  dt=YYYY-MM-DD/hh=HH/<uuid>.jsonl
                                                              │
                                                              ↓ corre cada 1h
                                                          Glue Crawler
                                                              │
                                                              ↓ catálogo
                                                  Glue Data Catalog (database: events)
                                                              │
                                                              ↓ JDBC
                                                          Athena Workgroup
                                                              │
                                                              ↓ SQL
                                              Equipo Business Analytics
                                              (DBeaver / DataGrip)

                          Backups de DynamoDB:
                          EventBridge cron(0 3 * * ? *)
                                  │ invoke
                                  ↓
                          Lambda backup-dynamodb
                                  │ ExportTableToPointInTime (async)
                                  ↓
                          DynamoDB service (lee PITR)
                                  │ put_object
                                  ↓
                          S3 jetsmart-backups/dynamodb/YYYY-MM-DD/...
                                  │ lifecycle
                                  ↓
                          90d → GLACIER → 365d expira
```

**Sin VPC.** Toda la seguridad la garantizan: IAM (LabRole con least-privilege por servicio), Cognito (autenticación), Cognito Authorizer (autorización a nivel de API), encriptación en tránsito (HTTPS de API Gateway, TLS de servicios AWS), encriptación en reposo (S3 SSE-S3, DynamoDB SSE).

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
| **Notificación proactiva (TP4, event-driven)** | UpdateItem master row `estado_vuelo=CANCELADO` → DynamoDB Stream → Lambda detector → SNS flight-events → SQS → Lambda proactive → Query GSI2 → SNS notifications a cada pasajero afectado |
| Análisis del negocio (offline) | Eventos → S3 → Athena → equipo de business analytics consulta vía SQL |
