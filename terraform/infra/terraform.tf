terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  # Backend parcial — los valores se pasan en terraform init con -backend-config.
  # Ejecución local:
  #   terraform init -backend-config="bucket=jetsmart-terraform-state-<SUFFIX>" \
  #                  -backend-config="key=infra/terraform.tfstate" \
  #                  -backend-config="region=us-east-1" \
  #                  -backend-config="use_lockfile=true" \
  #                  -backend-config="encrypt=true"
  # GitHub Actions: el workflow lo pasa automáticamente desde los secrets.
  backend "s3" {}
}
