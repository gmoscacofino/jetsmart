"""
Lambda: SQS analytics processor.

Triggered by SQS (batch_size=10). Each message is an event published to the
SNS events topic by chat_handler or payment_processor. Unwraps the SNS envelope
and escribe los eventos crudos en S3 en formato JSON Lines (.jsonl), particionado
por dt=YYYY-MM-DD/hh=HH para que Athena pueda hacer partition pruning.

El equipo de business analytics consulta los eventos directamente vía Athena
(Glue Crawler descubre el schema automáticamente).
"""
import os, json, logging
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION           = os.environ["AWS_REGION_VAR"]
ANALYTICS_BUCKET = os.environ["ANALYTICS_BUCKET"]
EVENTS_PREFIX    = os.environ.get("ANALYTICS_PREFIX", "events")

s3 = boto3.client("s3", region_name=REGION)


def handler(event, context):
    records = event.get("Records", [])
    log.info("Processing %d SQS records", len(records))

    rows = []
    for record in records:
        try:
            body = json.loads(record["body"])
            # SNS → SQS wrap: el mensaje real viene en body.Message (string JSON)
            if isinstance(body, dict) and "Message" in body:
                body = json.loads(body["Message"])
            rows.append(body)
        except Exception as e:
            log.warning("Skipping malformed record: %s", e)

    if not rows:
        return {"written": 0}

    now = datetime.now(timezone.utc)
    ingested_at = now.isoformat()
    for r in rows:
        r["ingested_at"] = ingested_at

    # Particionamiento Hive-style: dt=YYYY-MM-DD/hh=HH
    # El Glue Crawler reconoce este formato como particiones automáticamente.
    key = (
        f"{EVENTS_PREFIX}/"
        f"dt={now.strftime('%Y-%m-%d')}/"
        f"hh={now.strftime('%H')}/"
        f"{context.aws_request_id}.jsonl"
    )
    body = "\n".join(json.dumps(r, default=str) for r in rows).encode("utf-8")

    try:
        s3.put_object(
            Bucket=ANALYTICS_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
        )
        log.info("Wrote %d events to s3://%s/%s", len(rows), ANALYTICS_BUCKET, key)
        return {"written": len(rows), "key": key}
    except Exception as e:
        log.error("S3 put_object failed (will retry via SQS): %s", e)
        raise
