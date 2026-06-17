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
# 2 GSIs:
#   GSI1 ReservationsByFlight — "quiénes están en el vuelo X del día Y" (proactive notifications)
#   GSI2 ReservationsByPassenger — buscar PNR por DNI o email (call center)
#
# (En TP4 había un tercer GSI FlightByNumber para consultar vuelos por número +
# fecha. Lo usaba el script cancel_flight.py — al volcar el trigger a DynamoDB
# Streams el GSI quedó sin consumidor en runtime. Eliminado para no pagar WCU
# de escrituras replicadas a un índice sin uso.)

resource "aws_dynamodb_table" "business" {
  name         = "${local.name_prefix}-business"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  # Stream para event-driven proactive notifications: cuando ops cambia
  # estado_vuelo a CANCELADO en un master row FLIGHT#, una Lambda detector
  # consume el stream y publica al SNS flight-events. NEW_AND_OLD_IMAGES
  # permite comparar transiciones (no re-cancelaciones).
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # GSI1: "quiénes están afectados por una cancelación de vuelo X / fecha Y"
  attribute {
    name = "gsi2pk"
    type = "S"
  }

  attribute {
    name = "gsi2sk"
    type = "S"
  }

  # GSI2: buscar PNR por identificador del pasajero (DNI o email)
  attribute {
    name = "gsi3pk"
    type = "S"
  }

  attribute {
    name = "gsi3sk"
    type = "S"
  }

  # Nota: los nombres lógicos gsi2pk/gsi2sk/gsi3pk/gsi3sk se mantuvieron tras
  # eliminar el GSI1 FlightByNumber para no requerir migración de los ítems
  # ya escritos en la tabla. Renombrarlos a gsi1pk/gsi1sk implicaría
  # reescribir todos los items con gsi2pk/gsi3pk — costo grande sin beneficio.

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
