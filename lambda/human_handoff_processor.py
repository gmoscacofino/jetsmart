"""
Lambda: Human Handoff Processor.

Consume mensajes de la cola SQS human-handoff cuando el chatbot decide derivar
una conversación a un agente humano (vía la tool `escalate_to_human`).

Lo que haría en producción:
  POST a la API del sistema del call center con el handoff_id, transcript, etc.
  El call center crea un ticket en su CRM y un agente lo toma.

Lo que hace acá (mock):
  - Loguea el "POST" simulado a CloudWatch (visible para defender el demo).
  - Genera un call_center_ticket sintético (CC-XXXX).
  - Actualiza el item HANDOFF# en conversations table a status=ACK.
  - Publica un email vía SNS notifications confirmando al usuario que su
    pedido fue tomado por un agente.

Si N retries fallan, el mensaje cae a la DLQ human-handoff-dlq y se dispara
la alarma de CloudWatch correspondiente.
"""
import os, json, uuid, logging
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION                   = os.environ["AWS_REGION_VAR"]
CONVERSATIONS_TABLE_NAME = os.environ["CONVERSATIONS_TABLE_NAME"]
SNS_NOTIFICATIONS_ARN    = os.environ.get("SNS_NOTIFICATIONS_ARN", "")

dynamodb   = boto3.resource("dynamodb", region_name=REGION)
conv_table = dynamodb.Table(CONVERSATIONS_TABLE_NAME)
sns        = boto3.client("sns", region_name=REGION)


def _process_record(body: dict) -> None:
    handoff_id = body["handoff_id"]
    session_id = body["session_id"]
    user_id    = body["user_id"]
    reason     = body.get("reason", "")
    urgency    = body.get("urgency", "medium")
    now        = datetime.now(timezone.utc).isoformat()

    log.info("Processing handoff %s urgency=%s user=%s", handoff_id, urgency, user_id)

    # ── MOCK: POST al sistema del call center ────────────────────────────────
    # En producción acá iría algo como:
    #   requests.post("https://callcenter.jetsmart.internal/api/tickets", json={...})
    # El call center responde con su propio ticket id.
    cc_ticket = f"CC-{uuid.uuid4().hex[:8].upper()}"
    log.info("MOCK POST https://mock.callcenter.internal/tickets — payload: %s — ticket: %s",
             json.dumps({"handoff_id": handoff_id, "urgency": urgency, "reason": reason}),
             cc_ticket)

    # ── Actualizar el HANDOFF# en conversations table ────────────────────────
    # Hacemos un Query para encontrar el item por session_id (el SK incluye ts)
    resp = conv_table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk":     f"SESSION#{session_id}",
            ":prefix": "HANDOFF#",
        },
    )
    for item in resp.get("Items", []):
        if item.get("handoff_id") == handoff_id:
            conv_table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET #s = :status, call_center_ticket = :cc, acked_at = :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": "ACK",
                    ":cc":     cc_ticket,
                    ":now":    now,
                },
            )
            break

    # Update también el thin pointer USER#/HANDOFF#
    try:
        conv_table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"HANDOFF#{handoff_id}"},
            UpdateExpression="SET #s = :status, call_center_ticket = :cc",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "ACK", ":cc": cc_ticket},
        )
    except Exception as e:
        log.warning("No se pudo actualizar pointer USER#%s/HANDOFF#%s: %s", user_id, handoff_id, e)

    # ── Notificar al usuario por email vía SNS ───────────────────────────────
    if SNS_NOTIFICATIONS_ARN:
        try:
            sns.publish(
                TopicArn=SNS_NOTIFICATIONS_ARN,
                Subject=f"Tu solicitud de soporte fue derivada — {handoff_id}",
                Message=(
                    f"Tu pedido fue tomado por nuestro equipo de soporte humano.\n\n"
                    f"Ticket del chatbot:    {handoff_id}\n"
                    f"Ticket del call center: {cc_ticket}\n"
                    f"Prioridad:             {urgency}\n"
                    f"Motivo:                {reason}\n\n"
                    f"Un agente se va a contactar con vos a la brevedad."
                ),
            )
        except Exception as e:
            log.warning("SNS publish falló para handoff %s: %s", handoff_id, e)
    else:
        log.warning("SNS_NOTIFICATIONS_ARN no configurado — notificación omitida")


def handler(event, context):
    records = event.get("Records", [])
    log.info("Processing %d handoff record(s)", len(records))

    for record in records:
        try:
            # La cola ahora la alimenta el topic central `events` (filter
            # handoff_requested): cada record es un envelope SNS. El payload del
            # handoff viaja dentro de Message (CONTRATO central).
            envelope = json.loads(record["body"])
            handoff  = json.loads(envelope["Message"])
            _process_record(handoff)
        except Exception as e:
            log.error("Error procesando record %s: %s", record.get("messageId"), e)
            # Re-raise para que SQS lo reintente (eventualmente cae a DLQ)
            raise

    return {"statusCode": 200, "processed": len(records)}
