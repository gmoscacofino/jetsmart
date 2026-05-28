"""
Genera el boarding pass cuando la reserva queda confirmada.
Invocada directamente por Step Functions (Parallel branch PostBookingActions).

Input: estado completo de la reserva confirmada
  {user_id, reservation_id, flight_info: {ruta, fecha, ...},
   reservation: {pasajeros}, total_pagado, ...}

Sube el archivo a S3 y loguea la pre-signed URL.
"""
import os, logging
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION        = os.environ["AWS_REGION_VAR"]
TABLE_NAME    = os.environ["DYNAMODB_TABLE_NAME"]
ASSETS_BUCKET = os.environ["ASSETS_BUCKET"]

s3 = boto3.client("s3", region_name=REGION)


def handler(event, context):
    reservation_id = event.get("reservation_id", "UNKNOWN")
    user_id        = event.get("user_id", "UNKNOWN")
    flight_info    = event.get("flight_info", {})

    log.info("Generando boarding pass — reserva: %s", reservation_id)

    content = (
        f"BOARDING PASS — JetSmart\n"
        f"{'=' * 40}\n"
        f"Reserva:    {reservation_id}\n"
        f"Ruta:       {flight_info.get('ruta', '—')}\n"
        f"Fecha:      {flight_info.get('fecha', '—')}\n"
        f"Pasajeros:  {event.get('reservation', {}).get('pasajeros', 1)}\n"
        f"Total:      ${event.get('total_pagado', 0):,.0f}\n"
        f"Estado:     CONFIRMADA\n"
        f"Emitido:    {datetime.now(timezone.utc).isoformat()}\n"
    )

    key = f"boarding-passes/{user_id}/{reservation_id}.txt"

    s3.put_object(
        Bucket=ASSETS_BUCKET,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/plain",
    )

    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": ASSETS_BUCKET, "Key": key},
        ExpiresIn=900,
    )

    log.info("Boarding pass listo — reserva: %s — key: %s", reservation_id, key)

    return {**event, "boarding_pass_url": url}
