"""
Lambda: DynamoDB on-demand export to S3.

Triggered by EventBridge on a daily cron. Fires a DynamoDB
ExportTableToPointInTime against the main table — DynamoDB processes the
export asynchronously in the background and writes the result to the
backups bucket under dynamodb/YYYY-MM-DD/.

Requires Point-in-Time Recovery enabled on the source table (it is — see
terraform/infra/database.tf).
"""
import datetime as dt
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb")

TABLE_ARN     = os.environ["TABLE_ARN"]
BACKUP_BUCKET = os.environ["BACKUP_BUCKET"]


def handler(event, context):
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    s3_prefix = f"dynamodb/{today}/"

    response = dynamodb.export_table_to_point_in_time(
        TableArn     = TABLE_ARN,
        S3Bucket     = BACKUP_BUCKET,
        S3Prefix     = s3_prefix,
        ExportFormat = "DYNAMODB_JSON",
    )

    export_arn = response["ExportDescription"]["ExportArn"]
    log.info("Started export %s → s3://%s/%s", export_arn, BACKUP_BUCKET, s3_prefix)

    return {
        "exportArn": export_arn,
        "s3Bucket":  BACKUP_BUCKET,
        "s3Prefix":  s3_prefix,
    }
