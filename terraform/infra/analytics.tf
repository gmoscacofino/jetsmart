# ── Capa de Analytics: S3 + Glue Catalog + Athena ─────────────────────────────
#
# Data lake serverless alimentado por Kinesis Firehose (ver firehose.tf):
#   - CDC de business (Lambda emitter)  → reservation_events / flight_events / claim_events
#   - Eventos semánticos del chat (SNS) → interaction_events
# Athena consulta vía Glue Data Catalog. Tablas con partition projection (dt/hh)
# → sin crawler. Ver tps/entrega-tp4/analytics-arquitectura.md.

# ── S3: bucket del data lake ──────────────────────────────────────────────────

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
    id     = "transition-lake-to-glacier"
    status = "Enabled"

    filter { prefix = "lake/" }

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

# ── Glue Data Catalog: database + 4 tablas por entidad ────────────────────────

resource "aws_glue_catalog_database" "analytics" {
  name        = replace("${local.name_prefix}_analytics", "-", "_")
  description = "Data lake del chatbot JetSmart para Athena"
}

# Definición de las 4 tablas del lake (una por entidad). El emitter de CDC y los
# eventos semánticos producen JSON Lines que estos esquemas tipan.
locals {
  lake_serde_default     = { "ignore.malformed.json" = "true" }
  lake_serde_interaction = { "ignore.malformed.json" = "true", "mapping.event_ts" = "timestamp" }

  lake_tables = {
    reservation_events = {
      serde = local.lake_serde_default
      columns = [
        { name = "event_id", type = "string" },
        { name = "pnr", type = "string" },
        { name = "event_type", type = "string" },
        { name = "old_status", type = "string" },
        { name = "new_status", type = "string" },
        { name = "total", type = "double" },
        { name = "pax_count", type = "int" },
        { name = "user_id", type = "string" },
        { name = "vuelo", type = "string" },
        { name = "fecha", type = "string" },
        { name = "event_ts", type = "string" },
      ]
    }
    flight_events = {
      serde = local.lake_serde_default
      columns = [
        { name = "event_id", type = "string" },
        { name = "vuelo", type = "string" },
        { name = "origen", type = "string" },
        { name = "destino", type = "string" },
        { name = "fecha", type = "string" },
        { name = "hora_salida", type = "string" },
        { name = "old_estado", type = "string" },
        { name = "new_estado", type = "string" },
        { name = "event_ts", type = "string" },
      ]
    }
    claim_events = {
      serde = local.lake_serde_default
      columns = [
        { name = "event_id", type = "string" },
        { name = "claim_id", type = "string" },
        { name = "event_type", type = "string" },
        { name = "old_status", type = "string" },
        { name = "new_status", type = "string" },
        { name = "tipo", type = "string" },
        { name = "pnr", type = "string" },
        { name = "user_id", type = "string" },
        { name = "event_ts", type = "string" },
      ]
    }
    interaction_events = {
      serde = local.lake_serde_interaction
      columns = [
        { name = "event_type", type = "string" },
        { name = "user_id", type = "string" },
        { name = "event_ts", type = "string" },
      ]
    }
  }
}

resource "aws_glue_catalog_table" "lake" {
  for_each = local.lake_tables

  database_name = aws_glue_catalog_database.analytics.name
  name          = each.key
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL                      = "TRUE"
    classification                = "json"
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.range"         = "2026-01-01,NOW"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"
    "projection.hh.type"          = "integer"
    "projection.hh.range"         = "0,23"
    "projection.hh.digits"        = "2"
    "storage.location.template"   = "s3://${aws_s3_bucket.analytics.bucket}/lake/${each.key}/dt=$${dt}/hh=$${hh}/"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }
  partition_keys {
    name = "hh"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.analytics.bucket}/lake/${each.key}/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    dynamic "columns" {
      for_each = each.value.columns
      content {
        name = columns.value.name
        type = columns.value.type
      }
    }

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters            = each.value.serde
    }
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
