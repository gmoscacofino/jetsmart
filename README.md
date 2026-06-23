# JetSmart Chatbot — TP4 (Demostración final)
### Cloud Computing — 2026Q1 — ITBA

## Introducción

JetSmart Chatbot es un asistente conversacional desplegado en AWS con Terraform que replica la experiencia de compra de JetSmart. El usuario puede reservar vuelos, hacer check-in y gestionar reservas en lenguaje natural usando Claude (Anthropic).

**Esta entrega incorpora la re-arquitectura posterior al feedback del TP3 (defensa 17/06):** el core del chatbot (`chat-handler`) dejó de ser una Lambda detrás de API Gateway y pasó a correr como **servicio FastAPI nativo en ECS Fargate**, en subnets privadas de una **VPC**, detrás de un **Application Load Balancer (ALB)**. El JWT de Cognito se valida **in-app dentro del contenedor** (contra el JWKS del User Pool), no con un Cognito Authorizer. Para business analytics se mantiene el **data lake** (S3 + Glue + Athena). El bastion EC2 y RDS siguen sin existir.

## Diagrama
<img src="docs/Jetsmart - Diagrama.png" alt="Diagrama de Arquitectura" width="110%">

> Tocar sobre la imagen del diagrama para ampliarla y visualizarla mejor.

## Cambios respecto al TP3

| Punto del feedback de Faustino | Cómo se resolvió en TP4 |
|---|---|
| Bastion EC2 en subnet pública | **Eliminado.** Sin RDS no hay caso de uso para un bastion. |
| El core del chatbot fuera de la VPC | **Resuelto:** el `chat-handler` ahora vive **dentro de la VPC** como servicio Fargate en subnets privadas (`private-fargate`), detrás del ALB. El cómputo del chatbot ya no es una Lambda suelta. |
| RDS Proxy mal representado en el diagrama | **Eliminado junto con RDS.** Para business analytics el patrón correcto es un data lake (S3 + Athena), no un OLTP postgres. |
| "Sin tener en cuenta la justificación" | Ver `docs/justificaciones.md` — fundamentación escrita de cada decisión arquitectónica con alternativas y trade-offs. |

**Sobre la validación del JWT:** se hace **in-app dentro del contenedor** del `chat-handler` (`server.py` verifica firma RS256 contra el JWKS del User Pool, issuer y exp), no en API Gateway ni con un Cognito Authorizer. Reemplaza el código manual con `python-jose` que tenía la Lambda `chat-handler` en el TP3.

## Cambios introducidos en TP4 (demostración final)

Para llegar al demo presencial del 17/06 incorporamos cinco cambios que cierran las funcionalidades pendientes del TP1 y refinan la separación de dominios:

| Cambio | Detalle |
|---|---|
| **DynamoDB partida en dos tablas single-design** | `jetsmart-prod-conversations` (chatbot state) + `jetsmart-prod-business` (PSS-like). Ver `docs/07-data-layer.md` y justificación #13. |
| **Reservas migran a PNR-céntrico** | PNR de 6 chars alfanumérico (à la Navitaire). Sub-items `SEGMENT#`, `PAX#`, `BP#`, `EXTRA#`. 1 GSI en business (`ReservationsByFlight`). |
| **Derivación a humano (TP1 feature)** | Nueva tool `escalate_to_human` en `chat_handler` → SQS `human-handoff` → Lambda `human_handoff_processor` (mock call center) → SNS notifications. Ver Flujo 7 en `docs/04-flujos.md`. |
| **Notificaciones proactivas event-driven (TP1 feature)** | weather-poller (Fargate) u Ops setea `estado_vuelo=CANCELADO` en el master row `FLIGHT#` de la business table → DynamoDB Stream → Lambda `stream-emitter` detecta la transición y publica `event_type=flight_cancelled` al SNS central `events` → Lambda `proactive_notifications` (suscripción SNS→Lambda directo, filter `flight_cancelled`) hace Query al GSI `ReservationsByFlight`, marca cada PNR `AFFECTED_BY_CANCELLATION` y fan-out de emails vía SNS `notifications`. (`refund_trigger` también escucha `flight_cancelled`.) Ver Flujo 8 en `docs/04-flujos.md` y justificación #28. |
| **Boarding pass async event-driven** | La Booking Saga ya no invoca Lambda directo — su estado terminal de éxito publica `event_type=booking_confirmed` al SNS central `events`; la Lambda `boarding_pass_async` (suscripción SNS→Lambda directo, filter `booking_confirmed`) genera el BP y graba `bp_url` en el PNR. Fire-and-forget. No hay SQS `boarding-pass-generation`. |
| **Fan-out por SNS + DLQs de paths de plata + CloudWatch alarms** | El fan-out post-booking y de cancelación es SNS→Lambda directo con filter policies sobre un único topic `events`; la única SQS funcional es `human-handoff` (protege un downstream no elástico). DLQs reales: `human-handoff-dlq` (redrive de la cola) + `booking_failed_dlq` + `refund_failures_dlq` (sinks de los Catch de las Step Functions, revisión manual). Ver `terraform/infra/messaging.tf` y `cloudwatch.tf`. |

