# TP3 — JetSmart Chatbot con Terraform

Chatbot conversacional desplegado en AWS con Terraform que replica la experiencia de compra de JetSmart. El usuario puede reservar vuelos, hacer check-in, consultar el estado de su vuelo y gestionar sus reservas en lenguaje natural, con IA de Anthropic (Claude).

---

## Arquitectura

```
INTERNET
   │
   ├── Browser → S3 jetsmart-frontend (HTML/CSS/JS — static website hosting)
   ├── Browser → Cognito Hosted UI (login / registro)
   ├── Browser → API Gateway /callback → Lambda auth-callback → redirige con #token=...
   └── Browser → API Gateway /api/* → Lambda chat-handler ⟷ Anthropic API (HTTPS, internet)
                                            │               (claude-haiku-4-5, bucle tool use)
                      ┌─────────────────────┴──────────────────────────────┐
                      │ tool: create_reservation                            │ evento: chat_message
                      ↓                                                     ↓
             ┌─────────────────────────────────┐                    SNS events
             │  Step Functions — Saga          │                         │
             │                                 │                         ↓
             │  ReserveFlight (Lambda)         │                  SQS analytics
             │    ↓ ok / → CancelBooking       │                         │
             │  ReserveBooking (Lambda)        │                         ↓
             │    ↓ ok / → CancelBooking       │          Lambda analytics-processor
             │  CollectPayment (Lambda)        │          (en VPC — subnet privada)
             │    ↓ ok / → CancelBooking       │               │
             │  ConfirmBooking (Lambda)        │               ↓
             │    ↓ ok / → RefundPayment       │        RDS Proxy → RDS PostgreSQL
             │                                 │
             │  PostBookingActions (paralelo): │
             │  ┌─────────────┬─────────────┐  │
             │  Notification   BoardingPass  │  │
             │  (Lambda)       (Lambda)      │  │
             │  └─────────────┴─────────────┘  │
             │       ↓ BookingConfirmed ✓       │
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

FUERA DE LA VPC (servicios managed):
  S3 · Cognito · API Gateway · Step Functions · SNS · SQS · Secrets Manager · CloudWatch

INTERNET EXTERNO:
  chat-handler ⟷ Anthropic API — sin VPC, sin NAT; llamada directa HTTPS por cada mensaje
```

---

## Estructura del repositorio

```
jetsmart/
├── README.md
├── .gitignore
├── .github/
│   └── workflows/
│       └── terraform.yml        # CI/CD: validate en push, plan/apply manual
│
├── docs/
│   ├── 01-como-funciona-chatbot.md
│   ├── 02-arquitectura-general.md
│   ├── 03-networking.md
│   ├── 04-flujos.md
│   ├── 05-componentes-detalle.md
│   ├── 06-iam.md
│   └── 07-data-layer.md
│
├── lambda/
│   ├── chat_handler.py          # Chatbot principal + tool use + auth JWT
│   ├── payment_processor.py     # 7 handlers del patrón Saga
│   ├── auth_callback.py         # Intercambio de código OAuth2 por tokens
│   ├── cognito_trigger.py       # Post-registro: asigna grupo "users"
│   ├── boarding_pass.py         # Genera boarding pass en S3
│   ├── notification.py          # Publica en SNS notifications
│   └── analytics_processor.py  # Consume SQS, escribe en RDS via proxy
│
├── frontend/
│   ├── index.html
│   ├── styles.css
│   └── js/
│       ├── config.js            # URLs de API y Cognito
│       ├── auth.js              # Login/logout/token Cognito
│       └── chat.js              # Interfaz del chat
│
├── scripts/
│   ├── build-layers.sh         # Construye los Lambda Layers para Linux x86_64
│   └── deploy-frontend.sh      # Sincroniza frontend/ → S3
│
└── terraform/
    ├── .gitignore
    ├── backend/                 # Paso 0: crea el bucket S3 para el state (una sola vez)
    │   ├── terraform.tf
    │   ├── variables.tf
    │   ├── main.tf
    │   └── outputs.tf
    │
    └── infra/                   # Infraestructura completa
        ├── terraform.tf         # Versión de providers + configuración del backend S3
        ├── providers.tf         # AWS provider con default_tags
        ├── variables.tf         # Variables con validaciones (sensibles marcadas)
        ├── locals.tf            # name_prefix, cidrsubnet(), slice(), concat()
        ├── outputs.tf           # URLs, ARNs y endpoints
        ├── main.tf              # Módulo VPC (externo) + módulos custom
        ├── networking.tf        # Security Groups + VPC Endpoints
        ├── storage.tf           # S3 frontend + S3 assets + system prompt
        ├── database.tf          # DynamoDB + RDS + RDS Proxy + migración de schema
        ├── secrets.tf           # Secrets Manager (Anthropic + RDS)
        ├── messaging.tf         # SNS + SQS + DLQs + políticas
        ├── lambda.tf            # analytics-processor + 7 payment Lambdas (for_each)
        ├── layers.tf            # Lambda Layers (anthropic + psycopg2)
        ├── step_functions.tf    # State machine Saga + log group
        ├── cloudwatch.tf        # 13 log groups (for_each)
        ├── bastion.tf           # EC2 con SSM para acceso a RDS
        ├── iam.tf               # (LabRole preexistente — Academy no permite crear roles)
        ├── terraform.tfvars.example
        └── modules/
            ├── auth/            # Cognito + auth Lambdas + API Gateway callback
            └── chatbot-lambda/  # Lambda chat-handler + API Gateway chatbot
```

