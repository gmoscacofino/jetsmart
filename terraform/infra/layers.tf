# ── Lambda Layers ─────────────────────────────────────────────────────────────

resource "aws_lambda_layer_version" "anthropic" {
  layer_name          = "${local.name_prefix}-anthropic"
  filename            = "${path.module}/builds/anthropic-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/builds/anthropic-layer.zip")
  compatible_runtimes = ["python3.12"]
}

resource "aws_lambda_layer_version" "psycopg2" {
  layer_name          = "${local.name_prefix}-psycopg2"
  filename            = "${path.module}/builds/psycopg2-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/builds/psycopg2-layer.zip")
  compatible_runtimes = ["python3.12"]
}
