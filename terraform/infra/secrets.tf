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