---

## Módulos

### Módulo externo: `terraform-aws-modules/vpc/aws`

Crea toda la red (VPC, subnets en 2 AZs, route tables, Internet Gateway, NAT Gateway) a partir de parámetros simples. Evita escribir ~15 recursos manualmente y reduce errores de enrutamiento.

```hcl
# main.tf
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${local.name_prefix}-vpc"
  cidr = var.vpc_cidr

  azs             = local.azs
  public_subnets  = local.public_subnet_cidrs
  private_subnets = concat(local.private_compute_subnet_cidrs, local.private_data_subnet_cidrs)

  enable_nat_gateway   = true
  single_nat_gateway   = true   # un solo NAT Gateway para reducir costo en Academy
  enable_dns_hostnames = true
}
```

**Inputs clave:** `azs`, `public_subnets`, `private_subnets`, `cidr`
**Outputs usados:** `module.vpc.vpc_id`, `module.vpc.public_subnets`, `module.vpc.private_subnets`

---

### Módulo custom: `modules/auth`

Encapsula todos los recursos de autenticación OAuth2.

**Recursos que crea:**

| Recurso | Descripción |
|---|---|
| `aws_cognito_user_pool` | Directorio de usuarios con email como username |
| `aws_cognito_user_group` (×2) | Grupos `users` y `admins`, creados con `for_each` |
| `aws_cognito_user_pool_client` | App client público (SPA en S3); flujo `code` |
| `aws_cognito_user_pool_domain` | Hosted UI en `<name_prefix>.auth.us-east-1.amazoncognito.com` |
| `aws_lambda_function` auth-callback | Intercambia authorization code por tokens JWT |
| `aws_lambda_function` cognito-trigger | Post-registro: asigna grupo `users` automáticamente |
| `aws_api_gateway_rest_api` | Endpoint `GET /callback` que invoca auth-callback |

**Variables de entrada:**

| Variable | Descripción |
|---|---|
| `name_prefix` | Prefijo para todos los recursos del módulo |
| `aws_region` | Región AWS |
| `frontend_url` | URL del frontend (usada en callback_urls y logout_urls de Cognito) |

**Outputs:**

| Output | Descripción |
|---|---|
| `user_pool_id` | ID del User Pool |
| `client_id` | Client ID para el frontend |
| `hosted_ui_url` | URL de login de la Hosted UI |
| `callback_api_url` | URL del API Gateway de callback |
| `user_pool_arn` | ARN del User Pool |

---

### Módulo custom: `modules/chatbot-lambda`

Encapsula el punto de entrada principal del chatbot.

**Recursos que crea:**

| Recurso | Descripción |
|---|---|
| `aws_lambda_function` chat-handler | Lógica del chatbot con tool use de Anthropic |
| `aws_api_gateway_rest_api` | Endpoint `ANY /{proxy+}` que enruta todos los paths a chat-handler |
| `aws_api_gateway_method_settings` | Throttling: 10 req/s sostenido, 20 burst |
| `aws_lambda_permission` | Autoriza a API Gateway invocar la Lambda |

**Variables de entrada:**

| Variable | Descripción |
|---|---|
| `name_prefix` | Prefijo para todos los recursos |
| `aws_region` | Región AWS |
| `dynamodb_table_name` | Nombre de la tabla DynamoDB |
| `sns_topic_arn` | ARN del topic SNS events |
| `anthropic_secret_arn` | ARN del secreto con la API key de Anthropic |
| `step_functions_arn` | ARN del state machine de reservas |
| `system_prompt_bucket` | Bucket S3 donde está el system prompt |
| `system_prompt_key` | Key del object S3 del system prompt |
| `layer_arns` | Lista de ARNs de Lambda Layers a adjuntar |