Total Lambdas desplegadas: **18** (incluye las 7 de la Booking Saga y las 2 de la Refund Saga, expandidas con `for_each`; más `business-analytics-emitter`, `stream-emitter`, `boarding-pass-async`, `human-handoff-processor`, `proactive-notifications`, `notification`, `refund-trigger`, `auth-callback` y `cognito-trigger`).

## Requerimientos

### Cuenta y credenciales AWS

| Requisito | Detalle |
|-----------|---------|
| Cuenta AWS | Permisos para ECS Fargate, ECR, ELB (ALB), VPC, Lambda, S3, DynamoDB, Cognito, Step Functions, SNS, SQS, API Gateway, Glue, Athena. |
| Rol **LabRole** | Pre-existente en AWS Academy (`data.aws_iam_role.lab_role` en Terraform). |
| AWS CLI v2 | Credenciales en `~/.aws/credentials` o variables de entorno (para usar Athena localmente). |

## Instrucciones de ejecución

El flujo es via **GitHub Actions**, no requiere instalar Terraform ni AWS CLI localmente.

### Paso 1 — Configurar secrets en GitHub

Ir a **Settings → Secrets and variables → Actions → New repository secret**.

| Name | Secret |
|------|--------|
| `AWS_ACCESS_KEY_ID` | valor de `aws_access_key_id` |
| `AWS_SECRET_ACCESS_KEY` | valor de `aws_secret_access_key` |
| `AWS_SESSION_TOKEN` | valor de `aws_session_token` |
| `STATE_BUCKET_SUFFIX` | sufijo único para el bucket de estado, solo minúsculas, números y guiones (ej. `grupo7-2026`). Los nombres de bucket S3 son globales: si el job `backend` falla con `BucketAlreadyExists`, cambiar este sufijo por uno diferente (ej. `grupo7-2026b`). |
| `TF_VAR_ANTHROPIC_API_KEY` | API key de Anthropic (entregada al docente por separado). |

> **Nota TP4:** ya no se necesita `TF_VAR_RDS_PASSWORD` porque eliminamos RDS.

### Paso 2 — Crear el backend (primera vez)

Ir a **Actions → Terraform → Run workflow**, seleccionar **`backend`** y ejecutar.

Crea el bucket S3 `jetsmart-terraform-state-<STATE_BUCKET_SUFFIX>` que almacena el state de Terraform.

### Paso 3 — Planificar la infraestructura

Ir a **Actions → Terraform → Run workflow**, seleccionar **`plan`** y ejecutar.

Muestra todos los recursos que se van a crear sin modificar nada.

### Paso 4 — Aplicar la infraestructura

Ir a **Actions → Terraform → Run workflow**, seleccionar **`apply`** y ejecutar.

