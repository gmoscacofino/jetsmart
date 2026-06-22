"""
Stream emitter — consume el DynamoDB Stream de la business table y emite al
topic SNS central (`events`) cuando un master row FLIGHT# transiciona su
estado_vuelo a CANCELADO.

Es el trigger del flujo de notificaciones proactivas + refund Saga. Ops cambia
el estado del vuelo (consola DynamoDB / dashboard interno) y el Stream propaga
el cambio automáticamente.

Trigger: DynamoDB Stream con filter_criteria en el event source mapping
(reduce invocaciones). El handler hace un guard adicional para:
  - Confirmar que es un master row FLIGHT# (no un SEAT# ni un PNR# que pudo
    matchear el filtro por coincidencia).
  - Detectar TRANSICIÓN (OldImage.estado_vuelo != CANCELADO) — no re-publicar
    si el ítem ya estaba cancelado y solo cambió otro atributo.

Publica al SNS central `events` con MessageAttributes event_type=flight_cancelled.
El fan-out a las colas (proactive-notifications, refund) lo hace el topic via
filter policies sobre event_type.
"""
import os, json, logging
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION         = os.environ["AWS_REGION_VAR"]
SNS_EVENTS_ARN = os.environ["SNS_EVENTS_ARN"]

sns = boto3.client("sns", region_name=REGION)


def _get_s(image: dict, key: str) -> str:
    """Extrae un string de un image de DynamoDB Stream (formato AttributeValue)."""
    return (image or {}).get(key, {}).get("S", "") if image else ""


def handler(event, context):
    records = event.get("Records", [])
    log.info("StreamEmitter — %d stream records", len(records))

    published = 0
    skipped   = 0
    now_iso   = datetime.now(timezone.utc).isoformat()

    for record in records:
        event_name = record.get("eventName")
        if event_name != "MODIFY":
            skipped += 1
            continue

        ddb_data = record.get("dynamodb", {})
        new_image = ddb_data.get("NewImage")
        old_image = ddb_data.get("OldImage")
        if not new_image:
            skipped += 1
            continue

        pk = _get_s(new_image, "PK")
        sk = _get_s(new_image, "SK")

        # Guard: solo master rows de vuelo (no SEAT#, no PNR#, etc.)
        if not pk.startswith("FLIGHT#"):
            skipped += 1
            continue
        if "#SEAT#" in sk:
            skipped += 1
            continue

        new_estado = _get_s(new_image, "estado_vuelo")
        if new_estado != "CANCELADO":
            skipped += 1
            continue

        # Transición real: OldImage tenía otro estado (o no existía)
        old_estado = _get_s(old_image, "estado_vuelo") if old_image else ""
        if old_estado == "CANCELADO":
            log.info("Skip: %s/%s ya estaba CANCELADO (no es transición)", pk, sk)
            skipped += 1
            continue

        vuelo_numero = _get_s(new_image, "vuelo_numero")
        fecha        = _get_s(new_image, "fecha")
        reason       = _get_s(new_image, "cancellation_reason") or "operational"

        # El PK del master row codifica FLIGHT#{origen}#{destino} — lo desarmamos.
        origen, destino = "", ""
        pk_parts = pk.split("#")
        if len(pk_parts) >= 3:
            origen, destino = pk_parts[1], pk_parts[2]

        log.info("Detected cancellation transition: %s %s (was %s → CANCELADO)",
                 vuelo_numero, fecha, old_estado or "<absent>")

        payload = {
            "event_type":          "flight_cancelled",
            "vuelo_numero":        vuelo_numero,
            "fecha":               fecha,
            "origen":              origen,
            "destino":             destino,
            "cancellation_reason": reason,
            "detected_at":         now_iso,
            "source":              "dynamodb_stream",
        }

        try:
            sns.publish(
                TopicArn=SNS_EVENTS_ARN,
                Subject=f"flight_cancelled — {vuelo_numero} {fecha}",
                Message=json.dumps(payload),
                MessageAttributes={
                    "event_type": {"DataType": "String", "StringValue": "flight_cancelled"},
                },
            )
            published += 1
        except Exception as e:
            # Si publicar falla, el Stream va a reintentar este record con
            # backoff (event source mapping default = bisecando hasta el ítem
            # malo). Logueamos y dejamos que el framework retry.
            log.error("SNS publish failed for %s %s: %s", vuelo_numero, fecha, e)
            raise

    log.info("StreamEmitter done — published=%d skipped=%d", published, skipped)
    return {"published": published, "skipped": skipped}