**Outputs:**

| Output | Descripción |
|---|---|
| `api_url` | URL del API Gateway del chatbot |
| `chat_handler_arn` | ARN de la Lambda chat-handler |

---

## Funciones de Terraform utilizadas

| Función | Archivo | Uso |
|---|---|---|
| `cidrsubnet()` | `locals.tf` | Calcula los CIDRs de las 6 subnets a partir del CIDR de la VPC |
| `slice()` | `locals.tf`, `lambda.tf`, `database.tf`, `networking.tf` | Selecciona subnets por índice: Lambdas (primeras 2 privadas), RDS (últimas 2 privadas) |
| `concat()` | `main.tf` | Une los CIDRs de subnets de cómputo y datos para pasarlos al módulo VPC |
| `jsonencode()` | `messaging.tf`, `secrets.tf`, `step_functions.tf`, `storage.tf` | Genera JSON para políticas SQS, secretos, la definición inline del state machine y políticas S3 |
| `toset()` | `modules/auth/main.tf` | Convierte el map de grupos de Cognito en set para `for_each` |
| `filebase64sha256()` | `layers.tf` | Calcula el hash de los ZIPs de los Lambda Layers para detectar cambios |
| `filemd5()` | `storage.tf` | Calcula el etag del system prompt para forzar actualización en S3 cuando cambia |

**Ejemplo — `cidrsubnet()` y `slice()` en `locals.tf`:**

```hcl
locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  public_subnet_cidrs          = [cidrsubnet(var.vpc_cidr, 8, 1)]
  private_compute_subnet_cidrs = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i + 3)]
  private_data_subnet_cidrs    = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i + 5)]
}
```

Con `var.vpc_cidr = "10.0.0.0/16"` esto genera: `10.0.1.0/24` (pública), `10.0.3.0/24` y `10.0.4.0/24` (cómputo), `10.0.5.0/24` y `10.0.6.0/24` (datos).

---

## Meta-argumentos utilizados

### `for_each`

**`cloudwatch.tf`** — crea los 13 log groups desde un map local, sin repetir el recurso 13 veces:

```hcl
locals {
  log_groups = {
    lambda_chat                    = "/aws/lambda/${local.name_prefix}-chat-handler"
    lambda_payment_reserve_flight  = "/aws/lambda/${local.name_prefix}-payment-reserve-flight"
    lambda_payment_reserve_booking = "/aws/lambda/${local.name_prefix}-payment-reserve-booking"
    lambda_payment_collect         = "/aws/lambda/${local.name_prefix}-payment-collect"
    lambda_payment_confirm         = "/aws/lambda/${local.name_prefix}-payment-confirm"
    lambda_payment_refund          = "/aws/lambda/${local.name_prefix}-payment-refund"
    lambda_payment_cancel          = "/aws/lambda/${local.name_prefix}-payment-cancel"
    lambda_payment_release_flight  = "/aws/lambda/${local.name_prefix}-payment-release-flight"
    lambda_boarding                = "/aws/lambda/${local.name_prefix}-boarding-pass"
    lambda_notification            = "/aws/lambda/${local.name_prefix}-notification"
    lambda_auth                    = "/aws/lambda/${local.name_prefix}-auth-callback"
    lambda_cognito                 = "/aws/lambda/${local.name_prefix}-cognito-trigger"
    lambda_analytics               = "/aws/lambda/${local.name_prefix}-analytics-processor"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.log_groups
  name              = each.value
  retention_in_days = 30
}
```

**`lambda.tf`** — crea las 7 Lambdas del patrón Saga desde el mismo ZIP con handlers distintos:

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
  for_each = local.payment_handlers

  function_name = "${local.name_prefix}-payment-${each.key}"
  handler       = each.value
  # ...
  lifecycle {
    create_before_destroy = true
  }
}
```

**`modules/auth/main.tf`** — crea los grupos de Cognito desde un map:

```hcl
locals {
  cognito_groups = {
    users  = "Usuarios finales del chatbot"
    admins = "Administradores con acceso al dashboard de analytics"
  }
}

