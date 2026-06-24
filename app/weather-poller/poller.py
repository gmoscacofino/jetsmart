"""
weather-poller — proceso continuo en ECS Fargate.

Para cada vuelo activo en las próximas ~48h consulta el PRONÓSTICO de WeatherAPI
en el aeropuerto de origen, a la hora de salida del vuelo. Si el viento o la
visibilidad pronosticados superan los umbrales, escribe la transición
estado_vuelo -> "CANCELADO" en el master row del vuelo en la business table.

Ese write dispara el DynamoDB Stream consumido por lambda/stream_emitter.py
(filtra master rows FLIGHT# cuyo estado_vuelo pasa a CANCELADO) → SNS →
proactive notifications. El downstream NO es responsabilidad de este proceso.

Contrato WeatherAPI.com (https://www.weatherapi.com/docs/):
    GET {base}/forecast.json?key=<KEY>&q=iata:<IATA>&days=<1-3>&dt=YYYY-MM-DD&hour=<0-23>
    → forecast.forecastday[0].hour[0].{wind_kph, vis_km}
    OJO: vis_km viene en KILÓMETROS; el umbral interno es en metros (vis_km*1000).

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
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

# ── Logging estructurado a stdout (awslogs) ───────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("weather-poller")


# ── Configuración (env vars leídas al startup) ────────────────────────────────
REGION              = os.environ["AWS_REGION_VAR"]
BUSINESS_TABLE_NAME = os.environ["BUSINESS_TABLE_NAME"]
WEATHER_SECRET_ARN  = os.environ["WEATHER_SECRET_ARN"]

# Base URL de WeatherAPI.com. Configurable por env var.
CLIMA_API_BASE      = os.environ.get("CLIMA_API_BASE", "https://api.weatherapi.com/v1")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "1800"))

WIND_THRESHOLD_KMH = float(os.environ.get("WEATHER_WIND_THRESHOLD_KMH", "90"))
VISIBILITY_MIN_M   = float(os.environ.get("WEATHER_VISIBILITY_MIN_M", "550"))

# El seed escribe estado_vuelo="EN_HORARIO" (valores válidos del doc:
# EN_HORARIO | DEMORADO | CANCELADO). Consideramos "activos / cancelables" los
# estados no terminales. Configurable por env var.
ACTIVE_STATES = tuple(
    s.strip()
    for s in os.environ.get("ACTIVE_FLIGHT_STATES", "EN_HORARIO,DEMORADO").split(",")
    if s.strip()
)

LOOKAHEAD_HOURS = int(os.environ.get("LOOKAHEAD_HOURS", "48"))

# WeatherAPI free tier: pronóstico hasta 3 días. Un vuelo cuya fecha cae fuera de
# ese horizonte no se puede evaluar (se deja sin cancelar). Con LOOKAHEAD=48h el
# horizonte alcanza de sobra.
FORECAST_MAX_DAYS = int(os.environ.get("FORECAST_MAX_DAYS", "3"))

# ── Clientes AWS ──────────────────────────────────────────────────────────────
_session   = boto3.session.Session(region_name=REGION)
_dynamodb  = _session.resource("dynamodb")
_table     = _dynamodb.Table(BUSINESS_TABLE_NAME)
_secrets   = _session.client("secretsmanager")


def _load_api_key() -> str:
    """Lee la API key de WeatherAPI desde Secrets Manager una sola vez al startup.

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


