# ── SNS: backbone de eventos (topic central) ──────────────────────────────────
#
# Un único topic central de dominio. Los publishers (chat-handler Fargate, Step
# Functions de la Saga, stream-emitter) publican con el MessageAttribute
# `event_type`; cada subscriber filtra por ese atributo.
#
# Subscribers (SNS→Lambda DIRECTO salvo human-handoff):
#   chat_message / busqueda_… → Firehose interaction_events (comportamiento)
#   handoff_requested  → SQS human-handoff → λ (downstream NO elástico)
#   booking_confirmed  → λ notification + λ boarding-pass
#   booking_failed     → λ notification
#   flight_cancelled   → λ proactive-notifications + λ refund-trigger
#
# Solo human-handoff usa SQS: protege un downstream no elástico (call center).
# El resto va SNS→λ directo + alarma de Lambda Errors (la pérdida es tolerable o
# re-derivable desde DynamoDB) — no se ponen DLQs por agregar.

resource "aws_sns_topic" "events" {
  name = "${local.name_prefix}-events"
}

# ── SNS: canal de email a usuarios (NO es el backbone) ────────────────────────

resource "aws_sns_topic" "notifications" {
  name = "${local.name_prefix}-notifications"
}

resource "aws_sns_topic_subscription" "notifications_email" {
  for_each  = toset(var.notification_email_subscribers)
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = each.value
}

# ── SQS: única cola funcional — human-handoff ─────────────────────────────────
#
# Justificación: el call center (mock) es un downstream NO elástico que se puede
# caer; la cola desacopla y, si está caído, el pedido espera sin perderse. Su DLQ
# captura el caso persistente (pedido de soporte que no se puede perder).

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

# ── DLQs de Step Functions (escritas por Catch, no son redrive de cola) ───────
#
# Únicas DLQs además de human-handoff: ambas sobre paths de PLATA no recuperable.

# Refund Saga: un PNR cuyo reembolso falla tras los reintentos del Map → revisión
# manual (es plata, no se puede dejar sin reembolsar en silencio).
resource "aws_sqs_queue" "refund_failures_dlq" {
  name                      = "${local.name_prefix}-refund-failures-dlq"
  message_retention_seconds = 1209600
}

# Booking Saga: reserva que falló (transacción de pago) → revisión manual
# (¿se cobró?, ¿corrió la compensación completa?).
resource "aws_sqs_queue" "booking_failed_dlq" {
  name                      = "${local.name_prefix}-booking-failed-dlq"
  message_retention_seconds = 1209600
}

# ── Suscripción SQS: handoff_requested → cola human-handoff ────────────────────

resource "aws_sns_topic_subscription" "events_to_human_handoff" {
  topic_arn     = aws_sns_topic.events.arn
  protocol      = "sqs"
  endpoint      = aws_sqs_queue.human_handoff.arn
  filter_policy = jsonencode({ event_type = ["handoff_requested"] })
}

resource "aws_sqs_queue_policy" "human_handoff" {
  queue_url = aws_sqs_queue.human_handoff.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.human_handoff.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.events.arn } }
    }]
  })
}

# ── Suscripciones SNS → Lambda DIRECTO (sin SQS, sin DLQ) ─────────────────────
#
# El downstream es elástico o el resultado es recuperable desde DynamoDB → no se
# justifica una cola amortiguadora ni una DLQ. La durabilidad/visibilidad la dan
# el retry de SNS + una alarma de Lambda Errors (ver cloudwatch.tf).

locals {
  lambda_subscribers = {
    notification = {
      arn    = aws_lambda_function.notification.arn
      name   = aws_lambda_function.notification.function_name
      filter = { event_type = ["booking_confirmed", "booking_failed"] }
    }
    boarding_pass = {
      arn    = aws_lambda_function.boarding_pass_async.arn
      name   = aws_lambda_function.boarding_pass_async.function_name
      filter = { event_type = ["booking_confirmed"] }
    }
    proactive = {
      arn    = aws_lambda_function.proactive_notifications.arn
      name   = aws_lambda_function.proactive_notifications.function_name
      filter = { event_type = ["flight_cancelled"] }
    }
    refund = {
      arn    = aws_lambda_function.refund_trigger.arn
      name   = aws_lambda_function.refund_trigger.function_name
      filter = { event_type = ["flight_cancelled"] }
    }
  }
}

resource "aws_sns_topic_subscription" "events_to_lambda" {
  for_each = local.lambda_subscribers

  topic_arn     = aws_sns_topic.events.arn
  protocol      = "lambda"
  endpoint      = each.value.arn
  filter_policy = jsonencode(each.value.filter)
}

resource "aws_lambda_permission" "events_invoke" {
  for_each = local.lambda_subscribers

  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = each.value.name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.events.arn
}

# ── Suscripción comportamiento: SNS central → Firehose interaction_events ──────
#
# Captura los eventos semánticos del chat (búsquedas, intents, handoff) para el
# data lake. filter_policy = anything-but los event_type transaccionales (esos
# vienen del CDC de business, no del bus → sin doble conteo). raw_message_delivery
# para que Firehose reciba el JSON del evento, no el envelope de SNS.

resource "aws_sns_topic_subscription" "events_to_firehose" {
  topic_arn             = aws_sns_topic.events.arn
  protocol              = "firehose"
  endpoint              = aws_kinesis_firehose_delivery_stream.lake["interaction_events"].arn
  subscription_role_arn = data.aws_iam_role.lab_role.arn
  raw_message_delivery  = true

  filter_policy = jsonencode({
    event_type = [{ "anything-but" = ["booking_confirmed", "booking_failed", "flight_cancelled"] }]
  })
}
