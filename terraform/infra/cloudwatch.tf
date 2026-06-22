# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

locals {
  # Nota: el chat-handler ya no es Lambda (corre en ECS — log group en ecs.tf).
  log_groups = {
    lambda_payment_reserve_flight  = "/aws/lambda/${local.name_prefix}-payment-reserve-flight"
    lambda_payment_reserve_booking = "/aws/lambda/${local.name_prefix}-payment-reserve-booking"
    lambda_payment_collect         = "/aws/lambda/${local.name_prefix}-payment-collect"
    lambda_payment_confirm         = "/aws/lambda/${local.name_prefix}-payment-confirm"
    lambda_payment_refund          = "/aws/lambda/${local.name_prefix}-payment-refund"
    lambda_payment_cancel          = "/aws/lambda/${local.name_prefix}-payment-cancel"
    lambda_payment_release_flight  = "/aws/lambda/${local.name_prefix}-payment-release-flight"
    lambda_boarding_async          = "/aws/lambda/${local.name_prefix}-boarding-pass-async"
    lambda_notification            = "/aws/lambda/${local.name_prefix}-notification"
    lambda_auth                    = "/aws/lambda/${local.name_prefix}-auth-callback"
    lambda_cognito                 = "/aws/lambda/${local.name_prefix}-cognito-trigger"
    lambda_analytics_emitter       = "/aws/lambda/${local.name_prefix}-business-analytics-emitter"
    lambda_human_handoff           = "/aws/lambda/${local.name_prefix}-human-handoff-processor"
    lambda_proactive_notif         = "/aws/lambda/${local.name_prefix}-proactive-notifications"
    lambda_stream_emitter          = "/aws/lambda/${local.name_prefix}-stream-emitter"
    lambda_refund_get_pnrs         = "/aws/lambda/${local.name_prefix}-refund-get-pnrs"
    lambda_refund_pnr              = "/aws/lambda/${local.name_prefix}-refund-refund-pnr"
    lambda_refund_trigger          = "/aws/lambda/${local.name_prefix}-refund-trigger"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.log_groups
  name              = each.value
  retention_in_days = 30
}

# ── CloudWatch Alarms ──────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "analytics_emitter_errors" {
  alarm_name          = "${local.name_prefix}-business-analytics-emitter-errors"
  alarm_description   = "Errores del business-analytics-emitter — posible fallo de PutRecord a Firehose"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.business_analytics_emitter.function_name
  }

  alarm_actions = [aws_sns_topic.notifications.arn]
}

# ── DLQ depth alarms — disparan si algún mensaje cae a una DLQ ───────────────
#
# Solo las 3 DLQs justificadas: human-handoff (soporte) + las 2 de Step Functions
# (plata). El resto de los consumers va SNS→Lambda directo sin DLQ → su
# visibilidad la dan las alarmas de Lambda Errors (abajo).

locals {
  dlq_alarms = {
    human_handoff_dlq   = aws_sqs_queue.human_handoff_dlq.name
    refund_failures_dlq = aws_sqs_queue.refund_failures_dlq.name
    booking_failed_dlq  = aws_sqs_queue.booking_failed_dlq.name
  }

  # Lambdas SNS→directo sin DLQ — se monitorean por errores de invocación.
  direct_lambda_alarms = {
    notification   = aws_lambda_function.notification.function_name
    boarding_pass  = aws_lambda_function.boarding_pass_async.function_name
    proactive      = aws_lambda_function.proactive_notifications.function_name
    refund_trigger = aws_lambda_function.refund_trigger.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "dlq_messages_visible" {
  for_each = local.dlq_alarms

  alarm_name          = "${local.name_prefix}-${each.key}-messages-visible"
  alarm_description   = "Hay mensajes en la DLQ ${each.value} — investigar fallo del consumer"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = each.value
  }

  alarm_actions = [aws_sns_topic.notifications.arn]
}

# Alarmas de error para los consumers SNS→Lambda directo (reemplazan a la DLQ:
# la pérdida es tolerable o re-derivable, pero queremos visibilidad del fallo).
resource "aws_cloudwatch_metric_alarm" "direct_lambda_errors" {
  for_each = local.direct_lambda_alarms

  alarm_name          = "${local.name_prefix}-${each.key}-errors"
  alarm_description   = "Errores en ${each.value} (SNS→Lambda directo, sin DLQ)"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = each.value
  }

  alarm_actions = [aws_sns_topic.notifications.arn]
}
