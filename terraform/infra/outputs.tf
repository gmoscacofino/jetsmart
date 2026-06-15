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

output "frontend_bucket_name" {
  description = "Nombre del bucket S3 del frontend"
  value       = aws_s3_bucket.frontend.bucket
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

output "conversations_table_name" {
  description = "Nombre de la tabla DynamoDB de conversaciones (chatbot state)"
  value       = aws_dynamodb_table.conversations.name
}

output "business_table_name" {
  description = "Nombre de la tabla DynamoDB de negocio (PSS-like: vuelos, PNRs, pasajeros)"
  value       = aws_dynamodb_table.business.name
}

output "conversations_table_arn" {
  description = "ARN de la tabla DynamoDB de conversaciones"
  value       = aws_dynamodb_table.conversations.arn
}

output "business_table_arn" {
  description = "ARN de la tabla DynamoDB de negocio"
  value       = aws_dynamodb_table.business.arn
}

output "sqs_analytics_url" {
  description = "URL de la cola SQS de analytics"
  value       = aws_sqs_queue.analytics.url
}

output "sqs_booking_failed_dlq_url" {
  description = "URL de la DLQ de reservas fallidas"
  value       = aws_sqs_queue.booking_failed_dlq.url
}

output "sqs_human_handoff_url" {
  description = "URL de la cola SQS de derivación a humano"
  value       = aws_sqs_queue.human_handoff.url
}

output "sqs_human_handoff_dlq_url" {
  description = "URL de la DLQ de derivación a humano"
  value       = aws_sqs_queue.human_handoff_dlq.url
}

output "sqs_proactive_notifications_url" {
  description = "URL de la cola SQS de notificaciones proactivas"
  value       = aws_sqs_queue.proactive_notifications.url
}

output "sqs_proactive_notifications_dlq_url" {
  description = "URL de la DLQ de notificaciones proactivas"
  value       = aws_sqs_queue.proactive_notifications_dlq.url
}

output "sqs_boarding_pass_generation_url" {
  description = "URL de la cola SQS de generación async de boarding pass"
  value       = aws_sqs_queue.boarding_pass_generation.url
}

output "sqs_boarding_pass_generation_dlq_url" {
  description = "URL de la DLQ de boarding pass generation"
  value       = aws_sqs_queue.boarding_pass_generation_dlq.url
}

output "sns_flight_events_arn" {
  description = "ARN del topic SNS de eventos de operaciones (cancelaciones, demoras)"
  value       = aws_sns_topic.flight_events.arn
}

output "analytics_processor_function_name" {
  description = "Nombre de la Lambda analytics-processor"
  value       = aws_lambda_function.analytics_processor.function_name
}

# ── Analytics: S3 + Glue + Athena ─────────────────────────────────────────────

output "analytics_bucket" {
  description = "Bucket S3 donde analytics-processor escribe los eventos crudos"
  value       = aws_s3_bucket.analytics.bucket
}

output "glue_database_name" {
  description = "Database de Glue Data Catalog usado por Athena"
  value       = aws_glue_catalog_database.analytics.name
}

output "glue_crawler_name" {
  description = "Nombre del Glue Crawler (start-crawler para refrescar schema)"
  value       = aws_glue_crawler.events.name
}

output "athena_workgroup" {
  description = "Workgroup de Athena del equipo de business analytics"
  value       = aws_athena_workgroup.analytics.name
}
