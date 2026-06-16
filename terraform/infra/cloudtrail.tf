# ── CloudTrail ────────────────────────────────────────────────────────────────
#
# Trail multi-region que captura todas las API calls de management plane (IAM,
# Lambda config changes, S3 bucket policies, DynamoDB schema changes, etc.).
# Habilita auditoría forense y trazabilidad de quién hizo qué en la cuenta.
#
# Restricción AWS Academy: CloudTrail puede asumir LabRole y escribir a S3,
# pero NO se puede habilitar CloudWatch Logs integration. La consulta de logs
# se hace con Athena sobre el bucket S3 (paginando por prefix dt=YYYY/MM/DD).
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

  rule {
    id     = "expire-athena-query-results"
    status = "Enabled"

    filter { prefix = "athena-results/" }

    expiration { days = 14 }
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

# ── Glue Data Catalog + Athena para queries de auditoría ──────────────────────
#
# Como en Academy no podemos sinkar el trail a CloudWatch Logs, la única manera
# de consultar los eventos es Athena sobre el bucket S3. El crawler infiere el
# schema de los archivos JSON.GZ de CloudTrail y crea automáticamente las
# particiones por región/año/mes/día.
#
# Workgroup separado del de business analytics (`-analytics`) para mantener
# segregada la actividad de auditoría — distintos consumidores, distintos
# resultados, distinto cost attribution.

resource "aws_glue_catalog_database" "audit" {
  name        = replace("${local.name_prefix}_audit", "-", "_")
  description = "Database de Glue para los logs de CloudTrail (consultados desde Athena)"
}

resource "aws_glue_crawler" "cloudtrail" {
  name          = "${local.name_prefix}-cloudtrail-crawler"
  database_name = aws_glue_catalog_database.audit.name
  role          = data.aws_iam_role.lab_role.arn

  s3_target {
    path = "s3://${aws_s3_bucket.cloudtrail.bucket}/AWSLogs/${data.aws_caller_identity.current.account_id}/CloudTrail/"
  }

  configuration = jsonencode({
    Version = 1.0
    Grouping = {
      TableGroupingPolicy = "CombineCompatibleSchemas"
    }
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
    }
  })

  # Schedule cada 6 horas: balance entre frescura y costo. Para auditoría
  # ad-hoc también se puede invocar manualmente:
  #   aws glue start-crawler --name jetsmart-prod-cloudtrail-crawler
  schedule = "cron(0 */6 * * ? *)"

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }

  depends_on = [aws_cloudtrail.this]
}

resource "aws_athena_workgroup" "audit" {
  name        = "${local.name_prefix}-audit"
  description = "Workgroup para queries de auditoría sobre los logs de CloudTrail"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.cloudtrail.bucket}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  force_destroy = true
}
