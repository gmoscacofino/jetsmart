#!/usr/bin/env python3
"""
Script de testing local para simular una cancelación de vuelo.

⚠ TP4: ya NO es el disparador del flujo de proactive notifications.
El production trigger es el DynamoDB Stream de la business table:
cuando un master row FLIGHT# tiene estado_vuelo cambiado a CANCELADO
(por ops desde la consola, otra Lambda, etc.), la Lambda
`flight_cancellation_detector` consume el stream y publica al SNS
`flight_events` automáticamente.

Este script queda como tool de testing local: hace 2 cosas idénticas a
ops real — (1) UpdateItem sobre el FLIGHT# poniendo CANCELADO, (2)
publica directo al SNS por si el detector está caído. El paso (2) es
redundante en TP4 (el Stream ya lo dispara) pero lo mantenemos
ejecutable sin riesgo: proactive_notifications.py filtra duplicados.

Uso:
  export BUSINESS_TABLE_NAME=jetsmart-prod-business
  export SNS_FLIGHT_EVENTS_ARN=arn:aws:sns:us-east-1:<acct>:jetsmart-prod-flight-events
  python3 scripts/cancel_flight.py JA203 2026-06-20 "mal tiempo en Mendoza"

Para testear SOLO el Stream → detector, hacé el UpdateItem desde la consola
DynamoDB en lugar de correr este script.
"""
import sys, json, os
from datetime import datetime, timezone

import boto3

if len(sys.argv) < 3:
    print("Uso: python3 scripts/cancel_flight.py <VUELO> <FECHA YYYY-MM-DD> [REASON]")
    print("Ej:  python3 scripts/cancel_flight.py JA203 2026-06-20 'mal tiempo'")
    sys.exit(1)

VUELO  = sys.argv[1].upper()
FECHA  = sys.argv[2]
REASON = sys.argv[3] if len(sys.argv) > 3 else "operational"

REGION             = os.environ.get("AWS_REGION", "us-east-1")
BIZ_TABLE          = os.environ["BUSINESS_TABLE_NAME"]
SNS_FLIGHT_EVENTS  = os.environ["SNS_FLIGHT_EVENTS_ARN"]

ddb = boto3.client("dynamodb", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)

now_iso = datetime.now(timezone.utc).isoformat()

# ── 1) Marcar todos los items FLIGHT# del vuelo/fecha como CANCELADO ──────────
#
# El vuelo puede estar en distintos PK según ruta (FLIGHT#AEP#MDZ, etc).
# Hacemos Query a GSI1 FlightByNumber para encontrar el item exacto.
print(f"[1/2] Buscando FLIGHT items para {VUELO} {FECHA} via GSI1...")
resp = ddb.query(
    TableName=BIZ_TABLE,
    IndexName="FlightByNumber",
    KeyConditionExpression="vuelo_numero = :v AND fecha = :f",
    ExpressionAttributeValues={
        ":v": {"S": VUELO},
        ":f": {"S": FECHA},
    },
)
items = resp.get("Items", [])
print(f"      Encontrados {len(items)} items")
if not items:
    print(f"      WARNING: no se encontró {VUELO} {FECHA} en la tabla — el evento se publicará igual.")

for it in items:
    pk = it["PK"]["S"]
    sk = it["SK"]["S"]
    ddb.update_item(
        TableName=BIZ_TABLE,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        UpdateExpression=(
            "SET estado_vuelo = :s, cancellation_reason = :r, "
            "cancellation_at = :t, demora_minutos = :dm"
        ),
        ExpressionAttributeValues={
            ":s":  {"S": "CANCELADO"},
            ":r":  {"S": REASON},
            ":t":  {"S": now_iso},
            ":dm": {"N": "0"},
        },
    )
    print(f"      ✓ {pk} / {sk} → CANCELADO")

# ── 2) Publicar evento a SNS flight-events ────────────────────────────────────
print(f"\n[2/2] Publicando evento flight_cancelled a SNS...")
payload = {
    "event_type":   "flight_cancelled",
    "vuelo_numero": VUELO,
    "fecha":        FECHA,
    "reason":       REASON,
    "timestamp":    now_iso,
}
sns.publish(
    TopicArn=SNS_FLIGHT_EVENTS,
    Subject=f"flight_cancelled — {VUELO} {FECHA}",
    Message=json.dumps(payload),
)
print(f"      ✓ Publicado: {json.dumps(payload, indent=2)}")
print(f"\nFlujo completo:")
print(f"  SNS flight-events → SQS proactive-notifications → Lambda proactive_notifications")
print(f"  → Query GSI2 ReservationsByFlight → marca PNRs como AFFECTED_BY_CANCELLATION")
print(f"  → SNS notifications (email a cada pasajero afectado)")
print(f"\nRevisar CloudWatch logs:")
print(f"  /aws/lambda/jetsmart-prod-proactive-notifications")
