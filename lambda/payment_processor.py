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

from pricing import compute_total, PricingError

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claim_random_seat(origen: str, destino: str, master_sk: str, pnr: str, max_attempts: int = 3) -> tuple[str, str]:
    """
    Reserva un asiento estándar aleatorio del vuelo. Devuelve (seat_id, full_sk).
    Atomic via ConditionExpression — si hay race, reintenta hasta max_attempts.
    """
    import random as _r
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        FilterExpression="attribute_not_exists(reserved_by) AND seat_type = :t",
        ExpressionAttributeValues={
            ":pk":     f"FLIGHT#{origen}#{destino}",
            ":prefix": f"{master_sk}#SEAT#",
            ":t":      "estandar",
        },
    )
    candidates = resp.get("Items", [])
    if not candidates:
        raise ValueError("Sin asientos disponibles en el vuelo")

    for _ in range(max_attempts):
        choice = _r.choice(candidates)
        try:
            table.update_item(
                Key={"PK": choice["PK"], "SK": choice["SK"]},
                UpdateExpression="SET reserved_by = :pnr, reserved_at = :now",
                ConditionExpression="attribute_not_exists(reserved_by)",
                ExpressionAttributeValues={
                    ":pnr": f"PNR#{pnr}",
                    ":now": _now_iso(),
                },
            )
            return choice["seat_id"], choice["SK"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                candidates.remove(choice)
                if not candidates:
                    break
                continue
            raise
    raise ValueError("No se pudo bloquear un asiento tras múltiples intentos (race condition)")


# ── Paso 1 ────────────────────────────────────────────────────────────────────
# Verifica disponibilidad y bloquea UN asiento específico (atomic via
# ConditionExpression sobre el ítem SEAT#<row><letter>). El PNR ya viene
# generado upstream (chat_handler) para que el reserved_by sea estable y la
# compensación pueda liberar exactamente este asiento.
#
# Input: {payment_id, pnr, user_id, reservation: {origen, destino, fecha,
#         pasajeros, vuelo_numero, seat_id?}}
# Output: agrega flight_info con seat_id, _flight_pk, _seat_sk

def reserve_flight_handler(event, context):
    log.info("ReserveFlight — payment_id: %s pnr: %s",
             event.get("payment_id"), event.get("pnr"))

    pnr = event["pnr"]
    reservation = event["reservation"]
    origen      = reservation["origen"]
    destino     = reservation["destino"]
    fecha       = reservation["fecha"]
    vuelo_pref  = reservation.get("vuelo_numero", "")
    seat_pref   = (reservation.get("seat_id") or "").upper()

    # 1. Master row del vuelo — precio y disponibilidad general
    flight_pk = f"FLIGHT#{origen}#{destino}"
    master_sk = f"DATE#{fecha}#FLIGHT#{vuelo_pref}"

    master = table.get_item(Key={"PK": flight_pk, "SK": master_sk}).get("Item")
    if not master:
        raise ValueError(f"Vuelo {vuelo_pref} {origen}-{destino} {fecha} no existe")

    # 2. Reservar asiento — específico o aleatorio
    if seat_pref:
        seat_sk = f"{master_sk}#SEAT#{seat_pref}"
        try:
            table.update_item(
                Key={"PK": flight_pk, "SK": seat_sk},
                UpdateExpression="SET reserved_by = :pnr, reserved_at = :now",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(reserved_by)",
                ExpressionAttributeValues={
                    ":pnr": f"PNR#{pnr}",
                    ":now": _now_iso(),
                },
            )
            seat_id = seat_pref
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Doble caso: no existe el seat, o ya está tomado.
                check = table.get_item(Key={"PK": flight_pk, "SK": seat_sk}).get("Item")
                if not check:
                    raise ValueError(f"Asiento {seat_pref} no existe en el vuelo")
                raise ValueError(f"Asiento {seat_pref} ya está reservado — elegí otro")
            raise
    else:
        seat_id, seat_sk = _claim_random_seat(origen, destino, master_sk, pnr)

    return {
        **event,
        "flight_info": {
            "origen":               origen,
            "destino":              destino,
            "ruta":                 f"{origen}-{destino}",
            "vuelo_numero":         master.get("vuelo_numero", vuelo_pref),
            "fecha":                fecha,
            "precio_por_pasajero":  float(master.get("precio", 0)),
            "seat_id":              seat_id,
            # Guardamos las claves del item para release_flight (idempotente sin recalcular)
            "_flight_pk":           flight_pk,
            "_seat_sk":             seat_sk,
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

    pnr    = event["pnr"]
    tarifa = reservation_r.get("tarifa", "BASIC")
    extras = reservation_r.get("extras", []) or []

    # Server-side pricing — fuente única de verdad. El total que pasó el chat
    # se ignora; recalculamos con tarifa, extras y precio base del inventory.
    try:
        pricing = compute_total(
            Decimal(str(flight_info["precio_por_pasajero"])),
            tarifa, extras, pasajeros,
        )
    except PricingError as e:
        raise ValueError(f"Error de pricing server-side: {e}")
    total = pricing["total"]

    passenger_name = reservation_r.get("nombre_pasajero", "")
    email          = reservation_r.get("email_contacto", "")
    phone          = reservation_r.get("telefono", "")
    dni            = reservation_r.get("dni", "") or _passenger_key(passenger_name)

    now = _now_iso()
    user_id = event["user_id"]
    vuelo_n = flight_info.get("vuelo_numero", "")
    fecha   = flight_info["fecha"]
    seat_id = flight_info.get("seat_id", "")

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
                "pasajeros":       pasajeros,
                "tarifa":          tarifa,
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
        "seat":           seat_id,
        # GSI3: ReservationsByPassenger — KEYS_ONLY, suficiente para resolver PNR
        "gsi3pk":         f"DNI#{dni}",
        "gsi3sk":         f"PNR#{pnr}",
    }
    table.put_item(Item=pax_item)

    # EXTRA# — persistir cada extra contratado como ítem aparte para auditoría
    # y queries del tipo "qué llevó cada pasajero".
    for idx, (extra_type, amount) in enumerate(pricing["desglose"]["extras"].items(), start=1):
        table.put_item(Item={
            "PK":         f"PNR#{pnr}",
            "SK":         f"EXTRA#{idx:02d}",
            "pnr":        pnr,
            "extra_type": extra_type,
            "amount":     amount,
            "created_at": now,
        })

    # Si tenemos email distinto a DNI, stampar también un alias por email
    if email:
        table.put_item(Item={
            "PK":     f"PNR#{pnr}",
            "SK":     "PAX#01#EMAILALIAS",
            "pnr":    pnr,
            "gsi3pk": f"EMAIL#{email.lower()}",
            "gsi3sk": f"PNR#{pnr}",
        })

    # User thin pointer — denormalizado para "mis reservas" en O(1).
    # Vocabulario en español para consistencia con el resto del sistema.
    table.put_item(Item={
        "PK":              f"USER#{user_id}",
        "SK":              f"RESERVATION#{pnr}",
        "pnr":             pnr,
        "status":          "PENDIENTE",
        "origen":          flight_info["origen"],
        "destino":         flight_info["destino"],
        "vuelo_numero":    vuelo_n,
        "fecha":           fecha,
        "pasajeros":       pasajeros,
        "tarifa":          tarifa,
        "total":           total,
        "email":           email,
        "telefono":        phone,
        "nombre_pasajero": passenger_name,
        "seat":            seat_id,
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
    tarifa      = reservation.get("tarifa", "BASIC")
    extras      = reservation.get("extras", []) or []

    # Mismo cálculo que reserve_booking — double-check del total contra pricing.
    # El input "total_pagado" del paso siguiente debe coincidir con esto.
    try:
        pricing = compute_total(
            Decimal(str(flight_info["precio_por_pasajero"])),
            tarifa, extras, pasajeros,
        )
    except PricingError as e:
        raise ValueError(f"Error de pricing en collect_payment: {e}")

    total = float(pricing["total"])
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
    """
    Libera el asiento reservado por reserve_flight_handler. Idempotente:
    si el seat ya fue liberado o pertenece a otro PNR, no falla.
    """
    flight_info = event.get("flight_info") or {}
    pnr = event.get("pnr")

    if not flight_info or not pnr:
        log.info("ReleaseFlight — sin asiento bloqueado que liberar")
        return {"released": False, "reason": "no_flight_reserved"}

    pk = flight_info.get("_flight_pk")
    sk = flight_info.get("_seat_sk")
    if not (pk and sk):
        log.warning("ReleaseFlight — faltan keys del seat en flight_info")
        return {"released": False, "reason": "missing_seat_keys"}

    log.info("ReleaseFlight — pnr: %s seat: %s sk: %s",
             pnr, flight_info.get("seat_id"), sk)

    try:
        table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression="REMOVE reserved_by, reserved_at",
            ConditionExpression="reserved_by = :owned_pnr",
            ExpressionAttributeValues={":owned_pnr": f"PNR#{pnr}"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning(
                "ReleaseFlight — seat %s no estaba reservado por PNR %s "
                "(ya liberado o de otro PNR)", sk, pnr,
            )
            return {"released": False, "reason": "not_owned_or_already_released"}
        raise

    return {"released": True, "seat_id": flight_info.get("seat_id")}
