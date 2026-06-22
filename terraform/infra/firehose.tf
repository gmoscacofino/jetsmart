# ── Kinesis Data Firehose: ingesta al data lake ───────────────────────────────
#
# 4 delivery streams (uno por tabla del lake). Reemplazan a la Lambda
# analytics-processor: Firehose batchea por tamaño/tiempo y escribe a S3 en
# JSON Lines gzip, particionado dt/hh (prefijo con timestamp namespace).
#
# Fuentes:
#   - reservation/flight/claim  ← Lambda business-analytics-emitter (PutRecord)
#   - interaction               ← suscripción SNS del topic central (ver messaging.tf)
#
# Nota: JSON+gzip por robustez (sin acoplar el esquema). Parquet vía
# data_format_conversion es la optimización productiva — queda documentada.
# Los registros que fallan transform/entrega caen al prefijo lake-errors/.

resource "aws_kinesis_firehose_delivery_stream" "lake" {
  for_each = local.lake_tables

  name        = "${local.name_prefix}-${replace(each.key, "_", "-")}"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = data.aws_iam_role.lab_role.arn
    bucket_arn = aws_s3_bucket.analytics.arn

    prefix              = "lake/${each.key}/dt=!{timestamp:yyyy-MM-dd}/hh=!{timestamp:HH}/"
    error_output_prefix = "lake-errors/${each.key}/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"

    buffering_size     = 5  # MB
    buffering_interval = 60 # s
    compression_format = "GZIP"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = "/aws/kinesisfirehose/${local.name_prefix}-${replace(each.key, "_", "-")}"
      log_stream_name = "S3Delivery"
    }
  }
}

resource "aws_cloudwatch_log_group" "firehose" {
  for_each = local.lake_tables

  name              = "/aws/kinesisfirehose/${local.name_prefix}-${replace(each.key, "_", "-")}"
  retention_in_days = 30
}
