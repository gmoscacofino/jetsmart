# Tests de validación de variables
# Verifica que las reglas de validación definidas en variables.tf funcionan correctamente.
# Ejecutar con: terraform test (desde terraform/infra/)

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-1a", "us-east-1b", "us-east-1c"]
      state = "available"
    }
  }
  mock_data "aws_iam_role" {
    defaults = {
      arn       = "arn:aws:iam::123456789012:role/LabRole"
      name      = "LabRole"
      unique_id = "AROA000000000000000000"
    }
  }
  mock_data "aws_iam_instance_profile" {
    defaults = {
      arn  = "arn:aws:iam::123456789012:instance-profile/LabInstanceProfile"
      name = "LabInstanceProfile"
    }
  }
  mock_data "aws_ami" {
    defaults = {
      id                  = "ami-0abcdef1234567890"
      name                = "al2023-ami-2023.0.0-x86_64"
      owner_id            = "137112412989"
      root_device_type    = "ebs"
      virtualization_type = "hvm"
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

# Verifica que un valor de environment inválido es rechazado por la validación
run "rechaza_environment_invalido" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    rds_password        = "test-password-123"
    state_bucket_suffix = "test"
    environment         = "production"
  }

  expect_failures = [var.environment]
}

# Verifica que "dev" es aceptado
run "acepta_environment_dev" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    rds_password        = "test-password-123"
    state_bucket_suffix = "test"
    environment         = "dev"
  }
}

# Verifica que "staging" es aceptado
run "acepta_environment_staging" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    rds_password        = "test-password-123"
    state_bucket_suffix = "test"
    environment         = "staging"
  }
}

# Verifica que "prod" (el default) es aceptado
run "acepta_environment_prod" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    rds_password        = "test-password-123"
    state_bucket_suffix = "test"
    environment         = "prod"
  }
}
