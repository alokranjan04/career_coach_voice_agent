"""Call requests in Cloud Storage: one JSON object per call, plus per-phone call
history for rate limiting. All blocking — call via asyncio.to_thread.

The bucket has a 1-day delete rule, and the resume text is removed as soon as
the call ends."""
import json
import secrets
import time

from agent import config
from agent.integrations import google_credentials

_BUCKET = {"bucket": None}


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


def create(name: str, email: str, phone: str, language: str, resume: str) -> dict:
    rec = {"sid": secrets.token_hex(6), "token": secrets.token_urlsafe(16), "name": name,
           "email": email, "phone": phone, "language": language, "resume": resume,
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
