# 03 — Networking

## Por qué hay VPC

La arquitectura **usa una VPC** (`10.0.0.0/16`). Esta es la corrección central tras el feedback del TP3 (defensa del 17/06): el **core del chatbot dejó de ser Lambdas sueltas y pasó a correr en ECS Fargate dentro de subnets privadas**. Un cómputo en contenedor con identidad de red persistente (ENIs, IPs, reglas de tráfico) es exactamente lo que una VPC sirve para aislar.

### Regla aplicada

Las VPCs sirven para **aislar recursos con identidad de red persistente**: EC2, RDS, ElastiCache, **contenedores ECS/Fargate**, etc. Estos recursos tienen IPs, interfaces de red y necesitan reglas de tráfico definidas.

En esta arquitectura el recurso que justifica la VPC es **Fargate**: las tasks de `chat-handler` y `weather-poller` viven en subnets privadas, sin IP pública, y se exponen al exterior sólo a través del ALB (chat-handler) o del NAT (egress del poller). Sin VPC no se puede correr Fargate `awsvpc` con esa postura de aislamiento.

### Qué vive en la VPC y qué no

| Componente | Ubicación | Por qué |
|---|---|---|
| **chat-handler** (Fargate) | Subnets privadas `private-fargate` | Core del chatbot, expuesto sólo vía ALB |
| **weather-poller** (Fargate) | Subnets privadas `private-fargate` | Egress por NAT a la climAPI, sin inbound |
| **Lambdas** (Saga payment/refund, notification, workers) | Subnets privadas `private-lambda` (`vpc_config`) | Acceso a servicios AWS por endpoints + NAT |
| DynamoDB, SNS, SQS, Step Functions, S3, Secrets | Regionales (AWS) | Alcanzados por VPC endpoints, no viven en la red |
| Cognito, Glue, Athena | Regionales (AWS) | Servicios managed fuera de la VPC |

> **Nota sobre las Lambdas:** en el despliegue actual (`terraform/infra/lambda.tf`) todas las funciones llevan `vpc_config` apuntando a las subnets `private-lambda` y al SG `sg-lambda`. Es decir, **las Lambdas SÍ están en la VPC**. Esto difiere del diseño serverless anterior, donde eran regionales. Ver la nota al final sobre el trade-off de cold start que esto implica.

### Comparación con el diseño anterior (serverless rechazado)

| Componente | Diseño anterior (sin VPC) | Arquitectura actual (VPC + Fargate) |
|---|---|---|
| chat-handler | Lambda detrás de API Gateway | **Servicio Fargate** detrás de ALB, subnets privadas |
| weather-poller | — | **Task Fargate** (egress por NAT) |
| VPC | Ninguna (over-engineering según el diseño viejo) | `10.0.0.0/16` con 6 subnets |
| Subnets | — | 2 públicas + 2 private-fargate + 2 private-lambda |
| IGW / NAT Gateway | — | 1 IGW + 1 NAT Gateway |
| VPC Endpoints | — | 2 gateway (S3, DynamoDB) + 8 interface |
| Security Groups | — | 5 (alb, chat, poller, lambda, endpoints) |
| Lambdas | Regionales | En subnets `private-lambda` (`vpc_config`) |

---

## Topología de la VPC

VPC `10.0.0.0/16`, DNS support y DNS hostnames habilitados (`enable_dns_hostnames` es requisito del private DNS de los interface endpoints). Subnets desplegadas en 2 AZs para Multi-AZ real.

| Subnet | CIDR | AZ | Contenido |
|---|---|---|---|
| `public-0` / `public-1` | `10.0.0.0/24` / `10.0.1.0/24` | 2 AZs | ALB + NAT Gateway (`map_public_ip_on_launch`) |
| `private-fargate-0/1` | `10.0.10.0/24` / `10.0.11.0/24` | 2 AZs | Tasks Fargate (chat-handler, weather-poller) |
| `private-lambda-0/1` | `10.0.20.0/24` / `10.0.21.0/24` | 2 AZs | Lambdas en VPC (Saga, workers) |

