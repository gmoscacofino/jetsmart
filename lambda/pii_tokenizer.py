"""
PII tokenizer — protege PII del usuario antes de salir a la API de Anthropic.

Patrón:
  1. Antes de mandar mensajes a Claude → tokenize_text() reemplaza PII por placeholders.
  2. Cuando Claude llama una tool con args → detokenize_inputs() resuelve placeholders
     al valor real antes de ejecutar el handler.

Mapping {token → valor_real} se guarda por sesión en `conversations` DynamoDB:
    PK = SESSION#<sid>
    SK = TOKEN#<token>
    Atributos: kind, value, ttl (epoch)

Los tokens son determinísticos por sesión (HMAC del valor con clave por-sesión),
así el mismo email aparece como mismo token en todos los mensajes — Claude puede
razonar sobre identidad sin ver el valor.

Lo que NO hacemos:
- Tokenizar tool_results (sería deseable, queda como roadmap).
- Tokenizar nombres propios (regex confiable para nombres es difícil).
- Usar AWS Comprehend (más cobertura pero +latencia y +costo).
"""
import os
import re
import hmac
import hashlib
import logging
from datetime import datetime, timezone

log = logging.getLogger()

PII_TOKEN_TTL_SECONDS = 24 * 3600  # 24 h, suficiente para una sesión de compra

# ── Regex de detección ────────────────────────────────────────────────────────
# Email RFC simplificado.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# DNI argentino: 7-8 dígitos. Los números de vuelo son JA### (con letra al inicio),
# no matchean. Los precios son ≤6 dígitos. Boundary lookbehind sin ancho variable
# para evitar matchear partes de fechas/teléfonos.
_DNI_RE = re.compile(r"\b\d{7,8}\b")

# Teléfono argentino flexible: +54 9 11 1234-5678 o variantes. Mínimo 8 dígitos.
_PHONE_RE = re.compile(
    r"(?:\+?54\s*9?\s*)?(?:\(?\d{2,4}\)?[\s\-]?)?\d{4}[\s\-]?\d{4}"
)

# Fecha YYYY-MM-DD (ISO) o DD/MM/YYYY (LATAM).
_DATE_ISO_RE = re.compile(r"\b(19\d{2}|20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_DATE_LATAM_RE = re.compile(r"\b(0[1-9]|[12]\d|3[01])/(0[1-9]|1[0-2])/(19\d{2}|20\d{2})\b")

# Sexo / género escrito por user.
_SEXO_RE = re.compile(
    r"\b(?:Masculino|Femenino|Otro|masculino|femenino|otro|Hombre|Mujer|hombre|mujer|M|F|X)\b"
)


# ── Helpers de token ──────────────────────────────────────────────────────────

def _make_token(kind: str, value: str, session_id: str, secret: str) -> str:
    """
    Token determinístico por (kind, value, session_id).
    Mismo (kind, value) en la misma sesión → mismo token (idempotencia).
    Sessions distintas → tokens distintos (no se filtra info entre users).
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{kind}|{value}|{session_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:10]
    return f"<{kind}_{mac}>"


def _normalize_sexo(s: str) -> str:
    s_lower = s.lower()
    if s_lower in ("masculino", "hombre", "m"):
        return "Masculino"
    if s_lower in ("femenino", "mujer", "f"):
        return "Femenino"
    return "Otro"


def _normalize_date(s: str) -> str:
    """Normaliza dd/mm/yyyy → yyyy-mm-dd."""
    m = _DATE_LATAM_RE.fullmatch(s)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm}-{dd}"
    return s


# ── Token table API ───────────────────────────────────────────────────────────

def _save_token(conv_table, session_id: str, token: str, kind: str, value: str) -> None:
    """Guarda el mapping token → valor_real con TTL."""
    try:
        conv_table.put_item(Item={
            "PK":         f"SESSION#{session_id}",
            "SK":         f"TOKEN#{token}",
            "token":      token,
            "kind":       kind,
            "value":      value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ttl":        int(datetime.now(timezone.utc).timestamp()) + PII_TOKEN_TTL_SECONDS,
        })
    except Exception as e:
        log.warning("save_token failed (token=%s kind=%s): %s", token, kind, e)


def _resolve_token(conv_table, session_id: str, token: str) -> str | None:
    """Devuelve el valor real para un token, o None si no existe."""
    try:
        resp = conv_table.get_item(
            Key={"PK": f"SESSION#{session_id}", "SK": f"TOKEN#{token}"}
        )
        item = resp.get("Item")
        return item.get("value") if item else None
    except Exception as e:
        log.warning("resolve_token failed (token=%s): %s", token, e)
        return None


# ── API pública ───────────────────────────────────────────────────────────────

def tokenize_text(text: str, session_id: str, conv_table, secret: str) -> str:
    """
    Detecta PII en `text` y la reemplaza por tokens. Persiste los mappings.
    Retorna el texto con placeholders.

    Orden de detección: email → DNI → fecha ISO → fecha LATAM → teléfono → sexo.
    El orden importa porque el regex de teléfono podría matchear partes de DNIs
    o fechas si se aplica antes.
    """
    if not text or not isinstance(text, str):
        return text

    def _sub(kind: str):
        def replacer(match):
            value = match.group(0)
            # Normalización del valor antes de tokenizar (para que el detokenize
            # devuelva la versión que el handler espera).
            stored = value
            if kind == "DATE":
                stored = _normalize_date(value)
            elif kind == "SEXO":
                stored = _normalize_sexo(value)
            elif kind == "EMAIL":
                stored = value.strip().lower()
            elif kind == "DNI":
                stored = value
            token = _make_token(kind, stored, session_id, secret)
            _save_token(conv_table, session_id, token, kind, stored)
            return token
        return replacer

    text = _EMAIL_RE.sub(_sub("EMAIL"), text)
    text = _DNI_RE.sub(_sub("DNI"), text)
    text = _DATE_ISO_RE.sub(_sub("DATE"), text)
    text = _DATE_LATAM_RE.sub(_sub("DATE"), text)
    text = _PHONE_RE.sub(_sub("PHONE"), text)
    text = _SEXO_RE.sub(_sub("SEXO"), text)

    return text


_TOKEN_RE = re.compile(r"<(EMAIL|DNI|DATE|PHONE|SEXO)_[a-f0-9]{10}>")


def detokenize_string(value: str, session_id: str, conv_table) -> str:
    """
    Reemplaza tokens en `value` por sus valores reales (lookup en conv_table).
    Si un token no se encuentra (TTL expired), se deja el placeholder.
    """
    if not value or not isinstance(value, str) or "<" not in value:
        return value

    def repl(match):
        token = match.group(0)
        real = _resolve_token(conv_table, session_id, token)
        return real if real is not None else token

    return _TOKEN_RE.sub(repl, value)


def detokenize_inputs(inputs: dict, session_id: str, conv_table) -> dict:
    """
    Recorre recursivamente un dict y reemplaza tokens en todos los strings.
    Usado para resolver placeholders en los args de tool_use antes de invocar
    el handler real de la tool.
    """
    if isinstance(inputs, dict):
        return {k: detokenize_inputs(v, session_id, conv_table) for k, v in inputs.items()}
    if isinstance(inputs, list):
        return [detokenize_inputs(x, session_id, conv_table) for x in inputs]
    if isinstance(inputs, str):
        return detokenize_string(inputs, session_id, conv_table)
    return inputs
