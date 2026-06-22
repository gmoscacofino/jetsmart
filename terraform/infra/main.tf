# ── Módulo custom: Auth ────────────────────────────────────────────────────────

module "auth" {
  source = "./modules/auth"

  name_prefix  = local.name_prefix
  aws_region   = var.aws_region
  environment  = var.environment
  frontend_url = "http://${aws_s3_bucket_website_configuration.frontend.website_endpoint}"

  depends_on = [aws_s3_bucket_website_configuration.frontend]
}

# ── Chatbot core: ahora en ECS Fargate detrás del ALB ─────────────────────────
#
# El chat-handler dejó de ser Lambda + API Gateway. Vive en un contenedor Fargate
# (terraform/infra/ecs.tf) detrás del ALB (alb.tf), validando el JWT de Cognito
# in-app. El módulo chatbot-lambda quedó deprecado y desconectado.