### Routing

- **Route table pública** → `0.0.0.0/0` por el **Internet Gateway**. Asociada a las 2 subnets públicas.
- **Route table privada** → `0.0.0.0/0` por el **NAT Gateway**. Asociada a las 4 subnets privadas (fargate + lambda). El tráfico a servicios AWS con endpoint no sale por NAT: se resuelve por routing local hacia el endpoint.

### NAT Gateway

**1 solo NAT Gateway** en una subnet pública (con EIP). Es un single point of failure asumido por **budget del sandbox Academy**: en producción real iría 1 NAT por AZ para HA. Es una tensión de costo documentada, no una decisión de arquitectura.

### VPC Endpoints

Para que el tráfico a servicios AWS no salga por internet (least-privilege de egress) y para que Fargate pueda pullear la imagen y loguear desde subnet privada:

- **Gateway endpoints (gratis):** `S3` y `DynamoDB`. Se inyectan como rutas en la route table privada.
- **Interface endpoints (ENIs en las 2 subnets private-fargate, private DNS habilitado):**
  `sns`, `sqs`, `secretsmanager`, `states` (Step Functions), `ecr.api`, `ecr.dkr`, `logs` (CloudWatch Logs), `kinesis-firehose`.

`ecr.api` + `ecr.dkr` + `logs` son críticos para Fargate: sin ellos las tasks no pueden pullear la imagen del registry ni emitir logs `awslogs` desde una subnet privada. Por eso el `aws_ecs_service` depende explícitamente de los endpoints, S3 y el NAT.

---

## Edge networking — entrada y salida

### Application Load Balancer (entrada del chatbot)

El **ALB internet-facing** (`internal = false`, en las 2 subnets públicas) es el **punto de entrada del chat-handler**, reemplazando al API Gateway del diseño viejo.

- Listener **HTTP :80** → target group `chat-tg` (puerto **8000**, `target_type = "ip"` porque Fargate `awsvpc` registra targets por IP).
- Health check sobre `GET /health` (matcher 200, interval 30s).
- **No hay listener HTTPS:** Academy no habilita ACM. En producción real: ACM + listener 443 + auth Cognito nativa en el ALB.

### Internet Gateway / NAT para egress

El `chat-handler` que llama a la API de Anthropic, el `weather-poller` que llama a la climAPI y las Lambdas que necesitan salir lo hacen **por el NAT Gateway** (subnet privada → NAT → IGW). Ya no es la salida "implícita" de la infraestructura managed de Lambda: ahora el egress está contenido por la VPC y el NAT, que era justamente parte de lo pedido por Faustino.

### S3 estático con HTTP

El frontend está en un bucket S3 con **website hosting** (HTTP). Es público por necesidad del modelo de site estático.

> Por qué no HTTPS en el frontend: AWS Academy no expone Route 53 + ACM + CloudFront fácilmente y agregaría costo y tiempo de provisioning. Para el TP el HTTP es aceptable; en producción se pondría CloudFront delante con certificado ACM y dominio propio.

### Cognito Hosted UI con HTTPS

Cognito provee un endpoint HTTPS estable (`https://<domain>.auth.us-east-1.amazoncognito.com`). Es el único componente que recibe las credenciales del usuario.

### API Gateway con HTTPS (sólo auth-callback)

Queda **un solo** API Gateway: el de `auth-callback` (`jetsmart-prod-auth-api`, en `modules/auth`). Es el bridge HTTPS del **workaround Cognito**: la Lambda `auth-callback` detrás de API Gateway puentea entre Cognito Hosted UI (HTTPS) y el frontend S3 (HTTP). Su método queda con `authorization = "NONE"` porque Cognito redirige con el `code` en query string, sin Authorization header.

> El API Gateway del **chatbot** ya **no existe**: fue reemplazado por el ALB.

