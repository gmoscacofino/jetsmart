# ── Step Functions: Booking Saga ──────────────────────────────────────────────
#
# Orquesta la reserva con compensaciones. TP4 re-arquitectura: el post-procesado
# (notificación + boarding pass) ya NO se hace con un Parallel interno — el estado
# terminal de éxito PUBLICA `booking_confirmed` al SNS central, y el fan-out
# (notification + boarding-pass + analytics) lo hacen las suscripciones con filtro.
# El core de la Saga (reserve/collect/confirm/compensate) no cambió.
#
# Camino feliz:  ReserveFlight → ReserveBooking → CollectPayment → ConfirmBooking
#                → PublishBookingConfirmed (sns:publish) → BookingConfirmed
# Compensación:  <error> → [RefundPayment] → CancelBooking → ReleaseFlight
#                → PublishBookingFailed (sns:publish) → BookingDLQ → BookingFailed

resource "aws_sfn_state_machine" "booking" {
  name     = "${local.name_prefix}-booking-workflow"
  role_arn = data.aws_iam_role.lab_role.arn

  definition = jsonencode({
    Comment = "JetSmart — flujo de reserva y pago con compensaciones (patrón Saga)"
    StartAt = "ReserveFlight"

    States = {

      ReserveFlight = {
        Type     = "Task"
        Resource = aws_lambda_function.payment["reserve-flight"].arn
        Next     = "ReserveBooking"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "Lambda.SdkClientException"]
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "CancelBooking"
          ResultPath  = "$.error"
        }]
      }

      ReserveBooking = {
        Type     = "Task"
        Resource = aws_lambda_function.payment["reserve-booking"].arn
        Next     = "CollectPayment"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "Lambda.SdkClientException"]
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "CancelBooking"
          ResultPath  = "$.error"
        }]
      }

      CollectPayment = {
        Type     = "Task"
        Resource = aws_lambda_function.payment["collect"].arn
        Next     = "ConfirmBooking"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "Lambda.SdkClientException"]
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "CancelBooking"
          ResultPath  = "$.error"
        }]
      }

      ConfirmBooking = {
        Type     = "Task"
        Resource = aws_lambda_function.payment["confirm"].arn
        Next     = "PublishBookingConfirmed"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "Lambda.SdkClientException"]
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "RefundPayment"
          ResultPath  = "$.error"
        }]
      }

      # Publica el HECHO booking_confirmed al backbone. event_type va como
      # MessageAttribute (las filter policies operan sobre attributes).
      # Best-effort: si el publish falla, la reserva ya está confirmada igual.
      PublishBookingConfirmed = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.events.arn
          "Message.$" = "States.JsonToString($)"
          MessageAttributes = {
            event_type = { DataType = "String", StringValue = "booking_confirmed" }
          }
        }
        Next       = "BookingConfirmed"
        ResultPath = "$.publish_result"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "BookingConfirmed"
          ResultPath  = "$.publish_error"
        }]
      }

      BookingConfirmed = {
        Type = "Succeed"
      }

      # ── Compensación ────────────────────────────────────────────────────────

      RefundPayment = {
        Type       = "Task"
        Resource   = aws_lambda_function.payment["refund"].arn
        Next       = "CancelBooking"
        ResultPath = "$.refund"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "CancelBooking"
          ResultPath  = "$.refund_error"
        }]
      }

      CancelBooking = {
        Type       = "Task"
        Resource   = aws_lambda_function.payment["cancel"].arn
        Next       = "ReleaseFlight"
        ResultPath = "$.cancel"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "ReleaseFlight"
          ResultPath  = "$.cancel_error"
        }]
      }

      ReleaseFlight = {
        Type       = "Task"
        Resource   = aws_lambda_function.payment["release-flight"].arn
        Next       = "PublishBookingFailed"
        ResultPath = "$.release"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "PublishBookingFailed"
          ResultPath  = "$.release_error"
        }]
      }

      # Publica booking_failed al backbone (notification + analytics por filtro).
      PublishBookingFailed = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.events.arn
          "Message.$" = "States.JsonToString($)"
          MessageAttributes = {
            event_type = { DataType = "String", StringValue = "booking_failed" }
          }
        }
        Next       = "BookingDLQ"
        ResultPath = "$.publish_result"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "BookingDLQ"
          ResultPath  = "$.publish_error"
        }]
      }

      # SDK integration: deja el caso fallido en la DLQ para revisión manual.
      BookingDLQ = {
        Type     = "Task"
        Resource = "arn:aws:states:::sqs:sendMessage"
        Parameters = {
          QueueUrl        = aws_sqs_queue.booking_failed_dlq.url
          "MessageBody.$" = "States.JsonToString($)"
        }
        Next = "BookingFailed"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "BookingFailed"
          ResultPath  = "$.dlq_error"
        }]
      }

      BookingFailed = {
        Type  = "Fail"
        Error = "BookingFailed"
        Cause = "El flujo de reserva falló. Ver BookingDLQ para detalles."
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }
}

resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/states/${local.name_prefix}-booking-workflow"
  retention_in_days = 30
}

# ── Step Functions: Refund Saga ───────────────────────────────────────────────
#
# Disparada por refund_trigger (StartExecution name=flight_id, idempotente) cuando
# llega flight_cancelled. Fan-out por PNR con un Map de concurrencia acotada
# (MaxConcurrency=5 protege la pasarela). Cada PNR se reembolsa de forma
# idempotente; un fallo por PNR cae a refund-failures-dlq sin frenar al resto.

resource "aws_sfn_state_machine" "refund" {
  name     = "${local.name_prefix}-refund-workflow"
  role_arn = data.aws_iam_role.lab_role.arn

  definition = jsonencode({
    Comment = "JetSmart — reembolso por vuelo cancelado (fan-out por PNR)"
    StartAt = "GetAffectedPNRs"

    States = {

      GetAffectedPNRs = {
        Type     = "Task"
        Resource = aws_lambda_function.refund["get-pnrs"].arn
        Next     = "RefundFanout"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "Lambda.SdkClientException"]
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
      }

      RefundFanout = {
        Type           = "Map"
        ItemsPath      = "$.pnrs"
        MaxConcurrency = 5
        ResultPath     = "$.refund_results"
        Next           = "RefundDone"
        ItemProcessor = {
          ProcessorConfig = { Mode = "INLINE" }
          StartAt         = "RefundPNR"
          States = {
            RefundPNR = {
              Type     = "Task"
              Resource = aws_lambda_function.refund["refund-pnr"].arn
              End      = true
              Retry = [{
                ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "Lambda.SdkClientException"]
                IntervalSeconds = 2
                MaxAttempts     = 3
                BackoffRate     = 2
              }]
              Catch = [{
                ErrorEquals = ["States.ALL"]
                Next        = "RefundPNRFailed"
                ResultPath  = "$.error"
              }]
            }
            RefundPNRFailed = {
              Type     = "Task"
              Resource = "arn:aws:states:::sqs:sendMessage"
              Parameters = {
                QueueUrl        = aws_sqs_queue.refund_failures_dlq.url
                "MessageBody.$" = "States.JsonToString($)"
              }
              End = true
            }
          }
        }
      }

      RefundDone = {
        Type = "Succeed"
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.refund_workflow.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }
}

resource "aws_cloudwatch_log_group" "refund_workflow" {
  name              = "/aws/states/${local.name_prefix}-refund-workflow"
  retention_in_days = 30
}
