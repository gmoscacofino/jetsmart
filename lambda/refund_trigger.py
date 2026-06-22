"""
Refund trigger — arranca el Saga de reembolso cuando se cancela un vuelo.

Trigger: SQS (cola `refund`), alimentada por el topic central `events` con
filter policy event_type=flight_cancelled. Cada record es un envelope SNS
entregado a SQS.

Para cada record:
  1. Parsea el envelope SNS (CONTRATO central).
  2. Extrae el payload del vuelo (vuelo_numero, fecha).
  3. start_execution del state machine de refund con name determinístico
     (= {vuelo}-{fecha} sanitizado) → idempotencia: si el Saga para ese vuelo
     ya está corriendo / corrió, ExecutionAlreadyExists y lo tratamos como OK.
"""
import os, re, json, logging

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION              = os.environ["AWS_REGION_VAR"]
REFUND_SFN_ARN      = os.environ["REFUND_SFN_ARN"]
BUSINESS_TABLE_NAME = os.environ.get("BUSINESS_TABLE_NAME", "")

sfn = boto3.client("stepfunctions", region_name=REGION)

# Charset válido para nombres de ejecución de Step Functions: alfanumérico,
# guion y guion bajo. Cualquier otro char lo reemplazamos.
_NAME_SANITIZER = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_name(raw: str) -> str:
    return _NAME_SANITIZER.sub("-", raw)[:80] or "refund"


def handler(event, context):
    records = event.get("Records", [])
    log.info("RefundTrigger — %d SQS record(s)", len(records))

    started = 0
    skipped = 0

    for record in records:
        try:
            # SNS→Lambda directo: el evento llega bajo record["Sns"].
            envelope     = record["Sns"]
            attrs        = envelope.get("MessageAttributes", {})
            event_type   = (attrs.get("event_type") or {}).get("Value")
            payload      = json.loads(envelope["Message"])

            if event_type and event_type != "flight_cancelled":
                log.info("RefundTrigger — event_type %s ignorado", event_type)
                skipped += 1
                continue

            vuelo_numero = payload.get("vuelo_numero", "")
            fecha        = payload.get("fecha", "")
            if not vuelo_numero or not fecha:
                log.warning("RefundTrigger — payload sin vuelo/fecha: %s", payload)
                skipped += 1
                continue

            exec_name = _sanitize_name(f"{vuelo_numero}-{fecha}")
            sfn_input = json.dumps({"vuelo_numero": vuelo_numero, "fecha": fecha})

            try:
                sfn.start_execution(
                    stateMachineArn=REFUND_SFN_ARN,
                    name=exec_name,
                    input=sfn_input,
                )
                log.info("RefundTrigger — Saga iniciado: %s (exec=%s)",
                         vuelo_numero, exec_name)
                started += 1
            except ClientError as e:
                if e.response["Error"]["Code"] == "ExecutionAlreadyExists":
                    # Mismo vuelo+fecha ya disparó el Saga → idempotente.
                    log.info("RefundTrigger — Saga ya existe para %s (idempotente)",
                             exec_name)
                    skipped += 1
                    continue
                raise
        except Exception as e:
            log.error("RefundTrigger — error en record %s: %s",
                      record.get("messageId"), e)
            # Re-raise para que SQS reintente → eventualmente DLQ.
            raise

    log.info("RefundTrigger done — started=%d skipped=%d", started, skipped)
    return {"statusCode": 200, "started": started, "skipped": skipped}
