# TP3 — JetSmart Chatbot: Despliegue con Terraform (IaC)

## Descripción del proyecto

Chatbot conversacional que replica la experiencia de compra de JetSmart como canal end-to-end. El usuario puede reservar vuelos, hacer check-in, consultar el estado de su vuelo, gestionar reservas y realizar reclamos, todo en lenguaje natural gracias a la IA de Anthropic (Claude Haiku).

Los datos de vuelos son simulados (mock data en DynamoDB) ya que la API real de JetSmart no es pública.

---

## Arquitectura

Arquitectura 100% serverless con 13 Lambdas, API Gateway como punto de entrada HTTP, Step Functions para el flujo de reserva y pago (patrón Saga con compensaciones automáticas), y SNS→SQS para el pipeline de analytics.

```
INTERNET
   │
   ├── Browser → S3 (frontend estático)
   ├── Browser → Cognito Hosted UI (login / registro)
   ├── Browser → API Gateway /callback → Lambda auth-callback
   └── Browser → API Gateway /api/* → Lambda chat-handler
                                             │
                       ┌─────────────────────┴──────────────────────────┐
                       │ tool: create_reservation                        │ evento: chat_message
                       ↓                                                 ↓
              Step Functions (Saga)                               SNS topic events
              ┌────────────────────────────────────┐                    │
              │ ReserveFlight    (Lambda)           │                    ↓
              │ ReserveBooking   (Lambda)           │             SQS analytics ──→ DLQ
              │ CollectPayment   (Lambda)           │                    │
              │ ConfirmBooking   (Lambda)           │                    ↓
              │        │                            │     Lambda analytics-processor (VPC)
              │        ↓ PostBookingActions         │          │                   │
              │   ┌────┴──────────────────┐         │          ↓                   ↓
              │   │ Notification (Lambda) │         │   RDS PostgreSQL      DynamoDB aggregates
              │   │ BoardingPass (Lambda) │         │   (eventos_chat)      (dashboard admin)
              │   └───────────────────────┘         │
              │                                     │
              │ Compensaciones (rollback automático):│
              │ RefundPayment → CancelBooking        │
              │ → ReleaseFlight → NotifyBookingFailed│
              │ → SQS booking-failed-dlq             │
              └────────────────────────────────────┘
```

### Patrón Saga en Step Functions

El flujo de reserva y pago implementa el patrón Saga con compensaciones automáticas. Si cualquier paso falla, Step Functions ejecuta las acciones de rollback en orden inverso:

| Paso | Lambda | Fallo → compensación |
|------|--------|----------------------|
| 1 | `reserve-flight` — decrementa asientos (atómico) | → `CancelBooking` |
| 2 | `reserve-booking` — crea registro en DynamoDB | → `CancelBooking` |
| 3 | `collect` — procesa el pago | → `CancelBooking` |
| 4 | `confirm` — confirma la reserva | → `RefundPayment` |
| ∥ | `notification` + `boarding-pass` (paralelo) | fallo aquí no revierte el pago |

### Servicios utilizados

| Servicio | Rol |
|---|---|
| S3 (×2) | Frontend estático + assets privados (boarding passes) |
| Cognito | Autenticación OAuth2 con grupos: `users` y `admins` |
| API Gateway (×2) | Chatbot `/api/*` + callback OAuth2 `/callback` |
| Lambda (×13) | Chat, auth (×2), pagos Saga (×7), boarding pass, notificación, analytics |
| Step Functions | Orquestador del flujo Saga de reserva y pago |
| SNS (×2) | `events` (analytics del chat) + `notifications` (emails al usuario) |
| SQS (×2 + DLQs) | `analytics` (SNS→SQS fan-out) + `booking-failed-dlq` (errores Saga) |
| DynamoDB | Single Table Design: sesiones, mensajes, reservas, vuelos mock, analytics |
| RDS PostgreSQL | Log detallado de eventos para el dashboard del administrador |
| Secrets Manager | API key de Anthropic + credenciales de RDS |
| Lambda Layer | `psycopg2` para la Lambda analytics (compilado para Python 3.12) |
| IAM — LabRole | Rol único compartido por todas las Lambdas (restricción AWS Academy) |
| CloudWatch | 13 log groups con retención de 30 días (creados con `for_each`) |
| VPC | 1 VPC, 6 subnets en 2 AZs, 1 NAT Gateway, Internet Gateway |
| VPC Endpoints (×2) | DynamoDB (Gateway) + Secrets Manager (Interface) — para Lambda analytics |
| EC2 Bastion | Acceso a RDS via SSM (sin SSH, sin puerto 22) |

