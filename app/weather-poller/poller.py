"""
weather-poller — proceso continuo en ECS Fargate.

Dos pasadas en cadencias distintas, en un solo loop:

  1. FORECAST (planning, cada ~30 min, ventana 48h):
     por cada vuelo activo de las próximas 48h consulta el PRONÓSTICO de WeatherAPI
     para la hora de salida. Permite cancelar con horas de anticipación y dar tiempo
     al pasajero. Limitación: el forecast es predicción — no ve un deterioro súbito
     no modelado.

  2. CURRENT (go/no-go final, cada 5 min, ventana corta near-departure):
     por cada vuelo que sale en las próximas ~2h consulta el clima OBSERVADO actual.
     Atrapa el deterioro súbito que el pronóstico no predijo, cerca de la salida.

Ambas escriben la misma transición estado_vuelo -> "CANCELADO" en el master row del
vuelo. El write es idempotente (UpdateItem condicional), así que las dos pasadas
conviven sin coordinarse: si una ya canceló, la otra es no-op. Ese write dispara el
DynamoDB Stream consumido por lambda/stream_emitter.py → SNS → proactive notifications.

Contrato WeatherAPI.com (https://www.weatherapi.com/docs/):
  GET {base}/forecast.json?key=<KEY>&q=iata:<IATA>&days=<1-3>&dt=YYYY-MM-DD&hour=<0-23>
      → forecast.forecastday[0].hour[0].{wind_kph, vis_km}
  GET {base}/current.json?key=<KEY>&q=iata:<IATA>
      → current.{wind_kph, vis_km}
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

# Cadencias de las dos pasadas.
FORECAST_INTERVAL_SECONDS = int(os.environ.get("FORECAST_INTERVAL_SECONDS", "1800"))  # planning: 30 min
CURRENT_INTERVAL_SECONDS  = int(os.environ.get("CURRENT_INTERVAL_SECONDS", "300"))    # go/no-go: 5 min

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

# Ventana del forecast (planning) y del current (near-departure go/no-go).
LOOKAHEAD_HOURS      = int(os.environ.get("LOOKAHEAD_HOURS", "48"))
CURRENT_WINDOW_HOURS = int(os.environ.get("CURRENT_WINDOW_HOURS", "2"))

# WeatherAPI free tier: pronóstico hasta 3 días. Un vuelo cuya fecha cae fuera de
# ese horizonte no se puede evaluar por forecast (se deja sin cancelar). Con
# LOOKAHEAD=48h el horizonte alcanza de sobra.
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


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _weather(wind_kph, vis_km) -> dict | None:
    """Normaliza wind_kph + vis_km (KM) a {'wind_kmh', 'visibility_m' (metros)}.
    Devuelve None si no se pudo leer ninguno de los dos."""
    wind = _as_float(wind_kph)
    vis_km = _as_float(vis_km)
    if wind is None and vis_km is None:
        return None
    return {
        "wind_kmh": wind,
        # vis_km viene en KILÓMETROS → metros para comparar con el umbral.
        "visibility_m": vis_km * 1000 if vis_km is not None else None,
    }


# ── WeatherAPI: FORECAST por aeropuerto + fecha + hora de salida ───────────────
def _fetch_forecast(airport: str, fecha: str, hour: int) -> dict | None:
    """Pronóstico de WeatherAPI para `airport` (IATA) en la fecha/hora de salida.
    None si falla o la fecha cae fuera del horizonte del plan (free=3 días)."""
    days = (date.fromisoformat(fecha) - date.today()).days + 1
    if days < 1 or days > FORECAST_MAX_DAYS:
        log.info("forecast skip airport=%s fecha=%s — fuera del horizonte (%dd)", airport, fecha, days)
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
        log.warning("WeatherAPI forecast failed airport=%s fecha=%s hour=%s: %s", airport, fecha, hour, exc)
        return None

    hour_obj = _forecast_hour(body, fecha, hour)
    if hour_obj is None:
        log.warning("WeatherAPI sin hora airport=%s fecha=%s hour=%s", airport, fecha, hour)
        return None
    return _weather(hour_obj.get("wind_kph"), hour_obj.get("vis_km"))


def _forecast_hour(body, fecha: str, hour: int):
    """forecastday con date==fecha → la hora pedida (con &hour= la API devuelve una
    sola, pero matcheamos por timestamp por robustez; fallback al primero)."""
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


# ── WeatherAPI: CURRENT (clima observado actual) ──────────────────────────────
def _fetch_current(airport: str) -> dict | None:
    """Clima observado actual de WeatherAPI para `airport` (IATA). None si falla."""
    try:
        resp = requests.get(
            f"{CLIMA_API_BASE}/current.json",
            params={"key": WEATHER_API_KEY, "q": f"iata:{airport}"},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("WeatherAPI current failed airport=%s: %s", airport, exc)
        return None

    cur = body.get("current", {}) if isinstance(body, dict) else {}
    return _weather(cur.get("wind_kph"), cur.get("vis_km"))


# ── DynamoDB: Query GSI FlightsByDate ─────────────────────────────────────────
def _query_flights_for_date(fecha: str) -> list[dict]:
    """Query del GSI sparse `FlightsByDate` para una fecha, filtrando estados activos.
    Devuelve solo master rows FLIGHT# (sin asientos): la projection INCLUDE trae
    estado_vuelo, vuelo_numero, fecha y hora_salida."""
    items: list[dict] = []
    kwargs = {
        "IndexName": "FlightsByDate",
        "KeyConditionExpression": Key("gsi_flights_pk").eq(f"FLIGHTDATE#{fecha}"),
        "FilterExpression": Attr("estado_vuelo").is_in(list(ACTIVE_STATES)),
    }
    while True:
        resp = _table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _active_flights() -> list[dict]:
    """Vuelos activos de la ventana [hoy, hoy+LOOKAHEAD_HOURS] (forecast pass).
    Una Query por fecha de la ventana — Query acotado, no Scan de tabla."""
    today = date.today()
    horizon = today + timedelta(hours=LOOKAHEAD_HOURS)
    flights: list[dict] = []
    for i in range((horizon - today).days + 1):
        flights.extend(_query_flights_for_date((today + timedelta(days=i)).isoformat()))
    return flights


def _flights_departing_within(hours: int) -> list[dict]:
    """Vuelos activos que salen en las próximas `hours` (current pass).

    NOTA tz: `hora_salida`/`fecha` del seed son hora local del aeropuerto (UTC-3),
    y el contenedor corre en UTC. Para la ventana corta esto puede correr el set de
    vuelos elegidos algunas horas; es una simplificación asumida (en prod se usaría
    la tz del aeropuerto). No afecta el clima devuelto, sólo qué vuelos se chequean.
    """
    now = datetime.now()
    horizon = now + timedelta(hours=hours)
    fechas = sorted({now.date().isoformat(), horizon.date().isoformat()})
    flights: list[dict] = []
    for fecha in fechas:
        for it in _query_flights_for_date(fecha):
            dep = _departure_datetime(it)
            if dep is not None and now <= dep <= horizon:
                flights.append(it)
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


def _departure_datetime(item: dict) -> datetime | None:
    """Combina fecha 'YYYY-MM-DD' + hora_salida 'HH:MM' en un datetime naive."""
    try:
        return datetime.strptime(f"{item.get('fecha', '')} {item.get('hora_salida', '')}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


# ── DynamoDB: cancelación idempotente ─────────────────────────────────────────
def _cancel_flight(item: dict, reason: str) -> bool:
    """UpdateItem condicional e idempotente. True si canceló, False si ya estaba."""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET estado_vuelo = :new, cancellation_reason = :reason, cancelled_at = :now",
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


def _reason_bits(weather: dict) -> str:
    bits = []
    if weather.get("wind_kmh") is not None:
        bits.append(f"wind={weather['wind_kmh']}km/h(>{WIND_THRESHOLD_KMH})")
    if weather.get("visibility_m") is not None:
        bits.append(f"visibility={weather['visibility_m']}m(<{VISIBILITY_MIN_M})")
    return ", ".join(bits) if bits else "weather"


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


def _evaluate_and_cancel(flight: dict, weather: dict | None, source: str) -> bool:
    """Aplica umbrales y, si corresponde, cancela. Devuelve True si canceló ahora."""
    if not _should_cancel(weather):
        return False
    reason = f"{source}: {_reason_bits(weather)}"
    if _cancel_flight(flight, reason):
        log.info(
            "CANCELLED [%s] flight=%s airport=%s fecha=%s hora=%s reason=%s",
            source, flight.get("vuelo_numero"), _origin_airport(flight),
            flight.get("fecha"), flight.get("hora_salida"), reason,
        )
        return True
    log.info("skip [%s] flight=%s — already CANCELADO", source, flight.get("vuelo_numero"))
    return False


# ── Pasada FORECAST (planning, ventana 48h) ───────────────────────────────────
def forecast_pass() -> None:
    flights = _active_flights()
    log.info("forecast pass — active_flights=%d (ventana %dh)", len(flights), LOOKAHEAD_HOURS)
    if not flights:
        return

    cache: dict = {}  # (airport, fecha, hour) → weather
    cancelled = 0
    for flight in flights:
        airport = _origin_airport(flight)
        fecha = flight.get("fecha")
        hour = _departure_hour(flight)
        if not airport or not fecha or hour is None:
            log.warning("flight=%s sin airport/fecha/hora_salida — skip", flight.get("vuelo_numero"))
            continue
        key = (airport, fecha, hour)
        if key not in cache:
            cache[key] = _fetch_forecast(airport, fecha, hour)
        if _evaluate_and_cancel(flight, cache[key], "forecast"):
            cancelled += 1
    log.info("forecast pass end — cancelled=%d api_calls=%d", cancelled, len(cache))


# ── Pasada CURRENT (go/no-go, ventana near-departure) ─────────────────────────
def current_pass() -> None:
    flights = _flights_departing_within(CURRENT_WINDOW_HOURS)
    log.info("current pass — departing<=%dh=%d", CURRENT_WINDOW_HOURS, len(flights))
    if not flights:
        return

    cache: dict = {}  # airport → weather (el clima actual es el mismo para todos sus vuelos)
    cancelled = 0
    for flight in flights:
        airport = _origin_airport(flight)
        if not airport:
            continue
        if airport not in cache:
            cache[airport] = _fetch_current(airport)
        if _evaluate_and_cancel(flight, cache[airport], "current"):
            cancelled += 1
    log.info("current pass end — cancelled=%d api_calls=%d", cancelled, len(cache))


# ── Loop principal: tick corto (current); forecast cada N ticks ───────────────
def main() -> None:
    forecast_every = max(1, FORECAST_INTERVAL_SECONDS // CURRENT_INTERVAL_SECONDS)
    log.info(
        "weather-poller starting — table=%s region=%s base=%s "
        "forecast_every=%ds current_every=%ds wind=%s vis_min_m=%s",
        BUSINESS_TABLE_NAME, REGION, CLIMA_API_BASE,
        FORECAST_INTERVAL_SECONDS, CURRENT_INTERVAL_SECONDS, WIND_THRESHOLD_KMH, VISIBILITY_MIN_M,
    )
    tick = 0
    while True:
        try:
            # Forecast cada `forecast_every` ticks (incluye el arranque, tick 0).
            if tick % forecast_every == 0:
                forecast_pass()
            # Current en cada tick (cadencia corta).
            current_pass()
        except Exception:  # noqa: BLE001 — un ciclo malo nunca mata el loop
            log.exception("cycle failed — continuing")
        tick += 1
        time.sleep(CURRENT_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
