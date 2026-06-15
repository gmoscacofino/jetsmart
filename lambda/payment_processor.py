"""
Handlers de pago para Step Functions (patrón Saga).

Cada handler es invocado directamente por el state machine.
Recibe el estado actual como input y retorna el estado actualizado.
No hay SQS ni SNS entre pasos — Step Functions maneja la orquestación.

Flujo exitoso:
  reserve_flight → reserve_booking → collect_payment → confirm_booking

Compensaciones (rollback):
  refund_payment → cancel_booking → release_flight

TP4: las reservas son PNR-céntricas (PSS-like). Cada reserva crea varios items
en business table:
  PNR#{pnr}/#METADATA       — record locator canónico
  PNR#{pnr}/SEGMENT#{seq}   — tramo del PNR (con gsi2pk para "quién está en X")
  PNR#{pnr}/PAX#{seq}       — pasajero del PNR (con gsi3pk para buscar por DNI)
  USER#{uid}/RESERVATION#{pnr}  — thin pointer denormalizado
  PASSENGER#{dni}/#PROFILE  — CRM canónico (upsert)
  PASSENGER#{dni}/PNR#{pnr} — back-ref histórico
"""
import os, json, hashlib, secrets, logging
from decimal import Decimal
from datetime import datetime, timezone

from botocore.exceptions import ClientError

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION        = os.environ["AWS_REGION_VAR"]
TABLE_NAME    = os.environ["BUSINESS_TABLE_NAME"]
SNS_EVENTS    = os.environ.get("SNS_EVENTS_ARN", "")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table    = dynamodb.Table(TABLE_NAME)
sns      = boto3.client("sns", region_name=REGION)


# ── Helpers ───────────────────────────────────────────────────────────────────

# Charset PNR estándar de la industria: alfanumérico uppercase, sin caracteres
# ambiguos (0/O, 1/I) para evitar errores de transcripción humana.
_PNR_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate_pnr() -> str:
    """6-char alphanumeric uppercase PNR, formato Navitaire/Amadeus."""
    return "".join(secrets.choice(_PNR_CHARSET) for _ in range(6))


def _pnr_from_payment_id(payment_id: str) -> str:
    """
    Deriva un PNR estable y resistente a colisiones a partir del payment_id.

    Si el Saga se reintenta con mismo payment_id, genera el mismo PNR
    (idempotencia). Usamos SHA-256 en lugar de hash() de Python porque:
      - hash() es un hashtable hash, no criptográfico — colisiones tempranas
        (birthday paradox cerca de los 32K bookings).
      - hash() está seedeado por proceso → no es estable entre cold starts.
      - SHA-256 es colision-resistant: probabilidad de colisión sobre 32^6 PNRs
        sigue gobernada por el truncamiento (~1B posibles) pero la distribución
        es uniforme — el birthday paradox no se acelera por sesgos del hash.

    La idempotencia frente a posibles colisiones se cubre además en
    reserve_booking_handler verificando user_id del PNR existente antes de
    aceptarlo como "propio".
    """
    digest = hashlib.sha256(payment_id.encode("utf-8")).digest()
    # 8 bytes = 64 bits → suficiente para 6 chars * log2(32) = 30 bits
    n = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(6):
        chars.append(_PNR_CHARSET[n % len(_PNR_CHARSET)])
        n //= len(_PNR_CHARSET)
    return "".join(chars)


def _passenger_key(name: str) -> str:
    """Clave del pasajero CRM. Si no hay DNI, derivamos del nombre (best-effort)."""
    return name.lower().replace(" ", "_")[:40] if name else "anon"


# ── Paso 1 ────────────────────────────────────────────────────────────────────
# Verifica disponibilidad y bloquea el asiento (decremento atómico).
# Input:  {payment_id, user_id, reservation: {origen, destino, fecha, pasajeros, vuelo_numero}}
# Output: agrega flight_info al estado