---

## Estructura del repositorio

```
jetsmart/
├── README.md
├── .gitignore
├── .github/workflows/terraform.yml  # CI/CD: plan en PR, apply en merge a main
│
├── docs/                            # Documentación de arquitectura
│   ├── 01-como-funciona-chatbot.md
│   ├── 02-arquitectura-general.md
│   ├── 03-networking.md
│   ├── 04-flujos.md
│   ├── 05-componentes-detalle.md
│   ├── 06-iam.md
│   └── 07-data-layer.md
│
├── lambda/                          # Código fuente de las Lambdas
│   ├── chat_handler.py              # Chatbot principal + tool use + auth JWT
│   ├── payment_processor.py         # 7 handlers del patrón Saga
│   ├── auth_callback.py             # Intercambio de código OAuth2 por tokens
│   ├── cognito_trigger.py           # Post-registro: asigna grupo "users"
│   ├── boarding_pass.py             # Genera boarding pass en DynamoDB
│   ├── notification.py              # Publica en SNS notifications
│   └── analytics_processor.py      # Consume SQS, escribe en RDS y DynamoDB
│
├── frontend/                        # SPA estática servida desde S3
│   ├── index.html
│   ├── styles.css
│   └── js/
│       ├── config.js                # URLs de API, Cognito, frontend
│       ├── auth.js                  # Login/logout/token Cognito
│       └── chat.js
│
├── scripts/
│   ├── deploy-frontend.sh           # Sincroniza frontend/ → S3
│   └── build-layers.sh             # Construye el Lambda Layer localmente
│
└── terraform/
    ├── .gitignore                   # Excluye *.tfstate, .terraform/, layers/, tfvars
    ├── backend/                     # Paso 1 (una sola vez): bucket S3 para el state
    │   ├── terraform.tf
    │   ├── variables.tf
    │   ├── main.tf
    │   └── outputs.tf
    │
    └── infra/                       # Paso 2: infraestructura completa (~108 recursos)
        ├── terraform.tf             # Conexión al backend S3 + versiones de providers
        ├── providers.tf             # AWS provider con default_tags
        ├── variables.tf             # Variables con validaciones (sensibles marcadas)
        ├── locals.tf                # name_prefix, tags, cidrsubnet(), slice(), concat()
        ├── outputs.tf               # URLs, ARNs y valores de configuración
        ├── main.tf                  # Módulo VPC (externo) + módulos custom
        ├── networking.tf            # Security Groups + VPC Endpoints
        ├── storage.tf               # S3 frontend + S3 assets con lifecycle
        ├── database.tf              # DynamoDB + RDS + migración automática de schema
        ├── secrets.tf               # Secrets Manager (Anthropic + RDS)
        ├── messaging.tf             # SNS + SQS + DLQs + políticas
        ├── lambda.tf                # analytics-processor + 7 payment Lambdas (for_each)
        ├── step_functions.tf        # State machine Saga + log group
        ├── cloudwatch.tf            # 13 log groups (for_each)
        ├── bastion.tf               # EC2 con SSM para acceso a RDS
        ├── iam.tf                   # IAM groups del equipo (for_each)
        ├── layers.tf                # Lambda Layer (psycopg2 + anthropic)
        ├── terraform.tfvars.example # Plantilla — copiar a terraform.tfvars
        └── modules/
            ├── chatbot-lambda/      # Módulo custom: Lambda chat-handler + API Gateway
            └── auth/                # Módulo custom: Cognito + auth Lambdas + API Gateway
```

---

## Módulos

### Módulo externo: `terraform-aws-modules/vpc/aws`

Crea toda la red (VPC, subnets, route tables, Internet Gateway, NAT Gateway) a partir de parámetros simples. Evita escribir ~15 recursos manualmente y reduce el riesgo de errores en el enrutamiento.

```hcl
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  azs             = local.azs
  public_subnets  = local.public_subnet_cidrs
  private_subnets = concat(local.private_compute_subnet_cidrs, local.private_data_subnet_cidrs)

  enable_nat_gateway = true
  single_nat_gateway = true   # un solo NAT Gateway para reducir costo en Academy
}
```

### Módulo custom: `modules/chatbot-lambda`

Encapsula el punto de entrada principal del chatbot:
- **Lambda `chat-handler`**: recibe mensajes del usuario, ejecuta el tool use de Anthropic, guarda historial en DynamoDB, dispara Step Functions para reservas
- **API Gateway**: endpoint HTTPS `/api/*` que invoca la Lambda con proxy integration

