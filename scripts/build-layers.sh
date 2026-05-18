#!/usr/bin/env bash
# Construye las dependencias del Lambda Layer localmente.
# Uso: ./scripts/build-layers.sh
# Requiere: pip3, Python 3.12
#
# El resultado se guarda en terraform/infra/layers/ (gitignoreado).
# Terraform empaqueta ese directorio como un ZIP al hacer apply.
set -euo pipefail

LAYER_DIR="$(dirname "$0")/../terraform/infra/layers/anthropic/python"

echo "Limpiando layer anterior..."
rm -rf "$LAYER_DIR"
mkdir -p "$LAYER_DIR"

echo "Instalando dependencias para Linux x86_64..."
pip3 install \
  anthropic \
  psycopg2-binary \
  --target "$LAYER_DIR" \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: \
  --quiet

echo "Layer construido en: $LAYER_DIR"
echo "Tamaño: $(du -sh "$LAYER_DIR" | cut -f1)"