resource "aws_cognito_user_group" "this" {
  for_each     = local.cognito_groups
  name         = each.key
  description  = each.value
  user_pool_id = aws_cognito_user_pool.main.id
}
```

---

### `depends_on`

**`lambda.tf`** — la Lambda analytics no puede arrancar si RDS Proxy o Secrets Manager no existen:

```hcl
resource "aws_lambda_function" "analytics_processor" {
  # ...
  depends_on = [
    aws_db_proxy.main,
    aws_secretsmanager_secret_version.rds_credentials,
  ]
}
```

**`database.tf`** — la migración de schema no puede ejecutarse antes que la Lambda y RDS estén listos:

```hcl
resource "aws_lambda_invocation" "rds_migrate" {
  function_name = aws_lambda_function.analytics_processor.function_name
  input         = jsonencode({ migrate = true })

  depends_on = [
    aws_lambda_function.analytics_processor,
    aws_db_instance.rds,
    aws_secretsmanager_secret_version.rds_credentials,
  ]
}
```

**`main.tf`** — el módulo chatbot depende de que existan los recursos que le pasan como inputs:

```hcl
module "chatbot_lambda" {
  source = "./modules/chatbot-lambda"
  # ...
  depends_on = [
    aws_dynamodb_table.main,
    aws_sns_topic.events,
    aws_sfn_state_machine.booking,
    aws_secretsmanager_secret_version.anthropic_key,
    aws_s3_object.system_prompt,
  ]
}
```

---

### `lifecycle`

**`database.tf`** — protección contra borrado accidental de datos:

```hcl
resource "aws_dynamodb_table" "main" {
  lifecycle { prevent_destroy = true }
}

resource "aws_db_instance" "rds" {
  lifecycle { prevent_destroy = true }
}
```

**`lambda.tf`** y **`modules/chatbot-lambda/main.tf`** — zero downtime al actualizar código de Lambda:

```hcl
resource "aws_lambda_function" "payment" {
  for_each = local.payment_handlers
  # ...
  lifecycle { create_before_destroy = true }
}
```

**`modules/auth/main.tf`** y **`modules/chatbot-lambda/main.tf`** — evita downtime al redeploy de API Gateway:

```hcl
resource "aws_api_gateway_deployment" "main" {
  # ...
  lifecycle { create_before_destroy = true }
}
```

---

## Pipeline de GitHub Actions

El archivo `.github/workflows/terraform.yml` implementa dos jobs independientes:

| Job | Cuándo corre | Credenciales AWS | Qué hace |
|---|---|---|---|
| `validate` | En cada `push` a `main` y en cada PR | No necesita | Crea ZIPs placeholder, `init -backend=false`, `validate`, `fmt -check` (cubre `backend/` también), `terraform test` |
| `deploy` | Solo en `workflow_dispatch` manual | Sí (desde secrets) | Construye layers reales, `init` con backend S3, `plan`, `apply` (opcional) |

Esta separación es deliberada: **el validate puede correr siempre**, sin credenciales, garantizando que el código Terraform es sintácticamente válido. El deploy requiere credenciales de AWS Academy, que expiran con cada sesión, por lo que se ejecuta manualmente.

### Archivo completo: `.github/workflows/terraform.yml`

```yaml
name: Terraform

on:
  push:
    branches: [main]
    paths:
      - 'terraform/**'
      - '.github/workflows/terraform.yml'
  pull_request:
    branches: [main]
    paths:
      - 'terraform/**'
  workflow_dispatch:
    inputs:
      action:
        description: 'Acción a ejecutar'
        required: true
        default: 'plan'
        type: choice
        options: [plan, apply]

env:
  TF_VERSION: '1.10.0'
  AWS_REGION: 'us-east-1'
  WORKING_DIR: 'terraform/infra'

