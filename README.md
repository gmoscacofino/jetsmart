# JetSmart Chatbot — TP4 (Demostración final)
### Cloud Computing — 2026Q1 — ITBA

## Introducción

JetSmart Chatbot es un asistente conversacional desplegado en AWS con Terraform que replica la experiencia de compra de JetSmart. El usuario puede reservar vuelos, hacer check-in y gestionar reservas en lenguaje natural usando Claude (Anthropic).

**Esta entrega (TP4) incorpora la evolución arquitectónica posterior al feedback del TP3:** la VPC y RDS fueron eliminados a favor de una arquitectura **100% serverless con data lake** (S3 + Glue + Athena) para business analytics, y la autenticación se delegó al **Cognito Authorizer de API Gateway** en lugar de validación manual del JWT en la Lambda.

## Diagrama
<img src="docs/Jetsmart - Diagrama.png" alt="Diagrama de Arquitectura" width="110%">

> Tocar sobre la imagen del diagrama para ampliarla y visualizarla mejor.

## Cambios respecto al TP3

| Punto del feedback de Faustino | Cómo se resolvió en TP4 |
|---|---|
| Bastion EC2 en subnet pública | **Eliminado.** Sin RDS no hay caso de uso para un bastion. |
| Lambdas fuera de la VPC | **Decisión inversa, mejor justificada:** se eliminó la VPC entera. Las Lambdas no manejan recursos privados — DynamoDB, SNS, SQS, Step Functions son managed regionales. Sin recursos persistentes (RDS, EC2), la VPC era over-engineering. |
| RDS Proxy mal representado en el diagrama | **Eliminado junto con RDS.** Para business analytics el patrón correcto es un data lake (S3 + Athena), no un OLTP postgres. |
| "Sin tener en cuenta la justificación" | Ver `docs/justificaciones.md` — fundamentación escrita de cada decisión arquitectónica con alternativas y trade-offs. |

**Mejora adicional no observada por el feedback:** la validación del JWT pasó a hacerse en API Gateway con **Cognito Authorizer**, en lugar del código manual con `python-jose` que tenía la Lambda `chat-handler` en el TP3.

## Cambios introducidos en TP4 (demostración final)

Para llegar al demo presencial del 17/06 incorporamos cinco cambios que cierran las funcionalidades pendientes del TP1 y refinan la separación de dominios:

| Cambio | Detalle |
|---|---|
| **DynamoDB partida en dos tablas single-design** | `jetsmart-prod-conversations` (chatbot state) + `jetsmart-prod-business` (PSS-like). Ver `docs/07-data-layer.md` y justificación #13. |
| **Reservas migran a PNR-céntrico** | PNR de 6 chars alfanumérico (à la Navitaire). Sub-items `SEGMENT#`, `PAX#`, `BP#`. 3 GSIs en business (`FlightByNumber`, `ReservationsByFlight`, `ReservationsByPassenger`). |
| **Derivación a humano (TP1 feature)** | Nueva tool `escalate_to_human` en `chat_handler` → SQS `human-handoff` → Lambda `human_handoff_processor` (mock call center) → SNS notifications. Ver Flujo 7 en `docs/04-flujos.md`. |
| **Notificaciones proactivas (TP1 feature)** | Script offline `scripts/cancel_flight.py` → SNS `flight-events` → SQS `proactive-notifications` → Lambda → Query GSI2 → fan-out de emails. Ver Flujo 8 en `docs/04-flujos.md`. **No se dispara en vivo durante la demo** — se ejecuta antes y se muestra en CloudWatch logs. |
| **Boarding pass async via SQS** | Step Functions PostBookingActions ya no invoca Lambda directo — publica a SQS `boarding-pass-generation`; nueva Lambda `boarding_pass_async` consume y graba `bp_url` en PNR. Fire-and-forget + DLQ. |
| **3 SQS + DLQs nuevas + 1 SNS nuevo + 3 CloudWatch alarms** | Patrón consistente con el SQS de analytics. Ver `terraform/infra/messaging.tf` y `cloudwatch.tf`. |

Total Lambdas: **13 → 16** (eliminada `boarding-pass`, agregadas `boarding-pass-async`, `human-handoff-processor`, `proactive-notifications`, `backup-dynamodb`).

## Requerimientos

### Cuenta y credenciales AWS

| Requisito | Detalle |
|-----------|---------|
| Cuenta AWS | Permisos para Lambda, S3, DynamoDB, Cognito, Step Functions, SNS, SQS, API Gateway, Glue, Athena. |
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

> Tiempo estimado: **5–8 minutos** (mucho más rápido que TP3 — sin RDS, sin VPC, sin RDS Proxy).

Al finalizar, Terraform sube automáticamente el frontend a S3 y carga los vuelos mock en DynamoDB.

### Paso 5 — Ver los outputs del deploy

Al terminar el apply, en el job **Apply → Summary** aparecen las URLs:

| Recurso | Comportamiento esperado |
|---------|------------------------|
| **Frontend** | Abrirla en el browser muestra la app con el botón "Iniciar sesión". |
| **Chatbot API** | GET sin token → `401 Unauthorized` desde **Cognito Authorizer** (no llega a Lambda). Con JWT válido → respuesta del chatbot. |
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

