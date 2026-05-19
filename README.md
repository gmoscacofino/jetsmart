# TP3 — JetSmart Chatbot con Terraform
### Cloud Computing — 2026Q1 — ITBA

## Introducción

JetSmart Chatbot es un asistente conversacional desplegado en AWS con Terraform que replica la experiencia de compra de JetSmart. El usuario puede reservar vuelos, hacer check-in y gestionar reservas en lenguaje natural. La IA es opcional: por defecto el sistema corre en **modo demo** con respuestas predefinidas, sin necesitar una API key de Anthropic.

## Diagrama
<img src="docs/Jetsmart - Diagrama.png" alt="Diagrama de Arquitectura" width="110%">

> Tocar sobre la imagen del diagrama para apliarla y visualizarla mejor

## Requerimientos

### Cuenta y credenciales AWS

| Requisito | Detalle |
|-----------|---------|
| Cuenta AWS | Permisos para VPC, RDS, Lambda, S3, DynamoDB, Cognito, Step Functions, etc. |
| Rol **LabRole** | Pre-existente en AWS Academy (`data.aws_iam_role.lab_role` en Terraform) |
| AWS CLI v2 | Credenciales en `~/.aws/credentials` o variables de entorno |


## Instrucciones de ejecución

El flujo es via **GitHub Actions**, no requiere instalar Terraform ni AWS CLI localmente. Solo se necesitan credenciales de AWS Academy y acceso al repositorio.

### Paso 1 — Configurar secrets en GitHub

Ir a **Settings → Secrets and variables → Actions → New repository secret**.

| Name | Secret |
|------|--------|
| `AWS_ACCESS_KEY_ID` | valor de `aws_access_key_id` |
| `AWS_SECRET_ACCESS_KEY` | valor de `aws_secret_access_key` |
| `AWS_SESSION_TOKEN` | valor de `aws_session_token` |
| `STATE_BUCKET_SUFFIX` | sufijo único para el bucket de estado — solo minúsculas, números y guiones (ej. `grupo8-2026`). Los nombres de bucket S3 son globales: si el job `backend` falla con `BucketAlreadyExists`, cambiar este sufijo por uno diferente (ej. `grupo8-2026b`) |
| `TF_VAR_RDS_PASSWORD` | contraseña para la base de datos RDS |
| `TF_VAR_ANTHROPIC_API_KEY` | Cargar la API key de Anthropic que fue entregada al docente por separado. |

### Paso 2 — Crear el backend (primera vez)

Ir a **Actions → Terraform → Run workflow**, seleccionar **`backend`** y ejecutar.

Crea el bucket S3 `jetsmart-terraform-state-<STATE_BUCKET_SUFFIX>` que almacena el state de Terraform.

### Paso 3 — Planificar la infraestructura

Ir a **Actions → Terraform → Run workflow**, seleccionar **`plan`** y ejecutar.

Muestra todos los recursos que se van a crear sin modificar nada. Revisar el output antes de aplicar.

### Paso 4 — Aplicar la infraestructura

Ir a **Actions → Terraform → Run workflow**, seleccionar **`apply`** y ejecutar.

> Tiempo estimado: **15–20 minutos**.

Al finalizar, Terraform ejecuta automáticamente la Lambda de migración para crear el schema de RDS y sube el frontend al bucket S3.

### Paso 5 — Ver los outputs del deploy

Al terminar el apply, hacer clic en el job **Apply** y luego en la pestaña **Summary**. El workflow imprime las URLs de acceso:

| Recurso | Comportamiento esperado |
|---------|------------------------|
| **Frontend** | Abrirla en el browser muestra la app con el botón "Iniciar sesión" |
| **Chatbot API** | GET sin token → `401 Unauthorized`; con JWT válido → respuesta del chatbot |
| **Cognito Hosted UI** | Muestra el formulario de login/signup de AWS Cognito |
| **Auth Callback** | GET sin parámetros → `302` al frontend con `#error=missing_code` |

### Destruir la infraestructura

Ir a **Actions → Terraform → Run workflow**, seleccionar **`destroy`** y ejecutar. Destruye todos los recursos de la infraestructura.

## Verificación

Tras el deploy, abrir la URL de `frontend_url` que aparece en el Summary del job Apply.

### Paso previo — Crear cuenta

