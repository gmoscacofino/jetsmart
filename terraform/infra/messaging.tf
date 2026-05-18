# ── SNS: topic de eventos del chatbot (analytics) ─────────────────────────────

resource "aws_sns_topic" "events" {
  name = "${local.name_prefix}-events"
}

# ── SNS: topic de notificaciones al usuario ────────────────────────────────────
#
# Recibe eventos de booking_confirmed / booking_failed desde la Lambda notification.
# Agregar suscripciones de email manualmente:
#   aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint <email>

resource "aws_sns_topic" "notifications" {
  name = "${local.name_prefix}-notifications"
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

# ── Suscripción: eventos del chat → cola analytics ────────────────────────────

resource "aws_sns_topic_subscription" "events_to_analytics" {
  topic_arn = aws_sns_topic.events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.analytics.arn
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