> El apply incluye la creación de la VPC, los VPC endpoints, el NAT Gateway, el ALB y el levantamiento de las tasks Fargate (pull de imagen desde ECR); no hay RDS ni RDS Proxy.

Al finalizar, Terraform sube automáticamente el frontend a S3 y carga los vuelos mock en DynamoDB.

### Paso 5 — Ver los outputs del deploy

Al terminar el apply, en el job **Apply → Summary** aparecen las URLs:

| Recurso | Comportamiento esperado |
|---------|------------------------|
| **Frontend** | Abrirla en el browser muestra la app con el botón "Iniciar sesión". |
| **Chatbot (ALB)** | `GET /health` → `200` (sin auth). Las rutas `/api/*` sin token → `401 Unauthorized` validado **in-app en el contenedor** Fargate. Con JWT válido → respuesta del chatbot. |
| **Cognito Hosted UI** | Formulario de login/signup. |
| **Auth Callback** | Redirige al frontend con el token (`#id_token=...`). |

### Destruir la infraestructura

Ir a **Actions → Terraform → Run workflow**, seleccionar **`destroy`** y ejecutar.

## Verificación

### Crear cuenta y usar el chatbot

1. Abrir la URL del frontend.
2. **Iniciar sesión** → redirige a la Hosted UI de Cognito.
3. Crear cuenta con email + contraseña (mínimo 8 caracteres, una mayúscula y un número).
4. Confirmar el email con el código recibido.
5. El browser vuelve al frontend con la sesión activa.

### Smoke tests TP4

**1) Derivación a humano:**
```
En el chat: "quiero hablar con un humano"
→ Claude invoca tool escalate_to_human
→ Respuesta: "Tu pedido fue derivado al equipo de soporte humano (ticket HO-XXXXXXXX)"
→ Verificar:
   - Item HANDOFF# en jetsmart-prod-conversations
   - CloudWatch logs de /aws/lambda/jetsmart-prod-human-handoff-processor
     mostrando "MOCK POST https://mock.callcenter.internal/tickets"
   - Email recibido en la subscription de SNS notifications
```

**2) Notificaciones proactivas (event-driven):**
```bash
# Cancelar el vuelo desde la consola DynamoDB o vía CLI: UpdateItem sobre el
# master row FLIGHT# poniendo estado_vuelo=CANCELADO. El DynamoDB Stream
# dispara el flujo completo automáticamente.
aws dynamodb update-item \
  --table-name jetsmart-prod-business --region us-east-1 \
  --key '{"PK":{"S":"FLIGHT#AEP#MDZ"},"SK":{"S":"DATE#2026-06-22#FLIGHT#JA203"}}' \
  --update-expression "SET estado_vuelo = :s, cancellation_reason = :r" \
  --expression-attribute-values '{":s":{"S":"CANCELADO"},":r":{"S":"mal tiempo"}}'

→ Verificar:
   - CloudWatch logs de /aws/lambda/jetsmart-prod-stream-emitter
     ("Detected cancellation transition: JA203 ...")
   - CloudWatch logs de /aws/lambda/jetsmart-prod-proactive-notifications
     mostrando "Query GSI ReservationsByFlight ... N PNRs afectados"
   - Emails recibidos por pasajeros con reservas en ese vuelo
```

**3) Boarding pass async:**
```
En el chat: reservar vuelo → check-in → "dame mi boarding pass"
→ Si BP recién se generó: URL devuelta
→ Si BP todavía se está generando: "Tu boarding pass se está generando, intentá en unos segundos"
→ Verificar item PNR#{pnr}/BP#01 en jetsmart-prod-business
```

### Chatbot con Claude

Usa **Claude Haiku** con tool use sobre DynamoDB. Las respuestas son libres en lenguaje natural; el flujo de compra ejecuta Step Functions (Saga pattern con compensaciones).

Carga automática de **~660 vuelos mock** en DynamoDB (`scripts/seed_flights.py`):

