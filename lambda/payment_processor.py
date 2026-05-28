"""
Handlers de pago para Step Functions (patrón Saga).

Cada handler es invocado directamente por el state machine.
Recibe el estado actual como input y retorna el estado actualizado.
No hay SQS ni SNS entre pasos — Step Functions maneja la orquestación.

Flujo exitoso:
  reserve_flight → reserve_booking → collect_payment → confirm_booking

Compensaciones (rollback):
  refund_payment → cancel_booking → release_flight
"""
import os, json, uuid, logging
from decimal import Decimal
from datetime import datetime, timezone

from botocore.exceptions import ClientError

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION        = os.environ["AWS_REGION_VAR"]
TABLE_NAME    = os.environ["DYNAMODB_TABLE_NAME"]
SNS_EVENTS    = os.environ.get("SNS_EVENTS_ARN", "")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table    = dynamodb.Table(TABLE_NAME)
sns      = boto3.client("sns", region_name=REGION)


# ── Paso 1 ────────────────────────────────────────────────────────────────────
# Verifica disponibilidad y bloquea el asiento (decremento atómico).
# Input:  {payment_id, user_id, reservation: {origen, destino, fecha, pasajeros}}
# Output: agrega flight_info al estado

def reserve_flight_handler(event, context):
    log.info("ReserveFlight — payment_id: %s", event.get("payment_id"))

    reservation = event["reservation"]
    origen      = reservation["origen"]
    destino     = reservation["destino"]
    fecha       = reservation["fecha"]
    pasajeros   = int(reservation.get("pasajeros", 1))

    resp = table.get_item(
        Key={"PK": f"FLIGHT#{origen}#{destino}", "SK": f"DATE#{fecha}"}
    )
    if "Item" not in resp:
        raise ValueError(f"Sin vuelos disponibles para {origen}-{destino} el {fecha}")

    vuelo       = resp["Item"]
    disponibles = int(vuelo.get("asientos_disponibles", 0))

    if disponibles < pasajeros:
        raise ValueError(f"Asientos insuficientes: disponibles={disponibles}, solicitados={pasajeros}")

    table.update_item(
        Key={"PK": f"FLIGHT#{origen}#{destino}", "SK": f"DATE#{fecha}"},
        UpdateExpression="ADD asientos_disponibles :dec",
        ConditionExpression="asientos_disponibles >= :min",
        ExpressionAttributeValues={":dec": -pasajeros, ":min": pasajeros},
    )

    return {
        **event,
        "flight_info": {
            "origen":               origen,
            "destino":              destino,
            "ruta":                 f"{origen}-{destino}",
            "vuelo_numero":         vuelo.get("vuelo_numero", ""),
            "fecha":                fecha,
            "precio_por_pasajero":  float(vuelo.get("precio", 0)),
            "pasajeros_bloqueados": pasajeros,
        },
    }


# ── Paso 2 ────────────────────────────────────────────────────────────────────
# Crea la reserva en DynamoDB con estado PENDIENTE.
# Output: agrega reservation_id al estado

