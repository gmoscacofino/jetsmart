import os, json, uuid, time, hashlib, logging, re
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
import anthropic

from pricing import validate_inputs, PricingError, EXTRAS_FIJOS
from pii_tokenizer import tokenize_text, detokenize_inputs, detokenize_string

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION               = os.environ["AWS_REGION_VAR"]
# TP4: dos tablas — bounded contexts (conversations + business/PSS)
CONV_TABLE_NAME      = os.environ["CONVERSATIONS_TABLE_NAME"]
BIZ_TABLE_NAME       = os.environ["BUSINESS_TABLE_NAME"]
HUMAN_HANDOFF_QUEUE_URL = os.environ.get("HUMAN_HANDOFF_QUEUE_URL", "")
SNS_TOPIC_ARN        = os.environ["SNS_TOPIC_ARN"]
SECRET_ARN           = os.environ["ANTHROPIC_SECRET_ARN"]
SF_ARN               = os.environ.get("STEP_FUNCTIONS_ARN", "")
FRONTEND_URL         = os.environ.get("FRONTEND_URL") or "*"

SYSTEM_PROMPT_PATH   = "/opt/system_prompt.txt"

dynamodb   = boto3.resource("dynamodb", region_name=REGION)
conv_table = dynamodb.Table(CONV_TABLE_NAME)
biz_table  = dynamodb.Table(BIZ_TABLE_NAME)
sns        = boto3.client("sns", region_name=REGION)
sqs        = boto3.client("sqs", region_name=REGION)
sm         = boto3.client("secretsmanager", region_name=REGION)
sf         = boto3.client("stepfunctions", region_name=REGION)

MAX_HISTORY     = 40
MSG_TTL_SECONDS = 7 * 24 * 3600
HANDOFF_TTL_SECONDS = 30 * 24 * 3600
MAX_TOOL_ROUNDS = 5
HOLD_TTL_SECONDS = 600  # 10 minutos — soft-hold de asiento mientras el user completa la reserva

# HMAC key para tokens PII estables por sesión. Generada por random_password
# en Terraform (terraform/infra/secrets.tf) e inyectada como env var.
# Sin fallback: fail-fast en cold start si no está configurada.
PII_TOKEN_SECRET = os.environ["PII_TOKEN_SECRET"]

# Inicialización eager: ocurre en el cold start, no en el primer request.
def _init_anthropic() -> anthropic.Anthropic:
    secret  = sm.get_secret_value(SecretId=SECRET_ARN)
    api_key = json.loads(secret["SecretString"])["api_key"]
    return anthropic.Anthropic(api_key=api_key)

def _load_system_prompt() -> str:
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()

_anthropic_client  = _init_anthropic()
_raw_system_prompt = _load_system_prompt()

_system_prompt_cache: dict = {}

# Charset PNR estándar — mismo que payment_processor.py para consistencia.
# Alfanumérico uppercase sin caracteres ambiguos (0/O, 1/I).
_PNR_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _pnr_from_payment_id(payment_id: str) -> str:
    """
    Genera PNR estable desde payment_id usando SHA-256.
    Mismo algoritmo que payment_processor.py — duplicado a propósito para no
    acoplar este Lambda al package del Saga.
    """
    digest = hashlib.sha256(payment_id.encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(6):
        chars.append(_PNR_CHARSET[n % len(_PNR_CHARSET)])
        n //= len(_PNR_CHARSET)
    return "".join(chars)


def _build_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%A %d de %B de %Y")
    if _system_prompt_cache.get("date") != today:
        _system_prompt_cache["date"]   = today
        _system_prompt_cache["prompt"] = f"Fecha de hoy (UTC): {today}\n\n{_raw_system_prompt}"
    return _system_prompt_cache["prompt"]


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_user(event: dict) -> dict:
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims")
    if not claims:
        raise ValueError("Request sin claims de Cognito Authorizer")
    if not claims.get("sub"):
        raise ValueError("Claim 'sub' ausente en el token")
    return claims


def _user_id(user: dict) -> str:
    return user["sub"]


# ── DynamoDB: conversations table ─────────────────────────────────────────────

def _upsert_user_profile(user: dict):
    user_id = _user_id(user)
    try:
        conv_table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": "#METADATA"},
            UpdateExpression=(
                "SET email = if_not_exists(email, :email), "
                "last_seen = :now"
            ),
            ExpressionAttributeValues={
                ":email": user.get("email", ""),
                ":now":   datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        log.warning("Error actualizando perfil de usuario: %s", e)


def _get_history(session_id: str, user_id: str = "") -> list:
    resp = conv_table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk":     f"SESSION#{session_id}",
            ":prefix": "MSG#",
        },
        ScanIndexForward=False,
        Limit=MAX_HISTORY,
    )
    messages = []
    for i in reversed(resp.get("Items", [])):
        if user_id and i.get("user_id") and i["user_id"] != user_id:
            log.warning("Session %s owned by %s accessed by %s — clearing history",
                        session_id, i["user_id"], user_id)
            return []
        content = i["content"]
        if i.get("content_type") == "tool":
            content = json.loads(content)
        messages.append({"role": i["role"], "content": content})

    while len(messages) >= 2 and messages[-1]["role"] == messages[-2]["role"]:
        messages.pop()

    return messages


def _save_message(session_id: str, user_id: str, role: str, content):
    ts  = datetime.now(timezone.utc).isoformat()
    uid = str(uuid.uuid4())[:8]
    if isinstance(content, str):
        stored = content
        ctype  = "text"
    else:
        stored = json.dumps(content, default=str)
        ctype  = "tool"
    conv_table.put_item(Item={
        "PK":           f"SESSION#{session_id}",
        "SK":           f"MSG#{ts}#{uid}",
        "role":         role,
        "content":      stored,
        "content_type": ctype,
        "user_id":      user_id,
        "ttl":          int(time.time()) + MSG_TTL_SECONDS,
    })


# ── SNS ───────────────────────────────────────────────────────────────────────

def _emit_event(event_type: str, payload: dict, user_id: str):
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            # Trailing "\n": al entregarse por SNS→Firehose (raw) al data lake,
            # cada evento queda como una línea JSON válida (JSON Lines).
            Message=json.dumps({
                "event_type": event_type,
                "user_id":    user_id,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "payload":    payload,
            }) + "\n",
            Subject=event_type,
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": event_type},
            },
        )
    except Exception as e:
        log.warning("SNS emit fallido: %s", e)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  FRONTEND_URL,
    "Access-Control-Allow-Headers": "Authorization,Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json", **CORS_HEADERS},
        "body":       json.dumps(body),
    }


