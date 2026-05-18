# ── S3: Frontend estático ─────────────────────────────────────────────────────

resource "aws_s3_bucket" "frontend" {
  bucket = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-frontend"
}

resource "aws_s3_bucket_website_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  index_document { suffix = "index.html" }
  error_document { key = "index.html" }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadGetObject"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# ── S3: System prompt del chatbot ────────────────────────────────────────────
#
# Almacenado en S3 para evitar el límite de 4KB de env vars de Lambda.
# La Lambda lo lee desde S3 en el cold start — sin restricción de tamaño.

resource "aws_s3_object" "system_prompt" {
  bucket       = aws_s3_bucket.assets.id
  key          = "config/system_prompt.txt"
  source       = "${path.module}/templates/system_prompt.tpl"
  content_type = "text/plain"
  etag         = filemd5("${path.module}/templates/system_prompt.tpl")
}

# ── S3: Assets privados (boarding passes, backups) ────────────────────────────

resource "aws_s3_bucket" "assets" {
  bucket = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-assets"
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket = aws_s3_bucket.assets.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    id     = "expire-boarding-passes"
    status = "Enabled"

    filter { prefix = "boarding-passes/" }

    expiration { days = 90 }
  }

  rule {
    id     = "archive-backups"
    status = "Enabled"

    filter { prefix = "backups/" }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }
}
