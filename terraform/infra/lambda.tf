# ── Lambdas ───────────────────────────────────────────────────────────────────
#
# TP4: sólo el núcleo transaccional (payment + refund Saga) corre DENTRO de la
# VPC — toca dinero/PNR, así que aislamos su egress. El resto de las Lambdas
# (event glue: analytics, stream, boarding, handoff, notification, proactive,
# refund-trigger) corre FUERA de la VPC: sólo mueven datos entre servicios AWS
# gestionados vía IAM, sin acceso a recursos privados, así que la VPC sólo
# sumaría cold-starts por ENI sin ganar seguridad efectiva. auth_callback/
# cognito_trigger (módulo auth) también quedan fuera.
#
# Las que quedan en VPC acceden a DynamoDB por Gateway Endpoint (gratis) y a SNS
# por Interface Endpoint. vpc_config sólo en payment y refund.

# ── Lambda: Business Analytics Emitter (CDC → Firehose) ───────────────────────
#
# 2° consumer del Stream de business (el 1° es stream_emitter, operacional).
# Clasifica PNR/FLIGHT/CLAIM, deriva transición Old→New, redacta PII y hace
# PutRecord al Firehose de cada entidad → data lake. Ver analytics-arquitectura.md.

data "archive_file" "business_analytics_emitter" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/business_analytics_emitter.py"
  output_path = "${path.module}/builds/business_analytics_emitter.zip"
}

resource "aws_lambda_function" "business_analytics_emitter" {
  function_name    = "${local.name_prefix}-business-analytics-emitter"
  filename         = data.archive_file.business_analytics_emitter.output_path
  source_code_hash = data.archive_file.business_analytics_emitter.output_base64sha256
  runtime          = "python3.12"
  handler          = "business_analytics_emitter.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = 60

  environment {
    variables = {
      AWS_REGION_VAR       = var.aws_region
      FIREHOSE_RESERVATION = aws_kinesis_firehose_delivery_stream.lake["reservation_events"].name
      FIREHOSE_FLIGHT      = aws_kinesis_firehose_delivery_stream.lake["flight_events"].name
      FIREHOSE_CLAIM       = aws_kinesis_firehose_delivery_stream.lake["claim_events"].name
    }
  }
}

# 2° ESM sobre el mismo Stream. Filtro coarse por prefijo de PK (INSERT+MODIFY);
# el fine-filtering (master row, excluir SEAT#, PII) lo hace el código.
resource "aws_lambda_event_source_mapping" "business_analytics_stream" {
  event_source_arn  = aws_dynamodb_table.business.stream_arn
  function_name     = aws_lambda_function.business_analytics_emitter.arn
  starting_position = "LATEST"
  batch_size        = 50

  function_response_types = ["ReportBatchItemFailures"]

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
        dynamodb  = { Keys = { PK = { S = [{ prefix = "PNR#" }] } } }
      })
    }
    # Solo master rows FLIGHT#: tienen estado_vuelo (los SEAT# no) → excluye el
    # ruido de reservas de asiento sin invocar la Lambda.
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
        dynamodb = {
          Keys     = { PK = { S = [{ prefix = "FLIGHT#" }] } }
          NewImage = { estado_vuelo = { S = [{ exists = true }] } }
        }
      })
    }
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
        dynamodb  = { Keys = { PK = { S = [{ prefix = "CLAIM#" }] } } }
      })
    }
  }
}