**Variables de entrada:** `name_prefix`, `aws_region`, `dynamodb_table_name`, `sns_topic_arn`, `anthropic_secret_arn`, `step_functions_arn`, `system_prompt_bucket`, `system_prompt_key`

**Outputs:** `api_url`, `chat_handler_arn`

### Módulo custom: `modules/auth`

Encapsula todos los recursos de autenticación OAuth2:
- **Cognito User Pool + Hosted UI**: registro y login sin pantalla custom
- **Cognito Groups**: `users` (acceso al chatbot) y `admins` (acceso al dashboard)
- **Lambda `auth-callback`**: intercambia el authorization code de Cognito por tokens JWT y redirige al frontend con `#token=...`
- **Lambda `cognito-trigger`**: post-registro, asigna automáticamente el grupo `users`
- **API Gateway**: expone el endpoint `/callback` que invoca la Lambda auth

**Variables de entrada:** `name_prefix`, `aws_region`, `frontend_url`

**Outputs:** `user_pool_id`, `client_id`, `hosted_ui_url`, `callback_api_url`, `user_pool_arn`

---

## Funciones Terraform utilizadas

| Función | Archivo | Uso |
|---|---|---|
| `cidrsubnet()` | `locals.tf` | Calcula automáticamente los CIDRs de las 6 subnets a partir del CIDR de la VPC |
| `slice()` | `locals.tf`, `lambda.tf`, `database.tf` | Selecciona subnets por índice: Lambdas (primeras 2 privadas), RDS (últimas 2 privadas) |
| `concat()` | `main.tf` | Une los CIDRs de subnets de cómputo y datos para pasarlos al módulo VPC |
| `jsonencode()` | `messaging.tf`, `secrets.tf`, `step_functions.tf` | Genera JSON para políticas SQS, secretos y la definición del state machine |
| `templatefile()` | `modules/chatbot-lambda/main.tf` | Carga el system prompt de Claude desde `templates/system_prompt.tpl` |
| `toset()` | `iam.tf`, `modules/auth/main.tf` | Convierte listas en sets para `for_each` en IAM groups y Cognito groups |
| `join()` | `locals.tf` | Une la lista de rutas disponibles en un string para inyectar en el system prompt |

---

## Meta-argumentos utilizados

### `for_each`

**`cloudwatch.tf`** — crea los 13 log groups desde un map local, evitando repetición:
```hcl
resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.log_groups
  name              = each.value
  retention_in_days = 30
}
```

**`lambda.tf`** — crea las 7 Lambdas del patrón Saga desde un map, mismo ZIP distinto handler:
```hcl
locals {
  payment_handlers = {
    reserve-flight  = "payment_processor.reserve_flight_handler"
    reserve-booking = "payment_processor.reserve_booking_handler"
    collect         = "payment_processor.collect_payment_handler"
    confirm         = "payment_processor.confirm_booking_handler"
    refund          = "payment_processor.refund_payment_handler"
    cancel          = "payment_processor.cancel_booking_handler"
    release-flight  = "payment_processor.release_flight_handler"
  }
}

resource "aws_lambda_function" "payment" {
  for_each      = local.payment_handlers
  function_name = "${local.name_prefix}-payment-${each.key}"
  handler       = each.value
  # ...
}
```

**`iam.tf`** — crea los 4 IAM groups del equipo desde un set:
```hcl
resource "aws_iam_group" "teams" {
  for_each = toset(["infra-devops", "backend-dev", "security-auth", "analytics"])
  name     = "${local.name_prefix}-${each.value}"
}
```

**`modules/auth/main.tf`** — crea los grupos de Cognito desde un map:
```hcl
resource "aws_cognito_user_group" "this" {
  for_each    = local.cognito_groups
  name        = each.key
  description = each.value.description
}
```

### `depends_on`

**`lambda.tf`** — la Lambda analytics no puede iniciarse si RDS o el secreto no existen:
```hcl
resource "aws_lambda_function" "analytics_processor" {
  depends_on = [
    aws_db_instance.rds,
    aws_secretsmanager_secret_version.rds_credentials,
  ]
}
```

**`database.tf`** — la migración de schema RDS no puede correr si la Lambda no está lista:
```hcl
resource "aws_lambda_invocation" "rds_migrate" {
  depends_on = [
    aws_lambda_function.analytics_processor,
    aws_db_instance.rds,
    aws_secretsmanager_secret_version.rds_credentials,
  ]
}
```

### `lifecycle`

