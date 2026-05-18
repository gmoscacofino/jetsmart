# TP3 — JetSmart Chatbot con Terraform
### Cloud Computing — 2026Q1 — ITBA

## Introducción

JetSmart Chatbot es un asistente conversacional desplegado en AWS con Terraform que replica la experiencia de compra de JetSmart. El usuario puede reservar vuelos, hacer check-in y gestionar reservas en lenguaje natural. La IA es opcional: por defecto el sistema corre en **modo demo** con respuestas predefinidas, sin necesitar una API key de Anthropic.

## Arquitectura

```
INTERNET
   │
   ├── Browser → S3 frontend (HTML/CSS/JS)
   ├── Browser → Cognito Hosted UI (login)
   ├── Browser → API Gateway /callback → Lambda auth-callback
   └── Browser → API Gateway /api/* → Lambda chat-handler ⟷ Anthropic API (opcional)
                                            │
                      ┌─────────────────────┴──────────────────────┐
                      │ tool: create_reservation                    │ SNS events
                      ↓                                             ↓
             Step Functions — Saga                          SQS analytics
             ReserveFlight → ReserveBooking                         │
             → CollectPayment → ConfirmBooking              Lambda analytics-processor
             (con compensaciones: RefundPayment,                    │
              CancelBooking, ReleaseFlight)                  RDS Proxy → RDS PostgreSQL

DENTRO DE LA VPC:
  analytics-processor ←→ RDS Proxy ←→ RDS PostgreSQL
  EC2 Bastion ←→ SSM (acceso operativo a RDS sin SSH)

FUERA DE LA VPC (managed):
  S3 · Cognito · API Gateway · Step Functions · SNS · SQS · Secrets Manager · CloudWatch
```

## Requerimientos

### Cuenta y credenciales AWS

| Requisito | Detalle |
|-----------|---------|
| Cuenta AWS | Permisos para VPC, RDS, Lambda, S3, DynamoDB, Cognito, Step Functions, etc. |
| Rol **LabRole** | Pre-existente en AWS Academy (`data.aws_iam_role.lab_role` en Terraform) |
| AWS CLI v2 | Credenciales en `~/.aws/credentials` o variables de entorno |

Verificar credenciales activas:
```bash
aws sts get-caller-identity
```

### Herramientas

