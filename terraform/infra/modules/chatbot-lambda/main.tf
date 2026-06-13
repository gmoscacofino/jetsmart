# ── Lambda: Chat Handler ──────────────────────────────────────────────────────

data "archive_file" "chat_handler" {
  type        = "zip"
  source_file = "${path.module}/../../../../lambda/chat_handler.py"
  output_path = "${path.module}/builds/chat_handler.zip"
}

data "aws_iam_role" "lab_role" {
  name = "LabRole"
}

resource "aws_lambda_function" "chat_handler" {
  function_name    = "${var.name_prefix}-chat-handler"
  filename         = data.archive_file.chat_handler.output_path
  source_code_hash = data.archive_file.chat_handler.output_base64sha256
  runtime          = "python3.12"
  handler          = "chat_handler.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = 60
  layers           = var.layer_arns

  environment {
    variables = {
      AWS_REGION_VAR       = var.aws_region
      DYNAMODB_TABLE_NAME  = var.dynamodb_table_name
      SNS_TOPIC_ARN        = var.sns_topic_arn
      ANTHROPIC_SECRET_ARN = var.anthropic_secret_arn
      SYSTEM_PROMPT_BUCKET = var.system_prompt_bucket
      SYSTEM_PROMPT_KEY    = var.system_prompt_key
      SYSTEM_PROMPT_ETAG   = var.system_prompt_etag
      STEP_FUNCTIONS_ARN   = var.step_functions_arn
      FRONTEND_URL         = var.frontend_url
      COGNITO_USER_POOL_ID = var.cognito_user_pool_id
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ── API Gateway ───────────────────────────────────────────────────────────────

resource "aws_api_gateway_rest_api" "chatbot" {
  name        = "${var.name_prefix}-chatbot-api"
  description = "Endpoints del chatbot JetSmart"
}

# Cognito Authorizer — valida el JWT en el perímetro, antes de invocar Lambda.
# Reemplaza la validación manual que hacía chat_handler.py con python-jose.
resource "aws_api_gateway_authorizer" "cognito" {
  name            = "${var.name_prefix}-cognito-authorizer"
  type            = "COGNITO_USER_POOLS"
  rest_api_id     = aws_api_gateway_rest_api.chatbot.id
  provider_arns   = [var.cognito_user_pool_arn]
  identity_source = "method.request.header.Authorization"
}

# Recurso proxy {proxy+} — enruta cualquier path a chat_handler
resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.chatbot.id
  parent_id   = aws_api_gateway_rest_api.chatbot.root_resource_id
  path_part   = "{proxy+}"
}

# ANY /{proxy+} — Cognito Authorizer rechaza requests sin JWT válido
resource "aws_api_gateway_method" "proxy_any" {
  rest_api_id   = aws_api_gateway_rest_api.chatbot.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}

resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.chatbot.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy_any.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.chat_handler.invoke_arn
  timeout_milliseconds    = 29000 # API GW max; Lambda timeout de 60s cubre cold starts
}

# OPTIONS /{proxy+} — CORS preflight sin auth (los browsers no mandan token en OPTIONS).
# Respuesta MOCK con headers CORS — no invoca la Lambda.
resource "aws_api_gateway_method" "proxy_options" {
  rest_api_id   = aws_api_gateway_rest_api.chatbot.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "proxy_options" {
  rest_api_id = aws_api_gateway_rest_api.chatbot.id
  resource_id = aws_api_gateway_resource.proxy.id
  http_method = aws_api_gateway_method.proxy_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "proxy_options" {
  rest_api_id = aws_api_gateway_rest_api.chatbot.id
  resource_id = aws_api_gateway_resource.proxy.id
  http_method = aws_api_gateway_method.proxy_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "proxy_options" {
  rest_api_id = aws_api_gateway_rest_api.chatbot.id
  resource_id = aws_api_gateway_resource.proxy.id
  http_method = aws_api_gateway_method.proxy_options.http_method
  status_code = aws_api_gateway_method_response.proxy_options.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'${var.frontend_url}'"
  }
}

# ANY / (root) — sin auth: solo expone /health para healthchecks externos
resource "aws_api_gateway_method" "root_any" {
  rest_api_id   = aws_api_gateway_rest_api.chatbot.id
  resource_id   = aws_api_gateway_rest_api.chatbot.root_resource_id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "root" {
  rest_api_id             = aws_api_gateway_rest_api.chatbot.id
  resource_id             = aws_api_gateway_rest_api.chatbot.root_resource_id
  http_method             = aws_api_gateway_method.root_any.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.chat_handler.invoke_arn
  timeout_milliseconds    = 29000
}

# ── Deploy ────────────────────────────────────────────────────────────────────

resource "aws_api_gateway_deployment" "chatbot" {
  rest_api_id = aws_api_gateway_rest_api.chatbot.id

  # Force redeploy cuando cambian recursos del API
  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_resource.proxy.id,
      aws_api_gateway_method.proxy_any.id,
      aws_api_gateway_method.proxy_options.id,
      aws_api_gateway_method.root_any.id,
      aws_api_gateway_integration.proxy.id,
      aws_api_gateway_integration.proxy_options.id,
      aws_api_gateway_integration.root.id,
      aws_api_gateway_authorizer.cognito.id,
    ]))
  }

  depends_on = [
    aws_api_gateway_integration.proxy,
    aws_api_gateway_integration.proxy_options,
    aws_api_gateway_integration.root,
    aws_api_gateway_integration_response.proxy_options,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.chatbot.id
  deployment_id = aws_api_gateway_deployment.chatbot.id
  stage_name    = var.environment
}

# Throttling por método: 10 req/s sostenido, 20 burst
resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.chatbot.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

# ── Permiso para que API GW invoque la Lambda ─────────────────────────────────

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.chat_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.chatbot.execution_arn}/*/*"
}
