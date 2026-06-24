# ── Networking: VPC ───────────────────────────────────────────────────────────
#
# Arquitectura de red del TP4 (re-arquitectura post-defensa):
#   - 2 subnets públicas      → ALB + NAT Gateway
#   - 2 subnets privadas (A)  → Fargate (chat-handler + weather-poller)
#   - 2 subnets privadas (B)  → Lambdas en VPC (payment Saga, refund, workers)
#
# Segmentación por routing, no por función: el cómputo que necesita salir a
# internet lo hace por el NAT; los servicios AWS se alcanzan por VPC endpoints
# (gateway gratis para S3/DynamoDB, interface para el resto).

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # requerido para private DNS de los interface endpoints

  tags = { Name = "${local.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name_prefix}-igw" }
}

# ── Subnets públicas (ALB + NAT) ──────────────────────────────────────────────

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index) # 10.0.0.0/24, 10.0.1.0/24
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name_prefix}-public-${count.index}" }
}

# ── Subnets privadas para Fargate ─────────────────────────────────────────────

resource "aws_subnet" "private_fargate" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10) # 10.0.10.0/24, 10.0.11.0/24
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${local.name_prefix}-private-fargate-${count.index}" }
}

# ── Subnets privadas para Lambdas ─────────────────────────────────────────────

resource "aws_subnet" "private_lambda" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 20) # 10.0.20.0/24, 10.0.21.0/24
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${local.name_prefix}-private-lambda-${count.index}" }
}

# ── NAT Gateway (1 — en una subnet pública) ───────────────────────────────────
#
# Decisión de budget: 1 NAT (single point of failure). En producción real iría
# 1 NAT por AZ para HA. Documentado como tensión de costo del sandbox Academy.

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name_prefix}-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id

  tags       = { Name = "${local.name_prefix}-nat" }
  depends_on = [aws_internet_gateway.main]
}

# ── Route tables ──────────────────────────────────────────────────────────────

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name_prefix}-rt-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = { Name = "${local.name_prefix}-rt-private" }
}

resource "aws_route_table_association" "private_fargate" {
  count          = 2
  subnet_id      = aws_subnet.private_fargate[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_lambda" {
  count          = 2
  subnet_id      = aws_subnet.private_lambda[count.index].id
  route_table_id = aws_route_table.private.id
}

# ── Gateway VPC Endpoints (gratis — sólo S3 y DynamoDB) ───────────────────────

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name_prefix}-vpce-s3" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name_prefix}-vpce-dynamodb" }
}

# ── Interface VPC Endpoints ───────────────────────────────────────────────────
#
# ENIs en las 2 subnets privadas-fargate (una por AZ) — alcanzables por todo el
# cómputo de la VPC vía routing local. Cubren el tráfico AWS que NO debe salir
# por NAT (least-privilege de egress) y, crítico para Fargate, ECR + Logs para
# que las tasks puedan pullear la imagen y loguear desde subnet privada.

locals {
  interface_endpoints = [
    "sns",
    "secretsmanager",
    "states",  # Step Functions
    "ecr.api", # ECR control plane
    "ecr.dkr", # ECR docker registry (pull de imagen)
    "logs",    # CloudWatch Logs (awslogs de Fargate)
  ]
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(local.interface_endpoints)

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private_fargate[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-vpce-${each.value}" }
}
