# ── DynamoDB — Dos tablas (Bounded Contexts: Conversations + PSS Business) ─────
#
# TP4: separamos el dominio del chatbot (conversaciones efímeras) del dominio de
# negocio (vuelos, reservas PNR-céntricas, pasajeros). Esto da:
#   - failure isolation: si el chatbot se satura, no afecta el core de negocio
#   - retention policies independientes (chat tiene TTL, negocio persistente)
#   - prepara la arquitectura para sumar otros canales (web/mobile/IVR) que
#     compartirían business table pero tendrían su propio conversation store

# ── Tabla 1: Conversations ────────────────────────────────────────────────────
#
# Estado efímero del chatbot:
#   SESSION#{id}    / MSG#{ts}#{uid}              — historial de chat (TTL 7d)
#   USER#{id}       / #METADATA                   — perfil chat-scoped (email, last_seen)
#   SESSION#{id}    / HANDOFF#{ts}#{handoff_id}   — ticket de derivación a humano
#   USER#{id}       / HANDOFF#{handoff_id}        — thin pointer "mis derivaciones"

resource "aws_dynamodb_table" "conversations" {
  name         = "${local.name_prefix}-conversations"
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

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ── Tabla 2: Business / PSS-like ──────────────────────────────────────────────
#
# Datos persistentes del dominio de la aerolínea (Passenger Service System):
#   FLIGHT#{org}#{dst}     / DATE#{f}#FLIGHT#{vuelo}   — inventario de vuelos (precio, asientos, estado)
#   PNR#{pnr}              / #METADATA                  — reserva canónica (6-char PNR)
#   PNR#{pnr}              / SEGMENT#{seq}#{vuelo}#{f}  — leg del PNR (con gsi2pk para "quién vuela en X")
#   PNR#{pnr}              / PAX#{seq}                  — pasajero del PNR (con gsi3pk para "buscar PNR por DNI/email")
#   PNR#{pnr}              / BP#{seq}                   — referencia al boarding pass en S3
#   USER#{id}              / RESERVATION#{pnr}          — thin pointer denormalizado para "mis reservas"
#   PASSENGER#{dni}        / #PROFILE                   — CRM canónico del frecuente
#   PASSENGER#{dni}        / PNR#{pnr}                  — back-ref histórico
#   CLAIM#{id}             / #METADATA                  — reclamo canónico
#   USER#{id}              / CLAIM#{id}                  — thin pointer "mis reclamos"
#
# 3 GSIs:
#   GSI1 FlightByNumber       — consulta status por número de vuelo + fecha
#   GSI2 ReservationsByFlight — "quiénes están en el vuelo X del día Y" (proactive notifications)
#   GSI3 ReservationsByPassenger — buscar PNR por DNI o email (call center)

resource "aws_dynamodb_table" "business" {
  name         = "${local.name_prefix}-business"
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

  # GSI1: estado de vuelo por número + fecha
  attribute {
    name = "vuelo_numero"
    type = "S"
  }

  attribute {
    name = "fecha"
    type = "S"
  }

  # GSI2: "quiénes están afectados por una cancelación de vuelo X / fecha Y"
  attribute {
    name = "gsi2pk"
    type = "S"
  }

  attribute {
    name = "gsi2sk"
    type = "S"
  }

  # GSI3: buscar PNR por identificador del pasajero (DNI o email)
  attribute {
    name = "gsi3pk"
    type = "S"
  }

  attribute {
    name = "gsi3sk"
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
      "demora_minutos",
      "origen",
      "destino",
    ]
  }

  global_secondary_index {
    name            = "ReservationsByFlight"
    hash_key        = "gsi2pk"
    range_key       = "gsi2sk"
    projection_type = "INCLUDE"
    non_key_attributes = [
      "user_id",
      "email",
      "passenger_name",
      "status",
      "origen",
      "destino",
    ]
  }

  global_secondary_index {
    name            = "ReservationsByPassenger"
    hash_key        = "gsi3pk"
    range_key       = "gsi3sk"
    projection_type = "KEYS_ONLY"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}