# ── Tool use — capa de datos de JetSmart ──────────────────────────────────────
#
# TP4: las reservas usan PNRs de 6 chars (formato Navitaire/Amadeus).
# Los datos del PSS (vuelos, PNRs, pasajeros, claims) viven en biz_table.
# Las conversaciones y handoffs viven en conv_table.

TOOLS = [
    {
        "name": "list_flight_dates",
        "description": (
            "Lista todas las fechas con vuelos disponibles entre dos ciudades. "
            "Usar PRIMERO cuando el usuario no especifica una fecha concreta y quiere saber "
            "cuándo puede volar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":  {"type": "string", "description": "IATA origen (AEP, SCL, COR, MDZ, etc.)"},
                "destino": {"type": "string", "description": "IATA destino"},
            },
            "required": ["origen", "destino"],
        },
    },
    {
        "name": "search_flights",
        "description": (
            "Busca el detalle de un vuelo entre dos ciudades en una fecha concreta. "
            "Devuelve número de vuelo, horarios, precio por pasajero y asientos disponibles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":    {"type": "string", "description": "IATA origen"},
                "destino":   {"type": "string", "description": "IATA destino"},
                "fecha":     {"type": "string", "description": "YYYY-MM-DD"},
                "pasajeros": {"type": "integer", "description": "Cantidad de pasajeros (default 1)"},
            },
            "required": ["origen", "destino", "fecha"],
        },
    },
    {
        "name": "list_available_seats",
        "description": (
            "Lista categorías de asientos disponibles para un vuelo+fecha. "
            "Devuelve conteos por categoría (estandar, salida_rapida, salida_emergencia, "
            "primera_fila) y 6 ejemplos de seat_id por categoría. Usar en PASO 3 antes "
            "de pedirle al usuario que elija."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":       {"type": "string"},
                "destino":      {"type": "string"},
                "fecha":        {"type": "string"},
                "vuelo_numero": {"type": "string"},
            },
            "required": ["origen", "destino", "fecha", "vuelo_numero"],
        },
    },
    {
        "name": "hold_seat",
        "description": (
            "Reserva temporal (soft-hold) de un asiento por 10 minutos. "
            "Llamar en PASO 3 apenas el usuario elige un asiento específico. "
            "Si el user tenía otro hold previo en el MISMO vuelo, se libera automáticamente. "
            "Devuelve expires_at_epoch para el countdown del frontend. "
            "Si retorna ok=False, ofrecer otros asientos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":       {"type": "string"},
                "destino":      {"type": "string"},
                "fecha":        {"type": "string"},
                "vuelo_numero": {"type": "string"},
                "seat_id":      {"type": "string"},
            },
            "required": ["origen", "destino", "fecha", "vuelo_numero", "seat_id"],
        },
    },
    {
        "name": "check_hold_status",
        "description": (
            "Verifica el estado del hold actual del usuario sobre un asiento. "
            "OBLIGATORIO llamar al INICIO de cada turn entre PASO 3 (hold) y PASO 6c (confirmar). "
            "Devuelve status:'still_held' si el hold sigue vigente, "
            "'expired_seat_free' si expiró pero el asiento sigue libre (ofrecer retomar), "
            "'expired_seat_taken' si otro lo tomó (avisar y mostrar alternativas), "
            "'no_hold' si nunca hubo hold en este seat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":       {"type": "string"},
                "destino":      {"type": "string"},
                "fecha":        {"type": "string"},
                "vuelo_numero": {"type": "string"},
                "seat_id":      {"type": "string"},
            },
            "required": ["origen", "destino", "fecha", "vuelo_numero", "seat_id"],
        },
    },
    {
        "name": "release_hold",
        "description": (
            "Libera el hold del usuario sobre un asiento (cuando cambia de opinión). "
            "Idempotente — si no había hold, devuelve ok=True igual."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":       {"type": "string"},
                "destino":      {"type": "string"},
                "fecha":        {"type": "string"},
                "vuelo_numero": {"type": "string"},
                "seat_id":      {"type": "string"},
            },
            "required": ["origen", "destino", "fecha", "vuelo_numero", "seat_id"],
        },
    },
    {
        "name": "get_reservation",
        "description": "Consulta el estado de una reserva por PNR (record locator de 6 caracteres).",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string", "description": "PNR de 6 chars (ej: JS7K2P)"},
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "list_user_reservations",
        "description": "Lista todas las reservas del usuario autenticado.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_in",
        "description": "Realiza el check-in de una reserva confirmada (24h antes del vuelo).",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string", "description": "PNR de 6 chars"},
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "get_boarding_pass",
        "description": (
            "Obtiene el boarding pass de una reserva con check-in realizado. "
            "El BP se genera de forma asincrónica tras la confirmación de la reserva; "
            "si todavía está procesándose, devuelve un mensaje indicándolo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string", "description": "PNR de 6 chars"},
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "create_claim",
        "description": "Registra un reclamo sobre un vuelo o reserva.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "string", "description": "PNR relacionado (opcional)"},
                "tipo":           {"type": "string", "description": "equipaje_perdido | equipaje_daniado | vuelo_demorado | vuelo_cancelado | reembolso | otro"},
                "descripcion":    {"type": "string", "description": "Descripción del problema"},
            },
            "required": ["tipo", "descripcion"],
        },
    },
    {
        "name": "list_saved_passengers",
        "description": "Lista los pasajeros guardados del usuario a partir de reservas anteriores.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_reservation",
        "description": (
            "Crea una reserva real e inicia el flujo de pago (Saga). "
            "Llamar SÓLO cuando el usuario confirmó explícitamente todos los detalles. "
            "NO pedir el email al usuario: la herramienta lo completa automáticamente "
            "con el email del usuario autenticado (claim del JWT de Cognito). "
            "NO pasar `total` — el sistema lo calcula server-side a partir de tarifa+extras."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":          {"type": "string"},
                "destino":         {"type": "string"},
                "fecha":           {"type": "string", "description": "YYYY-MM-DD"},
                "pasajeros":       {"type": "integer"},
                "tarifa":          {"type": "string", "enum": ["BASIC", "LIGHT", "SMART", "FULL FLEX"]},
                "extras": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(EXTRAS_FIJOS.keys())},
                    "description": "Lista de extras contratados (vacía si no hay).",
                },
                "seat_id":          {"type": "string", "description": "ID asiento (ej '12A'). Vacío = sistema asigna aleatorio."},
                "telefono":         {"type": "string"},
                "nombre_pasajero":  {"type": "string"},
                "dni":              {"type": "string", "description": "DNI del pasajero principal (sin puntos)"},
                "fecha_nacimiento": {"type": "string", "description": "Fecha de nacimiento del pasajero principal (YYYY-MM-DD)"},
                "sexo":             {"type": "string", "enum": ["Masculino", "Femenino", "Otro"]},
                "vuelo_numero":     {"type": "string", "description": "Número de vuelo (JA203, etc.)"},
            },
            "required": ["origen", "destino", "fecha", "pasajeros", "tarifa", "vuelo_numero"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Deriva la conversación a un agente humano del call center cuando el usuario "
            "lo solicita explícitamente, cuando muestra frustración significativa, o cuando "
            "el problema está fuera del alcance del chatbot (legal, médico, seguridad, "
            "casos complejos que ninguna otra herramienta puede resolver). "
            "NO usar para preguntas que podés contestar con otras tools. "
            "Devuelve un ticket_id que el agente humano usará para retomar el contexto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason":  {
                    "type": "string",
                    "description": "Motivo breve (ej: 'usuario solicita hablar con un humano', 'reclamo complejo de equipaje', 'consulta legal')",
                },
                "urgency": {
                    "type": "string",
                    "enum":  ["low", "medium", "high"],
                    "description": "Urgencia. 'high' si vuelo en <24h o problema en curso.",
                },
            },
            "required": ["reason", "urgency"],
        },
    },
]