| Ruta | Vuelo mañana | Vuelo tarde | Precio desde |
|------|-------------|-------------|-------------|
| AEP → SCL | JA401 08:15 | JA403 18:00 | USD 89 |
| SCL → AEP | JA402 11:30 | JA404 21:00 | USD 89 |
| AEP → MDZ | JA201 07:30 | JA203 17:00 | USD 49 |
| MDZ → AEP | JA202 09:30 | JA204 19:00 | USD 49 |
| AEP → COR | JA101 06:45 | JA103 20:00 | USD 39 |
| COR → AEP | JA102 09:00 | JA104 22:00 | USD 39 |
| AEP → IGR | JA301 07:00 | — | USD 69 |
| SCL → IGR | JA601 09:00 | — | USD 119 |
| SCL → ANF | JA501 07:00 | — | USD 55 |
| SCL → COR | JA701 10:00 | — | USD 129 |

Flujo de prueba:

1. **Buscar vuelos** → `"¿Qué vuelos hay de Buenos Aires a Mendoza?"`.
2. **Reservar** → completar datos del pasajero, confirmar con `"sí, confirmar"`.
3. **Consultar reserva** → `"¿Cuál es el estado de mi reserva?"`.
4. **Check-in** → las 24 horas previas al vuelo.
5. **Reclamos** → `"Quiero reportar un problema con mi vuelo"`.

---

### Capa de analytics — Athena para el equipo de business analytics

Los cambios de negocio (reservas, vuelos, reclamos) viajan por el **Stream de DynamoDB** y la Lambda `business-analytics-emitter` (CDC) hace `PutRecord` a **Kinesis Data Firehose**, que batchea nativo (sin Lambda de transformación) y escribe en S3 como JSON Lines particionado por fecha. Hay un Firehose por entidad y cada uno escribe bajo su propio prefijo:

```
s3://jetsmart-prod-<account-id>-analytics/lake/{reservation_events|flight_events|claim_events|interaction_events}/dt=YYYY-MM-DD/hh=HH/<uuid>
```

(`interaction_events` lo alimenta el SNS central `events` con los eventos semánticos del chat; el resto sale del CDC de business.)

El **Glue Data Catalog** define **4 tablas tipadas** (una por entidad), con **partition projection** sobre `dt`/`hh` — **no hay Glue Crawler**, las particiones se proyectan en consulta. **Athena** expone los datos vía SQL al equipo de business analytics.

**Acceso desde DBeaver / DataGrip:**

