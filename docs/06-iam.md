# 06 — IAM: Roles, Grupos y Permisos

## Dos tipos de identidades

Hay que distinguir dos contextos completamente distintos:

1. **Roles de servicio** — los usa el código (Lambdas). Permiten que los servicios de AWS se hablen entre sí.
2. **Grupos IAM** — los usan las personas del equipo. Definen qué puede hacer cada integrante en la consola de AWS.

Además, hay un tercer contexto:

3. **Grupos de Cognito** — los usan los usuarios finales de la aplicación (no el equipo, sino las personas que usan el chatbot).

---

## 1. Roles de servicio — AWS Academy LabRole

### Restricción de AWS Academy

En AWS Academy no se pueden crear IAM roles personalizados. El entorno provee un rol único preconfigurado llamado **LabRole** que tienen todos los servicios que necesitan permisos.

```hcl
data "aws_iam_role" "lab_role" {
  name = "LabRole"
}
```

Todas las Lambdas usan este mismo `LabRole` como `role` en su configuración de Terraform. LabRole tiene permisos amplios sobre los servicios de AWS Academy.

### Consecuencia en el diseño

En producción real, cada Lambda tendría su propio rol con permisos mínimos (principio de least privilege). Por ejemplo:
- `chat-handler` tendría permisos solo para leer/escribir en DynamoDB y publicar en SNS
- `analytics-processor` tendría permisos solo para leer SQS, leer Secrets Manager y conectarse a RDS
- `boarding-pass` tendría permisos solo para escribir en S3

En este TP todas usan LabRole. Se documenta esta decisión como limitación del entorno de Academy, no como práctica recomendada.

### Referencia en Terraform

```hcl
resource "aws_lambda_function" "chat_handler" {
  role = data.aws_iam_role.lab_role.arn
  ...
}

resource "aws_lambda_function" "analytics_processor" {
  role = data.aws_iam_role.lab_role.arn
  ...
}
```

### Permisos que LabRole tiene (y que se usarían en roles granulares en prod)

| Lambda | Permisos necesarios en prod |
|---|---|
| chat-handler | DynamoDB (R/W tabla jetsmart), Secrets Manager (read), SNS (publish events) |
| reservations | DynamoDB (read tabla jetsmart) |
| admin-metrics | Secrets Manager (read RDS creds), CloudWatch Logs |
| payment-initiate | SQS (sendMessage a payment-validate-queue) |
| payment-validate | DynamoDB (read FLIGHT#), SNS (publish payment-validated) |
| payment-reserve | DynamoDB (R/W RESERVATION#, FLIGHT#), SNS (publish payment-reserved) |
| payment-process | SNS (publish payment-processed) |
| payment-confirm | DynamoDB (update RESERVATION#), SNS (publish payment-completed) |
| boarding-pass | DynamoDB (read RESERVATION#), S3 (put jetsmart-assets), SQS (deleteMessage) |
| notification | SQS (deleteMessage), CloudWatch Logs |
| analytics-processor | SQS (read/delete analytics-queue), Secrets Manager (read), CloudWatch Logs |
| auth-callback | CloudWatch Logs |
| cognito-trigger | DynamoDB (put USER#), Cognito (addUserToGroup), CloudWatch Logs |

---

## 2. Grupos IAM del equipo

Cada integrante del equipo pertenece a un grupo IAM que define qué puede hacer en la cuenta de AWS Academy.

### Grupo: `infra-devops`

Se encarga de: VPC, subnets, S3, deploys de Lambdas, arquitectura de mensajería.

Permisos:
- Lambda: deploy y actualizar funciones
- S3: leer y escribir en ambos buckets
- SNS: crear y gestionar topics
- SQS: crear y gestionar queues
- CloudWatch: leer logs y métricas
- VPC: solo lectura (para troubleshooting)

### Grupo: `backend-dev`

Se encarga de: código de las Lambdas, integración con DynamoDB, SNS, SQS y Anthropic.

Permisos:
- Lambda: deploy y ver logs
- DynamoDB: acceso completo a tablas del proyecto
- SQS: enviar, recibir y ver mensajes
- SNS: publicar en topics del proyecto
- Secrets Manager: solo lectura (no puede crear ni modificar secretos)
- CloudWatch: ver logs de Lambdas

### Grupo: `security-auth`

Se encarga de: Cognito, IAM (roles y políticas), Secrets Manager.

Permisos:
- Cognito: acceso completo
- IAM: gestionar roles y políticas (con restricción de no poder escalar sus propios permisos)
- Secrets Manager: crear, modificar y rotar secretos

### Grupo: `analytics`

Se encarga de: RDS, dashboard admin, Lambda analytics, exportaciones a S3.

Permisos:
- RDS: acceso completo al cluster del proyecto
- CloudWatch: leer métricas y logs
- S3 `jetsmart-assets`: solo lectura (para ver backups y exports)
- DynamoDB: solo lectura (para consultas puntuales si es necesario)
- SQS: solo lectura de la queue analytics (monitoreo)

---

## 3. Grupos de Cognito (usuarios de la app)

Estos grupos no son de AWS IAM — son grupos dentro del User Pool de Cognito. Definen qué ve cada usuario dentro de la aplicación.

### Grupo: `users`

Todos los usuarios registrados normales. Tienen acceso a:
- El chatbot completo (compra, check-in, estado de vuelo, reclamos, gestionar reserva, boarding pass)
- Su propio historial y reservas

### Grupo: `admins`

Usuarios con acceso al dashboard de analytics. Tienen acceso a:
- Todo lo que tiene `users`
- El dashboard de métricas del administrador

La Lambda del trigger post-registro asigna automáticamente el grupo `users` a cada nuevo usuario. Para promover a alguien a `admins`, se hace manualmente desde la consola de Cognito o via Terraform.

---

## Resumen visual

```
PERSONAS DEL EQUIPO (IAM Groups)
├── infra-devops    → Lambda deploys, S3, SNS, SQS, VPC
├── backend-dev     → Lambda código, DynamoDB, SNS, SQS
├── security-auth   → Cognito, IAM, Secrets Manager
└── analytics       → RDS, métricas, exports

SERVICIOS AWS (IAM Roles)
└── LabRole (único, compartido por todas las Lambdas — restricción de Academy)
    En producción, cada Lambda tendría su propio rol con permisos mínimos.

USUARIOS DE LA APP (Cognito Groups)
├── users   → acceso al chatbot
└── admins  → acceso al chatbot + dashboard analytics
```
