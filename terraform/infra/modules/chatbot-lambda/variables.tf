variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "conversations_table_name" {
  description = "Nombre de la tabla DynamoDB de conversaciones (sessions, msgs, handoffs)"
  type        = string
}

variable "business_table_name" {
  description = "Nombre de la tabla DynamoDB de negocio (vuelos, PNRs, pasajeros, claims)"
  type        = string
}

variable "human_handoff_queue_url" {
  description = "URL de la SQS de derivación a humano — chat_handler envía mensajes acá"
  type        = string
}

variable "sns_topic_arn" {
  description = "SNS topic ARN for analytics events"
  type        = string
}

variable "anthropic_secret_arn" {
  description = "Secrets Manager ARN containing the Anthropic API key"
  type        = string
}

variable "step_functions_arn" {
  description = "ARN del state machine de Step Functions — chat handler arranca una ejecución para iniciar el flujo de pago"
  type        = string
}

variable "layer_arns" {
  description = "Lista de ARNs de Lambda Layers a attachar al chat handler"
  type        = list(string)
  default     = []
}


variable "environment" {
  description = "Deployment environment (used as API Gateway stage name)"
  type        = string
}

variable "frontend_url" {
  description = "Frontend URL — sent as Access-Control-Allow-Origin header"
  type        = string
}

variable "cognito_user_pool_arn" {
  description = "Cognito User Pool ARN — usado por el API Gateway Cognito Authorizer"
  type        = string
}

variable "pii_token_secret" {
  description = "Clave HMAC para tokenizar PII en chat_handler antes de mandar a la API de Anthropic. Generada con random_password en el root module."
  type        = string
  sensitive   = true
}