# ── Validación server-side de datos de pasajero ───────────────────────────────
#
# Como tokenizamos PII antes de mandar a Anthropic, Claude solo ve placeholders
# y no puede validar formato. La validación es responsabilidad del server.

# Regex flexibles para inputs argentinos. Aceptan también nombres internacionales.
_NAME_RE  = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ\s'\-]{1,80}$")
_DNI_RE   = re.compile(r"^\d{7,8}$")
_PHONE_RE = re.compile(r"^[\d\s+()\-]{8,20}$")
_DOB_RE   = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEXO_OK  = {"Masculino", "Femenino", "Otro"}


def _validate_passenger_input(inputs: dict) -> str | None:
    """
    Valida los campos PII del passenger que llegan a create_reservation.
    Retorna None si todo OK, o un mensaje de error para que Claude le pida
    al user corregir.
    """
    nombre = (inputs.get("nombre_pasajero") or "").strip()
    if not nombre:
        return "El nombre del pasajero es obligatorio."
    if not _NAME_RE.match(nombre):
        return "El nombre del pasajero solo puede contener letras, espacios, apóstrofes y guiones (1-80 caracteres)."

    dni_raw = (inputs.get("dni") or "").replace(".", "").replace(" ", "").replace("-", "")
    if not dni_raw:
        return "El DNI del pasajero es obligatorio."
    if not _DNI_RE.match(dni_raw):
        return "El DNI debe tener entre 7 y 8 dígitos numéricos (sin puntos)."

    telefono = (inputs.get("telefono") or "").strip()
    if telefono and not _PHONE_RE.match(telefono):
        return "El teléfono tiene un formato inválido."

    dob = (inputs.get("fecha_nacimiento") or "").strip()
    if dob:
        if not _DOB_RE.match(dob):
            return "La fecha de nacimiento debe estar en formato YYYY-MM-DD."
        try:
            dob_d = date.fromisoformat(dob)
        except ValueError:
            return "La fecha de nacimiento no es una fecha válida."
        today = date.today()
        if dob_d > today:
            return "La fecha de nacimiento no puede ser futura."
        age_years = (today - dob_d).days / 365.25
        if age_years > 120:
            return "La fecha de nacimiento no parece válida (edad mayor a 120 años)."

    sexo = (inputs.get("sexo") or "").strip()
    if sexo and sexo not in _SEXO_OK:
        return f"El sexo debe ser uno de: {', '.join(sorted(_SEXO_OK))}."

    return None