def reserve_booking_handler(event, context):
    log.info("ReserveBooking — payment_id: %s", event.get("payment_id"))

    flight_info    = event["flight_info"]
    reservation_r  = event["reservation"]
    pasajeros      = int(reservation_r.get("pasajeros", 1))
    reservation_id = f"RES-{event['payment_id'].replace('-', '')[:8].upper()}"
    # Total calculado desde la base de datos — nunca del input del usuario.
    total = Decimal(str(flight_info["precio_por_pasajero"])) * pasajeros

    passenger_name = reservation_r.get("nombre_pasajero", "")
    email          = reservation_r.get("email_contacto", "")
    phone          = reservation_r.get("telefono", "")

    try:
        table.put_item(
            Item={
                "PK":              f"USER#{event['user_id']}",
                "SK":              f"RESERVATION#{reservation_id}",
                "reservation_id":  reservation_id,
                "status":          "PENDIENTE",
                "origin":          flight_info["origen"],
                "destination":     flight_info["destino"],
                "flight_number":   flight_info.get("vuelo_numero", ""),
                "flight_date":     flight_info["fecha"],
                "passenger_count": pasajeros,
                "tarifa":          reservation_r.get("tarifa", ""),
                "total":           total,
                "email":           email,
                "phone":           phone,
                "passenger_name":  passenger_name,
                "created_at":      datetime.now(timezone.utc).isoformat(),
            },
            ConditionExpression="attribute_not_exists(SK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.info("ReserveBooking — reserva ya existe, idempotente: %s", reservation_id)
            return {**event, "reservation_id": reservation_id}
        raise

    # Auto-save passenger profile for future bookings (upsert: increment reservation count).
    if passenger_name:
        passenger_key = passenger_name.lower().replace(" ", "_")[:40]
        table.update_item(
            Key={
                "PK": f"USER#{event['user_id']}",
                "SK": f"PASSENGER#{passenger_key}",
            },
            UpdateExpression=(
                "SET passenger_name = :name, email = :email, phone = :phone, "
                "last_booking = :now "
                "ADD reservation_count :one"
            ),
            ExpressionAttributeValues={
                ":name":  passenger_name,
                ":email": email,
                ":phone": phone,
                ":now":   datetime.now(timezone.utc).isoformat(),
                ":one":   1,
            },
        )

    return {**event, "reservation_id": reservation_id}


# ── Paso 3 ────────────────────────────────────────────────────────────────────
# Procesa el cobro (mock). En producción: llamar al gateway de pagos.
# Output: agrega total_pagado y transaction_id

def collect_payment_handler(event, context):
    log.info("CollectPayment — payment_id: %s, reserva: %s",
             event.get("payment_id"), event.get("reservation_id"))

    flight_info = event["flight_info"]
    reservation = event["reservation"]
    pasajeros   = int(reservation.get("pasajeros", 1))
    total = float(flight_info["precio_por_pasajero"]) * pasajeros
    tx_id = f"TX-{event['payment_id'].replace('-', '')[:12].upper()}"

    log.info("Pago mock aprobado — total: $%.2f — tx: %s", total, tx_id)

    return {**event, "total_pagado": total, "transaction_id": tx_id}


# ── Paso 4 ────────────────────────────────────────────────────────────────────
# Actualiza la reserva a CONFIRMADA y publica evento para analytics.
# Output: agrega confirmed=True

def confirm_booking_handler(event, context):
    log.info("ConfirmBooking — payment_id: %s, reserva: %s",
             event.get("payment_id"), event.get("reservation_id"))

    table.update_item(
        Key={
            "PK": f"USER#{event['user_id']}",
            "SK": f"RESERVATION#{event['reservation_id']}",
        },
        UpdateExpression="SET #s = :status, #t = :total, transaction_id = :tx",
        ExpressionAttributeNames={"#s": "status", "#t": "total"},
        ExpressionAttributeValues={
            ":status": "CONFIRMADA",
            ":total":  Decimal(str(event.get("total_pagado", 0))),
            ":tx":     event.get("transaction_id", ""),
        },
    )

    if SNS_EVENTS:
        try:
            sns.publish(
                TopicArn=SNS_EVENTS,
                Message=json.dumps({
                    "event_type":     "purchase_complete",
                    "user_id":        event["user_id"],
                    "reservation_id": event["reservation_id"],
                    "ruta":           event["flight_info"]["ruta"],
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                    "payload":        {"amount": event.get("total_pagado", 0)},
                }),
                Subject="purchase_complete",
            )
        except Exception as e:
            log.warning("Error publicando evento analytics: %s", e)

    return {**event, "confirmed": True}


# ── Compensación: refund ──────────────────────────────────────────────────────
# Revierte el cobro cuando ConfirmBooking falla.

def refund_payment_handler(event, context):
    tx_id = event.get("transaction_id")
    log.info("RefundPayment — tx: %s — payment_id: %s", tx_id, event.get("payment_id"))
    # En producción: llamar al gateway de pagos para revertir la transacción
    return {"refunded": True, "transaction_id": tx_id}


# ── Compensación: cancel booking ─────────────────────────────────────────────
# Cancela la reserva si fue creada (puede no existir si falló en ReserveFlight).

def cancel_booking_handler(event, context):
    reservation_id = event.get("reservation_id")
    if not reservation_id:
        log.info("CancelBooking — sin reserva que cancelar")
        return {"cancelled": False, "reason": "no_reservation"}

    log.info("CancelBooking — reserva: %s", reservation_id)

    table.update_item(
        Key={
            "PK": f"USER#{event['user_id']}",
            "SK": f"RESERVATION#{reservation_id}",
        },
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "CANCELADA"},
    )

    return {"cancelled": True, "reservation_id": reservation_id}


# ── Compensación: release flight ─────────────────────────────────────────────
# Libera el asiento bloqueado si ReserveFlight llegó a ejecutarse.

def release_flight_handler(event, context):
    flight_info = event.get("flight_info")
    if not flight_info:
        log.info("ReleaseFlight — sin asiento bloqueado que liberar")
        return {"released": False, "reason": "no_flight_reserved"}

    pasajeros = flight_info.get("pasajeros_bloqueados", 1)
    log.info("ReleaseFlight — ruta: %s — pasajeros: %d",
             flight_info.get("ruta"), pasajeros)

    try:
        table.update_item(
            Key={
                "PK": f"FLIGHT#{flight_info['origen']}#{flight_info['destino']}",
                "SK": f"DATE#{flight_info['fecha']}",
            },
            UpdateExpression="ADD asientos_disponibles :inc",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":inc": pasajeros},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning("ReleaseFlight — vuelo no encontrado en DynamoDB: %s", flight_info.get("ruta"))
            return {"released": False, "reason": "flight_not_found"}
        raise

    return {"released": True, "ruta": flight_info.get("ruta")}
