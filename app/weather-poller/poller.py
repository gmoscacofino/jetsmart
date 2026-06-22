"""
weather-poller — proceso continuo en ECS Fargate.

Poll-ea una API de clima externa (climAPI) para los aeropuertos de origen de los
vuelos activos en las próximas ~48h. Si las condiciones superan los umbrales
(viento / visibilidad) o si FORCE_CANCEL está activo, escribe la transición
estado_vuelo -> "CANCELADO" en el master row del vuelo en la business table.

Ese write es justamente lo que dispara el DynamoDB Stream consumido por
lambda/flight_cancellation_detector.py (filtra master rows FLIGHT# cuyo
estado_vuelo pasa a CANCELADO). El downstream (proactive notifications) NO es
responsabilidad de este proceso.

Logging estructurado a stdout para que el log driver awslogs de ECS lo capture.
"""
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import boto3
import requests
from botocore.exceptions import ClientError

# ── Logging estructurado a stdout (awslogs) ───────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("weather-poller")


# ── Configuración (env vars leídas al startup) ────────────────────────────────
def _get_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


REGION              = os.environ["AWS_REGION_VAR"]
BUSINESS_TABLE_NAME = os.environ["BUSINESS_TABLE_NAME"]
WEATHER_SECRET_ARN  = os.environ["WEATHER_SECRET_ARN"]

# Base URL de climAPI. Configurable por env var. El placeholder es solo eso:
# ajustar CLIMA_API_BASE (y _fetch_weather más abajo) al endpoint real.
CLIMA_API_BASE      = os.environ.get("CLIMA_API_BASE", "https://api.climapi.example")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "1800"))
FORCE_CANCEL          = _get_bool("FORCE_CANCEL", "false")

WIND_THRESHOLD_KMH = float(os.environ.get("WEATHER_WIND_THRESHOLD_KMH", "90"))
VISIBILITY_MIN_M   = float(os.environ.get("WEATHER_VISIBILITY_MIN_M", "550"))

# El seed escribe estado_vuelo="EN_HORARIO" (valores válidos del doc:
# EN_HORARIO | DEMORADO | CANCELADO — NO existe "PROGRAMADO"). Consideramos
# "activos / cancelables" ambos estados no terminales. Configurable por env var.
ACTIVE_STATES = tuple(
    s.strip()
    for s in os.environ.get("ACTIVE_FLIGHT_STATES", "EN_HORARIO,DEMORADO").split(",")
    if s.strip()
)

LOOKAHEAD_HOURS = int(os.environ.get("LOOKAHEAD_HOURS", "48"))

# ── Clientes AWS ──────────────────────────────────────────────────────────────
_session   = boto3.session.Session(region_name=REGION)
_dynamodb  = _session.resource("dynamodb")
_table     = _dynamodb.Table(BUSINESS_TABLE_NAME)
_secrets   = _session.client("secretsmanager")


def _load_api_key() -> str:
    """Lee la API key de climAPI desde Secrets Manager una sola vez al startup.

    El secreto guarda JSON, ej {"api_key": "..."}. Si no es JSON, se usa el
    string crudo como key.
    """
    resp = _secrets.get_secret_value(SecretId=WEATHER_SECRET_ARN)
    raw = resp.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("api_key") or data.get("apiKey") or raw
    except (json.JSONDecodeError, TypeError):
        return raw


# Cache de la key al import/startup.
WEATHER_API_KEY = _load_api_key()


