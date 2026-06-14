# 02 — Arquitectura general

## Qué estamos construyendo

Un chatbot conversacional que replica la experiencia de la web de JetSmart, funcionando como canal end-to-end para reservar vuelos, hacer check-in, consultar el estado de vuelos, gestionar reservas y hacer reclamos.

El chatbot usa inteligencia artificial (Claude de Anthropic) para entender lenguaje natural. Los datos de vuelos son simulados (mock data) ya que la API real de JetSmart no es pública.

---

## Cambios respecto al TP3 (post-feedback)

| Decisión TP3 | Decisión TP4 | Razón |
|---|---|---|
| Lambda `analytics-processor` escribe a RDS PostgreSQL via RDS Proxy | Escribe a S3 en JSON Lines particionado | El equipo de business analytics consume Athena con cliente SQL — patrón data lake estándar |
| Bastion EC2 en subnet pública para acceso a RDS | Eliminado | Sin RDS, no hay caso de uso |
| VPC con subnets públicas/privadas/datos, NAT Gateway, VPC Endpoints | Eliminada | Sin RDS ni EC2 no hay recursos persistentes que aislar — todas las Lambdas son regionales |
| Validación JWT manual con `python-jose` dentro de `chat-handler` | Cognito Authorizer en API Gateway | Mueve la validación al perímetro, libera código aplicativo, rechaza requests inválidas antes de invocar Lambda |
| `auth-callback` Lambda como bridge HTTPS | **Idéntica** (workaround invariable) | Frontend está en S3 HTTP; Cognito requiere redirect HTTPS — la Lambda detrás de API GW es el único bridge HTTPS posible |

---

## Decisiones de arquitectura

### Serverless puro, sin VPC

Para tener una VPC con sentido se necesitan recursos con identidad de red persistente (EC2, RDS, ElastiCache, contenedores). Cuando la arquitectura es 100% Lambda + servicios managed regionales (DynamoDB, SNS, SQS, Step Functions, S3, Cognito), la VPC sólo agrega complejidad sin beneficio.

Trade-off: perdemos la visibilidad de **VPC Flow Logs** a nivel de red. Ganamos: cold start mínimo, sin endpoints que mantener, sin SGs, sin route tables, sin NAT Gateway. La auditoría se mantiene vía **CloudTrail** (todas las API calls de Lambda a servicios AWS) y **X-Ray** (tracing distribuido).

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

### Mock data en lugar de API real de JetSmart

En producción, el backend se conectaría a la API interna de JetSmart para obtener disponibilidad de vuelos. Esa API no es pública. En este TP los datos (rutas, precios, fechas) se cargan como mock data en DynamoDB con esquema `PK=FLIGHT#{origen}#{destino}` / `SK=DATE#{fecha}`.

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
| 2 | S3 — assets | Storage | Boarding passes generados y system prompt del chatbot. |
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
| 16 | Lambda — boarding-pass | Cómputo | PostBookingActions: genera boarding pass en DynamoDB. |
| 17 | Lambda — notification | Cómputo | PostBookingActions + error path: notifica al usuario. |
| 18 | Lambda — analytics-processor | Cómputo | Consume SQS, escribe S3 JSON Lines. |
| 19 | Lambda — auth-callback | Cómputo | Bridge HTTPS del workaround. Intercambia code por tokens. |
| 20 | Lambda — cognito-trigger | Cómputo | Post-registro: asigna grupo `users`. |
| 20b | Lambda — backup-dynamodb | Cómputo | Disparada por EventBridge cron diario. Llama a `dynamodb:ExportTableToPointInTime` contra el bucket de backups. |
| 21 | Step Functions | Orquestación | State machine del patrón Saga (reserva y pago). |
| 21b | EventBridge Rule — backup-dynamodb-daily | Orquestación / Scheduling | Cron `0 3 * * ? *` (03:00 UTC). Único trigger basado en tiempo del sistema. |
| 22 | SNS — events | Mensajería | Eventos del chat (mensajes, compras) → fan-out a SQS analytics. |
| 23 | SNS — notifications | Mensajería | Notificaciones al usuario (booking confirmado / fallido). |
| 24 | SQS — analytics | Mensajería | Buffer de eventos hacia analytics-processor. |
| 25 | SQS — analytics-dlq | Mensajería | DLQ: eventos que fallaron 3 veces de escritura S3. |
| 26 | SQS — booking-failed-dlq | Mensajería | DLQ: flujos Saga que no pudieron completarse. |
| 27 | DynamoDB | Base de datos | Single Table Design: sesiones, reservas, vuelos mock. |
| 28 | Glue Catalog Database | Catálogo | Schema descubierto del bucket de eventos. |
| 29 | Glue Crawler | Catálogo | Corre cada hora, descubre nuevas particiones y campos. |
| 30 | Athena Workgroup | Consultas | Endpoint SQL para el equipo de business analytics. |
| 31 | Secrets Manager | Seguridad | API key Anthropic. |
| 32 | Lambda Layer — anthropic | Cómputo | SDK de Anthropic compilado para Python 3.12. |
| 33 | IAM — LabRole | Seguridad | Rol preexistente de AWS Academy — compartido por todas las Lambdas. |
| 34 | CloudWatch (14 log groups) | Observabilidad | Logs de todas las Lambdas con retención de 30 días. 13 vía `for_each` + 1 para `backup-dynamodb`. |

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
                                          ├─→ boarding-pass              │
                                          └─→ notification               │

                          Pipeline analytics:
                          chat-handler → SNS events ─┐
                          payment-confirm → SNS events ─┤
                          payment-cancel/refund → SNS events ─┘
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
| Búsqueda de vuelos | Tool use → `list_flight_dates` + `search_flights` → consulta DynamoDB |
| Reserva completa | Tool use → `create_reservation` → Step Functions Saga |
| Check-in | Tool use → `check_in` → UpdateItem en DynamoDB |
| Boarding pass | Tool use → `get_boarding_pass` → consulta DynamoDB |
| Mis reservas | Tool use → `list_user_reservations` → Query DynamoDB por usuario |
| Reclamos | Tool use → `create_claim` → PutItem en DynamoDB |
| Pasajeros guardados | Tool use → `list_saved_passengers` → Query DynamoDB |
| Análisis del negocio (offline) | Eventos → S3 → Athena → equipo de business analytics consulta vía SQL |
