provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "jetsmart-chatbot"
      ManagedBy = "Terraform"
      Layer     = "backend"
    }
  }
}

# S3 bucket para guardar el state file de Terraform
resource "aws_s3_bucket" "terraform_state" {
  bucket = "jetsmart-terraform-state-${var.state_bucket_suffix}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Tabla DynamoDB para el lock del state (evita que dos personas apliquen al mismo tiempo)
resource "aws_dynamodb_table" "terraform_lock" {
  name         = "jetsmart-terraform-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}
