"""
Lambda: Boarding Pass Async Generator.

TP4: el boarding pass se desacopló del Saga. Step Functions
(PostBookingActions Branch B) publica un mensaje a SQS boarding-pass-generation
con el estado completo de la reserva confirmada. Esta Lambda lo consume y:

  1. Genera el boarding pass (texto plano por simplicidad académica).
  2. PutObject en S3 boarding-passes bucket bajo {user_id}/{pnr}.txt
  3. Genera presigned URL (15 min).
  4. UpdateItem en business table: PNR#{pnr}/BP#01 con s3_key y bp_url.

Si la generación falla N veces, el mensaje cae a la DLQ
boarding-pass-generation-dlq sin afectar la reserva ya confirmada.

Diferencia vs el viejo boarding_pass.py (sync, invocado por Saga):
  - Ahora es event-driven (SQS trigger, no llamada directa).
  - Persistimos el bp_url en DynamoDB para que la tool get_boarding_pass del
    chat lo pueda leer cuando el usuario lo pida (no tiene que esperar al
    Saga). Si el item BP#01 no existe todavía, get_boarding_pass devuelve
    "generándose, intentá en unos segundos".
"""
import os, json, logging
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION              = os.environ["AWS_REGION_VAR"]
BUSINESS_TABLE_NAME = os.environ["BUSINESS_TABLE_NAME"]
BOARDING_PASSES_BUCKET       = os.environ["BOARDING_PASSES_BUCKET"]

dynamodb  = boto3.resource("dynamodb", region_name=REGION)
biz_table = dynamodb.Table(BUSINESS_TABLE_NAME)
s3        = boto3.client("s3", region_name=REGION)


def _generate_bp(saga_state: dict) -> dict:
    pnr            = saga_state.get("pnr") or saga_state.get("reservation_id", "UNKNOWN")
    user_id        = saga_state.get("user_id", "UNKNOWN")
    flight_info    = saga_state.get("flight_info", {})
    reservation    = saga_state.get("reservation", {})
    total          = saga_state.get("total_pagado", 0)
    passenger_name = reservation.get("nombre_pasajero", "Pasajero")
    pasajeros      = reservation.get("pasajeros", 1)

    log.info("Generando boarding pass — PNR: %s user: %s", pnr, user_id)

    content = (
        f"BOARDING PASS — JetSmart\n"
        f"{'=' * 40}\n"
        f"PNR:        {pnr}\n"
        f"Pasajero:   {passenger_name}\n"
        f"Ruta:       {flight_info.get('ruta', '—')}\n"
        f"Vuelo:      {flight_info.get('vuelo_numero', '—')}\n"
        f"Fecha:      {flight_info.get('fecha', '—')}\n"
        f"Pasajeros:  {pasajeros}\n"
        f"Total:      ${total:,.2f}\n"
        f"Estado:     CONFIRMADA\n"
        f"Emitido:    {datetime.now(timezone.utc).isoformat()}\n"
    )

    key = f"{user_id}/{pnr}.txt"

    s3.put_object(
        Bucket=BOARDING_PASSES_BUCKET,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/plain",
    )

    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BOARDING_PASSES_BUCKET, "Key": key},
        ExpiresIn=900,
    )

    # Persistimos referencia en business table — la tool get_boarding_pass la lee
    biz_table.put_item(Item={
        "PK":         f"PNR#{pnr}",
        "SK":         "BP#01",
        "pnr":        pnr,
        "user_id":    user_id,
        "s3_key":     key,
        "bp_url":     url,
        "issued_at":  datetime.now(timezone.utc).isoformat(),
    })

    log.info("BP listo — PNR: %s — s3://%s/%s", pnr, BOARDING_PASSES_BUCKET, key)
    return {"pnr": pnr, "s3_key": key, "bp_url": url}


def handler(event, context):
    records = event.get("Records", [])
    log.info("Processing %d boarding-pass message(s)", len(records))

    results = []
    for record in records:
        try:
            saga_state = json.loads(record["body"])
            results.append(_generate_bp(saga_state))
        except Exception as e:
            log.error("Error generando BP en record %s: %s", record.get("messageId"), e)
            raise  # SQS reintenta hasta DLQ

    return {"statusCode": 200, "generated": results}