# ── climAPI ───────────────────────────────────────────────────────────────────
# AJUSTAR ESTA FUNCIÓN al contrato real de climAPI. El schema exacto de
# request/response es desconocido: asumimos GET {CLIMA_API_BASE}/current con la
# key por header y query param ?airport=IATA, y una respuesta JSON con campos de
# viento (km/h) y visibilidad (m) en alguno de los nombres comunes probados.
# Cualquier error de HTTP/parseo se trata como "sin cancelación" (devuelve None).
def _fetch_weather(airport: str) -> dict | None:
    """Devuelve {'wind_kmh': float|None, 'visibility_m': float|None} o None si falla."""
    try:
        resp = requests.get(
            f"{CLIMA_API_BASE}/current",
            params={"airport": airport},
            headers={
                "Authorization": f"Bearer {WEATHER_API_KEY}",
                "x-api-key": WEATHER_API_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("climAPI fetch failed for airport=%s: %s", airport, exc)
        return None

    wind = _first_number(
        body, ["wind_kmh", "wind_speed_kmh", "wind", "windSpeed", "viento_kmh"]
    )
    visibility = _first_number(
        body, ["visibility_m", "visibility", "visibilidad_m", "visibilidad"]
    )
    return {"wind_kmh": wind, "visibility_m": visibility}


def _first_number(body, keys):
    """Devuelve el primer campo numérico presente (top-level o bajo 'current'/'data')."""
    candidates = [body]
    if isinstance(body, dict):
        for nest in ("current", "data", "observation", "weather"):
            if isinstance(body.get(nest), dict):
                candidates.append(body[nest])
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        for k in keys:
            if k in obj:
                try:
                    return float(obj[k])
                except (TypeError, ValueError):
                    continue
    return None


# ── DynamoDB: encontrar vuelos activos ────────────────────────────────────────
def _scan_active_flights() -> list[dict]:
    """Scan de la business table por master rows de vuelo activos en las próximas
    ~48h.

    PRODUCCIÓN: esto debería ser una Query sobre un GSI tipo `FlightsByDate`
    (gsipk = "FLIGHT", gsisk = fecha) para no escanear toda la tabla. El dataset
    del demo es chico (~660 vuelos), así que el Scan con FilterExpression es
    aceptable. La FilterExpression deja sólo master rows FLIGHT# (no SEAT#,
    no PNR#) en un estado activo.
    """
    today = date.today()
    horizon = today + timedelta(hours=LOOKAHEAD_HOURS)

    filt = (
        "begins_with(PK, :flight_prefix) AND begins_with(SK, :date_prefix) "
        "AND attribute_exists(estado_vuelo)"
    )
    values = {
        ":flight_prefix": "FLIGHT#",
        ":date_prefix": "DATE#",
    }

    flights: list[dict] = []
    kwargs = {"FilterExpression": filt, "ExpressionAttributeValues": values}
    while True:
        resp = _table.scan(**kwargs)
        for item in resp.get("Items", []):
            sk = item.get("SK", "")
            # Sólo master rows: SK = DATE#...#FLIGHT#<vuelo>, sin #SEAT#.
            if "#SEAT#" in sk:
                continue
            if item.get("estado_vuelo") not in ACTIVE_STATES:
                continue
            # Filtro de fecha within now..now+48h (fecha es "YYYY-MM-DD").
            fecha_str = item.get("fecha", "")
            try:
                fecha = date.fromisoformat(fecha_str)
            except ValueError:
                continue
            if not (today <= fecha <= horizon.date()):
                continue
            flights.append(item)

        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    return flights


def _origin_airport(item: dict) -> str:
    """Origen = primer token después de FLIGHT# en la PK: FLIGHT#{origen}#{destino}."""
    parts = item.get("PK", "").split("#")  # ["FLIGHT", origen, destino]
    return parts[1] if len(parts) >= 2 else ""


# ── DynamoDB: cancelación idempotente ─────────────────────────────────────────
def _cancel_flight(item: dict, reason: str) -> bool:
    """UpdateItem condicional e idempotente. Devuelve True si efectivamente
    canceló, False si ya estaba cancelado.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression=(
                "SET estado_vuelo = :new, "
                "cancellation_reason = :reason, "
                "cancelled_at = :now"
            ),
            ConditionExpression="estado_vuelo <> :cancelled",
            ExpressionAttributeValues={
                ":new": "CANCELADO",
                ":cancelled": "CANCELADO",
                ":reason": reason,
                ":now": now_iso,
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False  # ya estaba CANCELADO — idempotente, skip
        raise


def _cancel_reason(weather: dict | None) -> str:
    if FORCE_CANCEL:
        return "weather (FORCE_CANCEL demo flag)"
    if not weather:
        return "weather"
    bits = []
    if weather.get("wind_kmh") is not None:
        bits.append(f"wind={weather['wind_kmh']}km/h(>{WIND_THRESHOLD_KMH})")
    if weather.get("visibility_m") is not None:
        bits.append(f"visibility={weather['visibility_m']}m(<{VISIBILITY_MIN_M})")
    return "weather: " + ", ".join(bits) if bits else "weather"


def _should_cancel(weather: dict | None) -> bool:
    if weather is None:
        return False
    wind = weather.get("wind_kmh")
    vis = weather.get("visibility_m")
    if wind is not None and wind > WIND_THRESHOLD_KMH:
        return True
    if vis is not None and vis < VISIBILITY_MIN_M:
        return True
    return False


# ── Ciclo principal ───────────────────────────────────────────────────────────
def run_cycle() -> None:
    flights = _scan_active_flights()
    log.info(
        "cycle start — active_flights=%d force_cancel=%s",
        len(flights),
        FORCE_CANCEL,
    )

    if not flights:
        log.info("cycle end — no active flights in the next %dh", LOOKAHEAD_HOURS)
        return

    # FORCE_CANCEL: cancelar UN solo vuelo elegible por ciclo (el primero) para
    # un demo determinístico, sin tocar climAPI.
    if FORCE_CANCEL:
        target = flights[0]
        reason = _cancel_reason(None)
        did = _cancel_flight(target, reason)
        if did:
            log.info(
                "CANCELLED flight=%s airport=%s fecha=%s reason=%s",
                target.get("vuelo_numero"),
                _origin_airport(target),
                target.get("fecha"),
                reason,
            )
        else:
            log.info(
                "skip flight=%s — already CANCELADO", target.get("vuelo_numero")
            )
        return

    # Modo normal: 1 llamada a climAPI por aeropuerto de origen (cache por ciclo).
    origins = {_origin_airport(f) for f in flights if _origin_airport(f)}
    weather_by_airport = {ap: _fetch_weather(ap) for ap in origins}

    cancelled = 0
    for flight in flights:
        airport = _origin_airport(flight)
        weather = weather_by_airport.get(airport)
        if not _should_cancel(weather):
            continue
        reason = _cancel_reason(weather)
        if _cancel_flight(flight, reason):
            cancelled += 1
            log.info(
                "CANCELLED flight=%s airport=%s fecha=%s reason=%s",
                flight.get("vuelo_numero"),
                airport,
                flight.get("fecha"),
                reason,
            )
        else:
            log.info(
                "skip flight=%s — already CANCELADO", flight.get("vuelo_numero")
            )

    log.info("cycle end — cancelled=%d", cancelled)


def main() -> None:
    log.info(
        "weather-poller starting — table=%s region=%s interval=%ds "
        "clima_api_base=%s wind_threshold=%s visibility_min=%s force_cancel=%s",
        BUSINESS_TABLE_NAME,
        REGION,
        POLL_INTERVAL_SECONDS,
        CLIMA_API_BASE,
        WIND_THRESHOLD_KMH,
        VISIBILITY_MIN_M,
        FORCE_CANCEL,
    )
    while True:
        try:
            run_cycle()
        except Exception:  # noqa: BLE001 — un ciclo malo nunca mata el loop
            log.exception("run_cycle failed — continuing")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
