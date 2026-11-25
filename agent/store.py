"""Cloud Storage records. All blocking — call via asyncio.to_thread.

  calls/<sid>.json             one call request (bucket deletes after 1 day)
  phones/<phone>.json          recent call times, for rate limiting (deleted after 1 day)
  profiles/<phone>.json        name, email, resume, plan usage and past calls, so a
                               returning user only enters their number
  consents/<phone>/<ts>.json   consent records (DPDP Act): what was agreed, when, from where
  orders/<order_id>.json       Razorpay orders awaiting payment
  payments/<payment_id>.json   payment ledger, kept for tax law even after a data deletion
  spend/<YYYY-MM-DD>.json      estimated spend per IST day, for the daily cap
  alerts/<key>.json            alert throttle
  deletions/<sha256>.json      proof that a deletion request was carried out (no personal data)
  usage/<sha256>.json          free calls used, under a hash of the number (survives deletion
                               so it can't be reset to get more free calls)
"""
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from agent import config
from agent.integrations import google_credentials

IST = timezone(timedelta(hours=5, minutes=30))
_BUCKET = {"bucket": None}
HISTORY_CHARS = 30000          # cap on past-call text sent to the model
TRANSCRIPT_CHARS = 8000        # per stored call


class RateLimited(Exception):
    pass


def _bucket():
    if _BUCKET["bucket"] is None:
        from google.cloud import storage
        creds, project = google_credentials(["https://www.googleapis.com/auth/devstorage.read_write"])
        _BUCKET["bucket"] = storage.Client(credentials=creds, project=project).bucket(config.SESSIONS_BUCKET)
    return _BUCKET["bucket"]


def _read(path: str):
    blob = _bucket().blob(path)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def _write(path: str, data) -> None:
    _bucket().blob(path).upload_from_string(json.dumps(data, ensure_ascii=False),
                                            content_type="application/json")


def _delete(path: str) -> None:
    blob = _bucket().blob(path)
    if blob.exists():
        blob.delete()


def _list(prefix: str) -> list:
    return [b.name for b in _bucket().list_blobs(prefix=prefix)]


