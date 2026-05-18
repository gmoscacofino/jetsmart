# ── Security Groups ───────────────────────────────────────────────────────────

# Lambda analytics en VPC: solo salida (Lambda no recibe tráfico inbound desde la VPC)
resource "aws_security_group" "lambda" {
  name        = "${local.name_prefix}-sg-lambda"
  description = "Analytics Lambda en VPC: salida a RDS Proxy y VPC endpoints"
  vpc_id      = module.vpc.vpc_id

  egress {
    description = "HTTPS a VPC endpoints (Secrets Manager, SQS, CloudWatch)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

# Regla separada para evitar dependencia circular entre sg-lambda y sg-rds-proxy
resource "aws_security_group_rule" "lambda_to_proxy" {
  type                     = "egress"
  description              = "PostgreSQL a RDS Proxy"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.rds_proxy.id
  security_group_id        = aws_security_group.lambda.id
}

# RDS Proxy: acepta conexiones desde Lambda, sale hacia RDS
resource "aws_security_group" "rds_proxy" {
  name        = "${local.name_prefix}-sg-rds-proxy"
  description = "RDS Proxy: ingress desde Lambda, egress hacia RDS"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "PostgreSQL desde Lambda analytics"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }

  egress {
    description     = "PostgreSQL hacia RDS"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.rds.id]
  }
}

# RDS: acepta conexiones solo desde RDS Proxy y Bastion
resource "aws_security_group" "rds" {
  name        = "${local.name_prefix}-sg-rds"
  description = "PostgreSQL accesible solo desde RDS Proxy y Bastion"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "PostgreSQL desde RDS Proxy"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.rds_proxy.id]
  }
}

# VPC endpoints: acepta HTTPS desde Lambda analytics
resource "aws_security_group" "vpc_endpoints" {
  name        = "${local.name_prefix}-sg-vpc-endpoints"
  description = "HTTPS desde Lambda analytics hacia VPC interface endpoints"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "HTTPS desde Lambda analytics"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }
}

# ── VPC Endpoints ─────────────────────────────────────────────────────────────

# Interface endpoint para Secrets Manager
resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(module.vpc.private_subnets, 0, 2)
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

# Interface endpoint para SQS — analytics_processor consume desde SQS dentro de la VPC
resource "aws_vpc_endpoint" "sqs" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(module.vpc.private_subnets, 0, 2)
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

# Interface endpoint para CloudWatch Logs — analytics_processor escribe logs desde la VPC
resource "aws_vpc_endpoint" "cloudwatch_logs" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(module.vpc.private_subnets, 0, 2)
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}
