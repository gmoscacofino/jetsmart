# Tests de locals — verifica que el prefijo de nombres y los tags comunes
# se calculan correctamente desde las variables.
# Ejecutar con: terraform test (desde terraform/infra/)

mock_provider "aws" {
  mock_data "aws_iam_role" {
    defaults = {
      arn       = "arn:aws:iam::123456789012:role/LabRole"
      name      = "LabRole"
      unique_id = "AROA000000000000000000"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:user/test"
      user_id    = "AKIAIOSFODNN7EXAMPLE"
    }
  }
}

mock_provider "archive" {}

run "name_prefix_se_compone_de_project_y_environment" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    state_bucket_suffix = "test"
    environment         = "prod"
  }

  assert {
    condition     = local.name_prefix == "jetsmart-prod"
    error_message = "El name_prefix debería ser 'jetsmart-prod' con project=jetsmart y environment=prod"
  }
}

run "common_tags_contiene_managed_by_terraform" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    state_bucket_suffix = "test"
    environment         = "dev"
  }

  assert {
    condition     = local.common_tags["ManagedBy"] == "Terraform"
    error_message = "El tag ManagedBy=Terraform debe estar siempre presente"
  }

  assert {
    condition     = local.common_tags["Environment"] == "dev"
    error_message = "El tag Environment debe propagar el valor de var.environment"
  }
}

run "table_names_son_distintos_y_bien_formados" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    state_bucket_suffix = "test"
    environment         = "prod"
  }

  assert {
    condition     = local.table_conversations != local.table_business
    error_message = "Las dos tablas deben tener nombres distintos para no colisionar en DynamoDB"
  }

  assert {
    condition     = local.table_conversations == "jetsmart-prod-conversations"
    error_message = "La tabla de conversations debe ser 'jetsmart-prod-conversations'"
  }

  assert {
    condition     = local.table_business == "jetsmart-prod-business"
    error_message = "La tabla de business (PSS-like) debe ser 'jetsmart-prod-business'"
  }
}