def reserve_flight_handler(event, context):
    log.info("ReserveFlight — payment_id: %s", event.get("payment_id"))

    reservation = event["reservation"]
    origen      = reservation["origen"]
    destino     = reservation["destino"]
    fecha       = reservation["fecha"]
    pasajeros   = int(reservation.get("pasajeros", 1))
    vuelo_pref  = reservation.get("vuelo_numero", "")

    # Query — puede haber múltiples frecuencias por ruta/fecha (mañana + tarde)
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk":     f"FLIGHT#{origen}#{destino}",
            ":prefix": f"DATE#{fecha}#",
        },
    )
    items = resp.get("Items", [])
    if not items:
        raise ValueError(f"Sin vuelos disponibles para {origen}-{destino} el {fecha}")

    # Si el usuario eligió un vuelo específico, filtramos por número
    if vuelo_pref:
        items = [i for i in items if i.get("vuelo_numero") == vuelo_pref]
        if not items:
            raise ValueError(f"Vuelo {vuelo_pref} no encontrado para {origen}-{destino} {fecha}")

    vuelo = items[0]
    disponibles = int(vuelo.get("asientos_disponibles", 0))

    if disponibles < pasajeros:
        raise ValueError(f"Asientos insuficientes: disponibles={disponibles}, solicitados={pasajeros}")

    # Decremento atómico con condición — protege contra oversell concurrente
    table.update_item(
        Key={"PK": vuelo["PK"], "SK": vuelo["SK"]},
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
            # Guardamos las claves del item para release_flight (idempotente sin recalcular)
            "_flight_pk":           vuelo["PK"],
            "_flight_sk":           vuelo["SK"],
        },
    }


# ── Paso 2 ────────────────────────────────────────────────────────────────────
# Crea la reserva en DynamoDB con estado PENDIENTE — patrón PSS:
#   PNR#{pnr}/#METADATA, /SEGMENT#01, /PAX#01
#   USER#{uid}/RESERVATION#{pnr}  thin pointer
#   PASSENGER#{dni}/#PROFILE + /PNR#{pnr}  back-ref
# Output: agrega reservation_id (= pnr) al estado

