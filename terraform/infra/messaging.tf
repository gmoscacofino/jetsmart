# ── SNS: topic de eventos del chatbot (analytics) ─────────────────────────────

resource "aws_sns_topic" "events" {
  name = "${local.name_prefix}-events"
}

# ── SNS: topic de notificaciones al usuario ────────────────────────────────────
#
# Recibe eventos de booking_confirmed / booking_failed / handoff_ack /
# flight_cancellation desde varias Lambdas. Las suscripciones email se declaran
# en la variable var.notification_email_subscribers.
#
# Atención: después del primer apply, AWS manda un mail "Confirm subscription"
# a cada endpoint. Hasta que se clickee el link, la subscription queda en estado
# "PendingConfirmation" y NO llegan los mails reales.

resource "aws_sns_topic" "notifications" {
  name = "${local.name_prefix}-notifications"
}

resource "aws_sns_topic_subscription" "notifications_email" {
  for_each  = toset(var.notification_email_subscribers)
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = each.value
}

# ── SNS: topic de eventos de operaciones (vuelos) ─────────────────────────────
#
# Publicado por la Lambda flight_cancellation_detector cuando el DynamoDB
# Stream de business señala una transición de estado_vuelo a CANCELADO en
# un master row FLIGHT#. Consumido por la cola proactive-notifications, que
# dispara la Lambda proactive_notifications para hacer fan-out de emails a los
# pasajeros afectados.

resource "aws_sns_topic" "flight_events" {
  name = "${local.name_prefix}-flight-events"
}

# ── SQS: analytics ────────────────────────────────────────────────────────────

resource "aws_sqs_queue" "analytics" {
  name                       = "${local.name_prefix}-analytics"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = 360 # 6× Lambda timeout (60s) per AWS recommendation
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.analytics_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "analytics_dlq" {
  name                      = "${local.name_prefix}-analytics-dlq"
  message_retention_seconds = 1209600
}

# ── SQS: dead-letter queue de reservas fallidas ───────────────────────────────
#
# Recibe mensajes del estado BookingDLQ de Step Functions mediante SDK integration.
# Permite revisar y reprocesar manualmente los flujos de pago que fallaron.

resource "aws_sqs_queue" "booking_failed_dlq" {
  name                      = "${local.name_prefix}-booking-failed-dlq"
  message_retention_seconds = 1209600 # 14 días para investigar errores
}

# ── SQS: human-handoff ────────────────────────────────────────────────────────
#
# El chat_handler envía mensajes acá cuando el modelo invoca la tool
# `escalate_to_human`. La Lambda human_handoff_processor los consume,
# simula un POST al sistema del call center y actualiza el ticket HANDOFF#
# en conversations table a status=ACK.
#
# Decopla el chatbot del call center: si el call center está caído, el chatbot
# sigue respondiendo y el pedido queda esperando en la cola.

resource "aws_sqs_queue" "human_handoff" {
  name                       = "${local.name_prefix}-human-handoff"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = 360
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.human_handoff_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "human_handoff_dlq" {
  name                      = "${local.name_prefix}-human-handoff-dlq"
  message_retention_seconds = 1209600
}

# ── SQS: proactive-notifications ──────────────────────────────────────────────
#
# Suscrita al topic SNS flight-events. Cuando ops marca un vuelo como cancelado,
# este flow dispara la Lambda proactive_notifications que hace Query a GSI2
# para encontrar todos los PNRs afectados y publica un email por usuario.

resource "aws_sqs_queue" "proactive_notifications" {
  name                       = "${local.name_prefix}-proactive-notifications"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = 360
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.proactive_notifications_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "proactive_notifications_dlq" {
  name                      = "${local.name_prefix}-proactive-notifications-dlq"
  message_retention_seconds = 1209600
}

# ── SQS: boarding-pass-generation ─────────────────────────────────────────────
#
# Step Functions (PostBookingActions Branch B) publica mensajes acá vía
# SDK sqs:sendMessage. La Lambda boarding_pass_async consume, genera el PDF
# (texto plano por simplicidad), sube a S3 y graba el bp_url en el PNR.
#
# Patron "fire-and-forget" desde el Saga: si la generación falla, no afecta
# la reserva ya confirmada — sólo deja el BP pendiente en la DLQ para reintento.

resource "aws_sqs_queue" "boarding_pass_generation" {
  name                       = "${local.name_prefix}-boarding-pass-generation"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = 360
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.boarding_pass_generation_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "boarding_pass_generation_dlq" {
  name                      = "${local.name_prefix}-boarding-pass-generation-dlq"
  message_retention_seconds = 1209600
}

# ── Suscripción: eventos del chat → cola analytics ────────────────────────────

resource "aws_sns_topic_subscription" "events_to_analytics" {
  topic_arn = aws_sns_topic.events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.analytics.arn
}

# ── Suscripción: flight-events → cola proactive-notifications ─────────────────

resource "aws_sns_topic_subscription" "flight_events_to_proactive" {
  topic_arn = aws_sns_topic.flight_events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.proactive_notifications.arn
}

# ── Política SQS: analytics acepta mensajes del topic events ─────────────────

resource "aws_sqs_queue_policy" "analytics" {
  queue_url = aws_sqs_queue.analytics.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.analytics.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.events.arn } }
    }]
  })
}

# ── Política SQS: proactive-notifications acepta mensajes de flight-events ───

resource "aws_sqs_queue_policy" "proactive_notifications" {
  queue_url = aws_sqs_queue.proactive_notifications.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.proactive_notifications.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.flight_events.arn } }
    }]
  })
}
