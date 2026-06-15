"""
Lambda: DynamoDB on-demand export to S3.

Triggered by EventBridge on a daily cron. Fires a DynamoDB
ExportTableToPointInTime against the BUSINESS table only — DynamoDB
processes the export asynchronously in the background and writes the
result to the backups bucket under dynamodb/business/YYYY-MM-DD/.

`conversations` no se exporta: la tabla es efímera por diseño (TTL),
los eventos de negocio relevantes ya viajan al data lake `analytics/events/`
y PITR cubre el escenario de delete accidental dentro de los últimos 35 días.

Requires Point-in-Time Recovery enabled on the business table (it is —
see terraform/infra/database.tf).
"""
import datetime as dt
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb")

BUSINESS_TABLE_ARN = os.environ["BUSINESS_TABLE_ARN"]
BACKUP_BUCKET      = os.environ["BACKUP_BUCKET"]


def _export_table(table_arn: str, today: str) -> dict:
    # El ARN tiene formato arn:aws:dynamodb:region:acct:table/{name}
    table_name = table_arn.split("/")[-1]
    s3_prefix  = f"dynamodb/{table_name}/{today}/"

    response = dynamodb.export_table_to_point_in_time(
        TableArn     = table_arn,
        S3Bucket     = BACKUP_BUCKET,
        S3Prefix     = s3_prefix,
        ExportFormat = "DYNAMODB_JSON",
    )

    export_arn = response["ExportDescription"]["ExportArn"]
    log.info("Started export %s → s3://%s/%s", export_arn, BACKUP_BUCKET, s3_prefix)

    return {
        "table":     table_name,
        "exportArn": export_arn,
        "s3Bucket":  BACKUP_BUCKET,
        "s3Prefix":  s3_prefix,
    }


def handler(event, context):
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    try:
        export = _export_table(BUSINESS_TABLE_ARN, today)
        return {"exports": [export]}
    except Exception as e:
        log.error("Error exportando %s: %s", BUSINESS_TABLE_ARN, e)
        return {"exports": [{"table": BUSINESS_TABLE_ARN, "error": str(e)}]}