1. Abrir la URL del frontend en el browser.
2. Hacer clic en **Iniciar sesión** → redirige a la Hosted UI de Cognito.
3. Crear una cuenta nueva con email y contraseña (mínimo 8 caracteres, una mayúscula y un número).
4. Confirmar el email con el código que llega por correo.
5. Después de confirmar, el browser vuelve automáticamente al frontend con la sesión activa.

---

### Chatbot con Claude

El chatbot usa **Claude Haiku** con acceso real a los datos en DynamoDB. Las respuestas son libres en lenguaje natural y el flujo de compra ejecuta Step Functions.

El apply carga automáticamente **~660 vuelos de ejemplo** en DynamoDB (`scripts/seed_flights.py`): 20 rutas operadas por JetSmart (AEP↔SCL, AEP↔MDZ, AEP↔COR, AEP↔IGR, SCL↔ANF, SCL↔COR, SCL↔IGR) con vuelos los lunes, miércoles y viernes de los próximos 75 días. Los viernes tienen un precio ~15% más alto.

Rutas disponibles:

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

Flujo de prueba recomendado:

1. **Buscar vuelos** → `"¿Qué vuelos hay de Buenos Aires a Mendoza?"`
   - Claude llama a `list_flight_dates` y luego `search_flights` y devuelve disponibilidad real.
2. **Reservar** → completar datos del pasajero cuando el chatbot los pida, confirmar con `"sí, confirmar"`.
   - Se inicia una ejecución de Step Functions. El estado pasa por PENDIENTE → CONFIRMADA en segundos.
3. **Consultar reserva** → `"¿Cuál es el estado de mi reserva?"` → Claude llama a `list_user_reservations`.
4. **Check-in** → disponible únicamente las 24 horas previas al vuelo.
5. **Reclamos** → `"Quiero reportar un problema con mi vuelo"` → Claude registra el reclamo en DynamoDB.

Para verificar las ejecuciones de Step Functions:
```bash
aws stepfunctions list-executions \
  --state-machine-arn $(terraform -chdir=terraform/infra output -raw step_functions_arn) \
  --region us-east-1
```

---

### Acceso al bastion y consultas a RDS

El bastion es una instancia EC2 en la subnet pública accesible **solo via SSM** (sin SSH ni puerto 22 abierto). Se usa para conectarse a la base de datos de analytics desde la máquina local.

**Requisitos locales:** AWS CLI v2 + [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).

**1. Obtener los valores necesarios:**
```bash
cd terraform/infra
BASTION_ID=$(terraform output -raw bastion_instance_id)
RDS_PROXY=$(terraform output -raw rds_proxy_endpoint)
```

**2. Abrir el túnel SSM (deja la terminal abierta):**
```bash
aws ssm start-session \
  --target "$BASTION_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$RDS_PROXY\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"5433\"]}" \
  --region us-east-1
```

**3. Conectar psql en otra terminal:**
```bash
psql -h localhost -p 5433 -U jetsmart_admin -d jetsmart_analytics
# Ingresar la contraseña configurada en TF_VAR_RDS_PASSWORD
```

**Consultas de ejemplo:**
```sql
-- Ver tablas creadas por la migración
\dt

-- Eventos de analytics registrados
SELECT event_type, user_id, created_at FROM analytics_events ORDER BY created_at DESC LIMIT 20;

-- Reservas completadas
SELECT * FROM bookings WHERE status = 'CONFIRMADA' ORDER BY created_at DESC;

-- Búsquedas de vuelos por ruta
SELECT ruta, COUNT(*) AS busquedas FROM analytics_events
WHERE event_type = 'busqueda_vuelo'
GROUP BY ruta ORDER BY busquedas DESC;
```

## Pipeline de GitHub Actions

El archivo `.github/workflows/terraform.yml` implementa tres jobs:

| Job | Cuándo corre | Credenciales AWS | Qué hace |
|-----|--------------|------------------|----------|
| `validate` | En cada `push` y en cada **PR** | No necesita | `init -backend=false`, `validate`, `fmt -check`, `terraform test` |
| `backend` | `workflow_dispatch` → `backend` | Sí | Crea el bucket S3 de estado (una sola vez por cuenta) |
| `deploy` | `workflow_dispatch` → `plan` o `apply` | Sí | `init` con backend S3, `plan`, `apply`, sync frontend → S3, imprime URLs en Summary |
| `destroy` | `workflow_dispatch` → `destroy` | Sí | `init` con backend S3, `destroy -auto-approve` |

El job `validate` corre siempre sin credenciales, garantizando que el código es válido en cada PR.

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
