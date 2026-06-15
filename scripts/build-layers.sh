#!/usr/bin/env bash
# Construye los Lambda Layers y los empaqueta como ZIPs listos para Terraform.
# Uso: ./scripts/build-layers.sh
# Requiere: pip3, Python 3.12, zip
#
# Genera ZIPs en terraform/infra/builds/:
#   anthropic-layer.zip      — SDK de Anthropic (usado por chat-handler)
#   system-prompt-layer.zip  — system prompt del chatbot expuesto como /opt/system_prompt.txt
#
# Nota: la validación JWT manual (python-jose) se eliminó cuando se activó el
# Cognito Authorizer en API Gateway. La validación ahora la hace AWS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAYERS_DIR="$ROOT/terraform/infra/layers"
BUILDS_DIR="$ROOT/terraform/infra/builds"

mkdir -p "$BUILDS_DIR"

build_layer() {
  local name="$1"
  shift
  local packages=("$@")

  echo "==> Construyendo layer '$name'..."
  rm -rf "$LAYERS_DIR/$name"
  mkdir -p "$LAYERS_DIR/$name/python"

  pip3 install "${packages[@]}" \
    --target "$LAYERS_DIR/$name/python" \
    --platform manylinux2014_x86_64 \
    --python-version 3.12 \
    --only-binary=:all: \
    --quiet

  echo "    Empaquetando ${name}-layer.zip..."
  (cd "$LAYERS_DIR/$name" && zip -r "$BUILDS_DIR/${name}-layer.zip" python/ -q)
  echo "    Listo: $(du -sh "$BUILDS_DIR/${name}-layer.zip" | cut -f1)"
}

# Layer del SDK de Anthropic (Python packages → /opt/python/)
build_layer "anthropic" "anthropic"

# Layer del system prompt — archivo de texto montado en /opt/system_prompt.txt
# El prompt es ownership del artefacto de deploy: cambiarlo requiere un nuevo
# layer version, igual que cambiar código. Esto le da versionado inmutable
# (cada PublishLayerVersion devuelve un ARN distinto) y elimina la dependencia
# de S3 GetObject en cold start.
echo "==> Construyendo layer 'system-prompt'..."
SYS_PROMPT_DIR="$LAYERS_DIR/system-prompt"
rm -rf "$SYS_PROMPT_DIR"
mkdir -p "$SYS_PROMPT_DIR"
cp "$ROOT/terraform/infra/templates/system_prompt.tpl" "$SYS_PROMPT_DIR/system_prompt.txt"
(cd "$SYS_PROMPT_DIR" && zip -r "$BUILDS_DIR/system-prompt-layer.zip" system_prompt.txt -q)
echo "    Listo: $(du -sh "$BUILDS_DIR/system-prompt-layer.zip" | cut -f1)"

echo ""
echo "Layers disponibles en $BUILDS_DIR:"
ls -lh "$BUILDS_DIR/"*-layer.zip
