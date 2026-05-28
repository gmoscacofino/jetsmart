"""
Lambda: OAuth2 callback handler.

Cognito redirects to API Gateway GET /callback?code=<auth_code>
This function exchanges the code for tokens and redirects the browser
back to the S3 frontend with the id_token in the URL hash.
"""
import os, json, urllib.request, urllib.parse, base64, logging

log = logging.getLogger()
log.setLevel(logging.INFO)

COGNITO_DOMAIN = os.environ["COGNITO_DOMAIN"]   # e.g. jetsmart-prod.auth.us-east-1.amazoncognito.com
CLIENT_ID      = os.environ["CLIENT_ID"]
CLIENT_SECRET  = os.environ.get("CLIENT_SECRET", "")
CALLBACK_URL   = os.environ["CALLBACK_URL"]     # this Lambda's public URL (API GW)
FRONTEND_URL   = os.environ["FRONTEND_URL"]     # S3 website URL


def handler(event, context):
    path = event.get("path", "")

    if path.endswith("/logout"):
        return _redirect(FRONTEND_URL)

    qs     = event.get("queryStringParameters") or {}
    code   = qs.get("code")
    error  = qs.get("error")
    state  = qs.get("state", "")

    if error:
        return _redirect(f"{FRONTEND_URL}#error={urllib.parse.quote(error)}&state={urllib.parse.quote(state)}")

    if not code:
        return _redirect(f"{FRONTEND_URL}#error=missing_code")

    try:
        tokens = _exchange_code(code)
        id_token = tokens.get("id_token", "")
        return _redirect(f"{FRONTEND_URL}#id_token={id_token}&state={urllib.parse.quote(state)}")
    except Exception as e:
        log.error("Token exchange failed: %s", e)
        return _redirect(f"{FRONTEND_URL}#error=token_exchange_failed&state={urllib.parse.quote(state)}")


def _exchange_code(code: str) -> dict:
    token_endpoint = f"https://{COGNITO_DOMAIN}/oauth2/token"

    body = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "client_id":    CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "code":         code,
    }).encode()

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    if CLIENT_SECRET:
        creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"

    req  = urllib.request.Request(token_endpoint, data=body, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _redirect(location: str) -> dict:
    return {
        "statusCode": 302,
        "headers": {"Location": location},
        "body": "",
    }