| Herramienta | Versión mínima | Uso |
|-------------|----------------|-----|
| [Terraform](https://developer.hashicorp.com/terraform/downloads) | ≥ 1.10 | Infraestructura (`use_lockfile` requiere esta versión) |
| [AWS CLI](https://aws.amazon.com/cli/) | v2 | Credenciales y deploy del frontend |
| [Python](https://www.python.org/downloads/) + pip | 3.12 | Construir los Lambda Layers |

## Instrucciones de ejecución

### Paso 1 — Construir los Lambda Layers

Los layers se compilan para Linux x86_64. Ejecutar **una sola vez por máquina**:

```bash
chmod +x scripts/build-layers.sh
./scripts/build-layers.sh
```

Genera `terraform/infra/builds/anthropic-layer.zip` y `psycopg2-layer.zip` (ignorados por git).

### Paso 2 — Crear el backend remoto (una sola vez)

El state file nunca va en el repositorio. Crear el bucket S3 con locking nativo:

```bash
cd terraform/backend
terraform init
terraform apply -var="state_bucket_suffix=<tu-sufijo-unico>"
# Ejemplo: terraform apply -var="state_bucket_suffix=grupo8-2026"
```

Anotar el output `state_bucket_name` para el siguiente paso.

### Paso 3 — Crear el archivo de variables

```bash
cd terraform/infra
cp terraform.tfvars.example terraform.tfvars
```

Editar `terraform.tfvars` con los valores reales:

```hcl
aws_region   = "us-east-1"
project_name = "jetsmart"
environment  = "prod"

vpc_cidr = "10.0.0.0/16"

rds_instance_class    = "db.t3.micro"
rds_allocated_storage = 20
rds_db_name           = "jetsmart_analytics"
rds_username          = "jetsmart_admin"
rds_password          = "REEMPLAZAR_CON_PASSWORD_SEGURO"

# mock_mode = true: chatbot responde con datos demo sin API key de Anthropic
# mock_mode = false: requiere completar anthropic_api_key con una clave real
mock_mode         = true
anthropic_api_key = ""

state_bucket_suffix = "REEMPLAZAR_CON_SUFFIX_DEL_PASO_2"
```

> `terraform.tfvars` está en `.gitignore`. **Nunca commitear este archivo.**

### Paso 4 — Inicializar Terraform

```bash
terraform init \
  -backend-config="bucket=jetsmart-terraform-state-<SUFFIX>" \
  -backend-config="key=infra/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="use_lockfile=true" \
  -backend-config="encrypt=true"
```

### Paso 5 — Planificar y aplicar

```bash
terraform plan
terraform apply
# Confirmar con: yes
```

> Tiempo estimado: **15–20 minutos**. RDS tarda ~8 min, RDS Proxy otros ~5 min. Es normal no ver output durante esos períodos.

Al finalizar, Terraform ejecuta automáticamente la Lambda de migración para crear el schema de RDS.

### Paso 6 — Verificar outputs

```bash
terraform output chatbot_api_url       # URL del backend → configurar en frontend/js/config.js
terraform output frontend_url          # URL del frontend en S3
terraform output cognito_hosted_ui_url # URL de login de Cognito
terraform output auth_callback_url     # URL del callback OAuth2
```

### Paso 7 — Subir el frontend

```bash
./scripts/deploy-frontend.sh

# O manual:
aws s3 sync frontend/ s3://$(terraform output -raw frontend_bucket_name)/
```

### Paso 8 — Destruir la infraestructura

> `aws_dynamodb_table` y `aws_db_instance` tienen `prevent_destroy = true`. Para destruir, comentar esas líneas en `database.tf` primero.

```bash
terraform destroy
```

## Verificación

Tras el deploy, acceder a `terraform output frontend_url` en el browser:

1. **Login** → Cognito Hosted UI → crear cuenta
2. **Chat** → escribir "quiero volar a Santiago" → el chatbot responde (modo demo o Claude real según `mock_mode`)
3. **Reservas** → el flujo de compra pasa por Step Functions → el estado aparece en "Mis reservas"
4. **Check-in** → disponible 24 hs antes del vuelo

Para verificar el estado de Step Functions:
```bash
aws stepfunctions list-executions \
  --state-machine-arn $(terraform output -raw step_functions_arn) \
  --region us-east-1
```

## Pipeline de GitHub Actions

El archivo `.github/workflows/terraform.yml` implementa dos jobs:

| Job | Cuándo corre | Credenciales AWS | Qué hace |
|-----|--------------|------------------|----------|
| `validate` | En cada `push` y en cada **PR** | No necesita | `init -backend=false`, `validate`, `fmt -check`, `terraform test` |
| `deploy` | Solo en `workflow_dispatch` manual | Sí (desde secrets) | `init` con backend S3, `plan`, `apply` (opcional) |

El job `validate` corre siempre sin credenciales, garantizando que el código es válido en cada PR.

### Secrets del repositorio

Ir a **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Obligatorio | Descripción |
|--------|-------------|-------------|
| `AWS_ACCESS_KEY_ID` | Para deploy | Credencial de AWS Academy (se renueva por sesión) |
| `AWS_SECRET_ACCESS_KEY` | Para deploy | Credencial de AWS Academy |
| `AWS_SESSION_TOKEN` | Para deploy | Token de sesión de AWS Academy |
| `STATE_BUCKET_SUFFIX` | Para deploy | Sufijo del bucket de estado (ej. `grupo8-2026`) |
| `TF_VAR_RDS_PASSWORD` | Para deploy | Contraseña de RDS |
| `TF_VAR_ANTHROPIC_API_KEY` | Opcional | Requerida solo si `mock_mode = false` |

> Las credenciales de AWS Academy expiran al cerrar el lab. Actualizar los tres secrets `AWS_*` en cada nueva sesión antes de ejecutar un deploy.

### Ejecutar deploy manualmente

1. Ir a **Actions → Terraform → Run workflow**
2. Seleccionar `plan` (para revisar) o `apply` (para crear infraestructura)
3. Clic en **Run workflow**

El job `deploy` solo corre si `validate` pasó exitosamente.

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
| `terraform-aws-modules/vpc/aws` | Externo | VPC, subnets en 2 AZs, route tables, IGW, NAT Gateway |
| `modules/auth` | Custom | Cognito User Pool, grupos, Hosted UI, Lambda auth-callback, API Gateway callback |
| `modules/chatbot-lambda` | Custom | Lambda chat-handler, API Gateway chatbot, throttling |

### Funciones de Terraform

| Función | Archivo | Uso |
|---------|---------|-----|
| `cidrsubnet()` | `locals.tf` | Calcula los CIDRs de las 6 subnets a partir del CIDR de la VPC |
| `slice()` | `locals.tf`, `lambda.tf`, `database.tf` | Selecciona subnets por índice (cómputo vs datos) |
| `concat()` | `main.tf` | Une CIDRs de subnets de cómputo y datos para el módulo VPC |
| `jsonencode()` | `messaging.tf`, `secrets.tf`, `step_functions.tf`, `storage.tf` | Genera JSON para políticas, secretos y la definición del state machine |
| `toset()` | `modules/auth/main.tf` | Convierte el map de grupos Cognito en set para `for_each` |
| `filebase64sha256()` | `layers.tf` | Hash de los ZIPs de Lambda Layers para detectar cambios |
| `filemd5()` | `storage.tf` | Etag del system prompt para forzar actualización en S3 |

### Meta-argumentos

| Meta-argumento | Dónde | Por qué |
|----------------|-------|---------|
| `for_each` | `cloudwatch.tf` (13 log groups), `lambda.tf` (7 Lambdas Saga), `modules/auth/main.tf` (grupos Cognito) | Crea múltiples recursos desde un map sin repetir el bloque |
| `depends_on` | `lambda.tf`, `database.tf`, `main.tf` | Garantiza orden de creación: RDS Proxy y Secrets antes de la Lambda analytics; chatbot module después de todos sus inputs |
| `lifecycle { prevent_destroy }` | `database.tf` | Protege DynamoDB y RDS contra `terraform destroy` accidental |
| `lifecycle { create_before_destroy }` | `lambda.tf`, `modules/chatbot-lambda/main.tf`, `modules/auth/main.tf` | Zero downtime al actualizar Lambdas y API Gateway deployments |
