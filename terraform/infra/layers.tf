# ── Lambda Layers ─────────────────────────────────────────────────────────────

# SDK de Anthropic — empaquetado vía pip por scripts/build-layers.sh.
resource "aws_lambda_layer_version" "anthropic" {
  layer_name          = "${local.name_prefix}-anthropic"
  filename            = "${path.module}/builds/anthropic-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/builds/anthropic-layer.zip")
  compatible_runtimes = ["python3.12"]
}

# System prompt del chatbot — montado en /opt/system_prompt.txt dentro del runtime.
#
# Por qué un layer y no S3:
#   - Cero penalty de cold start (filesystem local vs GetObject sobre red).
#   - Versionado inmutable nativo: cada PublishLayerVersion devuelve un ARN
#     nuevo, con rollback de un click cambiando el ARN atachado.
#   - Sin permisos IAM extra (no requiere s3:GetObject).
#
# Por qué un layer separado del de Anthropic:
#   - Distintos ciclos de vida: el SDK cambia cuando Anthropic publica versión,
#     el prompt cambia cuando Producto tunea el bot. Mezclarlos contamina el
#     versionado y obliga a republicar el SDK cada vez que se ajusta una línea
#     del prompt.
resource "aws_lambda_layer_version" "system_prompt" {
  layer_name          = "${local.name_prefix}-system-prompt"
  filename            = "${path.module}/builds/system-prompt-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/builds/system-prompt-layer.zip")
  compatible_runtimes = ["python3.12"]
}