jobs:
  # ── Job 1: Validate (sin credenciales AWS, corre siempre) ──────────────────
  validate:
    name: Validate
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: ${{ env.WORKING_DIR }}

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{ env.TF_VERSION }}

      - name: Create builds directory
        run: mkdir -p builds

      # -backend=false evita conectarse a S3 — solo valida sintaxis y providers
      - name: Terraform Init (sin backend)
        run: terraform init -backend=false

      - name: Terraform Validate
        run: terraform validate

      - name: Terraform Format Check
        run: terraform fmt -check -recursive

  # ── Job 2: Plan / Apply (solo en workflow_dispatch manual) ────────────────
  deploy:
    name: ${{ github.event.inputs.action == 'apply' && 'Apply' || 'Plan' }}
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch'
    needs: validate
    defaults:
      run:
        working-directory: ${{ env.WORKING_DIR }}

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id:     ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-session-token:     ${{ secrets.AWS_SESSION_TOKEN }}
          aws-region:            ${{ env.AWS_REGION }}

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{ env.TF_VERSION }}

      - name: Create builds directory
        run: mkdir -p builds

      - name: Terraform Init
        run: |
          terraform init \
            -backend-config="bucket=jetsmart-terraform-state-${{ secrets.STATE_BUCKET_SUFFIX }}" \
            -backend-config="key=infra/terraform.tfstate" \
            -backend-config="region=${{ env.AWS_REGION }}" \
            -backend-config="use_lockfile=true" \
            -backend-config="encrypt=true"

      - name: Terraform Plan
        env:
          TF_VAR_anthropic_api_key:   ${{ secrets.TF_VAR_ANTHROPIC_API_KEY }}
          TF_VAR_rds_password:        ${{ secrets.TF_VAR_RDS_PASSWORD }}
          TF_VAR_state_bucket_suffix: ${{ secrets.STATE_BUCKET_SUFFIX }}
        run: terraform plan -out=tfplan

      - name: Terraform Apply
        if: github.event.inputs.action == 'apply'
        env:
          TF_VAR_anthropic_api_key:   ${{ secrets.TF_VAR_ANTHROPIC_API_KEY }}
          TF_VAR_rds_password:        ${{ secrets.TF_VAR_RDS_PASSWORD }}
          TF_VAR_state_bucket_suffix: ${{ secrets.STATE_BUCKET_SUFFIX }}
        run: terraform apply -auto-approve tfplan
```

### Secrets requeridos en GitHub

Ir a **Settings → Secrets and variables → Actions → New repository secret** y agregar:

| Secret | Descripción |
|---|---|
| `AWS_ACCESS_KEY_ID` | Credencial de AWS Academy (se renueva por sesión) |
| `AWS_SECRET_ACCESS_KEY` | Credencial de AWS Academy |
| `AWS_SESSION_TOKEN` | Token de sesión de AWS Academy |
| `TF_VAR_ANTHROPIC_API_KEY` | API key de Anthropic (`sk-ant-...`) |
| `TF_VAR_RDS_PASSWORD` | Contraseña para la base de datos RDS |
| `STATE_BUCKET_SUFFIX` | Sufijo del bucket de estado (ej. `grupo8-2026`) — debe coincidir con el usado en `terraform/backend` |

> Las credenciales de AWS Academy expiran al finalizar la sesión del lab. Actualizar los tres secrets `AWS_*` cada vez que se reinicie el lab antes de ejecutar un deploy.

### Cómo ejecutar el deploy manualmente

1. Ir a **Actions → Terraform → Run workflow**
2. Seleccionar `plan` (para revisar qué se va a crear) o `apply` (para crear la infraestructura)
3. Hacer clic en **Run workflow**

El job `deploy` solo aparece y corre si el job `validate` pasó exitosamente (`needs: validate`).

---

## Guía de ejecución paso a paso

### Prerrequisitos

- [Terraform](https://developer.hashicorp.com/terraform/downloads) >= 1.10 (`use_lockfile` requiere esta versión)
- [AWS CLI](https://aws.amazon.com/cli/) configurado con credenciales de AWS Academy
- Python 3.12 + pip3 (para construir los Lambda Layers)

### Paso 1 — Construir los Lambda Layers (una sola vez por sesión)

Los Layers se compilan para Linux x86_64 y se guardan como ZIPs en `terraform/infra/builds/`. Son ignorados por git.

```bash
chmod +x scripts/build-layers.sh
./scripts/build-layers.sh
```

### Paso 2 — Crear el backend remoto (una sola vez)

El state file de Terraform nunca se guarda en el repositorio. Se almacena en un bucket S3 con locking nativo (Terraform >= 1.10).

```bash
cd terraform/backend

terraform init

terraform apply -var="state_bucket_suffix=<tu-sufijo-unico>"
# Ejemplo: terraform apply -var="state_bucket_suffix=grupo8-2026"
```

Anotar el output `state_bucket_name` — se necesita en el siguiente paso.

### Paso 3 — Crear el archivo de variables

```bash
cd terraform/infra

cp terraform.tfvars.example terraform.tfvars
```

Editar `terraform.tfvars` completando los valores marcados:

```hcl
aws_region   = "us-east-1"
project_name = "jetsmart"
environment  = "prod"

vpc_cidr = "10.0.0.0/16"

lambda_timeout = 30

