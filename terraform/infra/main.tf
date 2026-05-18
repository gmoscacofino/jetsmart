# Data source — AZs disponibles en la región
data "aws_availability_zones" "available" {
  state = "available"
}

# ── Módulo externo: VPC (terraform-aws-modules/vpc/aws) ───────────────────────

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${local.name_prefix}-vpc"
  cidr = var.vpc_cidr

  azs             = local.azs
  public_subnets  = local.public_subnet_cidrs
  private_subnets = concat(local.private_compute_subnet_cidrs, local.private_data_subnet_cidrs)

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags = {
    Tier = "public"
  }

  private_subnet_tags = {
    Tier = "private"
  }
}

# ── Módulo custom: Auth ────────────────────────────────────────────────────────

module "auth" {
  source = "./modules/auth"

  name_prefix  = local.name_prefix
  aws_region   = var.aws_region
  frontend_url = "https://${aws_s3_bucket.frontend.bucket_regional_domain_name}"

  depends_on = [aws_s3_bucket.frontend]
}

# ── Módulo custom: Chatbot Lambda ─────────────────────────────────────────────

module "chatbot_lambda" {
  source = "./modules/chatbot-lambda"

  name_prefix          = local.name_prefix
  aws_region           = var.aws_region
  dynamodb_table_name  = aws_dynamodb_table.main.name
  sns_topic_arn        = aws_sns_topic.events.arn
  anthropic_secret_arn = aws_secretsmanager_secret.anthropic_key.arn
  system_prompt_bucket = aws_s3_bucket.assets.id
  system_prompt_key    = aws_s3_object.system_prompt.key
  system_prompt_etag   = aws_s3_object.system_prompt.etag
  step_functions_arn   = aws_sfn_state_machine.booking.arn
  layer_arns           = [aws_lambda_layer_version.anthropic.arn]
  mock_mode            = var.mock_mode

  depends_on = [
    aws_dynamodb_table.main,
    aws_sns_topic.events,
    aws_sfn_state_machine.booking,
    aws_secretsmanager_secret_version.anthropic_key,
    aws_s3_object.system_prompt,
  ]
}
