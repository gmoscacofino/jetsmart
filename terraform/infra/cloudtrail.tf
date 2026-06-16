# ── CloudTrail ────────────────────────────────────────────────────────────────
#
# Trail multi-region que captura todas las API calls de management plane (IAM,
# Lambda config changes, S3 bucket policies, DynamoDB schema changes, etc.).
# Habilita auditoría forense y trazabilidad de quién hizo qué en la cuenta.
#
# Restricción AWS Academy: CloudTrail puede asumir LabRole y escribir a S3,
# pero NO se puede habilitar CloudWatch Logs integration. La consulta de logs
# se hace ad-hoc: `aws s3 cp` + `gunzip | jq` para investigaciones puntuales,
# o creando una tabla on-demand en Athena cuando hace falta. NO declaramos
# Glue catalog + crawler en este TP porque el JSON classifier default de Glue
# no infiere bien el formato wrapped {"Records":[...]} de CloudTrail — en
# producción real iría con un custom classifier o una tabla manual con
# CloudTrailSerde.
#
# Lifecycle agresivo (90 días) para mantener el costo dentro del budget del lab.

resource "aws_s3_bucket" "cloudtrail" {
  bucket        = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-cloudtrail"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning ON: protección anti-tampering. Si alguien borra los logs, las
# versiones quedan hasta el lifecycle.
resource "aws_s3_bucket_versioning" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  rule {
    id     = "expire-cloudtrail-logs"
    status = "Enabled"

    filter { prefix = "AWSLogs/" }

    expiration { days = 90 }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.cloudtrail]
}

# Bucket policy requerida por CloudTrail para escribir en el bucket.
# Referencia: https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-s3-bucket-policy-for-cloudtrail.html
resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.cloudtrail.arn
        Condition = {
          StringEquals = {
            "aws:SourceArn" = "arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${local.name_prefix}-trail"
          }
        }
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cloudtrail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl"  = "bucket-owner-full-control"
            "aws:SourceArn" = "arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${local.name_prefix}-trail"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.cloudtrail]
}

# Trail: management events multi-region + global service events (IAM/STS).
# No usamos data events porque cuestan extra (~$0.10/100k) y la auditoría
# del management plane alcanza para los criterios de gobernanza del TP4.
resource "aws_cloudtrail" "this" {
  name           = "${local.name_prefix}-trail"
  s3_bucket_name = aws_s3_bucket.cloudtrail.id

  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true
  enable_logging                = true

  # CloudWatch Logs integration NO se habilita: restricción AWS Academy.
  # cloud_watch_logs_group_arn / cloud_watch_logs_role_arn quedan vacíos.

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}
