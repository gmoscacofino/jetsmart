# ── Lambda: Analytics Processor ───────────────────────────────────────────────
#
# Disparada por SQS cuando llegan mensajes del SNS events (chat).
# Escribe agregados en RDS. Debe estar en la VPC para acceder a RDS.

data "archive_file" "analytics_processor" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/analytics_processor.py"
  output_path = "${path.module}/builds/analytics_processor.zip"
}

resource "aws_lambda_function" "analytics_processor" {
  function_name    = "${local.name_prefix}-analytics-processor"
  filename         = data.archive_file.analytics_processor.output_path
  source_code_hash = data.archive_file.analytics_processor.output_base64sha256
  runtime          = "python3.12"
  handler          = "analytics_processor.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = 120
  layers           = [aws_lambda_layer_version.psycopg2.arn]

  vpc_config {
    subnet_ids         = slice(module.vpc.private_subnets, 0, 2)
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      AWS_REGION_VAR     = var.aws_region
      RDS_SECRET_ARN     = aws_secretsmanager_secret.rds_credentials.arn
      RDS_PROXY_ENDPOINT = aws_db_proxy.main.endpoint
    }
  }

  depends_on = [
    aws_db_proxy.main,
    aws_secretsmanager_secret_version.rds_credentials,
  ]
}

resource "aws_lambda_event_source_mapping" "analytics_sqs" {
  event_source_arn = aws_sqs_queue.analytics.arn
  function_name    = aws_lambda_function.analytics_processor.arn
  batch_size       = 10
}

# ── Lambda: Payment Steps (for_each — patrón Saga via Step Functions) ─────────
#
# Los 7 handlers del flujo de pago se crean desde el mismo ZIP con handlers
# distintos. Step Functions los invoca directamente — sin SQS ni SNS entre pasos.
#
# Paso feliz:   reserve-flight → reserve-booking → collect → confirm
# Compensación: refund → cancel → release-flight

locals {
  payment_handlers = {
    reserve-flight  = "payment_processor.reserve_flight_handler"
    reserve-booking = "payment_processor.reserve_booking_handler"
    collect         = "payment_processor.collect_payment_handler"
    confirm         = "payment_processor.confirm_booking_handler"
    refund          = "payment_processor.refund_payment_handler"
    cancel          = "payment_processor.cancel_booking_handler"
    release-flight  = "payment_processor.release_flight_handler"
  }
}

data "archive_file" "payment_processor" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/payment_processor.py"
  output_path = "${path.module}/builds/payment_processor.zip"
}

resource "aws_lambda_function" "payment" {
  for_each = local.payment_handlers

  function_name    = "${local.name_prefix}-payment-${each.key}"
  filename         = data.archive_file.payment_processor.output_path
  source_code_hash = data.archive_file.payment_processor.output_base64sha256
  runtime          = "python3.12"
  handler          = each.value
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR      = var.aws_region
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.main.name
      SNS_EVENTS_ARN      = aws_sns_topic.events.arn
      ASSETS_BUCKET       = aws_s3_bucket.assets.bucket
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ── Lambda: Boarding Pass ─────────────────────────────────────────────────────
#
# Invocada directamente por Step Functions (Parallel branch PostBookingActions).
# Recibe el estado de la reserva confirmada y genera el boarding pass en S3.

data "archive_file" "boarding_pass" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/boarding_pass.py"
  output_path = "${path.module}/builds/boarding_pass.zip"
}

resource "aws_lambda_function" "boarding_pass" {
  function_name    = "${local.name_prefix}-boarding-pass"
  filename         = data.archive_file.boarding_pass.output_path
  source_code_hash = data.archive_file.boarding_pass.output_base64sha256
  runtime          = "python3.12"
  handler          = "boarding_pass.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR      = var.aws_region
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.main.name
      ASSETS_BUCKET       = aws_s3_bucket.assets.bucket
    }
  }
}

# ── Lambda: Notification ──────────────────────────────────────────────────────
#
# Invocada directamente por Step Functions en dos puntos:
#   - PostBookingActions (branch Parallel) — booking_confirmed
#   - NotifyBookingFailed — booking_failed
# Recibe {event_type, data: <estado actual>}

data "archive_file" "notification" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/notification.py"
  output_path = "${path.module}/builds/notification.zip"
}

resource "aws_lambda_function" "notification" {
  function_name    = "${local.name_prefix}-notification"
  filename         = data.archive_file.notification.output_path
  source_code_hash = data.archive_file.notification.output_base64sha256
  runtime          = "python3.12"
  handler          = "notification.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR       = var.aws_region
      SNS_NOTIFICATION_ARN = aws_sns_topic.notifications.arn
    }
  }
}
