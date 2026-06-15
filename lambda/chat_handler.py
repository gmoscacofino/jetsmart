import os, json, uuid, time, logging, re
from datetime import datetime, timezone, date, timedelta

import boto3
import anthropic

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
    return claims


def _user_id(user: dict) -> str:
    return user.get("sub", "anonymous")


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
            "Llamar SÓLO cuando el usuario confirmó explícitamente todos los detalles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origen":          {"type": "string"},
                "destino":         {"type": "string"},
                "fecha":           {"type": "string", "description": "YYYY-MM-DD"},
                "pasajeros":       {"type": "integer"},
                "tarifa":          {"type": "string", "description": "BASIC, LIGHT, SMART, FULL FLEX"},
                "total":           {"type": "number"},
                "email_contacto":  {"type": "string"},
                "telefono":        {"type": "string"},
                "nombre_pasajero": {"type": "string"},
                "dni":             {"type": "string", "description": "DNI del pasajero principal (sin puntos)"},
                "vuelo_numero":    {"type": "string", "description": "Número de vuelo (JA203, etc.)"},
            },
            "required": ["origen", "destino", "fecha", "pasajeros", "tarifa", "total", "email_contacto"],
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


def _execute_tool(name: str, inputs: dict, user_id: str, session_id: str = "") -> str:
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
        fechas = []
        for i in items:
            if int(i.get("asientos_disponibles", 0)) > 0:
                sk_parts = i["SK"].split("#")
                fechas.append({
                    "fecha":                sk_parts[1],
                    "vuelo":                i.get("vuelo_numero"),
                    "hora_salida":          i.get("hora_salida"),
                    "precio_desde":         float(i.get("precio", 0)),
                    "asientos_disponibles": int(i.get("asientos_disponibles", 0)),
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
        found = resp.get("Items", [])
        if not found:
            return json.dumps({"disponible": False, "mensaje": f"No hay vuelos de {origen} a {destino} el {fecha}."})
        vuelos = []
        for item in found:
            asientos = int(item.get("asientos_disponibles", 0))
            if asientos >= pasajeros:
                vuelos.append({
                    "vuelo":                item["vuelo_numero"],
                    "hora_salida":          item.get("hora_salida"),
                    "hora_llegada":         item.get("hora_llegada"),
                    "duracion":             item.get("duracion"),
                    "precio_por_pasajero":  float(item["precio"]),
                    "precio_total":         float(item["precio"]) * pasajeros,
                    "asientos_disponibles": asientos,
                    "aerolinea":            item.get("aerolinea", "JetSmart"),
                    "estado_vuelo":         item.get("estado_vuelo", "EN_HORARIO"),
                })
        if not vuelos:
            total = sum(int(i.get("asientos_disponibles", 0)) for i in found)
            return json.dumps({
                "disponible": False,
                "mensaje": (
                    f"Hay vuelo(s) {origen}-{destino} {fecha} sin asientos suficientes "
                    f"para {pasajeros} pax (máx disponible: {total})."
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
            "origen":         item.get("origin"),
            "destino":        item.get("destination"),
            "fecha":          item.get("flight_date"),
            "pasajeros":      item.get("passenger_count"),
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
        flight_dt = date.fromisoformat(item["flight_date"])
        today = date.today()
        if flight_dt < today:
            return json.dumps({"ok": False, "mensaje": "No podés hacer check-in para un vuelo que ya pasó."})
        if flight_dt > today + timedelta(days=1):
            return json.dumps({"ok": False, "mensaje": f"El check-in abre 24 horas antes del vuelo. Tu vuelo es el {item['flight_date']}."})
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
            "flight_number":  item.get("flight_number", ""),
            "origin":         item.get("origin", ""),
            "destination":    item.get("destination", ""),
            "flight_date":    item.get("flight_date", ""),
        }, user_id)
        return json.dumps({
            "ok": True, "reservation_id": pnr,
            "vuelo":  item.get("flight_number", "—"),
            "origen": item.get("origin", "—"),
            "destino": item.get("destination", "—"),
            "fecha":  item.get("flight_date", "—"),
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
        return json.dumps({
            "ok":             True,
            "boarding_pass": {
                "reservation_id": pnr,
                "pasajero":       item.get("passenger_name", "Pasajero"),
                "vuelo":          item.get("flight_number", "—"),
                "origen":         item.get("origin", "—"),
                "destino":        item.get("destination", "—"),
                "fecha":          item.get("flight_date", "—"),
                "asiento":        item.get("seat", "ALEATORIO"),
                "url":            bp.get("bp_url"),
                "grupo":          "B",
                "puerta":         "12",
                "embarque":       "45 min antes de la salida",
            },
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

        payment_id = str(uuid.uuid4())
        reservation_data = {
            "origen":          inputs["origen"].upper(),
            "destino":         inputs["destino"].upper(),
            "fecha":           inputs["fecha"],
            "pasajeros":       int(inputs.get("pasajeros", 1)),
            "tarifa":          inputs.get("tarifa", "BASIC"),
            "total":           float(inputs.get("total", 0)),
            "email_contacto":  inputs.get("email_contacto", ""),
            "telefono":        inputs.get("telefono", ""),
            "nombre_pasajero": inputs.get("nombre_pasajero", ""),
            "dni":             inputs.get("dni", ""),
            "vuelo_numero":    inputs.get("vuelo_numero", ""),
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

        # Encolamos a la SQS — si el call center está caído, queda esperando
        if HUMAN_HANDOFF_QUEUE_URL:
            try:
                sqs.send_message(
                    QueueUrl=HUMAN_HANDOFF_QUEUE_URL,
                    MessageBody=json.dumps({
                        "handoff_id": handoff_id,
                        "session_id": session_id,
                        "user_id":    user_id,
                        "reason":     reason,
                        "urgency":    urgency,
                        "created_at": now,
                    }),
                )
            except Exception as e:
                log.error("SQS send_message human-handoff falló: %s", e)
                return json.dumps({
                    "ok": False,
                    "ticket_id": handoff_id,
                    "mensaje": "Registramos tu pedido pero hubo un problema enviándolo al call center. Te contactaremos lo antes posible.",
                })
        else:
            log.warning("HUMAN_HANDOFF_QUEUE_URL no configurado — handoff sólo persistido en DB")

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

    try:
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
                result = _execute_tool(tc.name, tc.input, user_id, session_id)
                log.info("Tool %s → %s", tc.name, result[:120])
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tc.id,
                    "content":     result,
                })

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