### Validación del JWT — in-app, no Cognito Authorizer

**No hay Cognito Authorizer en ningún lado.** Como el chatbot entra por ALB (HTTP, sin auth nativa por falta de ACM), el JWT de Cognito se valida **dentro del contenedor**: `server.py` verifica la firma RS256 contra el JWKS del User Pool, el issuer y la expiración antes de pasar los claims a la lógica. Las rutas autenticadas (`POST /api/chat`, `GET /api/reservations`, `POST /api/payment`) exigen token válido; `GET /health` queda sin auth para el health check del ALB.

---

## Modelo de seguridad

### Security Groups (least-privilege por referencia de SG)

El inbound se encadena por **referencia de SG**, no por CIDR: sólo el SG de origen correcto puede hablar con el destino.

| SG | Inbound | Egress |
|---|---|---|
| `sg-alb` | TCP **80** desde `0.0.0.0/0` (internet) | TCP 8000 → `sg-chat` |
| `sg-chat` (chat-handler) | TCP **8000** sólo desde `sg-alb` | Abierto (NAT: Anthropic, JWKS; endpoints) |
| `sg-poller` (weather-poller) | — (sin inbound) | Abierto (NAT: climAPI; endpoints) |
| `sg-lambda` (Lambdas en VPC) | — (sin inbound) | Abierto (endpoints + NAT) |
| `sg-endpoints` (interface endpoints) | TCP **443** desde `sg-chat`, `sg-poller`, `sg-lambda` | Abierto (respuestas) |

El egress se deja abierto en los SG de cómputo por correctness (DNS + 443 a endpoints/NAT). En producción real se acotaría el egress por destino.

### Capas de seguridad

| Capa | Mecanismo |
|---|---|
| Identidad del usuario | Cognito User Pool — registro y autenticación |
| Autorización de la app | JWT de Cognito validado **in-app** en el contenedor (RS256 contra JWKS) |
| Aislamiento de red | VPC: cómputo en subnets privadas, inbound encadenado por SG, sin IP pública |
| Identidad del cómputo | LabRole (task/execution role de ECS y execution role de Lambda; limitación Academy) |
| Encriptación | TLS para HTTPS (Cognito, API GW auth), SSE-S3 en buckets, SSE en DynamoDB |

Ningún recurso de datos tiene acceso público:
- DynamoDB sólo accesible por el cómputo con LabRole (vía gateway endpoint).
- S3 analytics y assets con `public_access_block` activo en todas las dimensiones.
- Secrets Manager sólo accesible por el cómputo autorizado (vía interface endpoint).
- S3 frontend público porque es **necesario para servir HTML/CSS/JS**, pero no contiene info sensible.

### Auditoría

- **CloudTrail** — registra toda API call del cómputo a otros servicios AWS (quién por su execution role, cuándo, qué operación, parámetros, resultado). Activo por default.
- **CloudWatch Logs** — logs aplicativos de Fargate (`awslogs`, log groups `/ecs/...-chat-handler` y `/ecs/...-weather-poller`, retención 30 días) y de las Lambdas.
- Con la VPC desplegada, **VPC Flow Logs** vuelve a ser viable para detección de comportamiento anómalo de red L3/L4 (no estaba disponible en el diseño sin VPC).

---

## Trade-off: cold start de las Lambdas en VPC

Meter las Lambdas en la VPC (`vpc_config`) tiene un costo: el aprovisionamiento de ENIs agrega latencia de cold start respecto de una Lambda regional. AWS mitigó esto con Hyperplane ENIs (las ENIs se comparten y pre-aprovisionan), así que el impacto hoy es mucho menor que los 500ms–2s del modelo viejo, pero sigue siendo no-cero. Se acepta a cambio del aislamiento de red y el egress contenido que pidió la re-arquitectura. Si una función resultara crítica en latencia y no necesitara la VPC, podría sacarse de `vpc_config` y volver a regional — pero el default de este despliegue es el aislamiento.
