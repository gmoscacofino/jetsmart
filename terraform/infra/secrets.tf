# ── Anthropic API Key ─────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "anthropic_key" {
  name                    = "${local.name_prefix}/anthropic-api-key"
  description             = "Anthropic Claude API key for the JetSmart chatbot backend"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "anthropic_key" {
  secret_id     = aws_secretsmanager_secret.anthropic_key.id
  secret_string = jsonencode({ api_key = var.anthropic_api_key })
}

# ── RDS Credentials ───────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "rds_credentials" {
  name                    = "${local.name_prefix}/rds-credentials"
  description             = "PostgreSQL credentials for the JetSmart analytics database"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "rds_credentials" {
  secret_id = aws_secretsmanager_secret.rds_credentials.id

  secret_string = jsonencode({
    host     = aws_db_instance.rds.address
    port     = aws_db_instance.rds.port
    dbname   = var.rds_db_name
    username = var.rds_username
    password = var.rds_password
  })

  depends_on = [aws_db_instance.rds]
}
