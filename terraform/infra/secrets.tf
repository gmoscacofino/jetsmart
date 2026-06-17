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

# ── PII Tokenizer Secret ──────────────────────────────────────────────────────
#
# Clave HMAC para generar tokens determinísticos de PII en chat_handler.
# Se regenera con cada destroy + apply. Se persiste en state mientras dure
# el lab. Como es deterministic por sesión, el lifecycle de la sesión (24h
# TTL de tokens) ya tolera regeneraciones esporádicas.
#
# NO se guarda en Secrets Manager (los Lambdas solo consumen como env var
# en runtime; agregar GetSecretValue añadiría una llamada extra en cold start).

resource "random_password" "pii_token_secret" {
  length  = 64
  special = false
}