# ── Lambda: Payment Steps (Saga booking, invocadas por Step Functions) ────────

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
  source_dir  = "${path.module}/../../lambda"
  output_path = "${path.module}/builds/payment_processor.zip"
  excludes    = ["tests", "tests/__init__.py", "tests/test_pricing.py", "__pycache__"]
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
      BUSINESS_TABLE_NAME = aws_dynamodb_table.business.name
      SNS_EVENTS_ARN      = aws_sns_topic.events.arn
    }
  }

  vpc_config {
    subnet_ids         = aws_subnet.private_lambda[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ── Lambda: Boarding Pass Async ───────────────────────────────────────────────
#
# Ahora alimentada por el SNS central (filtro booking_confirmed) → cola
# boarding-pass-generation. El mensaje llega envuelto en el envelope de SNS.

data "archive_file" "boarding_pass_async" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/boarding_pass_async.py"
  output_path = "${path.module}/builds/boarding_pass_async.zip"
}

resource "aws_lambda_function" "boarding_pass_async" {
  function_name    = "${local.name_prefix}-boarding-pass-async"
  filename         = data.archive_file.boarding_pass_async.output_path
  source_code_hash = data.archive_file.boarding_pass_async.output_base64sha256
  runtime          = "python3.12"
  handler          = "boarding_pass_async.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR         = var.aws_region
      BUSINESS_TABLE_NAME    = aws_dynamodb_table.business.name
      BOARDING_PASSES_BUCKET = aws_s3_bucket.boarding_passes.bucket
    }
  }
}

# boarding-pass: SNS→Lambda directo (suscripción + permiso en messaging.tf).

# ── Lambda: Human Handoff Processor ───────────────────────────────────────────
#
# Ahora alimentada por el SNS central (filtro handoff_requested) → cola
# human-handoff (antes el chat_handler hacía sqs:send_message directo).

data "archive_file" "human_handoff_processor" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/human_handoff_processor.py"
  output_path = "${path.module}/builds/human_handoff_processor.zip"
}

resource "aws_lambda_function" "human_handoff_processor" {
  function_name    = "${local.name_prefix}-human-handoff-processor"
  filename         = data.archive_file.human_handoff_processor.output_path
  source_code_hash = data.archive_file.human_handoff_processor.output_base64sha256
  runtime          = "python3.12"
  handler          = "human_handoff_processor.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR           = var.aws_region
      CONVERSATIONS_TABLE_NAME = aws_dynamodb_table.conversations.name
      SNS_NOTIFICATIONS_ARN    = aws_sns_topic.notifications.arn
    }
  }
}

resource "aws_lambda_event_source_mapping" "human_handoff_sqs" {
  event_source_arn = aws_sqs_queue.human_handoff.arn
  function_name    = aws_lambda_function.human_handoff_processor.arn
  batch_size       = 5
}

# ── Lambda: Proactive Notifications ───────────────────────────────────────────
#
# Suscrita al SNS central (filtro flight_cancelled) → cola proactive-notifications.
# Query a GSI ReservationsByFlight y fan-out de emails a los pasajeros afectados.

data "archive_file" "proactive_notifications" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/proactive_notifications.py"
  output_path = "${path.module}/builds/proactive_notifications.zip"
}

resource "aws_lambda_function" "proactive_notifications" {
  function_name    = "${local.name_prefix}-proactive-notifications"
  filename         = data.archive_file.proactive_notifications.output_path
  source_code_hash = data.archive_file.proactive_notifications.output_base64sha256
  runtime          = "python3.12"
  handler          = "proactive_notifications.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR        = var.aws_region
      BUSINESS_TABLE_NAME   = aws_dynamodb_table.business.name
      SNS_NOTIFICATIONS_ARN = aws_sns_topic.notifications.arn
      SNS_EVENTS_ARN        = aws_sns_topic.events.arn
    }
  }
}

# proactive-notifications: SNS→Lambda directo (suscripción + permiso en messaging.tf).

# ── Lambda: Stream Emitter (DynamoDB Stream → SNS central) ────────────────────
#
# Consume el Stream de business (filter MODIFY + estado_vuelo=CANCELADO) y publica
# flight_cancelled al SNS central con MessageAttribute event_type. Patrón CDC: el
# evento se deriva del cambio comprometido — evita el dual-write del poller.

data "archive_file" "stream_emitter" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/stream_emitter.py"
  output_path = "${path.module}/builds/stream_emitter.zip"
}

