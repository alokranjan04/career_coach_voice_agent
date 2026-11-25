"""Phone verification. The browser signs in with an SMS OTP (Identity Platform /
Firebase Auth); the server checks the resulting ID token once and then remembers
the verified number in a signed cookie, so the same number isn't asked again."""
import base64
import hashlib
import hmac
import time

from aiohttp import web

from agent import config

COOKIE = "cc_verified"


def verify_id_token(id_token: str) -> str:
    """Return the verified phone number (+91XXXXXXXXXX) from a Firebase ID token."""
    import google.auth.transport.requests
    from google.oauth2 import id_token as gid
    claims = gid.verify_firebase_token(id_token, google.auth.transport.requests.Request(),
                                       audience=config.GCP_PROJECT)
    phone = claims.get("phone_number", "")
    if not phone:
        raise ValueError("token has no phone number")
    return phone


def _sign(payload: str) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def set_cookie(response: web.StreamResponse, phone_digits: str) -> None:
    payload = f"{phone_digits}|{int(time.time())}"
    value = base64.urlsafe_b64encode(f"{payload}|{_sign(payload)}".encode()).decode()
    response.set_cookie(COOKIE, value, max_age=config.SESSION_DAYS * 86400, httponly=True,
                        secure=not config.PUBLIC_URL.startswith("http://"), samesite="Lax", path="/")


def verified_phone(request: web.Request) -> str:
    """91XXXXXXXXXX if the request carries a valid verification cookie, else ""."""
    raw = request.cookies.get(COOKIE, "")
    if not raw or not config.SESSION_SECRET:
        return ""
    try:
        phone, issued, sig = base64.urlsafe_b64decode(raw.encode()).decode().split("|")
    except Exception:
        return ""
    if not hmac.compare_digest(sig, _sign(f"{phone}|{issued}")):
        return ""
    if time.time() - int(issued) > config.SESSION_DAYS * 86400:
        return ""
    return phone
