# ── Capa de Analytics: S3 + Glue Catalog + Athena ─────────────────────────────
#
# Reemplaza el RDS PostgreSQL del TP3 por un data lake serverless. El equipo de
# business analytics consulta los eventos vía Athena con cliente SQL externo
# (DBeaver / DataGrip con Athena JDBC driver).
#
# Flujo:
#   analytics-processor Lambda → S3 (JSON Lines particionado)
#   Glue Crawler → descubre schema y particiones automáticamente
#   Athena → consultas SQL sobre S3 vía Glue Data Catalog

# ── S3: bucket de eventos crudos ──────────────────────────────────────────────

resource "aws_s3_bucket" "analytics" {
  bucket        = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-analytics"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "analytics" {
  bucket = aws_s3_bucket.analytics.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id

  rule {
    id     = "transition-old-events-to-glacier"
    status = "Enabled"

    filter { prefix = "events/" }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }

  rule {
    id     = "expire-athena-query-results"
    status = "Enabled"

    filter { prefix = "athena-results/" }

    expiration { days = 14 }
  }
}

# ── Glue Data Catalog ─────────────────────────────────────────────────────────

resource "aws_glue_catalog_database" "analytics" {
  name        = replace("${local.name_prefix}_analytics", "-", "_")
  description = "Database de eventos del chatbot JetSmart para Athena"
}

# El crawler infiere el schema de los archivos JSON Lines en S3 y crea
# automáticamente las particiones dt= y hh= como columnas Hive-style.
resource "aws_glue_crawler" "events" {
  name          = "${local.name_prefix}-events-crawler"
  database_name = aws_glue_catalog_database.analytics.name
  role          = data.aws_iam_role.lab_role.arn

  s3_target {
    path = "s3://${aws_s3_bucket.analytics.bucket}/events/"
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

  # Schedule: cada hora. Para la demo se puede invocar manualmente con
  # aws glue start-crawler --name <name>
  schedule = "cron(0 * * * ? *)"

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }
}

# ── Athena ────────────────────────────────────────────────────────────────────

resource "aws_athena_workgroup" "analytics" {
  name        = "${local.name_prefix}-analytics"
  description = "Workgroup del equipo de business analytics"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.analytics.bucket}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  force_destroy = true
}
