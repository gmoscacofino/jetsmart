import os, json, uuid, time, logging, base64, re
from datetime import datetime, timezone, date, timedelta

import boto3
import anthropic

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION               = os.environ["AWS_REGION_VAR"]
TABLE_NAME           = os.environ["DYNAMODB_TABLE_NAME"]
SNS_TOPIC_ARN        = os.environ["SNS_TOPIC_ARN"]
SECRET_ARN           = os.environ["ANTHROPIC_SECRET_ARN"]
SF_ARN               = os.environ.get("STEP_FUNCTIONS_ARN", "")
SYSTEM_PROMPT_BUCKET = os.environ["SYSTEM_PROMPT_BUCKET"]
SYSTEM_PROMPT_KEY    = os.environ["SYSTEM_PROMPT_KEY"]
MOCK_MODE            = os.environ.get("MOCK_MODE", "false").lower() == "true"
FRONTEND_URL         = os.environ.get("FRONTEND_URL", "*")
COGNITO_POOL_ID      = os.environ.get("COGNITO_USER_POOL_ID", "")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table    = dynamodb.Table(TABLE_NAME)
sns      = boto3.client("sns", region_name=REGION)
sm       = boto3.client("secretsmanager", region_name=REGION)
sf       = boto3.client("stepfunctions", region_name=REGION)
s3       = boto3.client("s3", region_name=REGION)

MAX_HISTORY     = 40
MSG_TTL_SECONDS = 7 * 24 * 3600
MAX_TOOL_ROUNDS = 5

# Inicialización eager: ocurre en el cold start, no en el primer request.
# El contenedor ya tiene el cliente y el prompt listos cuando llega el primer mensaje.
def _init_anthropic() -> anthropic.Anthropic:
    secret  = sm.get_secret_value(SecretId=SECRET_ARN)
    api_key = json.loads(secret["SecretString"])["api_key"]
    return anthropic.Anthropic(api_key=api_key)

def _load_system_prompt() -> str:
    obj = s3.get_object(Bucket=SYSTEM_PROMPT_BUCKET, Key=SYSTEM_PROMPT_KEY)
    return obj["Body"].read().decode("utf-8")

_anthropic_client  = None if MOCK_MODE else _init_anthropic()
_raw_system_prompt = ""   if MOCK_MODE else _load_system_prompt()

_system_prompt_cache: dict = {}


def _build_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%A %d de %B de %Y")
    if _system_prompt_cache.get("date") != today:
        _system_prompt_cache["date"]   = today
        _system_prompt_cache["prompt"] = f"Fecha de hoy (UTC): {today}\n\n{_raw_system_prompt}"
    return _system_prompt_cache["prompt"]


# ── Auth ──────────────────────────────────────────────────────────────────────

def _parse_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT inválido")
    try:
        padding = 4 - len(parts[1]) % 4
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * padding))
    except Exception:
        raise ValueError("JWT inválido")
    if payload.get("exp", 0) < time.time():
        raise ValueError("Token expirado")
    if COGNITO_POOL_ID:
        expected_iss = f"https://cognito-idp.{REGION}.amazonaws.com/{COGNITO_POOL_ID}"
        if payload.get("iss") != expected_iss:
            raise ValueError("Token de emisor no reconocido")
        if payload.get("token_use") not in ("id", "access"):
            raise ValueError("Tipo de token inválido")
    return payload


def _get_user(event: dict) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth    = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ValueError("Falta el Bearer token")
    return _parse_jwt(auth[7:].strip())


def _user_id(user: dict) -> str:
    return user.get("sub", "anonymous")


# ── DynamoDB ──────────────────────────────────────────────────────────────────

