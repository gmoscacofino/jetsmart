"""
Lambda: Proactive Notifications.

Suscrita (vía SQS) al topic SNS flight-events. Cuando ops marca un vuelo como
cancelado/demorado, hace fan-out de notificaciones a todos los pasajeros con
PNRs afectados.

Flujo:
  Ops actualiza estado_vuelo=CANCELADO en business table (consola/dashboard)
       → DynamoDB Stream
       → Lambda flight_cancellation_detector
       → SNS flight-events (event_type=flight_cancelled, vuelo, fecha, reason)
       → SQS proactive-notifications
       → este Lambda:
           1. Query GSI ReservationsByFlight con HK=FLIGHT#{vuelo}#{fecha}
              → lista de PNRs afectados (con user_id, email)
           2. Para cada PNR: marcar status=AFFECTED_BY_CANCELLATION
           3. Para cada email único: publicar email vía SNS notifications
           4. Emitir evento analytics para tracking
"""
import os, json, logging
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION                = os.environ["AWS_REGION_VAR"]
BUSINESS_TABLE_NAME   = os.environ["BUSINESS_TABLE_NAME"]
SNS_NOTIFICATIONS_ARN = os.environ.get("SNS_NOTIFICATIONS_ARN", "")
SNS_EVENTS_ARN        = os.environ.get("SNS_EVENTS_ARN", "")

dynamodb  = boto3.resource("dynamodb", region_name=REGION)
biz_table = dynamodb.Table(BUSINESS_TABLE_NAME)
sns       = boto3.client("sns", region_name=REGION)


def _process_flight_event(event_body: dict) -> dict:
    event_type   = event_body.get("event_type", "")
    vuelo_numero = event_body.get("vuelo_numero", "")
    fecha        = event_body.get("fecha", "")
    reason       = event_body.get("reason", "operational")

    if not vuelo_numero or not fecha:
        log.warning("Evento sin vuelo/fecha — saltando: %s", event_body)
        return {"affected_pnrs": 0, "reason": "missing_data"}

    if event_type != "flight_cancelled":
        log.info("Tipo de evento no soportado todavía: %s (sólo flight_cancelled)", event_type)
        return {"affected_pnrs": 0, "reason": "event_type_not_supported"}

    log.info("Procesando cancelación vuelo=%s fecha=%s reason=%s", vuelo_numero, fecha, reason)

    # ── Query GSI2: encontrar todos los PNRs en este vuelo+fecha ─────────────
    affected = []
    last_evaluated = None
    while True:
        query_args = {
            "IndexName": "ReservationsByFlight",
            "KeyConditionExpression": "gsi2pk = :pk",
            "ExpressionAttributeValues": {":pk": f"FLIGHT#{vuelo_numero}#{fecha}"},
        }
        if last_evaluated:
            query_args["ExclusiveStartKey"] = last_evaluated
        resp = biz_table.query(**query_args)
        affected.extend(resp.get("Items", []))
        last_evaluated = resp.get("LastEvaluatedKey")
        if not last_evaluated:
            break

    log.info("PNRs afectados: %d", len(affected))

    if not affected:
        return {"affected_pnrs": 0, "reason": "no_affected"}

    # ── Marcar cada PNR como AFFECTED_BY_CANCELLATION ────────────────────────
    emails_seen = set()
    notifications_sent = 0
    for seg in affected:
        pnr = seg.get("pnr") or (seg.get("gsi2sk", "").replace("PNR#", "") if seg.get("gsi2sk") else "")
        if not pnr:
            continue
        try:
            biz_table.update_item(
                Key={"PK": f"PNR#{pnr}", "SK": "#METADATA"},
                UpdateExpression=(
                    "SET #s = :status, cancellation_reason = :r, cancellation_notified_at = :t"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": "AFFECTED_BY_CANCELLATION",
                    ":r":      reason,
                    ":t":      datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            log.warning("No se pudo actualizar PNR %s: %s", pnr, e)

        # Dedup emails — un usuario con varios PNRs en el mismo vuelo no recibe duplicados
        email = seg.get("email", "")
        if not email or email in emails_seen:
            continue
        emails_seen.add(email)

        if SNS_NOTIFICATIONS_ARN:
            try:
                sns.publish(
                    TopicArn=SNS_NOTIFICATIONS_ARN,
                    Subject=f"Cancelación de vuelo {vuelo_numero} — {fecha}",
                    Message=(
                        f"Hola,\n\n"
                        f"Te escribimos para informarte que tu vuelo fue CANCELADO.\n\n"
                        f"Vuelo:      {vuelo_numero}\n"
                        f"Fecha:      {fecha}\n"
                        f"PNR:        {pnr}\n"
                        f"Motivo:     {reason}\n\n"
                        f"Tenés derecho a reprogramación sin costo o reembolso completo.\n"
                        f"Ingresá al chatbot y escribí 'gestionar mi reserva' o "
                        f"derivá a un agente humano si necesitás ayuda.\n\n"
                        f"Disculpá las molestias.\n— JetSmart"
                    ),
                )
                notifications_sent += 1
            except Exception as e:
                log.warning("SNS publish falló para %s: %s", email, e)

    # ── Emitir evento analytics ──────────────────────────────────────────────
    if SNS_EVENTS_ARN:
        try:
            sns.publish(
                TopicArn=SNS_EVENTS_ARN,
                Subject="flight_cancellation_notified",
                Message=json.dumps({
                    "event_type":     "flight_cancellation_notified",
                    "vuelo_numero":   vuelo_numero,
                    "fecha":          fecha,
                    "reason":         reason,
                    "affected_count": len(affected),
                    "emails_sent":    notifications_sent,
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                    "payload":        {"vuelo": vuelo_numero, "fecha": fecha},
                }),
            )
        except Exception as e:
            log.warning("No se pudo emitir evento analytics: %s", e)

    return {
        "vuelo_numero":      vuelo_numero,
        "fecha":             fecha,
        "affected_pnrs":     len(affected),
        "notifications_sent": notifications_sent,
    }


def handler(event, context):
    records = event.get("Records", [])
    log.info("Processing %d SQS record(s)", len(records))

    results = []
    for record in records:
        try:
            sqs_body = json.loads(record["body"])
            # SNS-wrapped: el body de la SQS contiene un JSON con {"Type":"Notification","Message":"<json>"}
            if isinstance(sqs_body, dict) and "Message" in sqs_body:
                flight_event = json.loads(sqs_body["Message"])
            else:
                flight_event = sqs_body
            results.append(_process_flight_event(flight_event))
        except Exception as e:
            log.error("Error procesando record %s: %s", record.get("messageId"), e)
            # Re-raise para que SQS reintente — eventualmente cae a DLQ
            raise

    return {"statusCode": 200, "results": results}
