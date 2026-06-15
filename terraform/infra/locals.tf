locals {
  # Prefijo usado en el nombre de todos los recursos
  name_prefix = "${var.project_name}-${var.environment}"

  # Tags aplicados a todos los recursos via default_tags del provider
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
  }

  # Nombres de las dos tablas DynamoDB (bounded contexts)
  table_conversations = "${local.name_prefix}-conversations"
  table_business      = "${local.name_prefix}-business"
}