# Generic helpers for other modules (alerts, ledgers).
get_json, put_json = _read, _write


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def phone_hash(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()[:32]


def check_and_record_call(phone: str) -> None:
    """Raise RateLimited if this number was called too recently or too often."""
    path = f"phones/{phone}.json"
    now = time.time()
    history = [t for t in (_read(path) or []) if now - t < 86400]
    if history and now - history[-1] < config.CALL_COOLDOWN_SECS:
        raise RateLimited("We just called this number. Please wait a few minutes before trying again.")
    if len(history) >= config.MAX_CALLS_PER_DAY:
        raise RateLimited("This number has reached today's limit of coaching calls. Please try again tomorrow.")
    _write(path, history + [now])


# ---------- profiles ----------

def load_profile(phone: str) -> dict:
    return _read(f"profiles/{phone}.json") or {}


def save_profile(profile: dict) -> None:
    profile["last_active"] = time.time()
    _write(f"profiles/{profile['phone']}.json", profile)


def append_call_to_profile(phone: str, entry: dict) -> None:
    profile = load_profile(phone)
    if not profile:
        return
    entry["transcript"] = entry.get("transcript", "")[:TRANSCRIPT_CHARS]
    entry["ended_at"] = time.time()
    profile.setdefault("calls", []).append(entry)
    save_profile(profile)


def history_text(profile: dict) -> str:
    """Past calls for the prompt, oldest first. Summaries for all calls,
    transcripts for the most recent ones that fit in HISTORY_CHARS."""
    calls = profile.get("calls", [])
    if not calls:
        return "(none: this is the first call)"
    blocks, budget = [], HISTORY_CHARS
    for i, c in reversed(list(enumerate(calls, 1))):
        head = f"CALL {i}: {c.get('date', '')}, {c.get('duration', 0)} seconds, ended: {c.get('ended', '')}"
        summary = c.get("summary") or {}
        lines = [head]
        if summary:
            lines += [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in summary.items() if str(v).strip()]
        else:
            lines.append("No summary was saved: the call ended before the recap.")
        if c.get("resources_sent"):
            lines.append("Resources emailed after this call: " + "; ".join(c["resources_sent"]))
        transcript = c.get("transcript", "")
        if transcript and budget - len(transcript) > 0:
            lines += ["Transcript:", transcript]
            budget -= len(transcript)
        blocks.append("\n".join(lines))
    return "\n\n".join(reversed(blocks))


# ---------- plan usage: free calls and day passes ----------

def free_calls_used(phone: str) -> int:
    """Kept under a hash of the number, outside the profile, so deleting your data
    doesn't hand out new free calls (disclosed in the privacy policy)."""
    return (_read(f"usage/{phone_hash(phone)}.json") or {}).get("free_calls_used", 0)


def entitlement(phone: str, profile: dict) -> str:
    """'free', 'pass' or 'payment_required' for the next call."""
    if free_calls_used(phone) < config.FREE_CALLS:
        return "free"
    p = active_pass(profile)
    if p and p.get("calls_used", 0) < config.DAY_PASS_CALLS:
        return "pass"
    return "payment_required"


def active_pass(profile: dict):
    now = time.time()
    passes = [p for p in profile.get("passes", []) if p.get("expires_at", 0) > now]
    return passes[-1] if passes else None


def consume_call(phone: str, kind: str) -> None:
    """Count a call against the free allowance or the day pass once it connects."""
    if kind == "free":
        _write(f"usage/{phone_hash(phone)}.json", {"free_calls_used": free_calls_used(phone) + 1})
        return
    profile = load_profile(phone)
    if kind == "pass" and profile and (p := active_pass(profile)):
        p["calls_used"] = p.get("calls_used", 0) + 1
        save_profile(profile)


def grant_day_pass(phone: str, order_id: str, payment_id: str, amount_paise: int) -> bool:
    """Idempotent: the checkout callback and the webhook may both report the payment."""
    if _read(f"payments/{payment_id}.json"):
        return False
    _write(f"payments/{payment_id}.json", {
        "payment_id": payment_id, "order_id": order_id, "amount_paise": amount_paise,
        "phone_hash": phone_hash(phone), "phone_last4": phone[-4:], "paid_at": time.time(),
        "product": "day_pass"})
    profile = load_profile(phone) or {"phone": phone}
    now = time.time()
    profile.setdefault("passes", []).append({
        "order_id": order_id, "payment_id": payment_id, "bought_at": now,
        "expires_at": now + config.DAY_PASS_HOURS * 3600, "calls_used": 0})
    save_profile(profile)
    return True


def save_order(order_id: str, phone: str, amount_paise: int) -> None:
    _write(f"orders/{order_id}.json", {"order_id": order_id, "phone": phone,
                                       "amount_paise": amount_paise, "created": time.time()})


def load_order(order_id: str):
    return _read(f"orders/{order_id}.json") if order_id.replace("_", "").isalnum() else None


# ---------- consent records (DPDP Act) ----------

def record_consent(phone: str, purposes: list, ip: str, user_agent: str) -> None:
    ts = datetime.now(IST).strftime("%Y%m%dT%H%M%S%f")
    _write(f"consents/{phone}/{ts}.json", {
        "phone": phone, "version": config.CONSENT_VERSION, "purposes": purposes,
        "given_at": time.time(), "ip": ip, "user_agent": user_agent[:300]})


def list_consents(phone: str) -> list:
    return [_read(path) for path in _list(f"consents/{phone}/")]


# ---------- data rights: access and erasure ----------

def export_user_data(phone: str) -> dict:
    return {"profile": load_profile(phone), "consents": list_consents(phone),
            "payments": [p for p in (_read(x) for x in _list("payments/"))
                         if p and p.get("phone_hash") == phone_hash(phone)]}


def delete_user_data(phone: str) -> dict:
    """Erase personal data for a number. Payment ledger entries stay (tax law),
    but they hold only a hash and the last 4 digits of the number."""
    removed = 0
    for path in [f"profiles/{phone}.json", f"phones/{phone}.json", *_list(f"consents/{phone}/")]:
        _delete(path)
        removed += 1
    for path in _list("calls/"):
        rec = _read(path) or {}
        if rec.get("phone") == phone:
            _delete(path)
            removed += 1
    _write(f"deletions/{phone_hash(phone)}.json", {"deleted_at": time.time(), "objects": removed})
    return {"objects_deleted": removed}


def retention_cleanup() -> dict:
    """Delete profiles inactive for RETENTION_DAYS, and older call transcripts."""
    cutoff = time.time() - config.RETENTION_DAYS * 86400
    deleted_profiles = pruned_calls = 0
    for path in _list("profiles/"):
        profile = _read(path) or {}
        if profile.get("last_active", 0) < cutoff:
            delete_user_data(profile.get("phone") or path.split("/")[-1].removesuffix(".json"))
            deleted_profiles += 1
            continue
        old = [c for c in profile.get("calls", []) if c.get("ended_at", time.time()) < cutoff]
        if old:
            profile["calls"] = [c for c in profile["calls"] if c not in old]
            _write(path, profile)
            pruned_calls += len(old)
    return {"deleted_profiles": deleted_profiles, "pruned_calls": pruned_calls}


# ---------- spend ledger ----------

def add_spend(components_inr: dict) -> dict:
    path = f"spend/{today_ist()}.json"
    day = _read(path) or {"total_inr": 0.0, "calls": 0, "by_component": {}}
    for k, v in components_inr.items():
        day["by_component"][k] = round(day["by_component"].get(k, 0.0) + v, 4)
        day["total_inr"] = round(day["total_inr"] + v, 4)
    day["calls"] += 1
    _write(path, day)
    return day


def spend_today() -> float:
    return (_read(f"spend/{today_ist()}.json") or {}).get("total_inr", 0.0)


def gemini_spend_this_month() -> float:
    month = today_ist()[:7]
    total = 0.0
    for path in _list(f"spend/{month}"):
        comp = (_read(path) or {}).get("by_component", {})
        total += sum(v for k, v in comp.items() if k.startswith("gemini"))
    return total


# ---------- call requests ----------

def create(profile: dict, kind: str) -> dict:
    rec = {"sid": secrets.token_hex(6), "token": secrets.token_urlsafe(16),
           "name": profile["name"], "email": profile["email"], "phone": profile["phone"],
           "whatsapp": bool(profile.get("whatsapp_opt_in")),
           "resume": profile["resume"], "history": history_text(profile), "entitlement": kind,
           "created": time.time(), "status": "calling", "call_uuid": "", "summary_saved": False}
    _write(f"calls/{rec['sid']}.json", rec)
    return rec


def load(sid: str):
    if not sid.isalnum():
        return None
    return _read(f"calls/{sid}.json")


def update(sid: str, **fields) -> dict:
    rec = load(sid) or {}
    rec.update(fields)
    _write(f"calls/{sid}.json", rec)
    return rec
