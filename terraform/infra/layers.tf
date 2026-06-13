# ── Lambda Layers ─────────────────────────────────────────────────────────────

resource "aws_lambda_layer_version" "anthropic" {
  layer_name          = "${local.name_prefix}-anthropic"
  filename            = "${path.module}/builds/anthropic-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/builds/anthropic-layer.zip")
  compatible_runtimes = ["python3.12"]
}
