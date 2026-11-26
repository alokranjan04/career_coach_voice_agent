"""Voice OTP: Ava's Vobiz number calls the user and reads out a 6-digit code.

Replaces SMS OTP (Identity Platform), whose messages weren't reaching Indian
phones. Records live in the bucket so any instance can answer the call:
  otp/<phone>.json      code, expiry, attempts, recent send times
  otp_ip/<hash>.json    recent sends per client IP
  otp_day/<date>.json   sends today, for the global cap
The code never appears in a URL: the call's answer URL carries a random,
single-use token that maps to the phone. All blocking.
"""
import hmac
import secrets
import time

from agent import config, store

TTL_SECS = 600
MAX_ATTEMPTS = 5
PER_PHONE_HOUR, PER_PHONE_DAY = 3, 6
PER_IP_HOUR = 10
DAILY_CAP = int(config.env("OTP_DAILY_CAP", "300"))


class OtpError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _recent(times: list, window: float) -> list:
    now = time.time()
    return [t for t in times if now - t < window]


def start(phone: str, ip: str) -> str:
    """Check limits, create a code, and return the token for the answer URL."""
    rec = store.get_json(f"otp/{phone}.json") or {}
    sends = _recent(rec.get("sends", []), 86400)
    if len(_recent(sends, 3600)) >= PER_PHONE_HOUR or len(sends) >= PER_PHONE_DAY:
        raise OtpError("Too many code calls to this number. Please try again later.", 429)
    if sends and time.time() - sends[-1] < 30:
        raise OtpError("We just called you with a code. Please wait 30 seconds before asking again.", 429)
    ip_path = f"otp_ip/{store.phone_hash(ip or 'unknown')}.json"
    ip_sends = _recent(store.get_json(ip_path) or [], 3600)
    if len(ip_sends) >= PER_IP_HOUR:
        raise OtpError("Too many code requests from this network. Please try again later.", 429)
    day_path = f"otp_day/{store.today_ist()}.json"
    day = store.get_json(day_path) or {"count": 0}
    if day["count"] >= DAILY_CAP:
        raise OtpError("Verification is busy right now. Please try again later.", 503)

    code = f"{secrets.randbelow(10**6):06d}"
    token = secrets.token_urlsafe(16)
    store.put_json(f"otp/{phone}.json", {"code": code, "token": token, "expires": time.time() + TTL_SECS,
                                         "attempts": 0, "spoken": False, "sends": sends + [time.time()]})
    store.put_json(f"otp_token/{token}.json", {"phone": phone, "expires": time.time() + TTL_SECS})
    store.put_json(ip_path, ip_sends + [time.time()])
    day["count"] += 1
    store.put_json(day_path, day)
    return token


def code_for_call(token: str):
    """The code to read out, once per token (the answer URL is single-use)."""
    if not token or not token.replace("-", "").replace("_", "").isalnum():
        return None
    t = store.get_json(f"otp_token/{token}.json")
    if not t or t.get("expires", 0) < time.time():
        return None
    rec = store.get_json(f"otp/{t['phone']}.json") or {}
    if rec.get("token") != token or rec.get("spoken"):
        return None
    rec["spoken"] = True
    store.put_json(f"otp/{t['phone']}.json", rec)
    store._delete(f"otp_token/{token}.json")
    return rec["code"]


def answer_xml(code: str) -> str:
    digits = ", ".join(code)  # commas make the voice pause between digits
    line = f"Your Tell Your Journey verification code is: {digits}."
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Speak voice="WOMAN" language="en-IN">Hello. {line} I repeat. {line}</Speak>'
            '<Wait length="1"/><Speak voice="WOMAN" language="en-IN">Goodbye.</Speak><Hangup/></Response>')


def verify(phone: str, code: str) -> None:
    """Raise OtpError unless the code is right; a correct code can't be reused."""
    rec = store.get_json(f"otp/{phone}.json") or {}
    if not rec.get("code") or rec.get("expires", 0) < time.time():
        raise OtpError("That code has expired. Please request a new one.")
    if rec.get("attempts", 0) >= MAX_ATTEMPTS:
        raise OtpError("Too many wrong attempts. Please request a new code.", 429)
    if not hmac.compare_digest(rec["code"], str(code).strip()):
        rec["attempts"] = rec.get("attempts", 0) + 1
        store.put_json(f"otp/{phone}.json", rec)
        raise OtpError("That code isn't right. Please check and try again.")
    store.put_json(f"otp/{phone}.json", {"sends": rec.get("sends", [])})  # keep only the rate-limit history
