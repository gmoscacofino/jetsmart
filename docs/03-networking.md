# 03 — Networking

## Por qué no hay VPC

La arquitectura **TP4 no usa VPC**. Esta es una decisión deliberada después del feedback del TP3.

### Regla aplicada

Las VPCs sirven para **aislar recursos con identidad de red persistente**: EC2, RDS, ElastiCache, contenedores ECS/EKS, instancias de Elasticsearch, etc. Estos recursos tienen IPs, interfaces de red y necesitan reglas de tráfico definidas.

**Cuando una arquitectura es 100% Lambda + servicios managed regionales**, no hay nada para aislar. Las Lambdas no tienen IP persistente. DynamoDB, SNS, SQS, Step Functions, S3, Cognito, Glue, Athena son endpoints regionales gestionados por AWS — no viven en tu red, viven en la red de AWS.

Forzar una VPC en este escenario significa crear subnets, route tables, Security Groups, VPC Endpoints e ENIs sin un solo recurso adentro que los justifique. Es over-engineering.

### Comparación con el TP3

| Componente | TP3 (con VPC) | TP4 (sin VPC) |
|---|---|---|
| RDS PostgreSQL | En subnet privada de datos | **Eliminado** (data lake S3 + Athena) |
| RDS Proxy | En subnet privada de cómputo | **Eliminado** |
| Bastion EC2 | En subnet pública con SSM | **Eliminado** |
| Lambda analytics-processor | En VPC para acceder a RDS | Regional — escribe a S3 |
| VPC + 6 subnets + IGW + NAT GW | Sí | **Eliminados** |
| Security Groups (lambda, rds, rds_proxy, vpc_endpoints, bastion) | 5 SGs | **Eliminados** |
| VPC Endpoints (Secrets Manager, SQS, CloudWatch Logs) | 3 endpoints Interface | **Eliminados** |

### Trade-offs honestos

**Pierdo:**
- **VPC Flow Logs** — ya no veo el tráfico de red L3/L4 de las Lambdas. Útil para detectar exfiltración o port scans en un escenario comprometido.
- **Contención de egress** — sin VPC sin NAT, no puedo "encerrar" a las Lambdas para que no salgan a internet. Lo mitigo con IAM least-privilege: cada Lambda sólo tiene permisos para los servicios AWS específicos que necesita (LabRole de Academy es amplio, pero en producción real serían roles por función).

**Gano:**
- **Cold start mínimo** — sin VPC, las Lambdas arrancan en ~200ms. Con VPC y ENI hubieran sido 500ms-2s.
- **Costo cero de networking** — sin NAT Gateway (USD/hora), sin Interface Endpoints (USD/hora).
- **Menos puntos de falla** — sin SGs ni route tables que mantener, sin endpoints que puedan caerse.
- **Mantenibilidad** — el Terraform es ~40% más chico y ~60% más rápido de aplicar.

### Auditoría sin VPC Flow Logs

La pérdida de Flow Logs se compensa con:

- **CloudTrail** — registra toda API call de Lambda a otros servicios AWS. Quién (qué Lambda identificada por su execution role), cuándo, qué operación (PutItem, Publish, etc.), con qué parámetros, resultado. Está activo por default.
- **CloudWatch Logs** — todo lo que loggea la Lambda (`print()`, `logger.info()`). Errores, traces aplicativos.

CloudTrail es **estrictamente más útil para auditoría aplicativa** que Flow Logs. Flow Logs sirve para detección de comportamiento anómalo de red, no para "¿qué hizo cada función?".

---

## Edge networking — lo que sí hay

Aunque no hay VPC propia, la arquitectura interactúa con varios componentes de red:

### Internet Gateway implícito

Cada Lambda, al estar en la infraestructura administrada de AWS, sale a internet sin pasar por una red de cliente. Para el `chat-handler` que llama a la API de Anthropic, este es el camino directo (sin NAT, sin pérdida de latencia).

### S3 estático con HTTP

El frontend está en un bucket S3 con **website hosting** (HTTP). Es público por necesidad del modelo de site estático.

> Por qué no HTTPS en el frontend: AWS Academy no expone los plans de Route 53 + ACM + CloudFront fácilmente y agregaría mucho costo y tiempo de provisioning. Para el TP el HTTP es aceptable; en producción se pondría CloudFront delante con certificado ACM y dominio propio.

### Cognito Hosted UI con HTTPS

Cognito provee un endpoint HTTPS estable (`https://<domain>.auth.us-east-1.amazoncognito.com`). Es el único componente que recibe las credenciales del usuario.

### API Gateway con HTTPS

Cada API Gateway (chatbot-api y auth-api) tiene URL HTTPS estable. Esto es lo que habilita el **workaround Cognito**: la Lambda `auth-callback` detrás de API Gateway es el bridge HTTPS entre Cognito Hosted UI (HTTPS) y el frontend S3 (HTTP).

### Cognito Authorizer

API Gateway del chatbot valida el JWT con un Cognito Authorizer (`aws_api_gateway_authorizer` tipo `COGNITO_USER_POOLS`). Las requests sin token o con token inválido reciben `401` antes de llegar a Lambda — el rechazo ocurre en el edge.

**Excepción:** API Gateway de `auth-callback` queda con `authorization = "NONE"` porque Cognito redirige a ese endpoint con el `code` en query string (sin Authorization header). Es parte del workaround.

---

## Modelo de seguridad

Sin VPC, la seguridad se basa en cuatro capas:

| Capa | Mecanismo |
|---|---|
| Identidad del usuario | Cognito User Pool — registro y autenticación |
| Autorización en el perímetro | API Gateway Cognito Authorizer — rechaza JWT inválidos |
| Identidad de las Lambdas | LabRole con permisos limitados a los servicios que cada función usa |
| Encriptación | TLS para HTTPS (API GW, Cognito), SSE-S3 en buckets, SSE en DynamoDB |

Ningún recurso de datos tiene acceso público:
- DynamoDB sólo accesible por Lambdas con LabRole.
- S3 analytics y assets con `public_access_block` activo en todas las dimensiones.
- Secrets Manager sólo accesible por Lambdas autorizadas.
- S3 frontend público porque es **necesario para servir HTML/CSS/JS**, pero no contiene info sensible.

---

## ¿Qué pasaría si tuviéramos requisitos de compliance?

Si en producción necesitáramos cumplir un estándar tipo PCI-DSS o HIPAA, la respuesta sería:
1. Volver a meter Lambdas en VPC para tener VPC Flow Logs.
2. Agregar VPC Endpoints (Gateway gratis para DynamoDB, Interface para los demás) para que el tráfico no salga a internet.
3. Mover el bucket S3 a una bucket policy que sólo permita acceso via VPC Endpoint.
4. Reemplazar LabRole por roles IAM per-función con least-privilege estricto.

Para el escenario actual (TP académico, chatbot de demo), todo eso es over-engineering.