1. Driver: descargar [Athena JDBC driver](https://docs.aws.amazon.com/athena/latest/ug/connect-with-jdbc.html).
2. Connection URL: `jdbc:awsathena://AwsRegion=us-east-1`.
3. Workgroup: `jetsmart-prod-analytics` (output del `terraform apply`).
4. Database: `jetsmart_prod_analytics`.
5. Tablas: `reservation_events`, `flight_events`, `claim_events`, `interaction_events`.

**Consultas de ejemplo:**

```sql
-- Eventos de reserva por tipo, últimos 7 días
SELECT event_type, COUNT(*) AS cantidad
FROM jetsmart_prod_analytics.reservation_events
WHERE dt >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY event_type
ORDER BY cantidad DESC;

-- Últimas 20 reservas confirmadas
SELECT event_ts, pnr, total, pax_count, user_id, vuelo
FROM jetsmart_prod_analytics.reservation_events
WHERE event_type = 'booking_confirmed'
ORDER BY event_ts DESC
LIMIT 20;

-- Interacciones del chat por tipo, últimos 30 días
SELECT event_type, COUNT(*) AS cantidad
FROM jetsmart_prod_analytics.interaction_events
WHERE dt >= date_format(current_date - interval '30' day, '%Y-%m-%d')
GROUP BY event_type
ORDER BY cantidad DESC
LIMIT 10;
```

> Las particiones (`dt`/`hh`) se resuelven con **partition projection**: no hace falta refrescarlas manualmente ni correr un crawler, Athena las proyecta en cada consulta.

## Pipeline de GitHub Actions

| Job | Cuándo corre | Credenciales AWS | Qué hace |
|-----|--------------|------------------|----------|
| `validate` | En cada `push` y en cada **PR** | No necesita | `init -backend=false`, `validate`, `fmt -check`, `terraform test` |
| `backend` | `workflow_dispatch` → `backend` | Sí | Crea el bucket S3 de estado (una sola vez por cuenta) |
| `deploy` | `workflow_dispatch` → `plan` o `apply` | Sí | `init` con backend S3, `plan`/`apply`, sube frontend a S3, seed de vuelos en DynamoDB, imprime URLs |
| `destroy` | `workflow_dispatch` → `destroy` | Sí | `init` con backend S3, `destroy -auto-approve` |

## Terraform

### Estado remoto

| Recurso | Nombre |
|---------|--------|
| Bucket S3 | `jetsmart-terraform-state-<suffix>` |
| Locking nativo | `.tflock` en S3 (Terraform ≥ 1.10, sin DynamoDB) |
| Clave del state | `infra/terraform.tfstate` |

### Módulos

| Módulo | Tipo | Descripción |
|--------|------|-------------|
| `modules/auth` | Custom | Cognito User Pool, grupos, Hosted UI, Lambda auth-callback, API Gateway callback/logout (bridge OAuth) |

> El `chat-handler` ya no es un módulo Lambda: corre como servicio ECS Fargate detrás de un ALB con Auto Scaling, definido en `ecs.tf` + `alb.tf` (no como módulo reutilizable).

### Funciones de Terraform

| Función | Archivo | Uso |
|---------|---------|-----|
| `jsonencode()` | `ecs.tf`, `messaging.tf`, `secrets.tf`, `step_functions.tf`, `storage.tf`, `analytics.tf` | Genera JSON para container definitions de Fargate, políticas, secretos y la definición del state machine |
| `toset()` | `networking.tf` (interface VPC endpoints), `messaging.tf`, `modules/auth/main.tf` | Convierte listas/maps en set para `for_each` |
| `filebase64sha256()` | `layers.tf` | Hash del ZIP del Lambda Layer Anthropic para detectar cambios |
| `filemd5()` | `storage.tf` | Etag del system prompt para forzar actualización en S3 |
| `replace()` | `analytics.tf` | Normaliza el name_prefix para el Glue Catalog (acepta solo `[a-z0-9_]`) |

### Meta-argumentos

| Meta-argumento | Dónde | Por qué |
|----------------|-------|---------|
| `for_each` | `cloudwatch.tf`, `lambda.tf` (Saga payment/refund), `networking.tf` (interface VPC endpoints), `firehose.tf`, `messaging.tf`, `modules/auth/main.tf` (grupos Cognito) | Crea múltiples recursos desde un set/map sin repetir el bloque |
| `depends_on` | `ecs.tf`, `networking.tf`, `main.tf`, `storage.tf` | Garantiza orden de creación: el servicio Fargate arranca después del listener del ALB, los VPC endpoints y el NAT Gateway (para registrar targets y pullear la imagen de ECR) |
| `lifecycle { ignore_changes }` | `ecs.tf` | El `desired_count` del `chat-handler` lo maneja el Auto Scaling en runtime — Terraform no pelea contra él |
| `lifecycle { create_before_destroy }` | `lambda.tf`, `modules/auth/main.tf` | Zero downtime al actualizar Lambdas y el API Gateway de auth |

## Documentación adicional

- [`docs/02-arquitectura-general.md`](docs/02-arquitectura-general.md) — decisiones de arquitectura.
- [`docs/03-networking.md`](docs/03-networking.md) — la VPC, subnets, NAT Gateway y VPC endpoints que alojan el cómputo Fargate.
- [`docs/05-componentes-detalle.md`](docs/05-componentes-detalle.md) — cada componente en detalle.
- [`docs/07-data-layer.md`](docs/07-data-layer.md) — DynamoDB Single Table Design + capa de analytics S3/Athena.
- [`docs/justificaciones.md`](docs/justificaciones.md) — fundamentación escrita de cada decisión (cheat sheet de presentación).