resource "aws_lambda_function" "stream_emitter" {
  function_name    = "${local.name_prefix}-stream-emitter"
  filename         = data.archive_file.stream_emitter.output_path
  source_code_hash = data.archive_file.stream_emitter.output_base64sha256
  runtime          = "python3.12"
  handler          = "stream_emitter.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR = var.aws_region
      SNS_EVENTS_ARN = aws_sns_topic.events.arn
    }
  }
}

resource "aws_lambda_event_source_mapping" "stream_emitter_stream" {
  event_source_arn  = aws_dynamodb_table.business.stream_arn
  function_name     = aws_lambda_function.stream_emitter.arn
  starting_position = "LATEST"
  batch_size        = 10

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["MODIFY"]
        dynamodb = {
          NewImage = {
            estado_vuelo = {
              S = ["CANCELADO"]
            }
          }
        }
      })
    }
  }
}

# ── Lambda: Notification ──────────────────────────────────────────────────────
#
# TP4: pasa de invocación directa por Step Functions → consumidor SQS de la cola
# `notification` (alimentada por el SNS central, filtro booking_confirmed/failed).
# Publica el email al SNS notifications. Lee el mensaje del envelope de SNS.

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
      AWS_REGION_VAR        = var.aws_region
      SNS_NOTIFICATIONS_ARN = aws_sns_topic.notifications.arn
    }
  }
}

# notification: SNS→Lambda directo (suscripción + permiso en messaging.tf).

# ── Lambda: Refund (refund Saga steps) ────────────────────────────────────────
#
# Empaquetadas desde el dir lambda (refund_processor.py puede reusar lógica de
# payment_processor / pricing). Dos handlers invocados por la refund Saga:
#   - get_affected_pnrs_handler → Query GSI ReservationsByFlight
#   - refund_pnr_handler        → refund + marcar PNR CANCELADO (idempotente)

locals {
  refund_handlers = {
    get-pnrs   = "refund_processor.get_affected_pnrs_handler"
    refund-pnr = "refund_processor.refund_pnr_handler"
  }
}

data "archive_file" "refund_processor" {
  type        = "zip"
  source_dir  = "${path.module}/../../lambda"
  output_path = "${path.module}/builds/refund_processor.zip"
  excludes    = ["tests", "tests/__init__.py", "tests/test_pricing.py", "__pycache__"]
}

resource "aws_lambda_function" "refund" {
  for_each = local.refund_handlers

  function_name    = "${local.name_prefix}-refund-${each.key}"
  filename         = data.archive_file.refund_processor.output_path
  source_code_hash = data.archive_file.refund_processor.output_base64sha256
  runtime          = "python3.12"
  handler          = each.value
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR      = var.aws_region
      BUSINESS_TABLE_NAME = aws_dynamodb_table.business.name
    }
  }

  vpc_config {
    subnet_ids         = aws_subnet.private_lambda[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ── Lambda: Refund Trigger (SQS refund → StartExecution refund Saga) ──────────
#
# Consume la cola refund (flight_cancelled filtrado) y arranca la refund Saga con
# name=flight_id (idempotencia: StartExecution duplicado rechazado).

data "archive_file" "refund_trigger" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/refund_trigger.py"
  output_path = "${path.module}/builds/refund_trigger.zip"
}

resource "aws_lambda_function" "refund_trigger" {
  function_name    = "${local.name_prefix}-refund-trigger"
  filename         = data.archive_file.refund_trigger.output_path
  source_code_hash = data.archive_file.refund_trigger.output_base64sha256
  runtime          = "python3.12"
  handler          = "refund_trigger.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR      = var.aws_region
      REFUND_SFN_ARN      = aws_sfn_state_machine.refund.arn
      BUSINESS_TABLE_NAME = aws_dynamodb_table.business.name
    }
  }
}

# refund-trigger: SNS→Lambda directo (suscripción + permiso en messaging.tf).
