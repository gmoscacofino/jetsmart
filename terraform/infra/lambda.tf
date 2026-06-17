# ── Lambda: Analytics Processor ───────────────────────────────────────────────
#
# Disparada por SQS cuando llegan mensajes del SNS events (chat).
# Escribe los eventos crudos en S3 (JSON Lines particionado por fecha) para que
# Glue Crawler los catalogue y el equipo de business analytics los consulte vía
# Athena con cliente SQL (DBeaver / DataGrip).
#
# Sin VPC: la Lambda sólo necesita acceso a S3 y SQS — servicios regionales
# accesibles directamente desde Lambda gestionada por AWS.

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

  environment {
    variables = {
      AWS_REGION_VAR   = var.aws_region
      ANALYTICS_BUCKET = aws_s3_bucket.analytics.bucket
      ANALYTICS_PREFIX = "events"
    }
  }

  depends_on = [aws_s3_bucket.analytics]
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
  source_dir  = "${path.module}/../../lambda"
  output_path = "${path.module}/builds/payment_processor.zip"
  # payment_processor.py importa pricing.py — empaquetamos el dir entero.
  # Los otros .py del dir se incluyen pero no se ejecutan (handler busca por nombre).
  excludes = ["tests", "tests/__init__.py", "tests/test_pricing.py", "__pycache__"]
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

  lifecycle {
    create_before_destroy = true
  }
}

# ── Lambda: Boarding Pass Async ───────────────────────────────────────────────
#
# TP4: la generación del boarding pass se desacopló del Saga. Step Functions
# publica un mensaje a SQS boarding-pass-generation (SDK sqs:sendMessage), y
# esta Lambda lo consume async. Si la generación falla N veces, el mensaje
# termina en la DLQ. La reserva ya está confirmada en este punto, así que un
# fallo aquí no revierte el pago — sólo deja el BP pendiente de regenerar.

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

resource "aws_lambda_event_source_mapping" "boarding_pass_async_sqs" {
  event_source_arn = aws_sqs_queue.boarding_pass_generation.arn
  function_name    = aws_lambda_function.boarding_pass_async.arn
  batch_size       = 1
}

# ── Lambda: Human Handoff Processor ───────────────────────────────────────────
#
# Consume mensajes de la cola human-handoff cuando el chatbot deriva al usuario
# a un agente humano. Hace un mock del POST al sistema del call center y
# actualiza el ticket HANDOFF# en conversations table a status=ACK.

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
# Consume mensajes de la cola proactive-notifications cuando el sistema de
# operaciones publica un evento de cancelación/cambio de vuelo a SNS
# flight-events. Hace Query a GSI2 ReservationsByFlight para encontrar todos
# los PNRs afectados y publica un email personalizado por usuario via SNS
# notifications.

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

resource "aws_lambda_event_source_mapping" "proactive_notifications_sqs" {
  event_source_arn = aws_sqs_queue.proactive_notifications.arn
  function_name    = aws_lambda_function.proactive_notifications.arn
  batch_size       = 5
}

# ── Lambda: Flight Cancellation Detector (DynamoDB Stream) ────────────────────
#
# Consume el Stream de business table y publica al SNS flight-events cuando
# detecta una transición de estado_vuelo a CANCELADO en un master row FLIGHT#.
# Reemplaza el trigger manual de scripts/cancel_flight.py. El resto del flujo
# (SNS → SQS proactive-notifications → Lambda → emails) queda igual.

data "archive_file" "flight_cancellation_detector" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/flight_cancellation_detector.py"
  output_path = "${path.module}/builds/flight_cancellation_detector.zip"
}

resource "aws_lambda_function" "flight_cancellation_detector" {
  function_name    = "${local.name_prefix}-flight-cancellation-detector"
  filename         = data.archive_file.flight_cancellation_detector.output_path
  source_code_hash = data.archive_file.flight_cancellation_detector.output_base64sha256
  runtime          = "python3.12"
  handler          = "flight_cancellation_detector.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = var.lambda_timeout

  environment {
    variables = {
      AWS_REGION_VAR        = var.aws_region
      SNS_FLIGHT_EVENTS_ARN = aws_sns_topic.flight_events.arn
    }
  }
}

# Stream → Lambda con filter_criteria: solo invoca cuando un MODIFY pone
# estado_vuelo=CANCELADO. Reduce drásticamente las invocaciones (no se ejecuta
# por escrituras de PNR#, SEAT#, etc.). El handler igual valida que sea master
# row FLIGHT# y que sea una transición real (no re-cancelación).
resource "aws_lambda_event_source_mapping" "flight_cancellation_stream" {
  event_source_arn  = aws_dynamodb_table.business.stream_arn
  function_name     = aws_lambda_function.flight_cancellation_detector.arn
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
      AWS_REGION_VAR        = var.aws_region
      SNS_NOTIFICATIONS_ARN = aws_sns_topic.notifications.arn
    }
  }
}
