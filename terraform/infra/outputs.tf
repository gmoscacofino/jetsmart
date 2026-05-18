output "chatbot_api_url" {
  description = "URL base del chatbot API Gateway (usarla en frontend/js/config.js)"
  value       = module.chatbot_lambda.api_url
}

output "auth_callback_url" {
  description = "URL del callback OAuth2 de Cognito"
  value       = module.auth.callback_api_url
}

output "frontend_url" {
  description = "URL del frontend estático en S3"
  value       = "http://${aws_s3_bucket_website_configuration.frontend.website_endpoint}"
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID"
  value       = module.auth.user_pool_id
}

output "cognito_client_id" {
  description = "Cognito App Client ID para config del frontend"
  value       = module.auth.client_id
}

output "cognito_hosted_ui_url" {
  description = "URL de la Hosted UI de Cognito (login)"
  value       = module.auth.hosted_ui_url
}

output "step_functions_arn" {
  description = "ARN del state machine de reserva y pago"
  value       = aws_sfn_state_machine.booking.arn
}

output "sns_events_arn" {
  description = "ARN del topic SNS de eventos del chat (analytics)"
  value       = aws_sns_topic.events.arn
}

output "dynamodb_table_name" {
  description = "Nombre de la tabla DynamoDB"
  value       = aws_dynamodb_table.main.name
}

output "rds_endpoint" {
  description = "Endpoint de RDS"
  value       = aws_db_instance.rds.address
  sensitive   = true
}

output "rds_proxy_endpoint" {
  description = "Endpoint del RDS Proxy (usado por analytics_processor)"
  value       = aws_db_proxy.main.endpoint
  sensitive   = true
}

output "sqs_analytics_url" {
  description = "URL de la cola SQS de analytics"
  value       = aws_sqs_queue.analytics.url
}

output "sqs_booking_failed_dlq_url" {
  description = "URL de la DLQ de reservas fallidas"
  value       = aws_sqs_queue.booking_failed_dlq.url
}
