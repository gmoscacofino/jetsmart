"""
Servicio FastAPI nativo del chat-handler de JetSmart, en ECS Fargate detrás de un
ALB HTTP.

Diseño: servicio web nativo (NO un adaptador de Lambda). Por cada request:
  1. Valida el JWT de Cognito en proceso (firma RS256 contra el JWKS, issuer, exp).
  2. Rutea método/path a la función de negocio correspondiente en `chat_core`.
  3. Cada función devuelve (status_code, payload) — se traduce a Response HTTP.

`chat_core` hace init eager en el import (lee el secret de Anthropic desde Secrets
Manager y varias env vars). ECS inyecta esas env vars en runtime, así que importar
chat_core acá funciona sin duplicar esa lógica.
"""
import os
import json
import logging

import jwt
import requests
from jwt.algorithms import RSAAlgorithm
from fastapi import FastAPI, Request, Response

# El import de chat_core dispara el cold-start eager init (Secrets Manager + env vars).
# Debe ocurrir DESPUÉS de que el proceso tenga las env vars (ECS las provee en runtime).
import chat_core

log = logging.getLogger("chat-handler")
logging.basicConfig(level=logging.INFO)

# ── Configuración de auth (Cognito) ───────────────────────────────────────────

COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_REGION = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION_VAR", "us-east-1")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")

COGNITO_ISSUER = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
JWKS_URL = f"{COGNITO_ISSUER}/.well-known/jwks.json"

FRONTEND_URL = os.environ.get("FRONTEND_URL") or "*"

CORS_HEADERS = {
    "Access-Control-Allow-Origin": FRONTEND_URL,
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}

# Cache de JWKS en un global de módulo. Se refresca si aparece un `kid` desconocido
# (rotación de claves de Cognito).
_jwks_cache: dict = {}


class AuthError(Exception):
    """Falla de validación del token → 401."""
    pass


def _fetch_jwks(force: bool = False) -> dict:
    """Devuelve {kid: jwk}. Cachea en _jwks_cache; refresca si force=True."""
    global _jwks_cache
    if _jwks_cache and not force:
        return _jwks_cache
    resp = requests.get(JWKS_URL, timeout=5)
    resp.raise_for_status()
    keys = resp.json().get("keys", [])
    _jwks_cache = {k["kid"]: k for k in keys}
    return _jwks_cache


def _public_key_for_kid(kid: str):
    """Resuelve la clave pública para un kid; refresca el JWKS una vez si no aparece."""
    jwks = _fetch_jwks()
    jwk = jwks.get(kid)
    if jwk is None:
        # kid desconocido → posible rotación de claves: refrescar una vez.
        jwks = _fetch_jwks(force=True)
        jwk = jwks.get(kid)
    if jwk is None:
        raise AuthError("kid desconocido en el token")
    return RSAAlgorithm.from_jwk(json.dumps(jwk))


def _validate_token(auth_header: str) -> dict:
    """
    Valida `Authorization: Bearer <jwt>` contra el JWKS de Cognito.
    Verifica firma RS256, exp e iss. Verifica aud sólo si COGNITO_CLIENT_ID está seteado.
    Devuelve el dict de claims decodificado. Lanza AuthError en cualquier fallo.
    """
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise AuthError("Falta header Authorization Bearer")
    token = auth_header.split(" ", 1)[1].strip()

    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as e:
        raise AuthError(f"Header del token inválido: {e}")
    if not kid:
        raise AuthError("Token sin kid")

    key = _public_key_for_kid(kid)

    decode_kwargs = {
        "algorithms": ["RS256"],
        "issuer": COGNITO_ISSUER,
    }
    if COGNITO_CLIENT_ID:
        decode_kwargs["audience"] = COGNITO_CLIENT_ID
    else:
        # Sin client id configurado no validamos aud (cubre access tokens, que no lo traen).
        decode_kwargs["options"] = {"verify_aud": False}

    try:
        claims = jwt.decode(token, key=key, **decode_kwargs)
    except jwt.PyJWTError as e:
        raise AuthError(f"Token inválido: {e}")

    if not claims.get("sub"):
        raise AuthError("Token sin claim 'sub'")
    return claims


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _json_response(status: int, payload: dict) -> Response:
    return Response(
        content=json.dumps(payload),
        status_code=status,
        headers={**CORS_HEADERS, "Content-Type": "application/json"},
        media_type="application/json",
    )


def _cors_preflight() -> Response:
    return Response(content="", status_code=200, headers=CORS_HEADERS)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="JetSmart Chat Handler")


@app.get("/health")
async def health() -> Response:
    return _json_response(200, {"status": "ok"})


@app.api_route("/api/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def api(request: Request, path: str) -> Response:
    # Preflight CORS: sin auth.
    if request.method == "OPTIONS":
        return _cors_preflight()

    # Auth: valida el JWT y obtiene los claims (la identidad del usuario).
    try:
        claims = _validate_token(request.headers.get("authorization", ""))
    except AuthError as e:
        log.info("Auth rechazada: %s", e)
        return _json_response(401, {"error": "unauthorized"})
    except Exception as e:
        # Fallo inesperado validando (ej. JWKS no alcanzable) → 401 conservador.
        log.warning("Error inesperado en auth: %s", e)
        return _json_response(401, {"error": "unauthorized"})

    method = request.method
    route = f"/api/{path}"

    # Body JSON (solo POST). Si viene mal formado → 400.
    body: dict = {}
    if method == "POST":
        raw = await request.body()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return _json_response(400, {"error": "JSON inválido"})
            if not isinstance(parsed, dict):
                return _json_response(400, {"error": "JSON inválido"})
            body = parsed

    # Routing → lógica de negocio nativa en chat_core.
    try:
        if method == "POST" and route == "/api/chat":
            status, payload = chat_core.handle_chat(claims, body)
        elif method == "GET" and route == "/api/reservations":
            status, payload = chat_core.handle_reservations(claims)
        elif method == "POST" and route == "/api/payment":
            status, payload = chat_core.handle_payment(claims, body)
        else:
            return _json_response(404, {"error": "Ruta no encontrada"})
    except Exception:
        log.exception("chat_core lanzó una excepción")
        return _json_response(500, {"error": "internal_server_error"})

    return _json_response(status, payload)
