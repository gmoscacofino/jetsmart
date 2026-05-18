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
    lambda_boarding                = "/aws/lambda/${local.name_prefix}-boarding-pass"
    lambda_notification            = "/aws/lambda/${local.name_prefix}-notification"
    lambda_auth                    = "/aws/lambda/${local.name_prefix}-auth-callback"
    lambda_cognito                 = "/aws/lambda/${local.name_prefix}-cognito-trigger"
    lambda_analytics               = "/aws/lambda/${local.name_prefix}-analytics-processor"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.log_groups
  name              = each.value
  retention_in_days = 30
}