rds_instance_class    = "db.t3.micro"
rds_allocated_storage = 20
rds_db_name           = "jetsmart_analytics"
rds_username          = "jetsmart_admin"
rds_password          = "REEMPLAZAR_CON_PASSWORD_SEGURO"  # sensible

anthropic_api_key = "sk-ant-REEMPLAZAR_CON_TU_API_KEY"   # sensible

state_bucket_suffix = "REEMPLAZAR_CON_SUFFIX_UNICO"       # mismo que Paso 2
```

> `terraform.tfvars` está en `.gitignore`. **Nunca commitear este archivo.**

### Paso 4 — Inicializar y planificar

```bash
# Reemplazar <SUFFIX> con el valor del Paso 2
terraform init \
  -backend-config="bucket=jetsmart-terraform-state-<SUFFIX>" \
  -backend-config="key=infra/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="use_lockfile=true" \
  -backend-config="encrypt=true"

terraform plan
```

### Paso 5 — Aplicar la infraestructura

```bash
terraform apply
# Confirmar con: yes
```

> Tiempo estimado: **15-20 minutos**. RDS tarda ~8 min, RDS Proxy otros ~5 min después de RDS. Es normal no ver output durante esos periodos — Terraform está esperando que AWS termine de crear los recursos.

Al finalizar, `terraform apply` invoca automáticamente la Lambda de migración para crear el schema de RDS (`aws_lambda_invocation.rds_migrate`).

### Paso 6 — Verificar los outputs

```bash
terraform output chatbot_api_url       # URL del backend → configurar en frontend/js/config.js
terraform output frontend_url          # URL del frontend en S3
terraform output cognito_hosted_ui_url # URL de login de Cognito
terraform output auth_callback_url     # URL del callback OAuth2
```

### Paso 7 — Subir el frontend

```bash
# Opción A: script
./scripts/deploy-frontend.sh

# Opción B: manual
aws s3 sync frontend/ s3://$(terraform output -raw frontend_bucket_name)/
```

### Paso 8 — Destruir la infraestructura

> `aws_dynamodb_table` y `aws_db_instance` tienen `prevent_destroy = true`. Para destruir, comentar esas líneas en `database.tf` primero.

```bash
terraform destroy
```

---

## Variables principales

| Variable | Tipo | Default | Descripción |
|---|---|---|---|
| `aws_region` | `string` | `us-east-1` | Región AWS |
| `project_name` | `string` | `jetsmart` | Prefijo de todos los recursos |
| `environment` | `string` | `prod` | Ambiente (`dev`/`staging`/`prod`) — con validación |
| `vpc_cidr` | `string` | `10.0.0.0/16` | CIDR de la VPC |
| `lambda_timeout` | `number` | `30` | Timeout de Lambdas en segundos |
| `rds_instance_class` | `string` | `db.t3.micro` | Clase de instancia RDS |
| `rds_allocated_storage` | `number` | `20` | Almacenamiento RDS en GB |
| `rds_db_name` | `string` | `jetsmart_analytics` | Nombre de la base de datos |
| `rds_username` | `string` | `jetsmart_admin` | Usuario de RDS (sensible) |
| `anthropic_api_key` | `string` | — | API key de Anthropic **(sensible)** |
| `rds_password` | `string` | — | Contraseña de RDS **(sensible)** |
| `state_bucket_suffix` | `string` | — | Sufijo del bucket de estado remoto |

---

## Outputs principales

| Output | Descripción | Sensible |
|---|---|---|
| `chatbot_api_url` | URL del API Gateway del chatbot | No |
| `frontend_url` | URL del frontend estático en S3 | No |
| `auth_callback_url` | URL del callback OAuth2 para Cognito | No |
| `cognito_user_pool_id` | ID del User Pool | No |
| `cognito_client_id` | Client ID para el frontend | No |
| `cognito_hosted_ui_url` | URL de login de Cognito | No |
| `step_functions_arn` | ARN del state machine de reserva | No |
| `dynamodb_table_name` | Nombre de la tabla DynamoDB | No |
| `sns_events_arn` | ARN del topic SNS de eventos | No |
| `sqs_analytics_url` | URL de la cola SQS de analytics | No |
| `sqs_booking_failed_dlq_url` | URL de la DLQ de reservas fallidas | No |
| `rds_endpoint` | Endpoint directo de RDS | Sí |
| `rds_proxy_endpoint` | Endpoint del RDS Proxy (usado por analytics_processor) | Sí |
| `bastion_instance_id` | ID del bastion para SSM port-forwarding | No |
