output "chatbot_api_url" {
  description = "URL base del chatbot (ALB HTTP — usarla en frontend/js/config.js)"
  value       = "http://${aws_lb.main.dns_name}"
}

output "alb_dns_name" {
  description = "DNS del Application Load Balancer del chat-handler"
  value       = aws_lb.main.dns_name
}

output "waf_web_acl_arn" {
  description = "ARN del Web ACL (WAFv2) asociado al ALB del chat-handler"
  value       = aws_wafv2_web_acl.chat.arn
}

output "ecr_chat_handler_url" {
  description = "URL del repo ECR de la imagen chat-handler"
  value       = aws_ecr_repository.chat_handler.repository_url
}

output "ecr_weather_poller_url" {
  description = "URL del repo ECR de la imagen weather-poller"
  value       = aws_ecr_repository.weather_poller.repository_url
}

output "ecs_cluster_name" {
  description = "Nombre del cluster ECS (para aws ecs wait services-stable)"
  value       = aws_ecs_cluster.main.name
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

output "sqs_refund_failures_dlq_url" {
  description = "URL de la DLQ de reembolsos fallidos (refund Saga)"
  value       = aws_sqs_queue.refund_failures_dlq.url
}

output "business_analytics_emitter_function_name" {
  description = "Nombre de la Lambda business-analytics-emitter (CDC → Firehose)"
  value       = aws_lambda_function.business_analytics_emitter.function_name
}

output "stream_emitter_function_name" {
  description = "Nombre de la Lambda stream-emitter (DynamoDB Stream → SNS central)"
  value       = aws_lambda_function.stream_emitter.function_name
}

output "refund_workflow_arn" {
  description = "ARN del state machine de refund (fan-out por PNR)"
  value       = aws_sfn_state_machine.refund.arn
}

output "business_table_stream_arn" {
  description = "ARN del stream de la business table (event source del stream-emitter)"
  value       = aws_dynamodb_table.business.stream_arn
}

# ── Analytics: S3 + Glue + Athena ─────────────────────────────────────────────

output "analytics_bucket" {
  description = "Bucket S3 del data lake (Firehose escribe en lake/<tabla>/)"
  value       = aws_s3_bucket.analytics.bucket
}

output "glue_database_name" {
  description = "Database de Glue Data Catalog usado por Athena"
  value       = aws_glue_catalog_database.analytics.name
}

output "lake_tables" {
  description = "Tablas del data lake en el Glue Catalog"
  value       = keys(local.lake_tables)
}

output "athena_workgroup" {
  description = "Workgroup de Athena del equipo de business analytics"
  value       = aws_athena_workgroup.analytics.name
}