**2) Notificaciones proactivas (offline, antes del demo):**
```bash
cd terraform/infra
export BUSINESS_TABLE_NAME=$(terraform output -raw business_table_name)
export SNS_FLIGHT_EVENTS_ARN=$(terraform output -raw sns_flight_events_arn)
python3 ../../scripts/cancel_flight.py JA203 2026-06-20 "mal tiempo en Mendoza"
→ Verificar:
   - FLIGHTSTATUS actualizado en business table
   - CloudWatch logs de /aws/lambda/jetsmart-prod-proactive-notifications
     mostrando "Query GSI2 ... N PNRs afectados"
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

Toda la actividad del chatbot (mensajes, compras, reclamos, check-ins) se publica en **SNS events**, se buffer en **SQS analytics** y la Lambda `analytics-processor` la escribe en S3 como JSON Lines particionado por fecha:

```
s3://jetsmart-prod-<account-id>-analytics/events/dt=YYYY-MM-DD/hh=HH/<uuid>.jsonl
```

**Glue Crawler** descubre el schema automáticamente (corre cada hora). **Athena** expone los datos vía SQL al equipo de business analytics.

**Acceso desde DBeaver / DataGrip:**

1. Driver: descargar [Athena JDBC driver](https://docs.aws.amazon.com/athena/latest/ug/connect-with-jdbc.html).
2. Connection URL: `jdbc:awsathena://AwsRegion=us-east-1`.
3. Workgroup: `jetsmart-prod-analytics` (output del `terraform apply`).
4. Database: `jetsmart_prod_analytics`.
5. Tabla: `events`.

**Consultas de ejemplo:**

```sql
-- Eventos por tipo, últimos 7 días
SELECT event_type, COUNT(*) AS cantidad
FROM jetsmart_prod_analytics.events
WHERE dt >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY event_type
ORDER BY cantidad DESC;

-- Últimos 20 eventos
SELECT timestamp, event_type, user_id, payload
FROM jetsmart_prod_analytics.events
ORDER BY timestamp DESC
LIMIT 20;

-- Búsquedas más frecuentes por ruta
SELECT
  json_extract_scalar(payload, '$.ruta') AS ruta,
  COUNT(*) AS busquedas
FROM jetsmart_prod_analytics.events
WHERE event_type = 'busqueda_vuelo'
  AND dt >= date_format(current_date - interval '30' day, '%Y-%m-%d')
GROUP BY 1
ORDER BY busquedas DESC
LIMIT 10;
```

**Refrescar particiones manualmente** (después de generar eventos nuevos en la demo, si no querés esperar al cron horario):

```bash
aws glue start-crawler --name jetsmart-prod-events-crawler --region us-east-1
```

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
| `modules/auth` | Custom | Cognito User Pool, grupos, Hosted UI, Lambda auth-callback, API Gateway callback |
| `modules/chatbot-lambda` | Custom | Lambda chat-handler, API Gateway chatbot, **Cognito Authorizer**, throttling, CORS preflight |

### Funciones de Terraform

| Función | Archivo | Uso |
|---------|---------|-----|
| `jsonencode()` | `messaging.tf`, `secrets.tf`, `step_functions.tf`, `storage.tf`, `analytics.tf` | Genera JSON para políticas, secretos y la definición del state machine |
| `toset()` | `modules/auth/main.tf` | Convierte el map de grupos Cognito en set para `for_each` |
| `filebase64sha256()` | `layers.tf` | Hash del ZIP del Lambda Layer Anthropic para detectar cambios |
| `filemd5()` | `storage.tf` | Etag del system prompt para forzar actualización en S3 |
| `sha1()` | `modules/chatbot-lambda/main.tf` | Triggers para redeploy del API Gateway cuando cambian recursos |
| `replace()` | `analytics.tf` | Normaliza el name_prefix para el Glue Catalog (acepta solo `[a-z0-9_]`) |

### Meta-argumentos

| Meta-argumento | Dónde | Por qué |
|----------------|-------|---------|
| `for_each` | `cloudwatch.tf` (13 log groups), `lambda.tf` (7 Lambdas Saga), `modules/auth/main.tf` (grupos Cognito) | Crea múltiples recursos desde un map sin repetir el bloque |
| `depends_on` | `lambda.tf`, `main.tf` | Garantiza orden de creación: analytics-processor depende del S3 bucket de analytics; chatbot module después de todos sus inputs |
| `lifecycle { prevent_destroy }` | `database.tf` | Protege DynamoDB contra `terraform destroy` accidental |
| `lifecycle { create_before_destroy }` | `lambda.tf`, `modules/chatbot-lambda/main.tf`, `modules/auth/main.tf` | Zero downtime al actualizar Lambdas y API Gateway deployments |

## Documentación adicional

- [`docs/02-arquitectura-general.md`](docs/02-arquitectura-general.md) — decisiones de arquitectura.
- [`docs/03-networking.md`](docs/03-networking.md) — por qué no hay VPC.
- [`docs/05-componentes-detalle.md`](docs/05-componentes-detalle.md) — cada componente en detalle.
- [`docs/07-data-layer.md`](docs/07-data-layer.md) — DynamoDB Single Table Design + capa de analytics S3/Athena.
- [`docs/justificaciones.md`](docs/justificaciones.md) — fundamentación escrita de cada decisión (cheat sheet de presentación).
