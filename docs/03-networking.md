# 03 — Networking

## Conceptos base

### VPC (Virtual Private Cloud)

La VPC es la red privada que contiene todos los recursos de la aplicación en AWS. Es como el edificio donde viven los servidores — vos decidís qué puertas tiene, quién puede entrar desde afuera y quién puede salir.

Sin VPC, los recursos flotarían expuestos en la infraestructura de AWS sin ningún aislamiento.

### Internet Gateway (IGW)

El Internet Gateway es la puerta de entrada y salida de internet hacia la VPC. Sin él, ningún recurso dentro de la VPC puede recibir tráfico de internet ni salir hacia él.

Se asocia a la VPC (uno por VPC) y se referencia en las route tables de las subnets públicas.

```
Internet ←──→ Internet Gateway ←──→ VPC
```

### Subnets

Una subnet es una división de la red dentro de la VPC. Hay dos tipos:

**Subnet pública** — su route table tiene una ruta hacia el Internet Gateway.
Los recursos acá son accesibles desde internet (si tienen IP pública).

**Subnet privada** — su route table NO tiene ruta al Internet Gateway.
Los recursos acá están aislados. Nadie de afuera puede llegar directamente.

### Route Tables

Una route table es una tabla de reglas que define adónde va el tráfico de red. Cada subnet está asociada a una route table.

**Route table pública:**
```
Destino          →  Vía
10.0.0.0/16      →  local (tráfico interno de la VPC)
0.0.0.0/0        →  Internet Gateway
```

**Route table privada:**
```
Destino          →  Vía
10.0.0.0/16      →  local (tráfico interno de la VPC)
0.0.0.0/0        →  NAT Gateway
```

La diferencia clave: la privada no apunta al IGW directamente — apunta al NAT Gateway para salidas a internet.

### NAT Gateway

La Lambda `analytics-processor` corre dentro de la VPC (necesita acceso a RDS en subnet privada) pero también puede necesitar salida a internet. El NAT Gateway resuelve esto.

Vive en la subnet pública y actúa de intermediario:

```
Lambda analytics (subnet privada)
    → NAT Gateway (subnet pública)
        → Internet Gateway
            → internet
```

Desde afuera nadie puede entrar a la Lambda. Pero la Lambda sí puede salir. El NAT Gateway "traduce" la dirección privada a una IP pública para que pueda salir a internet.

**Nota:** la Lambda `chat-handler` **no está en la VPC** — necesita llamar a la API de Anthropic directamente y ponerla en VPC requeriría un NAT Gateway adicional solo para eso. Al estar fuera de la VPC, llama a internet directamente sin pasar por la red interna.

### Zonas de disponibilidad (AZs)

AWS divide cada región en zonas de disponibilidad físicamente separadas (distintos centros de datos). Si una zona falla, las otras siguen funcionando.

La arquitectura usa **2 AZs** (AZ-a y AZ-b) para:
1. **Alta disponibilidad**: si una AZ cae, la aplicación sigue corriendo en la otra.
2. **Requisito de RDS**: RDS necesita al menos 2 subnets en distintas AZs para el subnet group.

---

## Diseño de subnets

La arquitectura tiene 6 subnets distribuidas en 2 AZs:

```
VPC: 10.0.0.0/16

┌─────────────────────────┬─────────────────────────┐
│   AZ-a (us-east-1a)     │   AZ-b (us-east-1b)     │
├─────────────────────────┼─────────────────────────┤
│ public-a  10.0.1.0/24   │ public-b  10.0.2.0/24   │
│ - NAT Gateway           │                         │
├─────────────────────────┼─────────────────────────┤
│ private-compute-a        │ private-compute-b       │
│   10.0.3.0/24           │   10.0.4.0/24           │
│ - Lambda analytics      │ - Lambda analytics      │
├─────────────────────────┼─────────────────────────┤
│ private-data-a           │ private-data-b          │
│   10.0.5.0/24           │   10.0.6.0/24           │
│ - RDS PostgreSQL        │ - RDS subnet group      │
└─────────────────────────┴─────────────────────────┘
```