def _upsert_user_profile(user: dict):
    user_id = _user_id(user)
    try:
        table.update_item(
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
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk":     f"SESSION#{session_id}",
            ":prefix": "MSG#",
        },
        ScanIndexForward=False,   # más recientes primero para aplicar Limit correctamente
        Limit=MAX_HISTORY,
    )
    messages = []
    for i in reversed(resp.get("Items", [])):  # revertir a orden cronológico
        # Descartar mensajes de otras sesiones robadas: si el item tiene user_id
        # y no coincide con el token actual, la sesión no pertenece a este usuario.
        if user_id and i.get("user_id") and i["user_id"] != user_id:
            log.warning("Session %s owned by %s accessed by %s — clearing history",
                        session_id, i["user_id"], user_id)
            return []
        content = i["content"]
        if i.get("content_type") == "tool":
            content = json.loads(content)
        messages.append({"role": i["role"], "content": content})

    # Si un request anterior falló después de guardar el mensaje del usuario pero antes
    # de guardar la respuesta del asistente, el historial termina con dos "user" seguidos.
    # La API de Anthropic requiere roles alternados, así que descartamos el final inválido.
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
    table.put_item(Item={
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
            Message=json.dumps({
                "event_type": event_type,
                "user_id":    user_id,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "payload":    payload,
            }),
            Subject=event_type,
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
# Claude puede invocar estas herramientas para obtener datos reales antes de
# formular su respuesta. El modelo decide cuándo usarlas según la pregunta.
#
# En producción estas funciones llamarían a la API interna de JetSmart:
#   search_flights  → GET https://api-interna.jetsmart.com/availability
#   get_reservation → GET https://api-interna.jetsmart.com/reservations/{id}
#
# En esta implementación consultan DynamoDB, que actúa como fuente de datos.

TOOLS = [
    {
        "name": "list_flight_dates",
        "description": (
            "Lista todas las fechas con vuelos disponibles entre dos ciudades. "
            "Usar PRIMERO cuando el usuario no especifica una fecha concreta y quiere saber "
            "cuándo puede volar (ej: 'quiero ir a Mendoza', 'qué días hay vuelos a Santiago'). "
            "Devuelve las fechas disponibles con precio base. "
            "Luego usar search_flights para el detalle de una fecha específica."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen": {
                    "type":        "string",
                    "description": "Código IATA del aeropuerto de origen (ej: AEP, SCL, COR, MDZ)",
                },
                "destino": {
                    "type":        "string",
                    "description": "Código IATA del aeropuerto de destino",
                },
            },
            "required": ["origen", "destino"],
        },
    },
    {
        "name": "search_flights",
        "description": (
            "Busca el detalle de un vuelo entre dos ciudades en una fecha concreta. "
            "Devuelve número de vuelo, horarios, precio por pasajero y asientos disponibles. "
            "Usar cuando el usuario ya eligió una fecha específica, o para confirmar disponibilidad "
            "antes de iniciar el flujo de compra."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen": {
                    "type":        "string",
                    "description": "Código IATA del aeropuerto de origen (ej: AEP, SCL, COR, MDZ, IGR, ANF, LIM)",
                },
                "destino": {
                    "type":        "string",
                    "description": "Código IATA del aeropuerto de destino",
                },
                "fecha": {
                    "type":        "string",
                    "description": "Fecha del vuelo en formato YYYY-MM-DD",
                },
                "pasajeros": {
                    "type":        "integer",
                    "description": "Cantidad de pasajeros (por defecto 1)",
                },
            },
            "required": ["origen", "destino", "fecha"],
        },
    },
    {
        "name": "get_reservation",
        "description": (
            "Consulta el estado de una reserva existente del usuario. "
            "Usar cuando el usuario pregunte por el estado, detalles o historial de una reserva."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type":        "string",
                    "description": "ID de la reserva, formato RES-XXXXXXXX",
                },
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "list_user_reservations",
        "description": (
            "Lista todas las reservas del usuario autenticado. "
            "Usar cuando el usuario pregunte por sus vuelos, quiera hacer check-in, "
            "ver el estado de sus reservas, o gestionar un viaje existente. "
            "No requiere ningún parámetro."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "check_in",
        "description": (
            "Realiza el check-in de una reserva confirmada. "
            "Usar cuando el usuario quiera hacer check-in para su vuelo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "string",
                    "description": "Código de reserva formato RES-XXXXXXXX",
                },
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "get_boarding_pass",
        "description": (
            "Obtiene el boarding pass de una reserva con check-in realizado. "
            "Usar cuando el usuario pida su tarjeta de embarque."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "string",
                    "description": "Código de reserva formato RES-XXXXXXXX",
                },
            },
            "required": ["reservation_id"],
        },
    },
    {
        "name": "create_claim",
        "description": (
            "Registra un reclamo sobre un vuelo o reserva. "
            "Usar para equipaje perdido/dañado, vuelo demorado/cancelado, reembolsos u otros problemas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "string",
                    "description": "Código de reserva relacionado (opcional)",
                },
                "tipo": {
                    "type": "string",
                    "description": "Tipo de reclamo: equipaje_perdido | equipaje_daniado | vuelo_demorado | vuelo_cancelado | reembolso | otro",
                },
                "descripcion": {
                    "type": "string",
                    "description": "Descripción detallada del problema",
                },
            },
            "required": ["tipo", "descripcion"],
        },
    },
    {
        "name": "list_saved_passengers",
        "description": (
            "Lista los pasajeros guardados del usuario a partir de reservas anteriores. "
            "Usar cuando el usuario quiera usar un pasajero ya registrado para una nueva reserva, "
            "o cuando pregunte si tiene pasajeros guardados."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "create_reservation",
        "description": (
            "Crea una reserva real en el sistema y dispara el proceso de pago. "
            "Llamar ÚNICAMENTE cuando el usuario haya completado TODOS los pasos del flujo de compra "
            "y haya confirmado explícitamente que quiere proceder. "
            "Esta herramienta genera la reserva real — no inventar IDs de reserva manualmente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen": {
                    "type": "string",
                    "description": "Código IATA del aeropuerto de origen (ej: AEP)",
                },
                "destino": {
                    "type": "string",
                    "description": "Código IATA del aeropuerto de destino (ej: MDZ)",
                },
                "fecha": {
                    "type": "string",
                    "description": "Fecha del vuelo en formato YYYY-MM-DD",
                },
                "pasajeros": {
                    "type": "integer",
                    "description": "Cantidad de pasajeros",
                },
                "tarifa": {
                    "type": "string",
                    "description": "Tarifa elegida: BASIC, LIGHT, SMART o FULL FLEX",
                },
                "total": {
                    "type": "number",
                    "description": "Precio total de la reserva en USD",
                },
                "email_contacto": {
                    "type": "string",
                    "description": "Email de contacto del pasajero",
                },
                "telefono": {
                    "type": "string",
                    "description": "Teléfono de contacto del pasajero",
                },
                "nombre_pasajero": {
                    "type": "string",
                    "description": "Nombre completo del pasajero principal (nombre + apellido)",
                },
            },
            "required": ["origen", "destino", "fecha", "pasajeros", "tarifa", "total", "email_contacto"],
        },
    },
]