def _execute_tool(name: str, inputs: dict, user_id: str, session_id: str = "", user_email: str = "") -> str:
    if name == "list_flight_dates":
        origen  = inputs["origen"].upper()
        destino = inputs["destino"].upper()
        log.info("list_flight_dates: %s→%s", origen, destino)
        resp = biz_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk":     f"FLIGHT#{origen}#{destino}",
                ":prefix": "DATE#",
            },
            ScanIndexForward=True,
        )
        items = resp.get("Items", [])
        if not items:
            return json.dumps({"disponible": False, "mensaje": f"No hay vuelos de {origen} a {destino}."})
        # Filtrar master rows (sin "#SEAT#" en el SK). Los ítems SEAT# también
        # empiezan con DATE# pero no son vuelos buscables — son inventario.
        fechas = []
        for i in items:
            if "#SEAT#" in i.get("SK", ""):
                continue
            sk_parts = i["SK"].split("#")
            fechas.append({
                "fecha":       sk_parts[1],
                "vuelo":       i.get("vuelo_numero"),
                "hora_salida": i.get("hora_salida"),
                "precio_desde": float(i.get("precio", 0)),
            })
        return json.dumps({"origen": origen, "destino": destino, "fechas": fechas})

    if name == "search_flights":
        origen    = inputs["origen"].upper()
        destino   = inputs["destino"].upper()
        fecha     = inputs["fecha"]
        pasajeros = int(inputs.get("pasajeros", 1))
        log.info("search_flights: %s→%s %s (%d pax)", origen, destino, fecha, pasajeros)
        resp = biz_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk":     f"FLIGHT#{origen}#{destino}",
                ":prefix": f"DATE#{fecha}#",
            },
            ScanIndexForward=True,
        )
        # Filtramos master rows — los SEAT# se cuentan aparte con COUNT
        masters = [i for i in resp.get("Items", []) if "#SEAT#" not in i.get("SK", "")]
        if not masters:
            return json.dumps({"disponible": False, "mensaje": f"No hay vuelos de {origen} a {destino} el {fecha}."})

        vuelos = []
        for item in masters:
            vuelo_n = item["vuelo_numero"]
            # COUNT real de asientos libres — pasa por el seat map
            cnt = biz_table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
                FilterExpression="attribute_not_exists(reserved_by)",
                Select="COUNT",
                ExpressionAttributeValues={
                    ":pk":     f"FLIGHT#{origen}#{destino}",
                    ":prefix": f"DATE#{fecha}#FLIGHT#{vuelo_n}#SEAT#",
                },
            )["Count"]
            if cnt >= pasajeros:
                vuelos.append({
                    "vuelo":                vuelo_n,
                    "hora_salida":          item.get("hora_salida"),
                    "hora_llegada":         item.get("hora_llegada"),
                    "duracion":             item.get("duracion"),
                    "precio_por_pasajero":  float(item["precio"]),
                    "precio_total":         float(item["precio"]) * pasajeros,
                    "asientos_disponibles": cnt,
                    "estado_vuelo":         item.get("estado_vuelo", "EN_HORARIO"),
                })

        if not vuelos:
            return json.dumps({
                "disponible": False,
                "mensaje": (
                    f"Hay vuelo(s) {origen}-{destino} {fecha} sin asientos suficientes "
                    f"para {pasajeros} pax."
                ),
            })
        _emit_event("busqueda_vuelo", {
            "origen": origen, "destino": destino, "fecha": fecha,
            "pasajeros": pasajeros, "ruta": f"{origen}-{destino}",
        }, user_id)
        return json.dumps({
            "disponible": True, "origen": origen, "destino": destino,
            "fecha": fecha, "pasajeros": pasajeros, "vuelos": vuelos,
        })

    if name == "list_available_seats":
        o = inputs["origen"].upper()
        d = inputs["destino"].upper()
        f = inputs["fecha"]
        v = inputs["vuelo_numero"]
        log.info("list_available_seats: %s→%s %s %s", o, d, f, v)
        # FilterExpression server-side: sólo seats sin reserved_by.
        # Los holds los filtramos en Python para distinguir "hold propio" vs "hold ajeno".
        resp = biz_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            FilterExpression="attribute_not_exists(reserved_by)",
            ExpressionAttributeValues={
                ":pk":     f"FLIGHT#{o}#{d}",
                ":prefix": f"DATE#{f}#FLIGHT#{v}#SEAT#",
            },
        )
        now_epoch = int(time.time())
        user_key = f"USER#{user_id}"
        by_cat = {"estandar": [], "salida_rapida": [], "salida_emergencia": [], "primera_fila": []}
        my_hold = None
        for s in resp.get("Items", []):
            held_by = s.get("held_by")
            hold_expires = int(s.get("hold_expires_at") or 0)
            seat_id = s.get("seat_id")
            cat = s.get("seat_type", "estandar")
            # Hold vigente de OTRO user → no disponible
            if held_by and held_by != user_key and hold_expires > now_epoch:
                continue
            # Hold vigente del MISMO user → marcarlo
            if held_by == user_key and hold_expires > now_epoch:
                my_hold = {"seat_id": seat_id, "expires_at_epoch": hold_expires}
                continue
            by_cat.setdefault(cat, []).append(seat_id)
        return json.dumps({
            "vuelo": v, "fecha": f,
            "categorias": {
                cat: {"disponibles": len(lst), "ejemplos": sorted(lst)[:6]}
                for cat, lst in by_cat.items()
            },
            "tu_hold_actual": my_hold,
        })

    if name == "hold_seat":
        o = inputs["origen"].upper()
        d = inputs["destino"].upper()
        f = inputs["fecha"]
        v = inputs["vuelo_numero"]
        seat_id = inputs["seat_id"].upper()
        log.info("hold_seat: %s→%s %s %s seat=%s user=%s", o, d, f, v, seat_id, user_id)

        now_epoch = int(time.time())
        expires_at = now_epoch + HOLD_TTL_SECONDS
        user_key = f"USER#{user_id}"
        flight_pk = f"FLIGHT#{o}#{d}"
        new_seat_sk = f"DATE#{f}#FLIGHT#{v}#SEAT#{seat_id}"

        # Paso 1 — intentar el hold del seat nuevo (atomic).
        try:
            biz_table.update_item(
                Key={"PK": flight_pk, "SK": new_seat_sk},
                UpdateExpression="SET held_by = :user, hold_expires_at = :exp",
                ConditionExpression=(
                    "attribute_exists(PK) AND attribute_not_exists(reserved_by) AND "
                    "(attribute_not_exists(held_by) OR held_by = :user "
                    "OR hold_expires_at <= :now)"
                ),
                ExpressionAttributeValues={
                    ":user": user_key,
                    ":exp":  expires_at,
                    ":now":  now_epoch,
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Distinguir: no existe vs ya reservado vs holdeado por otro
                check = biz_table.get_item(Key={"PK": flight_pk, "SK": new_seat_sk}).get("Item")
                if not check:
                    return json.dumps({"ok": False, "motivo": "seat_no_existe",
                                       "mensaje": f"El asiento {seat_id} no existe en este vuelo."})
                if check.get("reserved_by"):
                    return json.dumps({"ok": False, "motivo": "seat_reservado",
                                       "mensaje": f"El asiento {seat_id} ya está confirmado por otro pasajero."})
                return json.dumps({"ok": False, "motivo": "hold_ajeno",
                                   "mensaje": f"Otro pasajero está reservando {seat_id} ahora. Elegí otro."})
            raise

        # Paso 2 — liberar holds previos del MISMO user en el mismo vuelo
        # (best-effort, fuera de transacción atómica con el hold nuevo).
        prev_resp = biz_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            FilterExpression="held_by = :user AND attribute_not_exists(reserved_by)",
            ExpressionAttributeValues={
                ":pk":     flight_pk,
                ":prefix": f"DATE#{f}#FLIGHT#{v}#SEAT#",
                ":user":   user_key,
            },
        )
        for prev in prev_resp.get("Items", []):
            if prev["SK"] == new_seat_sk:
                continue
            try:
                biz_table.update_item(
                    Key={"PK": prev["PK"], "SK": prev["SK"]},
                    UpdateExpression="REMOVE held_by, hold_expires_at",
                    ConditionExpression="held_by = :user",
                    ExpressionAttributeValues={":user": user_key},
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    log.warning("No pude liberar hold previo %s: %s", prev["SK"], e)

        return json.dumps({
            "ok": True,
            "seat_id": seat_id,
            "expires_at_epoch": expires_at,
            "ttl_seconds": HOLD_TTL_SECONDS,
            "vuelo_numero": v,
            "fecha": f,
            "origen": o,
            "destino": d,
            "mensaje": f"Reservé temporalmente el asiento {seat_id}. Tenés {HOLD_TTL_SECONDS // 60} minutos para confirmar la compra.",
        })

    if name == "check_hold_status":
        o = inputs["origen"].upper()
        d = inputs["destino"].upper()
        f = inputs["fecha"]
        v = inputs["vuelo_numero"]
        seat_id = inputs["seat_id"].upper()
        log.info("check_hold_status: seat=%s user=%s", seat_id, user_id)

        now_epoch = int(time.time())
        user_key = f"USER#{user_id}"
        flight_pk = f"FLIGHT#{o}#{d}"
        seat_sk = f"DATE#{f}#FLIGHT#{v}#SEAT#{seat_id}"

        item = biz_table.get_item(Key={"PK": flight_pk, "SK": seat_sk}).get("Item")
        if not item:
            return json.dumps({"status": "no_hold", "seat_id": seat_id,
                               "mensaje": f"Asiento {seat_id} no existe."})

        if item.get("reserved_by"):
            return json.dumps({
                "status": "expired_seat_taken", "seat_id": seat_id,
                "mensaje": f"El asiento {seat_id} ya fue confirmado por otro pasajero.",
            })

        held_by = item.get("held_by")
        hold_expires = int(item.get("hold_expires_at") or 0)

        if held_by == user_key and hold_expires > now_epoch:
            return json.dumps({
                "status": "still_held", "seat_id": seat_id,
                "expires_at_epoch": hold_expires,
                "seconds_remaining": hold_expires - now_epoch,
                "vuelo_numero": v, "fecha": f,
            })

        if held_by and held_by != user_key and hold_expires > now_epoch:
            return json.dumps({
                "status": "expired_seat_taken", "seat_id": seat_id,
                "mensaje": f"Otro pasajero está reservando {seat_id} ahora.",
            })

        # held_by ausente o expirado, sin reserved_by → seat libre
        return json.dumps({
            "status": "expired_seat_free", "seat_id": seat_id,
            "vuelo_numero": v, "fecha": f,
            "mensaje": f"El hold sobre {seat_id} venció pero el asiento sigue libre. Lo puedo retomar si querés.",
        })

    if name == "release_hold":
        o = inputs["origen"].upper()
        d = inputs["destino"].upper()
        f = inputs["fecha"]
        v = inputs["vuelo_numero"]
        seat_id = inputs["seat_id"].upper()
        log.info("release_hold: seat=%s user=%s", seat_id, user_id)
        flight_pk = f"FLIGHT#{o}#{d}"
        seat_sk = f"DATE#{f}#FLIGHT#{v}#SEAT#{seat_id}"
        user_key = f"USER#{user_id}"
        try:
            biz_table.update_item(
                Key={"PK": flight_pk, "SK": seat_sk},
                UpdateExpression="REMOVE held_by, hold_expires_at",
                ConditionExpression="held_by = :user",
                ExpressionAttributeValues={":user": user_key},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        return json.dumps({"ok": True, "seat_id": seat_id, "released": True})

    if name == "get_reservation":
        pnr = inputs["reservation_id"].upper()
        log.info("get_reservation: %s user=%s", pnr, user_id)
        # Leemos el thin pointer del user para verificar ownership
        resp = biz_table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{pnr}"})
        item = resp.get("Item")
        if not item:
            return json.dumps({"encontrada": False, "mensaje": f"No se encontró la reserva {pnr}."})
        return json.dumps({
            "encontrada":     True,
            "reservation_id": pnr,
            "pnr":            pnr,
            "origen":         item.get("origen"),
            "destino":        item.get("destino"),
            "fecha":          item.get("fecha"),
            "pasajeros":      item.get("pasajeros"),
            "status":         item.get("status"),
            "total":          float(item.get("total", 0)),
        }, default=str)

    if name == "list_user_reservations":
        log.info("list_user_reservations: user=%s", user_id)
        resp = biz_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk":     f"USER#{user_id}",
                ":prefix": "RESERVATION#",
            },
            ScanIndexForward=False,
            Limit=20,
        )
        items = resp.get("Items", [])
        if not items:
            return json.dumps({"reservas": [], "mensaje": "No tenés reservas registradas."})
        reservas = [
            {
                "reservation_id": i.get("pnr"),
                "pnr":            i.get("pnr"),
                "vuelo":          i.get("vuelo_numero", "—"),
                "origen":         i.get("origen", "—"),
                "destino":        i.get("destino", "—"),
                "fecha":          i.get("fecha", "—"),
                "pasajeros":      int(i.get("pasajeros", 1)),
                "tarifa":         i.get("tarifa", "—"),
                "status":         i.get("status", "—"),
                "total":          float(i.get("total", 0)),
            }
            for i in items
        ]
        return json.dumps({"reservas": reservas}, default=str)

    if name == "check_in":
        pnr = inputs["reservation_id"].upper()
        log.info("check_in: %s user=%s", pnr, user_id)
        resp = biz_table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{pnr}"})
        item = resp.get("Item")
        if not item:
            return json.dumps({"ok": False, "mensaje": f"No se encontró la reserva {pnr}."})
        if item.get("status") == "CHECK-IN":
            return json.dumps({"ok": True, "ya_realizado": True, "mensaje": "Ya tenés check-in realizado para esta reserva."})
        if item.get("status") not in ("CONFIRMADA",):
            return json.dumps({"ok": False, "mensaje": f"No se puede hacer check-in: la reserva está en estado {item.get('status')}."})
        flight_dt = date.fromisoformat(item["fecha"])
        today = date.today()
        if flight_dt < today:
            return json.dumps({"ok": False, "mensaje": "No podés hacer check-in para un vuelo que ya pasó."})
        if flight_dt > today + timedelta(days=1):
            return json.dumps({"ok": False, "mensaje": f"El check-in abre 24 horas antes del vuelo. Tu vuelo es el {item['fecha']}."})
        # Update thin pointer + PNR canonical
        biz_table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{pnr}"},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "CHECK-IN"},
        )
        biz_table.update_item(
            Key={"PK": f"PNR#{pnr}", "SK": "#METADATA"},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "CHECK-IN"},
        )
        _emit_event("checkin_realizado", {
            "reservation_id": pnr, "pnr": pnr,
            "vuelo_numero":   item.get("vuelo_numero", ""),
            "origen":         item.get("origen", ""),
            "destino":        item.get("destino", ""),
            "fecha":          item.get("fecha", ""),
        }, user_id)
        return json.dumps({
            "ok": True, "reservation_id": pnr,
            "vuelo":  item.get("vuelo_numero", "—"),
            "origen": item.get("origen", "—"),
            "destino": item.get("destino", "—"),
            "fecha":  item.get("fecha", "—"),
            "mensaje": "Check-in realizado correctamente. Ya podés obtener tu boarding pass.",
        })

    if name == "get_boarding_pass":
        pnr = inputs["reservation_id"].upper()
        log.info("get_boarding_pass: %s user=%s", pnr, user_id)
        # Verificamos ownership vía thin pointer
        resp = biz_table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{pnr}"})
        item = resp.get("Item")
        if not item:
            return json.dumps({"ok": False, "mensaje": f"No se encontró la reserva {pnr}."})
        if item.get("status") not in ("CHECK-IN",):
            return json.dumps({"ok": False, "mensaje": "Necesitás hacer check-in antes de obtener el boarding pass."})
        # Buscamos el BP en el PNR canonical
        bp_resp = biz_table.get_item(Key={"PK": f"PNR#{pnr}", "SK": "BP#01"})
        bp = bp_resp.get("Item")
        if not bp or not bp.get("bp_url"):
            return json.dumps({
                "ok": True,
                "procesando": True,
                "mensaje": "Tu boarding pass se está generando, intentá en unos segundos.",
            })
        destino_email = user_email or "tu casilla registrada"
        return json.dumps({
            "ok":             True,
            "enviado_por_mail": True,
            "destino_email":  destino_email,
            "boarding_pass": {
                "reservation_id": pnr,
                "pasajero":       item.get("nombre_pasajero", "Pasajero"),
                "vuelo":          item.get("vuelo_numero", "—"),
                "origen":         item.get("origen", "—"),
                "destino":        item.get("destino", "—"),
                "fecha":          item.get("fecha", "—"),
                "asiento":        item.get("seat", "ALEATORIO"),
                "grupo":          "B",
                "puerta":         "12",
                "embarque":       "45 min antes de la salida",
            },
            "mensaje": f"Te enviamos el boarding pass a {destino_email}. Llega en unos minutos.",
        })

    if name == "create_claim":
        tipo        = inputs["tipo"]
        descripcion = inputs["descripcion"]
        pnr         = inputs.get("reservation_id", "").upper()
        claim_id    = f"CLM-{str(uuid.uuid4())[:8].upper()}"
        log.info("create_claim: %s tipo=%s user=%s", claim_id, tipo, user_id)
        now = datetime.now(timezone.utc).isoformat()
        # CLAIM canónico en su propio namespace
        biz_table.put_item(Item={
            "PK":          f"CLAIM#{claim_id}",
            "SK":          "#METADATA",
            "claim_id":    claim_id,
            "user_id":     user_id,
            "tipo":        tipo,
            "descripcion": descripcion,
            "pnr":         pnr,
            "status":      "RECIBIDO",
            "created_at":  now,
        })
        # Thin pointer en USER#
        biz_table.put_item(Item={
            "PK":          f"USER#{user_id}",
            "SK":          f"CLAIM#{claim_id}",
            "claim_id":    claim_id,
            "tipo":        tipo,
            "status":      "RECIBIDO",
            "pnr":         pnr,
            "created_at":  now,
        })
        _emit_event("reclamo_iniciado", {"claim_id": claim_id, "tipo": tipo, "pnr": pnr}, user_id)
        return json.dumps({
            "ok": True, "claim_id": claim_id,
            "mensaje": f"Reclamo registrado con código {claim_id}. Te contactaremos en 48-72 horas hábiles.",
        })

    if name == "list_saved_passengers":
        log.info("list_saved_passengers: user=%s", user_id)
        # Ahora los pasajeros guardados se buscan via las reservas del user (PASSENGER CRM
        # vive en su propio namespace y se accede vía DNI/email; el chat sólo expone los
        # nombres derivados de las reservas pasadas).
        resp = biz_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk":     f"USER#{user_id}",
                ":prefix": "RESERVATION#",
            },
            Limit=20,
        )
        names_seen = {}
        for r in resp.get("Items", []):
            n = r.get("passenger_name") or ""
            if not n:
                continue
            if n in names_seen:
                names_seen[n] += 1
            else:
                names_seen[n] = 1
        if not names_seen:
            return json.dumps({
                "pasajeros": [],
                "mensaje":   "No tenés pasajeros guardados. Al completar una reserva, el pasajero se guarda automáticamente.",
            })
        pasajeros = [
            {"passenger_name": n, "reservas": c}
            for n, c in sorted(names_seen.items(), key=lambda x: -x[1])
        ]
        return json.dumps({"pasajeros": pasajeros})

    if name == "create_reservation":
        if not SF_ARN:
            return json.dumps({"error": "Procesador de pagos no configurado"})

        tarifa = inputs.get("tarifa", "BASIC")
        extras = inputs.get("extras", []) or []
        # Validar tarifa y extras antes de iniciar el Saga (fail-fast).
        # El total real lo computa el server con el precio del inventory.
        try:
            validate_inputs(tarifa, extras)
        except PricingError as e:
            return json.dumps({"error": f"Datos de reserva inválidos: {e}"})

        # Validar formato de datos del pasajero (DNI, nombre, teléfono, DOB, sexo).
        # Server-side authoritative: como tokenizamos PII hacia Anthropic, Claude
        # solo ve placeholders y no puede validar los valores reales.
        pax_err = _validate_passenger_input(inputs)
        if pax_err:
            return json.dumps({"error": pax_err})

        # Normalizar DNI (sacar puntos/espacios/guiones que pudo haber tipeado el user)
        dni_clean = (inputs.get("dni") or "").replace(".", "").replace(" ", "").replace("-", "")

        payment_id = str(uuid.uuid4())
        pnr = _pnr_from_payment_id(payment_id)
        reservation_data = {
            "origen":           inputs["origen"].upper(),
            "destino":          inputs["destino"].upper(),
            "fecha":            inputs["fecha"],
            "pasajeros":        int(inputs.get("pasajeros", 1)),
            "tarifa":           tarifa,
            "extras":           extras,
            "seat_id":          (inputs.get("seat_id") or "").upper(),
            "email_contacto":   user_email or inputs.get("email_contacto", ""),
            "telefono":         inputs.get("telefono", ""),
            "nombre_pasajero":  inputs.get("nombre_pasajero", "").strip(),
            "dni":              dni_clean,
            "fecha_nacimiento": inputs.get("fecha_nacimiento", "").strip(),
            "sexo":             inputs.get("sexo", "").strip(),
            "vuelo_numero":     inputs.get("vuelo_numero", ""),
        }

        log.info("create_reservation: %s→%s %s pnr=%s user=%s",
                 reservation_data["origen"], reservation_data["destino"],
                 reservation_data["fecha"], pnr, user_id)

        try:
            sf.start_execution(
                stateMachineArn=SF_ARN,
                name=payment_id,
                input=json.dumps({
                    "payment_id":  payment_id,
                    "pnr":         pnr,
                    "user_id":     user_id,
                    "reservation": reservation_data,
                }),
            )
            return json.dumps({
                "procesando": True,
                "payment_id": payment_id,
                "pnr":        pnr,
                "mensaje":    f"Reserva {pnr} en proceso. Aparecerá en 'Mis Reservas' en unos segundos.",
            })
        except Exception as e:
            log.error("Error iniciando Step Functions: %s", e)
            return json.dumps({"error": f"No se pudo procesar la reserva: {str(e)}"})

    if name == "escalate_to_human":
        reason  = inputs.get("reason", "sin motivo especificado")
        urgency = inputs.get("urgency", "medium")
        handoff_id = f"HO-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(timezone.utc).isoformat()
        ttl = int(time.time()) + HANDOFF_TTL_SECONDS

        log.info("escalate_to_human: handoff=%s urgency=%s user=%s", handoff_id, urgency, user_id)

        # Item del ticket (conversation-scoped)
        conv_table.put_item(Item={
            "PK":          f"SESSION#{session_id}",
            "SK":          f"HANDOFF#{now}#{handoff_id}",
            "handoff_id":  handoff_id,
            "session_id":  session_id,
            "user_id":     user_id,
            "reason":      reason,
            "urgency":     urgency,
            "status":      "QUEUED",
            "created_at":  now,
            "ttl":         ttl,
        })
        # Thin pointer por user
        conv_table.put_item(Item={
            "PK":          f"USER#{user_id}",
            "SK":          f"HANDOFF#{handoff_id}",
            "handoff_id":  handoff_id,
            "session_id":  session_id,
            "status":      "QUEUED",
            "urgency":     urgency,
            "created_at":  now,
            "ttl":         ttl,
        })

        # Publicamos al topic central `events` con event_type=handoff_requested.
        # El fan-out a la cola human-handoff lo hace el topic via filter policy —
        # si el call center está caído, el mensaje queda esperando en la cola.
        handoff_dict = {
            "handoff_id": handoff_id,
            "session_id": session_id,
            "user_id":    user_id,
            "reason":     reason,
            "urgency":    urgency,
            "created_at": now,
        }
        try:
            sns.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=f"handoff_requested — {handoff_id}",
                Message=json.dumps(handoff_dict) + "\n",
                MessageAttributes={
                    "event_type": {"DataType": "String", "StringValue": "handoff_requested"},
                },
            )
        except Exception as e:
            log.error("SNS publish handoff_requested falló: %s", e)
            return json.dumps({
                "ok": False,
                "ticket_id": handoff_id,
                "mensaje": "Registramos tu pedido pero hubo un problema enviándolo al call center. Te contactaremos lo antes posible.",
            })

        _emit_event("handoff_escalated", {
            "handoff_id": handoff_id,
            "urgency":    urgency,
            "reason":     reason,
        }, user_id)

        return json.dumps({
            "ok":        True,
            "ticket_id": handoff_id,
            "mensaje": (
                f"Tu pedido fue derivado al equipo de soporte humano (ticket {handoff_id}). "
                f"Te van a contactar al email registrado según prioridad ({urgency})."
            ),
        })

    return json.dumps({"error": f"Tool desconocida: {name}"})


