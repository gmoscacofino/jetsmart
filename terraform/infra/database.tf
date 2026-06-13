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
