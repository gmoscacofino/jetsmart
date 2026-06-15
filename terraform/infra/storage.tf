# ── S3: Frontend estático ─────────────────────────────────────────────────────

resource "aws_s3_bucket" "frontend" {
  bucket        = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-frontend"
  force_destroy = true
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

# ── S3: Boarding passes ───────────────────────────────────────────────────────
#
# Bucket privado donde la Lambda boarding-pass-async escribe los BP de cada PNR.
# El contenido es write-once por PNR (no hay overwrites) y se accede vía
# presigned URL desde el chatbot.
#
# Renombrado desde "assets" en TP4: cuando el system prompt se movió a Lambda
# Layer, el bucket pasó a contener únicamente boarding passes.

resource "aws_s3_bucket" "boarding_passes" {
  bucket        = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-boarding-passes"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "boarding_passes" {
  bucket = aws_s3_bucket.boarding_passes.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "boarding_passes" {
  bucket = aws_s3_bucket.boarding_passes.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versionado ON — protege contra delete accidental (feedback de Faustino en TP2).
# Los BP son write-once por PNR; el riesgo real es DELETE, no overwrite. El
# noncurrent_version_expiration evita acumulación infinita de versiones huérfanas
# después de que la regla de expiración corre.
resource "aws_s3_bucket_versioning" "boarding_passes" {
  bucket = aws_s3_bucket.boarding_passes.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "boarding_passes" {
  bucket = aws_s3_bucket.boarding_passes.id

  rule {
    id     = "expire-boarding-passes"
    status = "Enabled"

    filter {}

    expiration { days = 90 }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.boarding_passes]
}