# ── Route handlers ────────────────────────────────────────────────────────────

def _handle_chat(event: dict, user: dict) -> dict:
    try:
        body    = json.loads(event.get("body") or "{}")
        message = body["message"]
    except (KeyError, json.JSONDecodeError):
        return _response(400, {"error": "Se requiere el campo message"})

    if not message or not str(message).strip():
        return _response(400, {"error": "El mensaje no puede estar vacío"})

    session_id = body.get("session_id") or str(uuid.uuid4())

    user_id = _user_id(user)
    _upsert_user_profile(user)
    history = _get_history(session_id, user_id)
    _save_message(session_id, user_id, "user", message)

    messages = history + [{"role": "user", "content": message}]

    # Metadata acumulada para el frontend (countdown del hold).
    response_metadata: dict = {}

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            # Tokenizar PII en todos los mensajes user antes de mandar a Anthropic.
            # El system prompt no contiene PII (info de negocio). Los tool_results
            # quedan en cleartext porque Claude necesita razonar sobre datos del
            # vuelo (no PII directa) — mitigación parcial pero significativa.
            tokenized_messages = _tokenize_messages_for_anthropic(messages, session_id)
            resp = _anthropic_client.messages.create(
                model      = "claude-haiku-4-5-20251001",
                max_tokens = 1024,
                system     = _build_system_prompt(),
                messages   = tokenized_messages,
                tools      = TOOLS,
            )

            if resp.stop_reason != "tool_use":
                break

            tool_calls   = []
            content_list = []
            tool_results = []
            for b in resp.content:
                if b.type == "tool_use":
                    tool_calls.append(b)
                    content_list.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                elif hasattr(b, "text"):
                    content_list.append({"type": "text", "text": b.text})

            for tc in tool_calls:
                # Detokenizar los args: el handler necesita los valores reales para
                # validar y persistir. Claude vio tokens pero el server resuelve.
                real_input = detokenize_inputs(tc.input, session_id, conv_table)
                result = _execute_tool(tc.name, real_input, user_id, session_id, user.get("email", ""))
                log.info("Tool %s → %s", tc.name, result[:120])
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tc.id,
                    "content":     result,
                })
                # Extraer metadata de hold para el frontend
                _update_hold_metadata(response_metadata, tc.name, result)

            _save_message(session_id, user_id, "assistant", content_list)
            _save_message(session_id, user_id, "user", tool_results)

            messages.append({"role": "assistant", "content": content_list})
            messages.append({"role": "user",      "content": tool_results})

        assistant_text = next(
            (b.text for b in resp.content if hasattr(b, "text")),
            "Lo siento, no pude completar la consulta en este momento. Por favor intentá de nuevo."
        )
        if resp.stop_reason == "tool_use":
            log.warning("Claude agotó %d rondas de tool use sin texto final", MAX_TOOL_ROUNDS)

    except Exception as e:
        log.error("Error Anthropic API: %s", e)
        return _response(502, {"error": "Servicio de IA no disponible"})

    # Claude pudo haber repetido tokens en su respuesta natural ("Tu DNI <DNI_xxx>").
    # Antes de mostrar al user (y de persistir el historial) resolvemos los tokens.
    assistant_text = detokenize_string(assistant_text, session_id, conv_table)

    options = []
    match = re.search(r'\[OPCIONES:\s*([^\]]+)\]', assistant_text, re.IGNORECASE)
    if match:
        options = [o.strip() for o in match.group(1).split('|') if o.strip()]
        assistant_text = assistant_text[:match.start()].rstrip()

    _save_message(session_id, user_id, "assistant", assistant_text)
    _emit_event("chat_message", {"session_id": session_id, "message_length": len(message)}, user_id)

    body_out = {"response": assistant_text, "session_id": session_id, "options": options}
    if response_metadata:
        body_out["metadata"] = response_metadata
    return _response(200, body_out)


