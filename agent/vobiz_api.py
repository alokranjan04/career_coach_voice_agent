"""Vobiz REST API: place an outbound call, hang it up, verify credentials.
Docs: https://www.vobiz.ai/docs/call/make-call.md. All blocking."""
import requests

from agent import config

API = "https://api.vobiz.ai/api/v1"


class CallError(Exception):
    pass


def _headers() -> dict:
    return {"X-Auth-ID": config.VOBIZ_AUTH_ID, "X-Auth-Token": config.VOBIZ_AUTH_TOKEN,
            "Content-Type": "application/json"}


def place_call(to: str, answer_url: str, hangup_url: str) -> str:
    """Start a call; returns Vobiz's request_uuid. The path is case-sensitive:
    a lowercase /account/.../call/ returns 401."""
    if not (config.VOBIZ_AUTH_ID and config.VOBIZ_AUTH_TOKEN and config.VOBIZ_FROM_NUMBER):
        raise CallError("Calling isn't set up yet.")
    body = {"from": config.VOBIZ_FROM_NUMBER, "to": to,
            "answer_url": answer_url, "answer_method": "POST",
            "hangup_url": hangup_url, "hangup_method": "POST",
            # ring_timeout: give up if unanswered. NOT hangup_on_ring, which ends
            # the call that many seconds after ringing starts, even once answered.
            "ring_timeout": config.RING_SECS,
            "time_limit": config.MAX_CALL_SECS + 60,
            "caller_name": "Disha Career Coach"}
    r = requests.post(f"{API}/Account/{config.VOBIZ_AUTH_ID}/Call/", json=body,
                      headers=_headers(), timeout=15)
    if r.status_code == 402:
        raise CallError("Our calling balance is low. Please try again later.")
    if r.status_code == 429:
        raise CallError("We're placing a lot of calls right now. Please try again in a minute.")
    if r.status_code >= 300:
        print(f"[vobiz] make call failed {r.status_code}: {r.text[:300]}", flush=True)
        raise CallError("We couldn't place the call. Please check the number and try again.")
    return r.json().get("request_uuid", "")


def hangup(call_uuid: str) -> None:
    if not call_uuid:
        return
    r = requests.delete(f"{API}/Account/{config.VOBIZ_AUTH_ID}/Call/{call_uuid}/",
                        headers=_headers(), timeout=10)
    if r.status_code not in (204, 404):
        print(f"[vobiz] hangup {call_uuid} returned {r.status_code}: {r.text[:200]}", flush=True)


def check_credentials() -> dict:
    """GET /auth/me: 200 means the ID and token pair is valid."""
    r = requests.get(f"{API}/auth/me", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()
