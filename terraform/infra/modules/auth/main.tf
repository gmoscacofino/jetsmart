# ── Cognito User Pool ─────────────────────────────────────────────────────────

resource "aws_cognito_user_pool" "main" {
  name = "${var.name_prefix}-user-pool"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_uppercase = true
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
  }

  # Trigger post-registro: asigna grupo al usuario nuevo
  lambda_config {
    post_confirmation = aws_lambda_function.cognito_trigger.arn
  }
}

# ── Cognito: Grupos de usuarios ───────────────────────────────────────────────

locals {
  cognito_groups = {
    users = "Usuarios finales del chatbot"
  }
}

resource "aws_cognito_user_group" "this" {
  for_each = local.cognito_groups

  name         = each.key
  description  = each.value
  user_pool_id = aws_cognito_user_pool.main.id
}

# ── Cognito: App Client ───────────────────────────────────────────────────────

resource "aws_cognito_user_pool_client" "frontend" {
  name         = "${var.name_prefix}-frontend-client"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = false # cliente público (SPA en S3)

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["email", "openid", "profile"]

  callback_urls = ["https://${aws_api_gateway_rest_api.auth.id}.execute-api.${var.aws_region}.amazonaws.com/${var.environment}/callback"]
  # logout_uri al que Cognito redirige DESPUÉS de invalidar la sesión del Hosted UI.
  # El frontend hace Auth.logout() → ${cognitoDomain}/logout?logout_uri=FRONTEND_URL
  # Cognito valida que logout_uri match exacto contra esta lista antes de redirigir.
  logout_urls = [var.frontend_url]

  supported_identity_providers = ["COGNITO"]

  explicit_auth_flows = [
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH"
  ]
}

# ── Cognito: Hosted UI domain ─────────────────────────────────────────────────

data "aws_caller_identity" "current" {}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.name_prefix}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# ── IAM: LabRole preexistente de AWS Academy ──────────────────────────────────

data "aws_iam_role" "lab_role" {
  name = "LabRole"
}

# ── Lambda: Auth Callback ─────────────────────────────────────────────────────

data "archive_file" "auth_callback" {
  type        = "zip"
  source_file = "${path.module}/../../../../lambda/auth_callback.py"
  output_path = "${path.module}/builds/auth_callback.zip"
}

resource "aws_lambda_function" "auth_callback" {
  function_name    = "${var.name_prefix}-auth-callback"
  filename         = data.archive_file.auth_callback.output_path
  source_code_hash = data.archive_file.auth_callback.output_base64sha256
  runtime          = "python3.12"
  handler          = "auth_callback.handler"
  role             = data.aws_iam_role.lab_role.arn

  environment {
    variables = {
      CLIENT_ID      = aws_cognito_user_pool_client.frontend.id
      COGNITO_DOMAIN = "${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
      CALLBACK_URL   = "https://${aws_api_gateway_rest_api.auth.id}.execute-api.${var.aws_region}.amazonaws.com/${var.environment}/callback"
      FRONTEND_URL   = "${var.frontend_url}/index.html"
    }
  }
}

# ── Lambda: Cognito Post-Confirmation Trigger ─────────────────────────────────

data "archive_file" "cognito_trigger" {
  type        = "zip"
  source_file = "${path.module}/../../../../lambda/cognito_trigger.py"
  output_path = "${path.module}/builds/cognito_trigger.zip"
}

resource "aws_lambda_function" "cognito_trigger" {
  function_name    = "${var.name_prefix}-cognito-trigger"
  filename         = data.archive_file.cognito_trigger.output_path
  source_code_hash = data.archive_file.cognito_trigger.output_base64sha256
  runtime          = "python3.12"
  handler          = "cognito_trigger.handler"
  role             = data.aws_iam_role.lab_role.arn

  environment {
    variables = {
      DEFAULT_GROUP = "users"
    }
  }
}

resource "aws_lambda_permission" "cognito_trigger" {
  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cognito_trigger.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.main.arn
}

# ── API Gateway: Callback + Logout endpoints ─────────────────────────────────

resource "aws_api_gateway_rest_api" "auth" {
  name        = "${var.name_prefix}-auth-api"
  description = "Handles Cognito OAuth2 callback"
}

resource "aws_api_gateway_resource" "callback" {
  rest_api_id = aws_api_gateway_rest_api.auth.id
  parent_id   = aws_api_gateway_rest_api.auth.root_resource_id
  path_part   = "callback"
}

resource "aws_api_gateway_method" "callback_get" {
  rest_api_id   = aws_api_gateway_rest_api.auth.id
  resource_id   = aws_api_gateway_resource.callback.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "callback" {
  rest_api_id             = aws_api_gateway_rest_api.auth.id
  resource_id             = aws_api_gateway_resource.callback.id
  http_method             = aws_api_gateway_method.callback_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.auth_callback.invoke_arn
}

resource "aws_api_gateway_resource" "logout" {
  rest_api_id = aws_api_gateway_rest_api.auth.id
  parent_id   = aws_api_gateway_rest_api.auth.root_resource_id
  path_part   = "logout"
}

resource "aws_api_gateway_method" "logout_get" {
  rest_api_id   = aws_api_gateway_rest_api.auth.id
  resource_id   = aws_api_gateway_resource.logout.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "logout" {
  rest_api_id             = aws_api_gateway_rest_api.auth.id
  resource_id             = aws_api_gateway_resource.logout.id
  http_method             = aws_api_gateway_method.logout_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.auth_callback.invoke_arn
}

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.auth.id

  depends_on = [
    aws_api_gateway_integration.callback,
    aws_api_gateway_integration.logout,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.auth.id
  deployment_id = aws_api_gateway_deployment.main.id
  stage_name    = var.environment
}

# Throttling — limita el ratio de codes intercambiados / logouts. Sin esto, un
# atacante podría martillear /callback con codes aleatorios consumiendo invokes
# de la Lambda (y costo). Más conservador que chatbot-api (5/10 vs 10/20) porque
# el flujo de auth es raro por usuario (login + logout cada algunas horas).
resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.auth.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

resource "aws_lambda_permission" "api_gateway_callback" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auth_callback.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.auth.execution_arn}/*/*"
}