def _tokenize_messages_for_anthropic(messages: list, session_id: str) -> list:
    """
    Tokeniza PII en el `content` de cada mensaje user antes de mandarlo a Anthropic.
    Preserva la estructura de tool_use / tool_result intactos (no son texto plano).
    Los mensajes assistant nunca contienen PII generada por el LLM que no haya
    venido del input — pero los procesamos por consistencia (idempotente).
    """
    out = []
    for m in messages:
        content = m.get("content")
        new_content = content
        if isinstance(content, str):
            new_content = tokenize_text(content, session_id, conv_table, PII_TOKEN_SECRET)
        elif isinstance(content, list):
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    block = {**block, "text": tokenize_text(block.get("text", ""), session_id, conv_table, PII_TOKEN_SECRET)}
                # tool_use / tool_result quedan tal cual — los inputs ya están
                # tokenizados desde la conversación anterior; los results no los
                # tokenizamos por ahora (roadmap).
                new_content.append(block)
        out.append({**m, "content": new_content})
    return out


def _update_hold_metadata(metadata: dict, tool_name: str, raw_result: str) -> None:
    """
    Mira el JSON de las tools que afectan el hold y actualiza el dict metadata
    que se devolverá al frontend para que arme/cierre el countdown.
    """
    try:
        r = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(r, dict):
        return

    if tool_name == "hold_seat" and r.get("ok"):
        metadata["hold"] = {
            "seat_id":          r["seat_id"],
            "expires_at_epoch": r["expires_at_epoch"],
            "vuelo_numero":     r.get("vuelo_numero"),
            "fecha":            r.get("fecha"),
        }
        metadata.pop("hold_cleared", None)
    elif tool_name == "check_hold_status":
        if r.get("status") == "still_held":
            metadata["hold"] = {
                "seat_id":          r["seat_id"],
                "expires_at_epoch": r["expires_at_epoch"],
                "vuelo_numero":     r.get("vuelo_numero"),
                "fecha":            r.get("fecha"),
            }
            metadata.pop("hold_cleared", None)
        else:
            metadata["hold_cleared"] = True
            metadata.pop("hold", None)
    elif tool_name == "release_hold" and r.get("ok"):
        metadata["hold_cleared"] = True
        metadata.pop("hold", None)
    elif tool_name == "create_reservation" and r.get("procesando"):
        metadata["hold_cleared"] = True
        metadata.pop("hold", None)