def _execute_tool(name: str, inputs: dict, user_id: str) -> str:
    """
    Ejecuta la herramienta solicitada por Claude y devuelve el resultado como JSON string.

    Punto de extensión: para integrar la API real de JetSmart, reemplazar las
    consultas a DynamoDB por llamadas HTTP a la API interna con su token de auth.
    """
    if name == "list_flight_dates":
        origen  = inputs["origen"].upper()
        destino = inputs["destino"].upper()

        log.info("list_flight_dates: %s→%s", origen, destino)

        resp = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk":     f"FLIGHT#{origen}#{destino}",
                ":prefix": "DATE#",
            },
            ScanIndexForward=True,
        )
        items = resp.get("Items", [])

        if not items:
            return json.dumps({
                "disponible": False,
                "mensaje":    f"No hay vuelos disponibles de {origen} a {destino}.",
            })

        fechas = [
            {
                "fecha":               i["SK"].replace("DATE#", ""),
                "vuelo":               i.get("vuelo_numero"),
                "precio_desde":        float(i.get("precio", 0)),
                "asientos_disponibles": int(i.get("asientos_disponibles", 0)),
            }
            for i in items
            if int(i.get("asientos_disponibles", 0)) > 0
        ]

        return json.dumps({
            "origen":  origen,
            "destino": destino,
            "fechas":  fechas,
        })

    if name == "search_flights":
        origen    = inputs["origen"].upper()
        destino   = inputs["destino"].upper()
        fecha     = inputs["fecha"]
        pasajeros = int(inputs.get("pasajeros", 1))

        log.info("search_flights: %s→%s %s (%d pax)", origen, destino, fecha, pasajeros)

        resp = table.get_item(
            Key={"PK": f"FLIGHT#{origen}#{destino}", "SK": f"DATE#{fecha}"}
        )
        item = resp.get("Item")

        if not item:
            return json.dumps({
                "disponible": False,
                "mensaje":    f"No hay vuelos de {origen} a {destino} el {fecha}.",
            })

        asientos = int(item.get("asientos_disponibles", 0))
        if asientos < pasajeros:
            return json.dumps({
                "disponible": False,
                "mensaje":    (
                    f"Vuelo {item['vuelo_numero']} encontrado pero sin asientos suficientes "
                    f"(disponibles: {asientos}, solicitados: {pasajeros})."
                ),
            })

        _emit_event("busqueda_vuelo", {
            "origen":   origen,
            "destino":  destino,
            "fecha":    fecha,
            "pasajeros": pasajeros,
            "ruta":     f"{origen}-{destino}",
        }, user_id)

        return json.dumps({
            "disponible":          True,
            "vuelo":               item["vuelo_numero"],
            "origen":              origen,
            "destino":             destino,
            "fecha":               fecha,
            "hora_salida":         item.get("hora_salida"),
            "hora_llegada":        item.get("hora_llegada"),
            "duracion":            item.get("duracion"),
            "precio_por_pasajero": float(item["precio"]),
            "precio_total":        float(item["precio"]) * pasajeros,
            "asientos_disponibles": asientos,
            "aerolinea":           item.get("aerolinea", "JetSmart"),
        })

    if name == "get_reservation":
        reservation_id = inputs["reservation_id"]
        log.info("get_reservation: %s (user: %s)", reservation_id, user_id)

        resp = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{reservation_id}"}
        )
        item = resp.get("Item")

        if not item:
            return json.dumps({
                "encontrada": False,
                "mensaje":    f"No se encontró la reserva {reservation_id}.",
            })

        return json.dumps({
            "encontrada":     True,
            "reservation_id": reservation_id,
            "origen":         item.get("origin"),
            "destino":        item.get("destination"),
            "fecha":          item.get("flight_date"),
            "pasajeros":      item.get("passenger_count"),
            "status":         item.get("status"),
            "total":          float(item.get("total", 0)),
        }, default=str)

    if name == "list_user_reservations":
        log.info("list_user_reservations: user=%s", user_id)
        resp = table.query(
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
                "reservation_id": i.get("reservation_id"),
                "vuelo":          i.get("flight_number", "—"),
                "origen":         i.get("origin", "—"),
                "destino":        i.get("destination", "—"),
                "fecha":          i.get("flight_date", "—"),
                "pasajeros":      int(i.get("passenger_count", 1)),
                "tarifa":         i.get("tarifa", "—"),
                "status":         i.get("status", "—"),
                "total":          float(i.get("total", 0)),
            }
            for i in items
        ]
        return json.dumps({"reservas": reservas}, default=str)

    if name == "check_in":
        reservation_id = inputs["reservation_id"]
        log.info("check_in: %s user=%s", reservation_id, user_id)
        resp = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{reservation_id}"}
        )
        item = resp.get("Item")
        if not item:
            return json.dumps({"ok": False, "mensaje": f"No se encontró la reserva {reservation_id}."})
        if item.get("status") == "CHECK-IN":
            return json.dumps({"ok": True, "ya_realizado": True, "mensaje": "Ya tenés check-in realizado para esta reserva."})
        if item.get("status") not in ("CONFIRMADA",):
            return json.dumps({"ok": False, "mensaje": f"No se puede hacer check-in: la reserva está en estado {item.get('status')}."})
        flight_dt = date.fromisoformat(item["flight_date"])
        today = date.today()
        if flight_dt < today:
            return json.dumps({"ok": False, "mensaje": "No podés hacer check-in para un vuelo que ya pasó."})
        if flight_dt > today + timedelta(days=1):
            return json.dumps({"ok": False, "mensaje": f"El check-in abre 24 horas antes del vuelo. Tu vuelo es el {item['flight_date']}."})
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{reservation_id}"},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": "CHECK-IN"},
        )
        _emit_event("checkin_realizado", {
            "reservation_id": reservation_id,
            "flight_number":  item.get("flight_number", ""),
            "origin":         item.get("origin", ""),
            "destination":    item.get("destination", ""),
            "flight_date":    item.get("flight_date", ""),
        }, user_id)
        return json.dumps({
            "ok":             True,
            "reservation_id": reservation_id,
            "vuelo":          item.get("flight_number", "—"),
            "origen":         item.get("origin", "—"),
            "destino":        item.get("destination", "—"),
            "fecha":          item.get("flight_date", "—"),
            "mensaje":        "Check-in realizado correctamente. Ya podés obtener tu boarding pass.",
        })

    if name == "get_boarding_pass":
        reservation_id = inputs["reservation_id"]
        log.info("get_boarding_pass: %s user=%s", reservation_id, user_id)
        resp = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"RESERVATION#{reservation_id}"}
        )
        item = resp.get("Item")
        if not item:
            return json.dumps({"ok": False, "mensaje": f"No se encontró la reserva {reservation_id}."})
        if item.get("status") not in ("CHECK-IN",):
            return json.dumps({"ok": False, "mensaje": "Necesitás hacer check-in antes de obtener el boarding pass."})
        return json.dumps({
            "ok":             True,
            "boarding_pass": {
                "reservation_id": reservation_id,
                "pasajero":       item.get("passenger_name", "Pasajero"),
                "vuelo":          item.get("flight_number", "—"),
                "origen":         item.get("origin", "—"),
                "destino":        item.get("destination", "—"),
                "fecha":          item.get("flight_date", "—"),
                "asiento":        item.get("seat", "ALEATORIO"),
                "grupo":          "B",
                "puerta":         "12",
                "embarque":       "45 min antes de la salida",
            },
        })

    if name == "create_claim":
        tipo        = inputs["tipo"]
        descripcion = inputs["descripcion"]
        res_id      = inputs.get("reservation_id", "")
        claim_id    = f"CLM-{str(uuid.uuid4())[:8].upper()}"
        log.info("create_claim: %s tipo=%s user=%s", claim_id, tipo, user_id)
        from datetime import datetime, timezone as tz
        table.put_item(Item={
            "PK":             f"USER#{user_id}",
            "SK":             f"CLAIM#{claim_id}",
            "claim_id":       claim_id,
            "tipo":           tipo,
            "descripcion":    descripcion,
            "reservation_id": res_id,
            "status":         "RECIBIDO",
            "created_at":     datetime.now(tz.utc).isoformat(),
        })
        _emit_event("reclamo_iniciado", {
            "claim_id":       claim_id,
            "tipo":           tipo,
            "reservation_id": res_id,
        }, user_id)
        return json.dumps({
            "ok":       True,
            "claim_id": claim_id,
            "mensaje":  f"Reclamo registrado con código {claim_id}. Te contactaremos en 48-72 horas hábiles.",
        })

    if name == "list_saved_passengers":
        log.info("list_saved_passengers: user=%s", user_id)
        resp = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk":     f"USER#{user_id}",
                ":prefix": "PASSENGER#",
            },
        )
        items = resp.get("Items", [])
        if not items:
            return json.dumps({
                "pasajeros": [],
                "mensaje":   "No tenés pasajeros guardados. Al completar una reserva, el pasajero se guarda automáticamente.",
            })
        pasajeros = [
            {
                "passenger_name": i.get("passenger_name", ""),
                "email":          i.get("email", ""),
                "phone":          i.get("phone", ""),
                "reservas":       int(i.get("reservation_count", 1)),
            }
            for i in items
        ]
        return json.dumps({"pasajeros": pasajeros})

    if name == "create_reservation":
        if not SF_ARN:
            return json.dumps({"error": "Procesador de pagos no configurado"})

        payment_id = str(uuid.uuid4())
        reservation_data = {
            "origen":           inputs["origen"].upper(),
            "destino":          inputs["destino"].upper(),
            "fecha":            inputs["fecha"],
            "pasajeros":        int(inputs.get("pasajeros", 1)),
            "tarifa":           inputs.get("tarifa", "BASIC"),
            "total":            float(inputs.get("total", 0)),
            "email_contacto":   inputs.get("email_contacto", ""),
            "telefono":         inputs.get("telefono", ""),
            "nombre_pasajero":  inputs.get("nombre_pasajero", ""),
        }

        log.info("create_reservation: %s→%s %s user=%s",
                 reservation_data["origen"], reservation_data["destino"],
                 reservation_data["fecha"], user_id)

        try:
            sf.start_execution(
                stateMachineArn=SF_ARN,
                name=payment_id,
                input=json.dumps({
                    "payment_id":  payment_id,
                    "user_id":     user_id,
                    "reservation": reservation_data,
                }),
            )
            return json.dumps({
                "procesando": True,
                "payment_id": payment_id,
                "mensaje":    "La reserva está siendo procesada. Aparecerá en 'Mis Reservas' en unos segundos.",
            })
        except Exception as e:
            log.error("Error iniciando Step Functions desde create_reservation: %s", e)
            return json.dumps({"error": f"No se pudo procesar la reserva: {str(e)}"})

    return json.dumps({"error": f"Tool desconocida: {name}"})


