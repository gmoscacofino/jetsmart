"""
Refund processor — handlers del Saga de reembolso (Step Functions).

Se dispara cuando un vuelo se cancela (flight_cancelled del topic central). El
Saga tiene dos pasos delegados acá:

  get_affected_pnrs_handler  → Map source: lista de PNRs en el vuelo cancelado.
  refund_pnr_handler         → ejecutado por cada item del Map (idempotente).

Cada paso es invocado directamente por el state machine y recibe/retorna el
estado como dict JSON.
"""
import os, logging

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION              = os.environ["AWS_REGION_VAR"]
BUSINESS_TABLE_NAME = os.environ["BUSINESS_TABLE_NAME"]

dynamodb  = boto3.resource("dynamodb", region_name=REGION)
biz_table = dynamodb.Table(BUSINESS_TABLE_NAME)


def get_affected_pnrs_handler(event, context):
    """
    Input: refund Saga input {"vuelo_numero": "...", "fecha": "...", ...}
    Output: {"vuelo_numero", "fecha", "pnrs": [{"pnr","user_id","email","passenger_name"}, ...]}

    Query el GSI ReservationsByFlight con gsi2pk = FLIGHT#{vuelo}#{fecha}.
    """
    vuelo_numero = event.get("vuelo_numero", "")
    fecha        = event.get("fecha", "")

    log.info("GetAffectedPnrs — vuelo=%s fecha=%s", vuelo_numero, fecha)

    pnrs = []
    last_evaluated = None
    while True:
        query_args = {
            "IndexName": "ReservationsByFlight",
            "KeyConditionExpression": Key("gsi2pk").eq(f"FLIGHT#{vuelo_numero}#{fecha}"),
        }
        if last_evaluated:
            query_args["ExclusiveStartKey"] = last_evaluated
        resp = biz_table.query(**query_args)

        for item in resp.get("Items", []):
            pnr = item.get("pnr") or (
                item.get("gsi2sk", "").replace("PNR#", "") if item.get("gsi2sk") else ""
            )
            if not pnr:
                continue
            pnrs.append({
                "pnr":            pnr,
                "user_id":        item.get("user_id", ""),
                "email":          item.get("email", ""),
                "passenger_name": item.get("passenger_name", ""),
            })

        last_evaluated = resp.get("LastEvaluatedKey")
        if not last_evaluated:
            break

    log.info("GetAffectedPnrs — %d PNRs afectados", len(pnrs))
    return {"vuelo_numero": vuelo_numero, "fecha": fecha, "pnrs": pnrs}


def _refund_payment_mock(pnr: str) -> None:
    """
    MOCK del reembolso al gateway de pagos. En producción acá iría la llamada al
    PSP (Stripe/Adyen) para revertir el cobro asociado al PNR.
    """
    log.info("MOCK refund payment — PNR: %s (gateway reversal simulado)", pnr)


def refund_pnr_handler(event, context):
    """
    Input: UN item de la lista pnrs, ej {"pnr": "...", "user_id": ..., ...}
    Idempotente:
      (a) "refund" del pago (mock — solo loguea).
      (b) update_item PNR#{pnr}/#METADATA status = REFUNDED con
          ConditionExpression status <> REFUNDED.
    ConditionalCheckFailedException → ya reembolsado (idempotente, success).
    Cualquier otro error → raise (Step Functions Retry/Catch lo maneja).
    """
    pnr = event.get("pnr")
    if not pnr:
        log.warning("RefundPnr — sin PNR en el input: %s", event)
        return {"pnr": None, "status": "SKIPPED", "reason": "no_pnr"}

    log.info("RefundPnr — PNR: %s", pnr)

    # (a) Reembolso del pago (mock)
    _refund_payment_mock(pnr)

    # (b) Marcar PNR como REFUNDED — idempotente via ConditionExpression
    try:
        biz_table.update_item(
            Key={"PK": f"PNR#{pnr}", "SK": "#METADATA"},
            UpdateExpression="SET #s = :refunded",
            ConditionExpression="#s <> :refunded",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":refunded": "REFUNDED"},
        )
        log.info("RefundPnr — PNR %s marcado REFUNDED", pnr)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Ya estaba REFUNDED — idempotente, no es un error.
            log.info("RefundPnr — PNR %s ya estaba REFUNDED (idempotente)", pnr)
            return {"pnr": pnr, "status": "REFUNDED"}
        # Error real → raise para que Step Functions reintente/capture.
        log.error("RefundPnr — error actualizando PNR %s: %s", pnr, e)
        raise

    return {"pnr": pnr, "status": "REFUNDED"}