def _handle_reservations(user: dict) -> dict:
    user_id = _user_id(user)
    resp    = biz_table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk":     f"USER#{user_id}",
            ":prefix": "RESERVATION#",
        },
        ScanIndexForward=False,
        Limit=20,
    )
    reservations = [
        {
            "reservation_id": i.get("pnr"),
            "pnr":            i.get("pnr"),
            "vuelo_numero":   i.get("vuelo_numero", "—"),
            "origen":         i.get("origen", "—"),
            "destino":        i.get("destino", "—"),
            "fecha":          i.get("fecha", "—"),
            "pasajeros":      int(i.get("pasajeros", 1)),
            "status":         i.get("status", "CONFIRMADA"),
        }
        for i in resp.get("Items", [])
    ]
    return _response(200, {"reservations": reservations})


def _handle_payment(event: dict, user: dict) -> dict:
    try:
        reservation = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "JSON inválido"})

    if not SF_ARN:
        return _response(503, {"error": "Procesador de pagos no configurado"})

    user_id    = _user_id(user)
    payment_id = str(uuid.uuid4())

    try:
        sf.start_execution(
            stateMachineArn=SF_ARN,
            name=payment_id,
            input=json.dumps({
                "payment_id":  payment_id,
                "user_id":     user_id,
                "reservation": reservation,
            }),
        )
    except Exception as e:
        log.error("Error iniciando Step Functions: %s", e)
        return _response(502, {"error": "Procesador de pagos no disponible"})

    return _response(202, {"payment_id": payment_id, "status": "PROCESANDO"})


# ── Handler principal ─────────────────────────────────────────────────────────

def handler(event, context):
    method = event.get("httpMethod", "")
    path   = event.get("path", "")

    log.info("%s %s", method, path)

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    if method == "GET" and path == "/health":
        return _response(200, {"status": "ok"})

    try:
        user = _get_user(event)
    except Exception as e:
        return _response(401, {"error": str(e)})

    if method == "POST" and path == "/api/chat":
        return _handle_chat(event, user)

    if method == "GET" and path == "/api/reservations":
        return _handle_reservations(user)

    if method == "POST" and path == "/api/payment":
        return _handle_payment(event, user)

    return _response(404, {"error": "Ruta no encontrada"})
