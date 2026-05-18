# ── DynamoDB — Single Table Design ───────────────────────────────────────────

resource "aws_dynamodb_table" "main" {
  name         = "${local.name_prefix}-main"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # GSI1: consulta de vuelos por número (estado de vuelo)
  attribute {
    name = "vuelo_numero"
    type = "S"
  }

  attribute {
    name = "fecha"
    type = "S"
  }

  global_secondary_index {
    name            = "FlightByNumber"
    hash_key        = "vuelo_numero"
    range_key       = "fecha"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "estado_vuelo",
      "horario_salida_real",
      "puerta",
      "demora_minutos"
    ]
  }

  # TTL: mensajes de chat se eliminan automáticamente después de 90 días
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # PITR: restauración a cualquier punto de los últimos 35 días
  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

}

# ── RDS PostgreSQL — Analytics ────────────────────────────────────────────────

resource "aws_db_subnet_group" "rds" {
  name        = "${local.name_prefix}-rds-subnet-group"
  description = "Subnet group for JetSmart RDS instance"
  subnet_ids  = slice(module.vpc.private_subnets, 2, 4)
}

resource "aws_db_instance" "rds" {
  identifier        = "${local.name_prefix}-rds"
  engine            = "postgres"
  engine_version    = "15"
  instance_class    = var.rds_instance_class
  allocated_storage = var.rds_allocated_storage
  storage_type      = "gp2"
  storage_encrypted = true

  db_name  = var.rds_db_name
  username = var.rds_username
  password = var.rds_password

  db_subnet_group_name   = aws_db_subnet_group.rds.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  # Backups automáticos — 7 días de retención
  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "Mon:04:00-Mon:05:00"

  # No Multi-AZ en Academy para reducir costo
  multi_az = false

  deletion_protection = false
  skip_final_snapshot = true
}

# ── RDS Proxy ─────────────────────────────────────────────────────────────────
#
# Pool de conexiones entre Lambda analytics y RDS.
# Evita que cada instancia de Lambda abra su propia conexión directa a RDS.
# Lee las credenciales de Secrets Manager para autenticarse contra RDS.

resource "aws_db_proxy" "main" {
  name                   = "${local.name_prefix}-rds-proxy"
  debug_logging          = false
  engine_family          = "POSTGRESQL"
  idle_client_timeout    = 1800
  require_tls            = true
  role_arn               = data.aws_iam_role.lab_role.arn
  vpc_subnet_ids         = slice(module.vpc.private_subnets, 0, 2)
  vpc_security_group_ids = [aws_security_group.rds_proxy.id]

  auth {
    auth_scheme = "SECRETS"
    secret_arn  = aws_secretsmanager_secret.rds_credentials.arn
    iam_auth    = "DISABLED"
  }

  depends_on = [aws_db_instance.rds]
}

resource "aws_db_proxy_default_target_group" "main" {
  db_proxy_name = aws_db_proxy.main.name

  connection_pool_config {
    max_connections_percent   = 100
    connection_borrow_timeout = 15
  }
}

resource "aws_db_proxy_target" "main" {
  db_proxy_name          = aws_db_proxy.main.name
  target_group_name      = aws_db_proxy_default_target_group.main.name
  db_instance_identifier = aws_db_instance.rds.identifier
}

