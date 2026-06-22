"""
Adaptador FastAPI para ejecutar el handler de Lambda (chat_handler.py) en ECS Fargate
detrás de un ALB HTTP.

Diseño: adaptador THIN. No reimplementa lógica de negocio. Por cada request HTTP:
  1. Valida el JWT de Cognito en proceso (reemplaza el Cognito Authorizer de API Gateway).
  2. Sintetiza un `event` estilo Lambda-proxy.
  3. Llama `chat_handler.handler(event, None)`.
  4. Traduce el dict {statusCode, headers, body} de vuelta a una Response de FastAPI.

`chat_handler` hace init eager en el import (lee el secret de Anthropic desde Secrets
Manager y varias env vars). ECS inyecta esas env vars en runtime, así que importar
chat_handler acá funciona sin duplicar esa lógica.
"""
import os
import json
import logging

import jwt
import requests
from jwt.algorithms import RSAAlgorithm
from fastapi import FastAPI, Request, Response

# El import de chat_handler dispara el cold-start eager init (Secrets Manager + env vars).
# Debe ocurrir DESPUÉS de que el proceso tenga las env vars (ECS las provee en runtime).
import chat_handler

log = logging.getLogger("chat-handler-adapter")
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
    """Devuelve {kid: clave_publica}. Cachea en _jwks_cache; refresca si force=True."""
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
    Verifica firma RS256, exp e iss. Verifica aud sólo si COGNITO_CLIENT_ID está seteado
    (los ID tokens de Cognito traen `aud`; los access tokens traen `client_id` en su lugar).
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


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="JetSmart Chat Handler Adapter")


def _lambda_response_to_fastapi(result: dict) -> Response:
    """Traduce el dict Lambda-proxy {statusCode, headers, body} a una Response de FastAPI."""
    status = int(result.get("statusCode", 500))
    body = result.get("body", "")
    headers = dict(result.get("headers") or {})
    # Aseguramos CORS en todas las respuestas (el handler ya los incluye, pero somos defensivos).
    for k, v in CORS_HEADERS.items():
        headers.setdefault(k, v)
    # No dejamos que un Content-Length viejo se filtre; FastAPI lo recalcula.
    headers.pop("Content-Length", None)
    headers.pop("content-length", None)
    return Response(
        content=body,
        status_code=status,
        headers=headers,
        media_type="application/json",
    )


def _cors_preflight_response() -> Response:
    return Response(content="", status_code=200, headers=CORS_HEADERS)


def _unauthorized() -> Response:
    return Response(
        content=json.dumps({"error": "unauthorized"}),
        status_code=401,
        headers={**CORS_HEADERS, "Content-Type": "application/json"},
        media_type="application/json",
    )


async def _build_event(request: Request, path: str, claims: dict) -> dict:
    """Sintetiza el event estilo Lambda-proxy que chat_handler.handler espera."""
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8") if raw_body else None
    qsp = dict(request.query_params) or None
    return {
        "httpMethod": request.method,
        "path": path,
        "headers": dict(request.headers),
        "queryStringParameters": qsp,
        "body": body_str,
        "requestContext": {"authorizer": {"claims": claims}},
    }


async def _dispatch(request: Request, path: str) -> Response:
    """Auth + sintetizar event + invocar el handler de Lambda + traducir la respuesta."""
    # Preflight CORS: sin auth.
    if request.method == "OPTIONS":
        return _cors_preflight_response()

    try:
        claims = _validate_token(request.headers.get("authorization", ""))
    except AuthError as e:
        log.info("Auth rechazada: %s", e)
        return _unauthorized()
    except Exception as e:
        # Fallo inesperado validando (ej. JWKS no alcanzable) → 401 conservador.
        log.warning("Error inesperado en auth: %s", e)
        return _unauthorized()

    event = await _build_event(request, path, claims)

    try:
        result = chat_handler.handler(event, None)
    except Exception:
        log.exception("chat_handler.handler lanzó una excepción")
        return Response(
            content=json.dumps({"error": "internal_server_error"}),
            status_code=500,
            headers={**CORS_HEADERS, "Content-Type": "application/json"},
            media_type="application/json",
        )

    if not isinstance(result, dict) or "statusCode" not in result:
        log.error("chat_handler.handler devolvió una forma inesperada: %r", result)
        return Response(
            content=json.dumps({"error": "internal_server_error"}),
            status_code=500,
            headers={**CORS_HEADERS, "Content-Type": "application/json"},
            media_type="application/json",
        )

    return _lambda_response_to_fastapi(result)


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> Response:
    """Health check para el ALB. Sin auth — invoca el handler con el path /health."""
    event = {
        "httpMethod": "GET",
        "path": "/health",
        "headers": {},
        "queryStringParameters": None,
        "body": None,
        "requestContext": {},
    }
    try:
        result = chat_handler.handler(event, None)
        return _lambda_response_to_fastapi(result)
    except Exception:
        log.exception("health check falló")
        return Response(
            content=json.dumps({"status": "error"}),
            status_code=500,
            media_type="application/json",
        )


# Catch-all para /api/* — el handler enruta internamente por (httpMethod, path).
@app.api_route("/api/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def api(request: Request, path: str) -> Response:
    return await _dispatch(request, f"/api/{path}")
