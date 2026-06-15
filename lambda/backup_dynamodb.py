"""
Lambda: DynamoDB on-demand export to S3.

Triggered by EventBridge on a daily cron. Fires a DynamoDB
ExportTableToPointInTime against BOTH tables (conversations + business)
— DynamoDB processes the exports asynchronously in the background and
writes the results to the backups bucket under
dynamodb/{table_name}/YYYY-MM-DD/.

Requires Point-in-Time Recovery enabled on the source tables (it is —
see terraform/infra/database.tf).
"""
import datetime as dt
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb")

CONVERSATIONS_TABLE_ARN = os.environ["CONVERSATIONS_TABLE_ARN"]
BUSINESS_TABLE_ARN      = os.environ["BUSINESS_TABLE_ARN"]
BACKUP_BUCKET           = os.environ["BACKUP_BUCKET"]


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

    exports = []
    for arn in (CONVERSATIONS_TABLE_ARN, BUSINESS_TABLE_ARN):
        try:
            exports.append(_export_table(arn, today))
        except Exception as e:
            log.error("Error exportando %s: %s", arn, e)
            exports.append({"table": arn, "error": str(e)})

    return {"exports": exports}