**`database.tf`** — protección contra borrado accidental de datos en producción:
```hcl
resource "aws_dynamodb_table" "main" {
  lifecycle { prevent_destroy = true }
}

resource "aws_db_instance" "rds" {
  lifecycle { prevent_destroy = true }
}
```

**`lambda.tf`** — zero downtime en actualizaciones de código de las Lambdas de pago:
```hcl
resource "aws_lambda_function" "payment" {
  for_each = local.payment_handlers
  # ...
  lifecycle { create_before_destroy = true }
}
```

---

## Prerrequisitos

1. [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.10 (necesario para `use_lockfile = true`)
2. [AWS CLI](https://aws.amazon.com/cli/) configurado con credenciales de AWS Academy
3. Una API key de Anthropic — crear cuenta en [console.anthropic.com](https://console.anthropic.com)

> No se requiere Docker ni ningún registro de contenedores. La arquitectura es 100% serverless.

---

## Ejecución con GitHub Actions

El repositorio incluye el workflow `.github/workflows/terraform.yml`. En un PR hace `plan`; al mergear a `main` hace `apply`.

### Secrets requeridos en GitHub

Ir a **Settings → Secrets and variables → Actions** y agregar:

| Secret | Descripción |
|---|---|
| `AWS_ACCESS_KEY_ID` | Credenciales de AWS Academy |
| `AWS_SECRET_ACCESS_KEY` | Credenciales de AWS Academy |
| `AWS_SESSION_TOKEN` | Credenciales de AWS Academy (token de sesión) |
| `TF_VAR_ANTHROPIC_API_KEY` | API key de Anthropic (`sk-ant-...`) |
| `TF_VAR_RDS_PASSWORD` | Contraseña para la base de datos RDS |
| `STATE_BUCKET_SUFFIX` | Sufijo único del bucket de estado (ej. `grupo8-2026`) |

### Pasos previos antes de correr el workflow

1. Crear el backend manualmente una sola vez (ver Paso 1 de la guía local abajo).
2. El workflow usa `terraform init` con `-backend-config` dinámico — **no es necesario editar `terraform.tf`**.

> Las credenciales de AWS Academy expiran cada sesión. Actualizar los secrets cada vez que se reinicie el lab.

---

## Guía de ejecución paso a paso

### Paso 1 — Crear el backend remoto (una sola vez)

El state file de Terraform nunca se guarda en el repositorio. Se almacena en S3 con locking nativo (Terraform 1.10+).

```bash
cd terraform/backend

terraform init

terraform apply -var="state_bucket_suffix=mi-sufijo-unico-2026"
```

Tomar nota del output `state_bucket_name`.

### Paso 2 — Configurar el backend en la infraestructura principal

Editar `infra/terraform.tf` y reemplazar el valor del bucket con el suffix usado en el paso anterior:

```hcl
backend "s3" {
  bucket       = "jetsmart-terraform-state-mi-sufijo-unico-2026"
  key          = "infra/terraform.tfstate"
  region       = "us-east-1"
  use_lockfile = true   # locking nativo S3, requiere Terraform >= 1.10
  encrypt      = true
}
```

### Paso 3 — Crear el archivo de variables

```bash
cd terraform/infra
cp terraform.tfvars.example terraform.tfvars
```

Editar `terraform.tfvars` y completar los valores marcados:
- `anthropic_api_key` — la key obtenida en console.anthropic.com
- `rds_password` — contraseña segura para la base de datos
- `state_bucket_suffix` — el mismo sufijo usado en el Paso 1

> `terraform.tfvars` está en `.gitignore` y **nunca debe commitearse** al repositorio.

### Paso 4 — Inicializar y aplicar

```bash
# Reemplazar <SUFFIX> con el valor usado en el Paso 1
terraform init \
  -backend-config="bucket=jetsmart-terraform-state-<SUFFIX>" \
  -backend-config="key=infra/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="use_lockfile=true" \
  -backend-config="encrypt=true"

terraform plan     # revisar los recursos a crear (~108 recursos)

terraform apply    # crear la infraestructura (~10-15 min, RDS es el más lento)
```

Al finalizar, `terraform apply` invoca automáticamente la Lambda de migración para crear el schema de RDS (`aws_lambda_invocation.rds_migrate`).

### Paso 5 — Verificar los outputs

```bash
terraform output chatbot_api_url       # URL del backend (configurar en frontend/js/config.js)
terraform output frontend_url          # URL del frontend S3
terraform output cognito_hosted_ui_url # URL de login de Cognito
terraform output auth_callback_url     # URL del callback OAuth2
```

### Paso 6 — Configurar el frontend

Actualizar `frontend/js/config.js` con los valores obtenidos en el paso anterior y subir los archivos al bucket S3 del frontend:

```bash
aws s3 sync frontend/ s3://$(terraform output -raw frontend_bucket_name)/
```

### Paso 7 — Destruir la infraestructura

> **Atención:** DynamoDB y RDS tienen `prevent_destroy = true`. Para destruir, primero remover esas protecciones del código o eliminar los recursos manualmente desde la consola.

```bash
terraform destroy
```

---

## Variables principales

| Variable | Descripción | Default |
|---|---|---|
| `aws_region` | Región AWS | `us-east-1` |
| `environment` | Ambiente (`dev`/`staging`/`prod`) | `prod` |
| `vpc_cidr` | CIDR de la VPC | `10.0.0.0/16` |
| `lambda_timeout` | Timeout de las Lambdas en segundos | `30` |
| `rds_instance_class` | Clase de la instancia RDS | `db.t3.micro` |
| `rds_allocated_storage` | Almacenamiento RDS en GB | `20` |
| `anthropic_api_key` | API key de Anthropic **(sensible)** | — |
| `rds_password` | Contraseña de RDS **(sensible)** | — |
| `state_bucket_suffix` | Sufijo del bucket de estado remoto | — |

---

## Outputs principales

| Output | Descripción |
|---|---|
| `chatbot_api_url` | URL del API Gateway del chatbot |
| `frontend_url` | URL del frontend estático en S3 |
| `auth_callback_url` | URL del callback OAuth2 para Cognito |
| `cognito_user_pool_id` | ID del User Pool |
| `cognito_client_id` | Client ID para el frontend |
| `cognito_hosted_ui_url` | URL de login de Cognito |
| `step_functions_arn` | ARN del state machine de reserva |
| `dynamodb_table_name` | Nombre de la tabla DynamoDB |
| `rds_endpoint` | Endpoint de RDS *(sensible)* |
| `sns_events_arn` | ARN del topic SNS de eventos |
| `sqs_analytics_url` | URL de la cola SQS de analytics |
| `sqs_booking_failed_dlq_url` | URL de la DLQ de reservas fallidas |

---

## Decisiones de arquitectura relevantes

**Step Functions con patrón Saga para reservas:** el flujo de reserva y pago es una transacción distribuida que debe ser atómica. Si el pago falla después de reservar el asiento, el asiento debe liberarse. Step Functions orquesta esta lógica con compensaciones automáticas. Cada Lambda es idempotente y puede reintentarse de forma segura.

**Decremento atómico de asientos:** `reserve-flight` usa `ConditionExpression="asientos_disponibles >= :min"` en DynamoDB para garantizar que dos usuarios simultáneos no puedan reservar el mismo asiento. Si la condición falla, Step Functions lanza `ConditionalCheckFailedException` y el state machine lo propaga como error de disponibilidad.

**Chat sincrónico, Saga asincrónica:** el chatbot responde en tiempo real. Al confirmar la reserva, `chat-handler` llama a Step Functions con `startExecution` (no espera el resultado) y retorna un transaction ID inmediatamente. El resultado de la Saga llega via notificación SNS al usuario.

**Lambda analytics en VPC:** la única Lambda dentro de la VPC es `analytics-processor` porque necesita acceso directo a RDS en subnet privada. El resto de Lambdas no están en la VPC para evitar el overhead del ENI y poder llamar a internet directamente (Anthropic API).

**Migración automática de schema RDS:** `database.tf` incluye un `aws_lambda_invocation` que invoca `analytics-processor` con `{"migrate": true}` en cada `terraform apply`. El handler usa `CREATE TABLE IF NOT EXISTS`, por lo que es idempotente y seguro re-ejecutar.

**Mock data de vuelos en DynamoDB:** la API de JetSmart no es pública. Los datos de vuelos (rutas, precios, disponibilidad) se almacenan como mock data en DynamoDB con el esquema de claves `PK=FLIGHT#{origen}#{destino}` / `SK=DATE#{fecha}`.

**State backend S3 con locking nativo:** se usa `use_lockfile = true` (Terraform 1.10+) en lugar de DynamoDB para el lock del state. La carpeta `terraform/backend/` solo crea el bucket S3, simplificando el bootstrap inicial.

**Un solo NAT Gateway:** en alta disponibilidad real se usaría uno por AZ. En Academy se usa uno solo para reducir costo (~$32/mes vs ~$64/mes).
