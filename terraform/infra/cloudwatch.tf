# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

locals {
  log_groups = {
    lambda_chat                    = "/aws/lambda/${local.name_prefix}-chat-handler"
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
    lambda_analytics               = "/aws/lambda/${local.name_prefix}-analytics-processor"
    lambda_human_handoff           = "/aws/lambda/${local.name_prefix}-human-handoff-processor"
    lambda_proactive_notif         = "/aws/lambda/${local.name_prefix}-proactive-notifications"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.log_groups
  name              = each.value
  retention_in_days = 30
}

# ── CloudWatch Alarms ──────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "analytics_processor_errors" {
  alarm_name          = "${local.name_prefix}-analytics-processor-errors"
  alarm_description   = "Analytics processor Lambda errors — posible fallo de escritura a S3 o lectura SQS"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.analytics_processor.function_name
  }

  alarm_actions = [aws_sns_topic.notifications.arn]
}

# ── DLQ depth alarms — disparan si algún mensaje cae a una DLQ ───────────────

locals {
  dlq_alarms = {
    human_handoff_dlq            = aws_sqs_queue.human_handoff_dlq.name
    proactive_notifications_dlq  = aws_sqs_queue.proactive_notifications_dlq.name
    boarding_pass_generation_dlq = aws_sqs_queue.boarding_pass_generation_dlq.name
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