Las Lambdas del flujo de chat y pagos **no están en la VPC** — corren en la infraestructura administrada de AWS y acceden a DynamoDB, SNS y SQS directamente por endpoints públicos de AWS (sin pasar por la VPC).

---

## Security Groups

Un Security Group es un firewall a nivel de recurso. Define qué tráfico puede entrar y salir de cada componente.

La regla de diseño: **cada recurso solo acepta tráfico del componente que necesita hablarle**.

### SG-Lambda (analytics-processor en VPC)
```
Entrada:  (ninguna — Lambda no recibe conexiones entrantes)
Salida:   puerto 5432 hacia SG-RDS-Proxy (PostgreSQL)
          puerto 443 dentro del CIDR de la VPC (hacia VPC Endpoints)
```

### SG-RDS-Proxy
```
Entrada:  puerto 5432 desde SG-Lambda únicamente
Salida:   puerto 5432 hacia SG-RDS únicamente
```

El RDS Proxy está entre Lambda y RDS. Lambda no habla directamente con RDS — habla con el proxy, que mantiene el pool de conexiones.

### SG-RDS
```
Entrada:  puerto 5432 desde SG-RDS-Proxy
          puerto 5432 desde SG-Bastion (acceso operativo)
Salida:   ninguna
```

### SG-Bastion
```
Entrada:  ninguna (sin SSH, sin puerto 22)
Salida:   todo outbound (necesita conectarse a SSM y a RDS)
```

### SG-VPC-Endpoints
```
Entrada:  puerto 443 desde SG-Lambda únicamente
Salida:   ninguna
```

---

## VPC Endpoints

Los VPC Endpoints permiten que recursos dentro de la VPC se comuniquen con servicios de AWS sin que el tráfico salga a internet. Evitan pasar por el NAT Gateway, lo que reduce costos y mejora la seguridad.

Los tres son de tipo **Interface** (crean una ENI dentro de la subnet privada con `private_dns_enabled = true`):

### Secrets Manager Interface Endpoint
La Lambda analytics lee las credenciales de RDS desde Secrets Manager sin salir a internet.

```
Lambda analytics-processor → VPC Interface Endpoint → Secrets Manager
```

### SQS Interface Endpoint
La Lambda analytics consume mensajes de la cola `analytics-queue` desde dentro de la VPC, sin pasar por el NAT Gateway.

```
Lambda analytics-processor → VPC Interface Endpoint → SQS analytics-queue
```

### CloudWatch Logs Interface Endpoint
La Lambda analytics escribe sus logs directamente en CloudWatch sin salir a internet.

```
Lambda analytics-processor → VPC Interface Endpoint → CloudWatch Logs
```

---

## Resumen del flujo de red

### Chat (fuera de la VPC)

```
Usuario
  │
  ↓ HTTPS
API Gateway
  │
  ↓ invocación
Lambda chat-handler (fuera de VPC)
  │
  ├──→ DynamoDB (endpoint público de AWS)
  ├──→ Secrets Manager (endpoint público de AWS)
  ├──→ SNS (endpoint público de AWS)
  └──→ Anthropic API (internet — sin VPC, sin NAT)
```

### Analytics (dentro de la VPC)

```
SQS analytics-queue (VPC Interface Endpoint)
  │
  ↓ trigger
Lambda analytics-processor (subnet privada — cómputo)
  │
  ├──→ RDS Proxy (subnet privada — cómputo) → RDS PostgreSQL (subnet privada — datos)
  ├──→ SQS (VPC Interface Endpoint — sin internet)
  ├──→ Secrets Manager (VPC Interface Endpoint — sin internet)
  └──→ CloudWatch Logs (VPC Interface Endpoint — sin internet)
```
