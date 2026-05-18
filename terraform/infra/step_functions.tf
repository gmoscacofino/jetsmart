# ── Step Functions: Flujo de Reserva y Pago (patrón Saga) ─────────────────────
#
# Orquesta 4 pasos de reserva en secuencia con compensaciones automáticas.
# Si cualquier paso falla, el state machine ejecuta las acciones de rollback.
#
# Flujo exitoso:  ReserveFlight → ReserveBooking → CollectPayment → ConfirmBooking
#                 → PostBookingActions (Parallel: Notify + BoardingPass)
#                 → BookingConfirmed (Succeed)
#
# Flujo de error: cualquier paso → CancelBooking → ReleaseFlight
#                 → NotifyBookingFailed → BookingDLQ → BookingFailed (Fail)
#
# Compensación:   ConfirmBooking falla → RefundPayment → CancelBooking → ...

resource "aws_sfn_state_machine" "booking" {
  name     = "${local.name_prefix}-booking-workflow"
  role_arn = data.aws_iam_role.lab_role.arn

  definition = jsonencode({
    Comment = "JetSmart — flujo de reserva y pago con compensaciones (patrón Saga)"
    StartAt = "ReserveFlight"

    States = {

      # ── Camino exitoso ──────────────────────────────────────────────────────

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
        Next     = "PostBookingActions"
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

      # Notificación y boarding pass en paralelo (best-effort: fallo aquí no revierte el pago)
      PostBookingActions = {
        Type = "Parallel"
        Next = "BookingConfirmed"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "BookingConfirmed"
          ResultPath  = "$.post_actions_error"
        }]
        Branches = [
          {
            StartAt = "NotifyBookingConfirmed"
            States = {
              NotifyBookingConfirmed = {
                Type     = "Task"
                Resource = aws_lambda_function.notification.arn
                Parameters = {
                  "event_type" = "booking_confirmed"
                  "data.$"     = "$"
                }
                End = true
              }
            }
          },
          {
            StartAt = "GenerateBoardingPass"
            States = {
              GenerateBoardingPass = {
                Type     = "Task"
                Resource = aws_lambda_function.boarding_pass.arn
                End      = true
              }
            }
          }
        ]
      }

      BookingConfirmed = {
        Type = "Succeed"
      }

      # ── Camino de compensación ──────────────────────────────────────────────

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
        Next       = "NotifyBookingFailed"
        ResultPath = "$.release"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "NotifyBookingFailed"
          ResultPath  = "$.release_error"
        }]
      }

      NotifyBookingFailed = {
        Type     = "Task"
        Resource = aws_lambda_function.notification.arn
        Parameters = {
          "event_type" = "booking_failed"
          "data.$"     = "$"
        }
        Next       = "BookingDLQ"
        ResultPath = "$.notify_result"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "BookingDLQ"
          ResultPath  = "$.notify_error"
        }]
      }

      # SDK integration: escribe directamente en SQS sin Lambda
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
