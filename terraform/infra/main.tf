# ── Módulo custom: Auth ────────────────────────────────────────────────────────

module "auth" {
  source = "./modules/auth"

  name_prefix  = local.name_prefix
  aws_region   = var.aws_region
  environment  = var.environment
  frontend_url = "http://${aws_s3_bucket_website_configuration.frontend.website_endpoint}"

  depends_on = [aws_s3_bucket_website_configuration.frontend]
}

# ── Módulo custom: Chatbot Lambda ─────────────────────────────────────────────

module "chatbot_lambda" {
  source = "./modules/chatbot-lambda"

  name_prefix              = local.name_prefix
  aws_region               = var.aws_region
  conversations_table_name = aws_dynamodb_table.conversations.name
  business_table_name      = aws_dynamodb_table.business.name
  human_handoff_queue_url  = aws_sqs_queue.human_handoff.url
  sns_topic_arn            = aws_sns_topic.events.arn
  anthropic_secret_arn     = aws_secretsmanager_secret.anthropic_key.arn
  step_functions_arn       = aws_sfn_state_machine.booking.arn
  layer_arns = [
    aws_lambda_layer_version.anthropic.arn,
    aws_lambda_layer_version.system_prompt.arn,
  ]
  environment           = var.environment
  frontend_url          = "http://${aws_s3_bucket_website_configuration.frontend.website_endpoint}"
  cognito_user_pool_id  = module.auth.user_pool_id
  cognito_user_pool_arn = module.auth.user_pool_arn

  depends_on = [
    aws_dynamodb_table.conversations,
    aws_dynamodb_table.business,
    aws_sqs_queue.human_handoff,
    aws_sns_topic.events,
    aws_sfn_state_machine.booking,
    aws_secretsmanager_secret_version.anthropic_key,
    aws_lambda_layer_version.system_prompt,
    module.auth,
  ]
}