# ── WeatherAPI: pronóstico por aeropuerto + fecha + hora de salida ────────────
def _fetch_forecast(airport: str, fecha: str, hour: int) -> dict | None:
    """Pide el pronóstico de WeatherAPI para `airport` (IATA) en la fecha/hora de
    salida del vuelo. Devuelve {'wind_kmh': float|None, 'visibility_m': float|None}
    o None si falla o la fecha cae fuera del horizonte de forecast.

    `days` debe cubrir la fecha objetivo (free tier = 3). `dt`+`hour` la acotan al
    momento del vuelo: con &hour=, la API devuelve un solo elemento en hour[].
    """
    days = (date.fromisoformat(fecha) - date.today()).days + 1
    if days < 1 or days > FORECAST_MAX_DAYS:
        log.info(
            "forecast skip airport=%s fecha=%s — fuera del horizonte (%dd)",
            airport, fecha, days,
        )
        return None

    try:
        resp = requests.get(
            f"{CLIMA_API_BASE}/forecast.json",
            params={
                "key": WEATHER_API_KEY,
                "q": f"iata:{airport}",
                "days": days,
                "dt": fecha,
                "hour": hour,
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning(
            "WeatherAPI forecast failed airport=%s fecha=%s hour=%s: %s",
            airport, fecha, hour, exc,
        )
        return None

    hour_obj = _forecast_hour(body, fecha, hour)
    if hour_obj is None:
        log.warning("WeatherAPI sin hora airport=%s fecha=%s hour=%s", airport, fecha, hour)
        return None

    wind = _as_float(hour_obj.get("wind_kph"))
    vis_km = _as_float(hour_obj.get("vis_km"))
    return {
        "wind_kmh": wind,
        # vis_km viene en KILÓMETROS → convertir a metros para comparar con el umbral.
        "visibility_m": vis_km * 1000 if vis_km is not None else None,
    }


def _forecast_hour(body, fecha: str, hour: int):
    """Extrae el objeto hour del forecast: forecastday con date==fecha → la hora
    pedida. Con &hour= la API devuelve un solo elemento, pero matcheamos por el
    timestamp por robustez; fallback al primero disponible."""
    days = (body or {}).get("forecast", {}).get("forecastday", []) if isinstance(body, dict) else []
    target_day = next((d for d in days if d.get("date") == fecha), None)
    if target_day is None and days:
        target_day = days[0]
    if not target_day:
        return None
    hours = target_day.get("hour", [])
    if not hours:
        return None
    for h in hours:
        ts = h.get("time", "")  # "YYYY-MM-DD HH:MM"
        if len(ts) >= 13 and ts[11:13].isdigit() and int(ts[11:13]) == hour:
            return h
    return hours[0]


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── DynamoDB: encontrar vuelos activos (Query GSI FlightsByDate) ──────────────
def _active_flights() -> list[dict]:
    """Query del GSI `FlightsByDate`, una partición por fecha de la ventana
    [hoy, hoy+LOOKAHEAD_HOURS].

    El índice es sparse (solo los master rows FLIGHT# estampan gsi_flights_pk),
    así que NO devuelve asientos ni PNRs — sin necesidad de filtrar #SEAT#. La
    FilterExpression deja solo los estados activos (EN_HORARIO / DEMORADO); como la
    partición ya es "los vuelos de esa fecha", el filtro recorre pocos ítems: es un
    Query acotado, no un Scan de tabla. La projection INCLUDE trae estado_vuelo,
    vuelo_numero, fecha y hora_salida (esta última la usa el forecast).
    """
    today = date.today()
    horizon = today + timedelta(hours=LOOKAHEAD_HOURS)
    fechas = [
        (today + timedelta(days=i)).isoformat()
        for i in range((horizon - today).days + 1)
    ]

    flights: list[dict] = []
    for fecha in fechas:
        kwargs = {
            "IndexName": "FlightsByDate",
            "KeyConditionExpression": Key("gsi_flights_pk").eq(f"FLIGHTDATE#{fecha}"),
            "FilterExpression": Attr("estado_vuelo").is_in(list(ACTIVE_STATES)),
        }
        while True:
            resp = _table.query(**kwargs)
            flights.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    return flights


def _origin_airport(item: dict) -> str:
    """Origen = primer token después de FLIGHT# en la PK: FLIGHT#{origen}#{destino}."""
    parts = item.get("PK", "").split("#")  # ["FLIGHT", origen, destino]
    return parts[1] if len(parts) >= 2 else ""


def _departure_hour(item: dict) -> int | None:
    """Hora (0-23) a partir de hora_salida 'HH:MM'. None si falta o es inválida."""
    hs = item.get("hora_salida", "")
    if len(hs) >= 2 and hs[:2].isdigit():
        return int(hs[:2])
    return None


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


def _cancel_reason(weather: dict) -> str:
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
    flights = _active_flights()
    log.info("cycle start — active_flights=%d", len(flights))

    if not flights:
        log.info("cycle end — no active flights in the next %dh", LOOKAHEAD_HOURS)
        return

    # Cache de forecast por (aeropuerto, fecha, hora): dos vuelos del mismo origen
    # a la misma hora comparten una sola llamada a la API.
    forecast_cache: dict = {}
    cancelled = 0
    for flight in flights:
        airport = _origin_airport(flight)
        fecha = flight.get("fecha")
        hour = _departure_hour(flight)
        if not airport or not fecha or hour is None:
            log.warning(
                "flight=%s sin airport/fecha/hora_salida — skip",
                flight.get("vuelo_numero"),
            )
            continue

        cache_key = (airport, fecha, hour)
        if cache_key not in forecast_cache:
            forecast_cache[cache_key] = _fetch_forecast(airport, fecha, hour)
        weather = forecast_cache[cache_key]

        if not _should_cancel(weather):
            continue
        reason = _cancel_reason(weather)
        if _cancel_flight(flight, reason):
            cancelled += 1
            log.info(
                "CANCELLED flight=%s airport=%s fecha=%s hora=%s reason=%s",
                flight.get("vuelo_numero"),
                airport,
                fecha,
                flight.get("hora_salida"),
                reason,
            )
        else:
            log.info("skip flight=%s — already CANCELADO", flight.get("vuelo_numero"))

    log.info("cycle end — cancelled=%d forecast_calls=%d", cancelled, len(forecast_cache))


def main() -> None:
    log.info(
        "weather-poller starting — table=%s region=%s interval=%ds "
        "base=%s wind_threshold=%s visibility_min=%s",
        BUSINESS_TABLE_NAME,
        REGION,
        POLL_INTERVAL_SECONDS,
        CLIMA_API_BASE,
        WIND_THRESHOLD_KMH,
        VISIBILITY_MIN_M,
    )
    while True:
        try:
            run_cycle()
        except Exception:  # noqa: BLE001 — un ciclo malo nunca mata el loop
            log.exception("run_cycle failed — continuing")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
