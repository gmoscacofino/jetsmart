"""
business-analytics-emitter — 2° consumer del DynamoDB Stream de la tabla business.

CDC para el data lake: clasifica cada cambio por entidad (PNR / FLIGHT / CLAIM),
deriva la transición Old→New, redacta PII y hace PutRecord al Firehose que
corresponde. NO toca el flujo operacional (eso lo hace stream_emitter, el 1er
consumer). Ver tps/entrega-tp4/analytics-arquitectura.md.

Lee INSERT (creación) + MODIFY (transiciones). Excluye:
  - items SEAT# (alto volumen, operacional)
  - items PAX# / PASSENGER# y el texto libre de reclamos (PII)
  - REMOVE (cleanup, no es hecho de negocio)
"""

import os
import json
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION              = os.environ["AWS_REGION_VAR"]
FH_RESERVATION      = os.environ["FIREHOSE_RESERVATION"]
FH_FLIGHT           = os.environ["FIREHOSE_FLIGHT"]
FH_CLAIM            = os.environ["FIREHOSE_CLAIM"]

firehose = boto3.client("firehose", region_name=REGION)
_deser   = TypeDeserializer()


def _img(record_img):
    """Deserializa una imagen DynamoDB-JSON ({"S":..}) a dict Python plano."""
    if not record_img:
        return {}
    return {k: _deser.deserialize(v) for k, v in record_img.items()}


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _event_ts(record):
    epoch = record.get("dynamodb", {}).get("ApproximateCreationDateTime")
    if epoch is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _put(stream_name, payload):
    firehose.put_record(
        DeliveryStreamName=stream_name,
        Record={"Data": (json.dumps(payload) + "\n").encode("utf-8")},
    )


# ── Clasificadores por entidad ────────────────────────────────────────────────

def _handle_pnr(record, keys, new, old, event_id, ts):
    sk = keys.get("SK", "")
    pnr = keys.get("PK", "").split("PNR#", 1)[-1]

    # Transiciones de estado de la reserva (#METADATA).
    if sk == "#METADATA":
        new_status = new.get("status")
        old_status = old.get("status")
        if record["eventName"] == "INSERT":
            event_type = "booking_created"
        elif new_status and new_status != old_status:
            event_type = {
                "CONFIRMADA": "booking_confirmed",
                "CANCELADA":  "booking_cancelled",
            }.get(new_status, "booking_updated")
        else:
            return None  # MODIFY sin cambio de estado → no es evento de negocio
        _put(FH_RESERVATION, {
            "event_id":   event_id,
            "pnr":        pnr,
            "event_type": event_type,
            "old_status": old_status,
            "new_status": new_status,
            "total":      _num(new.get("total")),
            "pax_count":  new.get("pasajeros"),  # payment_processor escribe "pasajeros" (int)
            "user_id":    new.get("user_id"),
            "vuelo":      None,
            "fecha":      None,
            "event_ts":   ts,
        })
        return True

    # Segmento de vuelo (INSERT al reservar) → aporta vuelo/fecha para el JOIN
    # reservas ↔ vuelos. Sin estado.
    if sk.startswith("SEGMENT#"):
        if record["eventName"] != "INSERT":
            return None
        _put(FH_RESERVATION, {
            "event_id":   event_id,
            "pnr":        pnr,
            "event_type": "reservation_segment",
            "old_status": None,
            "new_status": None,
            "total":      None,
            "pax_count":  None,
            "user_id":    new.get("user_id"),
            "vuelo":      new.get("vuelo_numero"),
            "fecha":      new.get("fecha"),
            "event_ts":   ts,
        })
        return True

    return None  # PAX#, BP#, EXTRA# → PII o no relevante


def _handle_flight(record, keys, new, old, event_id, ts):
    sk = keys.get("SK", "")
    # Solo master rows; excluir asientos (operacional, alto volumen).
    if "#SEAT#" in sk:
        return None
    pk_parts = keys.get("PK", "").split("#")  # FLIGHT#{origen}#{destino}
    origen  = pk_parts[1] if len(pk_parts) > 1 else None
    destino = pk_parts[2] if len(pk_parts) > 2 else None

    new_estado = new.get("estado_vuelo")
    old_estado = old.get("estado_vuelo")
    if record["eventName"] != "INSERT" and new_estado == old_estado:
        return None  # MODIFY que no cambió el estado del vuelo

    _put(FH_FLIGHT, {
        "event_id":    event_id,
        "vuelo":       new.get("vuelo_numero"),
        "origen":      origen,
        "destino":     destino,
        "fecha":       new.get("fecha"),
        "hora_salida": new.get("hora_salida"),
        "old_estado":  old_estado,
        "new_estado":  new_estado,
        "event_ts":    ts,
    })
    return True


def _handle_claim(record, keys, new, old, event_id, ts):
    if keys.get("SK", "") != "#METADATA":
        return None
    claim_id = keys.get("PK", "").split("CLAIM#", 1)[-1]
    new_status = new.get("status")
    old_status = old.get("status")
    if record["eventName"] == "INSERT":
        event_type = "claim_created"
    elif new_status and new_status != old_status:
        event_type = "claim_resolved" if new_status in ("RESUELTO", "CERRADO") else "claim_updated"
    else:
        return None
    # PII: NO se incluye `descripcion` (texto libre del reclamo).
    _put(FH_CLAIM, {
        "event_id":   event_id,
        "claim_id":   claim_id,
        "event_type": event_type,
        "old_status": old_status,
        "new_status": new_status,
        "tipo":       new.get("tipo"),
        "pnr":        new.get("pnr"),
        "user_id":    new.get("user_id"),
        "event_ts":   ts,
    })
    return True


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            if record["eventName"] == "REMOVE":
                continue  # cleanup/TTL, no es hecho de negocio
            ddb   = record["dynamodb"]
            keys  = {k: _deser.deserialize(v) for k, v in ddb.get("Keys", {}).items()}
            new   = _img(ddb.get("NewImage"))
            old   = _img(ddb.get("OldImage"))
            pk    = keys.get("PK", "")
            ev_id = record["eventID"]            # único → dedup en Athena
            ts    = _event_ts(record)

            if pk.startswith("PNR#"):
                _handle_pnr(record, keys, new, old, ev_id, ts)
            elif pk.startswith("FLIGHT#"):
                _handle_flight(record, keys, new, old, ev_id, ts)
            elif pk.startswith("CLAIM#"):
                _handle_claim(record, keys, new, old, ev_id, ts)
        except Exception:
            log.exception("Fallo procesando record %s", record.get("eventID"))
            seq = record.get("dynamodb", {}).get("SequenceNumber")
            if seq:
                failures.append({"itemIdentifier": seq})

    return {"batchItemFailures": failures}
