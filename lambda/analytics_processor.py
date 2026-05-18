"""
Lambda: SQS analytics processor.

Triggered by SQS (batch_size=10). Each message is an event published to the
SNS events topic by chat_handler or payment_processor. Unwraps the SNS envelope
and writes the raw event log to RDS PostgreSQL via RDS Proxy.
"""
import os, json, logging
from datetime import datetime, timezone

import boto3
import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION             = os.environ["AWS_REGION_VAR"]
RDS_SECRET_ARN     = os.environ["RDS_SECRET_ARN"]
RDS_PROXY_ENDPOINT = os.environ["RDS_PROXY_ENDPOINT"]

sm = boto3.client("secretsmanager", region_name=REGION)

_rds_conn = None


def _get_rds_conn():
    global _rds_conn
    if _rds_conn and not _rds_conn.closed:
        if _rds_conn.status != psycopg2.extensions.STATUS_READY:
            try:
                _rds_conn.rollback()
            except Exception:
                _rds_conn = None
        if _rds_conn:
            return _rds_conn
    secret = sm.get_secret_value(SecretId=RDS_SECRET_ARN)
    creds  = json.loads(secret["SecretString"])
    _rds_conn = psycopg2.connect(
        host            = RDS_PROXY_ENDPOINT,
        port            = creds.get("port", 5432),
        dbname          = creds["dbname"],
        user            = creds["username"],
        password        = creds["password"],
        connect_timeout = 5,
        sslmode         = "require",
    )
    _rds_conn.autocommit = False
    return _rds_conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS eventos_chat (
    id          BIGSERIAL PRIMARY KEY,
    tipo_evento VARCHAR(50)  NOT NULL,
    usuario_id  VARCHAR(100) NOT NULL,
    timestamp   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    datos       JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_eventos_tipo      ON eventos_chat (tipo_evento);
CREATE INDEX IF NOT EXISTS idx_eventos_usuario   ON eventos_chat (usuario_id);
CREATE INDEX IF NOT EXISTS idx_eventos_timestamp ON eventos_chat (timestamp);
"""


def handler(event, context):
    if event.get("migrate"):
        conn = _get_rds_conn()
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
        log.info("Schema migration complete")
        return {"migrated": True}

    records = event.get("Records", [])
    log.info("Processing %d SQS records", len(records))

    rows = []
    for record in records:
        try:
            body = json.loads(record["body"])
            if "Message" in body:
                body = json.loads(body["Message"])
            rows.append(body)
        except Exception as e:
            log.warning("Skipping malformed record: %s", e)

    if not rows:
        return

    try:
        _write_to_rds(rows)
    except Exception as e:
        log.error("RDS write failed (will retry via SQS): %s", e)
        raise


def _write_to_rds(rows: list):
    conn = _get_rds_conn()
    values = [
        (
            r.get("event_type", "unknown"),
            r.get("user_id", "anon"),
            r.get("timestamp", datetime.now(timezone.utc).isoformat()),
            json.dumps(r.get("payload", {})),
        )
        for r in rows
    ]
    sql = """
        INSERT INTO eventos_chat (tipo_evento, usuario_id, timestamp, datos)
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    conn.commit()
    log.info("Inserted %d rows into RDS", len(values))