# ── Mock mode ─────────────────────────────────────────────────────────────────
# Devuelve respuestas predefinidas sin llamar a Anthropic API.
# Activo cuando MOCK_MODE=true — permite probar el flujo completo sin API key.

def _mock_chat_response(message: str) -> tuple:
    msg = message.lower()
    if any(w in msg for w in ["vuelo", "volar", "buscar", "quiero ir", "viajar", "destino"]):
        return (
            "Encontré vuelos disponibles ✈️\n\n"
            "**FO 1234** — Buenos Aires (AEP) → Santiago (SCL)\n"
            "📅 15 de junio de 2026 | 08:30 → 10:45 hs\n"
            "💰 $89 USD por pasajero | 14 asientos disponibles\n\n"
            "¿Querés reservar este vuelo?",
            ["Sí, reservar", "Ver otras fechas", "Cambiar destino"],
        )
    if any(w in msg for w in ["reservar", "confirmar", "comprar", "sí", "si"]):
        return (
            "¡Reserva confirmada! 🎉\n\n"
            "📋 Código: **RES-DEMO0001**\n"
            "✈️ FO 1234 — AEP → SCL\n"
            "📅 15 de junio de 2026 | 08:30 hs\n"
            "💳 Total: $89 USD\n\n"
            "Podés hacer check-in a partir de 24 hs antes del vuelo.",
            ["Hacer check-in", "Ver mis reservas", "Volver al inicio"],
        )
    if any(w in msg for w in ["check-in", "checkin", "check in"]):
        return (
            "Check-in realizado exitosamente ✅\n\n"
            "**RES-DEMO0001** | FO 1234 — AEP → SCL | 15 jun 2026\n\n"
            "Ya podés obtener tu boarding pass.",
            ["Obtener boarding pass", "Ver mis reservas"],
        )
    if any(w in msg for w in ["boarding", "tarjeta", "embarque", "pass"]):
        return (
            "🎫 **Boarding Pass**\n\n"
            "Pasajero: DEMO USER\n"
            "Vuelo: FO 1234 — AEP → SCL\n"
            "📅 15 jun 2026 | Salida 08:30\n"
            "Asiento: **14A** | Grupo B | Puerta 12\n"
            "Embarque: 45 min antes de la salida",
            ["Volver al inicio"],
        )
    if any(w in msg for w in ["reserva", "mis reservas"]):
        return (
            "📋 **Tus reservas:**\n\n"
            "1. **RES-DEMO0001** — FO 1234 AEP→SCL\n"
            "   📅 15 jun 2026 | Estado: CONFIRMADA\n\n"
            "¿Qué querés hacer?",
            ["Hacer check-in", "Ver boarding pass"],
        )
    if any(w in msg for w in ["reclamo", "problema", "queja", "perdido", "dañado", "cancelado"]):
        return (
            "Tu reclamo fue registrado 📝\n\n"
            "Código: **CLM-DEMO001**\n"
            "Te contactaremos en 48-72 horas hábiles.",
            ["Volver al inicio"],
        )
    return (
        "¡Hola! Soy el asistente virtual de JetSmart ✈️ *(modo demo)*\n\n"
        "Puedo ayudarte con:\n"
        "• Buscar y reservar vuelos\n"
        "• Consultar tus reservas\n"
        "• Hacer check-in\n"
        "• Obtener tu boarding pass\n"
        "• Gestionar reclamos\n\n"
        "¿Con qué te ayudo?",
        ["Buscar vuelos", "Mis reservas", "Hacer check-in"],
    )


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

    if MOCK_MODE:
        assistant_text, options = _mock_chat_response(message)
        _save_message(session_id, user_id, "assistant", assistant_text)
        return _response(200, {"response": assistant_text, "session_id": session_id, "options": options})

    messages = history + [{"role": "user", "content": message}]

    try:
        # Bucle de tool use: Claude puede consultar datos reales antes de responder.
        # Cada iteración: Claude responde con tool_use → ejecutamos → devolvemos resultado.
        # El bucle termina cuando Claude devuelve texto (stop_reason != "tool_use").
        for _ in range(MAX_TOOL_ROUNDS):
            resp = _anthropic_client.messages.create(
                model      = "claude-haiku-4-5-20251001",
                max_tokens = 1024,
                system     = _build_system_prompt(),
                messages   = messages,
                tools      = TOOLS,
            )

            if resp.stop_reason != "tool_use":
                break

            # Iterar una sola vez: clasificar bloques del asistente y preparar tool calls
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
                result = _execute_tool(tc.name, tc.input, user_id)
                log.info("Tool %s → %s", tc.name, result[:120])
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tc.id,
                    "content":     result,
                })

            # Persistir la ronda de tool use en DynamoDB (evita pérdida de contexto)
            _save_message(session_id, user_id, "assistant", content_list)
            _save_message(session_id, user_id, "user", tool_results)

            # Agregar al historial en memoria: turno asistente + resultados
            messages.append({"role": "assistant", "content": content_list})
            messages.append({"role": "user",      "content": tool_results})

        # Extraer texto final; default si Claude agotó las rondas de tool use sin responder
        assistant_text = next(
            (b.text for b in resp.content if hasattr(b, "text")),
            "Lo siento, no pude completar la consulta en este momento. Por favor intentá de nuevo."
        )
        if resp.stop_reason == "tool_use":
            log.warning("Claude agotó %d rondas de tool use sin texto final", MAX_TOOL_ROUNDS)

    except Exception as e:
        log.error("Error Anthropic API: %s", e)
        return _response(502, {"error": "Servicio de IA no disponible"})

    # Extraer opciones estructuradas del response antes de guardarlo
    options = []
    match = re.search(r'\[OPCIONES:\s*([^\]]+)\]', assistant_text, re.IGNORECASE)
    if match:
        options = [o.strip() for o in match.group(1).split('|') if o.strip()]
        assistant_text = assistant_text[:match.start()].rstrip()

    _save_message(session_id, user_id, "assistant", assistant_text)
    _emit_event("chat_message", {"session_id": session_id, "message_length": len(message)}, user_id)

    return _response(200, {"response": assistant_text, "session_id": session_id, "options": options})


def _handle_reservations(user: dict) -> dict:
    user_id = _user_id(user)
    resp    = table.query(
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
            "reservation_id": i.get("reservation_id"),
            "flight_number":  i.get("flight_number", "—"),
            "origin":         i.get("origin", "—"),
            "destination":    i.get("destination", "—"),
            "date":           i.get("flight_date", "—"),
            "passengers":     int(i.get("passenger_count", 1)),
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