def reserve_booking_handler(event, context):
    log.info("ReserveBooking — payment_id: %s", event.get("payment_id"))

    flight_info    = event["flight_info"]
    reservation_r  = event["reservation"]
    pasajeros      = int(reservation_r.get("pasajeros", 1))

    pnr = _pnr_from_payment_id(event["payment_id"])
    total = Decimal(str(flight_info["precio_por_pasajero"])) * pasajeros

    passenger_name = reservation_r.get("nombre_pasajero", "")
    email          = reservation_r.get("email_contacto", "")
    phone          = reservation_r.get("telefono", "")
    dni            = reservation_r.get("dni", "") or _passenger_key(passenger_name)

    now = datetime.now(timezone.utc).isoformat()
    user_id = event["user_id"]
    vuelo_n = flight_info.get("vuelo_numero", "")
    fecha   = flight_info["fecha"]

    # PNR canónico — usamos PutItem con condición para idempotencia (Saga retry-safe)
    try:
        table.put_item(
            Item={
                "PK":              f"PNR#{pnr}",
                "SK":              "#METADATA",
                "pnr":             pnr,
                "user_id":         user_id,
                "status":          "PENDIENTE",
                "total":           total,
                "passenger_count": pasajeros,
                "tarifa":          reservation_r.get("tarifa", ""),
                "email_contacto":  email,
                "telefono":        phone,
                "created_at":      now,
                "payment_id":      event["payment_id"],
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Verificar ownership antes de tratar como idempotente:
            # SHA-256 hace colisiones astronómicamente improbables, pero si el
            # PNR existente pertenece a otro user o a otro payment_id, NO podemos
            # devolverlo como propio (sería data leak / IDOR).
            existing = table.get_item(
                Key={"PK": f"PNR#{pnr}", "SK": "#METADATA"}
            ).get("Item", {})
            same_user = existing.get("user_id") == user_id
            same_pay  = existing.get("payment_id") == event["payment_id"]
            if same_user and same_pay:
                log.info("ReserveBooking — PNR ya existe, idempotente: %s", pnr)
                return {**event, "reservation_id": pnr, "pnr": pnr}
            # Colisión genuina (improbable con SHA-256, pero defensivo).
            log.error(
                "ReserveBooking — PNR collision detectada: %s ya pertenece a otro "
                "user/payment. existente_user=%s nuevo_user=%s",
                pnr, existing.get("user_id"), user_id,
            )
            raise ValueError(f"PNR collision para {pnr} — reintentar con otro payment_id")
        raise

    # SEGMENT — stampa gsi2pk para "quién está en este vuelo/fecha"
    table.put_item(Item={
        "PK":           f"PNR#{pnr}",
        "SK":           f"SEGMENT#01#{vuelo_n}#{fecha}",
        "pnr":          pnr,
        "origen":       flight_info["origen"],
        "destino":      flight_info["destino"],
        "fecha":        fecha,
        "vuelo_numero": vuelo_n,
        "cabin":        "ECONOMY",
        "fare_class":   reservation_r.get("tarifa", "BASIC"),
        # GSI2: ReservationsByFlight — proactive_notifications hace Query acá
        "gsi2pk":         f"FLIGHT#{vuelo_n}#{fecha}",
        "gsi2sk":         f"PNR#{pnr}",
        "user_id":        user_id,
        "email":          email,
        "passenger_name": passenger_name,
        "status":         "PENDIENTE",
    })

    # PAX — stampa gsi3pk para "buscar PNR por DNI/email"
    pax_item = {
        "PK":             f"PNR#{pnr}",
        "SK":             "PAX#01",
        "pnr":            pnr,
        "seq":            1,
        "full_name":      passenger_name,
        "dni":            dni,
        "email":          email,
        "phone":          phone,
        "seat":           "ALEATORIO",
        # GSI3: ReservationsByPassenger — KEYS_ONLY, suficiente para resolver PNR
        "gsi3pk":         f"DNI#{dni}",
        "gsi3sk":         f"PNR#{pnr}",
    }
    table.put_item(Item=pax_item)

    # Si tenemos email distinto a DNI, stampar también un alias por email
    if email:
        table.put_item(Item={
            "PK":     f"PNR#{pnr}",
            "SK":     "PAX#01#EMAILALIAS",
            "pnr":    pnr,
            "gsi3pk": f"EMAIL#{email.lower()}",
            "gsi3sk": f"PNR#{pnr}",
        })

    # User thin pointer — denormalizado para "mis reservas" en O(1)
    table.put_item(Item={
        "PK":              f"USER#{user_id}",
        "SK":              f"RESERVATION#{pnr}",
        "pnr":             pnr,
        "status":          "PENDIENTE",
        "origin":          flight_info["origen"],
        "destination":     flight_info["destino"],
        "flight_number":   vuelo_n,
        "flight_date":     fecha,
        "passenger_count": pasajeros,
        "tarifa":          reservation_r.get("tarifa", ""),
        "total":           total,
        "email":           email,
        "phone":           phone,
        "passenger_name":  passenger_name,
        "created_at":      now,
    })

    # Passenger CRM — upsert con back-ref
    if passenger_name:
        pkey = _passenger_key(passenger_name)
        table.update_item(
            Key={"PK": f"PASSENGER#{pkey}", "SK": "#PROFILE"},
            UpdateExpression=(
                "SET passenger_name = :name, email = :email, phone = :phone, "
                "last_booking = :now ADD reservation_count :one"
            ),
            ExpressionAttributeValues={
                ":name":  passenger_name,
                ":email": email,
                ":phone": phone,
                ":now":   now,
                ":one":   1,
            },
        )
        table.put_item(Item={
            "PK":  f"PASSENGER#{pkey}",
            "SK":  f"PNR#{pnr}",
            "pnr": pnr,
        })

    return {**event, "reservation_id": pnr, "pnr": pnr}


# ── Paso 3 ────────────────────────────────────────────────────────────────────
# Procesa el cobro (mock). En producción: llamar al gateway de pagos.
# Output: agrega total_pagado y transaction_id

def collect_payment_handler(event, context):
    log.info("CollectPayment — payment_id: %s, PNR: %s",
             event.get("payment_id"), event.get("pnr"))

    flight_info = event["flight_info"]
    reservation = event["reservation"]
    pasajeros   = int(reservation.get("pasajeros", 1))
    total = float(flight_info["precio_por_pasajero"]) * pasajeros
    tx_id = f"TX-{event['payment_id'].replace('-', '')[:12].upper()}"

    log.info("Pago mock aprobado — total: $%.2f — tx: %s", total, tx_id)

    return {**event, "total_pagado": total, "transaction_id": tx_id}


# ── Paso 4 ────────────────────────────────────────────────────────────────────
# Actualiza el PNR a CONFIRMADA + thin pointer + publica evento analytics.

def confirm_booking_handler(event, context):
    pnr = event["pnr"]
    log.info("ConfirmBooking — payment_id: %s, PNR: %s",
             event.get("payment_id"), pnr)

    user_id = event["user_id"]
    total = Decimal(str(event.get("total_pagado", 0)))
    tx = event.get("transaction_id", "")

    # PNR canónico — sólo permitimos transición PENDIENTE → CONFIRMADA.
    # Defensivo contra: doble invocación del Saga, race condition por colisión
    # de PNR (improbable post-SHA256 pero el guard cierra el riesgo).
    try:
        table.update_item(
            Key={"PK": f"PNR#{pnr}", "SK": "#METADATA"},
            UpdateExpression="SET #s = :status, #t = :total, transaction_id = :tx",
            ConditionExpression="#s = :pending AND user_id = :uid",
            ExpressionAttributeNames={"#s": "status", "#t": "total"},
            ExpressionAttributeValues={
                ":status":  "CONFIRMADA",
                ":total":   total,
                ":tx":      tx,
                ":pending": "PENDIENTE",
                ":uid":     user_id,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning(
                "ConfirmBooking — PNR %s no está en PENDIENTE o user_id no coincide. "
                "Posible doble confirmación o colisión. Skipping update.", pnr,
            )
            return {**event, "confirmed": False, "reason": "not_pending_or_owner_mismatch"}
        raise

    # Thin pointer — mismo guard por consistencia con el canonical.
    try:
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{pnr}"},
            UpdateExpression="SET #s = :status, #t = :total, transaction_id = :tx",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status", "#t": "total"},
            ExpressionAttributeValues={
                ":status":  "CONFIRMADA",
                ":total":   total,
                ":tx":      tx,
                ":pending": "PENDIENTE",
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        log.warning("ConfirmBooking — thin pointer no estaba en PENDIENTE, skipping")

    # SEGMENT status (consumido por GSI2 — el filtro de proactive_notifications)
    vuelo_n = event["flight_info"].get("vuelo_numero", "")
    fecha   = event["flight_info"]["fecha"]
    try:
        table.update_item(
            Key={"PK": f"PNR#{pnr}", "SK": f"SEGMENT#01#{vuelo_n}#{fecha}"},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "CONFIRMADA"},
        )
    except Exception as e:
        log.warning("No pudo actualizar SEGMENT status: %s", e)

    if SNS_EVENTS:
        try:
            sns.publish(
                TopicArn=SNS_EVENTS,
                Message=json.dumps({
                    "event_type":     "purchase_complete",
                    "user_id":        user_id,
                    "reservation_id": pnr,
                    "pnr":            pnr,
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

def refund_payment_handler(event, context):
    tx_id = event.get("transaction_id")
    log.info("RefundPayment — tx: %s — payment_id: %s", tx_id, event.get("payment_id"))
    # En producción: llamar al gateway de pagos para revertir la transacción
    return {"refunded": True, "transaction_id": tx_id}


# ── Compensación: cancel booking ─────────────────────────────────────────────

def cancel_booking_handler(event, context):
    pnr = event.get("pnr") or event.get("reservation_id")
    if not pnr:
        log.info("CancelBooking — sin PNR que cancelar")
        return {"cancelled": False, "reason": "no_reservation"}

    log.info("CancelBooking — PNR: %s", pnr)
    user_id = event.get("user_id", "")

    # PNR canónico — sólo cancelamos si el PNR pertenece al user del Saga
    # (defensivo contra colisión de PNR + idempotente: si ya está CANCELADA no falla).
    try:
        table.update_item(
            Key={"PK": f"PNR#{pnr}", "SK": "#METADATA"},
            UpdateExpression="SET #s = :status",
            ConditionExpression="attribute_exists(PK) AND user_id = :uid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "CANCELADA",
                ":uid":    user_id,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning(
                "CancelBooking — PNR %s no existe o user_id no coincide. Skipping.", pnr,
            )
        else:
            log.warning("Cancel PNR %s falló: %s", pnr, e)
    except Exception as e:
        log.warning("Cancel PNR %s falló: %s", pnr, e)

    # Thin pointer
    if user_id:
        try:
            table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{pnr}"},
                UpdateExpression="SET #s = :status",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":status": "CANCELADA"},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                log.warning("Cancel pointer USER#%s/RESERVATION#%s falló: %s", user_id, pnr, e)
        except Exception as e:
            log.warning("Cancel pointer USER#%s/RESERVATION#%s falló: %s", user_id, pnr, e)

    return {"cancelled": True, "pnr": pnr}


# ── Compensación: release flight ─────────────────────────────────────────────

def release_flight_handler(event, context):
    flight_info = event.get("flight_info")
    if not flight_info:
        log.info("ReleaseFlight — sin asiento bloqueado que liberar")
        return {"released": False, "reason": "no_flight_reserved"}

    pasajeros = flight_info.get("pasajeros_bloqueados", 1)
    log.info("ReleaseFlight — ruta: %s — pasajeros: %d",
             flight_info.get("ruta"), pasajeros)

    # Usamos las claves guardadas en reserve_flight si están disponibles —
    # si no, reconstruimos del flight_info (fallback compat)
    pk = flight_info.get("_flight_pk") or f"FLIGHT#{flight_info['origen']}#{flight_info['destino']}"
    sk = flight_info.get("_flight_sk") or f"DATE#{flight_info['fecha']}#FLIGHT#{flight_info.get('vuelo_numero','')}"

    try:
        table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression="ADD asientos_disponibles :inc",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":inc": pasajeros},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning("ReleaseFlight — vuelo no encontrado: %s/%s", pk, sk)
            return {"released": False, "reason": "flight_not_found"}
        raise

    return {"released": True, "ruta": flight_info.get("ruta")}
