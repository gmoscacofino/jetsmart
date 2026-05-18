#!/usr/bin/env bash
# Construye los Lambda Layers y los empaqueta como ZIPs listos para Terraform.
# Uso: ./scripts/build-layers.sh
# Requiere: pip3, Python 3.12, zip
#
# Genera dos ZIPs en terraform/infra/builds/:
#   anthropic-layer.zip  — SDK de Anthropic (usado por chat-handler)
#   psycopg2-layer.zip   — Driver PostgreSQL (usado por analytics-processor)
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

build_layer "anthropic" "anthropic"
build_layer "psycopg2"  "psycopg2-binary"

echo ""
echo "Layers disponibles en $BUILDS_DIR:"
ls -lh "$BUILDS_DIR/"*-layer.zip
