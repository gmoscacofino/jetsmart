#!/usr/bin/env bash
# Sube el frontend estático al bucket S3 y muestra la URL.
# Uso: ./scripts/deploy-frontend.sh
# Requiere: AWS CLI configurado, terraform apply ejecutado previamente.
set -euo pipefail

TERRAFORM_DIR="$(dirname "$0")/../terraform/infra"

echo "Leyendo outputs de Terraform..."
BUCKET=$(cd "$TERRAFORM_DIR" && terraform output -raw frontend_bucket_name)
URL=$(cd "$TERRAFORM_DIR" && terraform output -raw frontend_url)

echo "Sincronizando frontend/ → s3://$BUCKET/"
aws s3 sync "$(dirname "$0")/../frontend/" "s3://$BUCKET/" \
  --delete \
  --cache-control "max-age=3600"

echo ""
echo "Frontend disponible en: $URL"
