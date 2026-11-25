"""Cloud Storage records. All blocking — call via asyncio.to_thread.

  calls/<sid>.json       one call request (deleted by the bucket after 1 day)
  phones/<phone>.json    recent call times, for rate limiting (deleted after 1 day)
  profiles/<phone>.json  kept: name, email, resume and every past call, so a
                         returning user only enters their number
"""
import json
import secrets
import time

from agent import config
from agent.integrations import google_credentials

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
    _write(f"profiles/{profile['phone']}.json", profile)


def append_call_to_profile(phone: str, entry: dict) -> None:
    profile = load_profile(phone)
    if not profile:
        return
    entry["transcript"] = entry.get("transcript", "")[:TRANSCRIPT_CHARS]
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


# ---------- call requests ----------

def create(profile: dict) -> dict:
    rec = {"sid": secrets.token_hex(6), "token": secrets.token_urlsafe(16),
           "name": profile["name"], "email": profile["email"], "phone": profile["phone"],
           "resume": profile["resume"], "history": history_text(profile),
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
