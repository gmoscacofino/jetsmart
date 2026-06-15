# ── Backups de DynamoDB ───────────────────────────────────────────────────────
#
# Mecanismo de backup complementario al PITR (point_in_time_recovery enabled en
# database.tf). PITR cubre los últimos 35 días de forma continua; los exports a
# S3 cubren retención de archivo plurianual para compliance regulatoria.
#
# Alcance (TP4): solo la tabla `business`. La tabla `conversations` no se
# exporta — es efímera por diseño (TTL), los eventos relevantes ya viajan al
# data lake `analytics/events/` y PITR cubre el delete accidental sin sumar
# storage en S3.
#
# Flujo:
#   EventBridge cron diario 03:00 UTC
#         ↓ invoke
#   Lambda backup-dynamodb
#         ↓ dynamodb:ExportTableToPointInTime (async, corre en background)
#   S3 bucket dedicado de backups
#     dynamodb/business/YYYY-MM-DD/AWSDynamoDB/<export-id>/data/*.json.gz
#
# Lifecycle progresivo (alineado a retención AFIP RG 1415, 10 años):
#   -    0 días: STANDARD
#   -   30 días: STANDARD_IA   (acceso esporádico, retrieval inmediato)
#   -   90 días: GLACIER       (retrieval 3-5 h)
#   -  365 días: DEEP_ARCHIVE  (retrieval 12 h, costo mínimo)
#   - 3650 días: expira
#
# Versionado: enabled. Protege contra DELETE/PUT accidental sobre objetos
# existentes. Como los exports usan paths con fecha (YYYY-MM-DD), la misma key
# no se sobreescribe en operación normal — las versiones no-current solo
# aparecen ante intervención manual, por eso se expiran a los 90 días.

# ── S3: bucket dedicado de backups ────────────────────────────────────────────

resource "aws_s3_bucket" "backups" {
  bucket        = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-backups"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "dynamodb-exports-archival"
    status = "Enabled"

    filter { prefix = "dynamodb/" }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    transition {
      days          = 365
      storage_class = "DEEP_ARCHIVE"
    }

    expiration {
      days                         = 3650
      expired_object_delete_marker = false
    }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "GLACIER"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  rule {
    id     = "expire-orphan-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

# ── Lambda: backup-dynamodb ───────────────────────────────────────────────────

data "archive_file" "backup_dynamodb" {
  type        = "zip"
  source_file = "${path.module}/../../lambda/backup_dynamodb.py"
  output_path = "${path.module}/builds/backup_dynamodb.zip"
}

resource "aws_lambda_function" "backup_dynamodb" {
  function_name    = "${local.name_prefix}-backup-dynamodb"
  filename         = data.archive_file.backup_dynamodb.output_path
  source_code_hash = data.archive_file.backup_dynamodb.output_base64sha256
  runtime          = "python3.12"
  handler          = "backup_dynamodb.handler"
  role             = data.aws_iam_role.lab_role.arn
  timeout          = 60

  environment {
    variables = {
      AWS_REGION_VAR     = var.aws_region
      BUSINESS_TABLE_ARN = aws_dynamodb_table.business.arn
      BACKUP_BUCKET      = aws_s3_bucket.backups.bucket
    }
  }

  depends_on = [aws_s3_bucket.backups]
}

resource "aws_cloudwatch_log_group" "backup_dynamodb" {
  name              = "/aws/lambda/${local.name_prefix}-backup-dynamodb"
  retention_in_days = 30
}

# ── EventBridge: trigger diario ───────────────────────────────────────────────
#
# Cron a las 03:00 UTC = 00:00 ART. Hora valle de actividad del chatbot.

resource "aws_cloudwatch_event_rule" "backup_dynamodb_daily" {
  name                = "${local.name_prefix}-backup-dynamodb-daily"
  description         = "Dispara la Lambda backup-dynamodb cada día a las 03:00 UTC"
  schedule_expression = "cron(0 3 * * ? *)"
}

resource "aws_cloudwatch_event_target" "backup_dynamodb_target" {
  rule      = aws_cloudwatch_event_rule.backup_dynamodb_daily.name
  target_id = "backup-dynamodb-lambda"
  arn       = aws_lambda_function.backup_dynamodb.arn
}

resource "aws_lambda_permission" "allow_eventbridge_backup" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backup_dynamodb.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.backup_dynamodb_daily.arn
}

# ── Bucket policy: permitir a DynamoDB escribir el export ─────────────────────
#
# Sin esta policy, dynamodb:ExportTableToPointInTime falla con AccessDenied
# al intentar PutObject en el bucket. El principal es el service principal de
# DynamoDB; la condición SourceAccount restringe al propio account.

resource "aws_s3_bucket_policy" "backups_dynamodb_write" {
  bucket = aws_s3_bucket.backups.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowDynamoDBExport"
      Effect    = "Allow"
      Principal = { Service = "dynamodb.amazonaws.com" }
      Action = [
        "s3:PutObject",
        "s3:GetBucketLocation",
        "s3:AbortMultipartUpload",
      ]
      Resource = [
        aws_s3_bucket.backups.arn,
        "${aws_s3_bucket.backups.arn}/*",
      ]
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.backups]
}
