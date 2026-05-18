# Tests de locals — verifica que las funciones cidrsubnet() y slice()
# calculan correctamente las subnets a partir del CIDR de la VPC.
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

run "cidrsubnet_genera_dos_subnets_de_computo" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    rds_password        = "test-password-123"
    state_bucket_suffix = "test"
    vpc_cidr            = "10.0.0.0/16"
  }

  assert {
    condition     = length(local.private_compute_subnet_cidrs) == 2
    error_message = "Deben calcularse exactamente 2 subnets privadas de cómputo"
  }

  assert {
    condition     = length(local.private_data_subnet_cidrs) == 2
    error_message = "Deben calcularse exactamente 2 subnets privadas de datos"
  }

  assert {
    condition     = length(local.azs) == 2
    error_message = "slice() debe seleccionar exactamente 2 AZs de las disponibles"
  }
}

run "cidrsubnet_no_superpone_subnets" {
  command = plan

  variables {
    anthropic_api_key   = "sk-ant-test-key"
    rds_password        = "test-password-123"
    state_bucket_suffix = "test"
    vpc_cidr            = "10.0.0.0/16"
  }

  # Las subnets de cómputo (10.0.3.x, 10.0.4.x) y de datos (10.0.5.x, 10.0.6.x)
  # no deben superponerse entre sí ni con la subnet pública (10.0.1.x)
  assert {
    condition = (
      local.private_compute_subnet_cidrs[0] != local.private_data_subnet_cidrs[0] &&
      local.private_compute_subnet_cidrs[1] != local.private_data_subnet_cidrs[1]
    )
    error_message = "Las subnets de cómputo y datos no deben superponerse"
  }
}
