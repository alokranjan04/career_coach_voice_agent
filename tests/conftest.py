"""Shared test setup: fake config, an in-memory bucket, and stand-ins for every
external service (Gemini Live, Vobiz, Razorpay, Twilio, Gmail, Sheets), so the
unit tests run offline and in CI without secrets."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(
    GEMINI_API_KEY="test-key", GOOGLE_SPREADSHEET_ID="sheet", GMAIL_USER="coach@example.com",
    GMAIL_APP_PASSWORD="x", VOBIZ_AUTH_ID="MA_TEST", VOBIZ_AUTH_TOKEN="t", VOBIZ_FROM_NUMBER="918065481243",
    SESSIONS_BUCKET="test-bucket", SESSION_SECRET="test-secret", FIREBASE_WEB_API_KEY="AIzaTest",
    RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="rzp_secret", RAZORPAY_WEBHOOK_SECRET="whsec",
    TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tw", TWILIO_WHATSAPP_FROM="whatsapp:+14155238886",
    TWILIO_CONTENT_SID="HX1", CALL_COOLDOWN_SECS="0", MAX_CALLS_PER_DAY="50", RESEARCH_ENABLED="1",
    PUBLIC_URL="http://testserver")
os.chdir(ROOT)

import json  # noqa: E402

import pytest  # noqa: E402

from agent import auth, integrations, pipeline, quality, resources, store, vobiz_api, whatsapp  # noqa: E402


class World:
    """Everything the fakes recorded during a test."""

    def __init__(self):
        self.db, self.rows, self.mails, self.whatsapp, self.placed, self.hung = {}, [], [], [], [], []


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(store, "_read", lambda p: json.loads(w.db[p]) if p in w.db else None)
    monkeypatch.setattr(store, "_write", lambda p, d: w.db.__setitem__(p, json.dumps(d)))
    monkeypatch.setattr(store, "_delete", lambda p: w.db.pop(p, None))
    monkeypatch.setattr(store, "_list", lambda prefix: sorted(k for k in w.db if k.startswith(prefix)))
    monkeypatch.setattr(store, "get_json", store._read)
    monkeypatch.setattr(store, "put_json", store._write)
    monkeypatch.setattr(integrations, "append_row", lambda tab, h, r: w.rows.append((tab, dict(zip(h, r)))))
    monkeypatch.setattr(integrations, "read_rows", lambda tab: [r for t, r in w.rows if t == tab])

    def delete_rows_where(tab, keep):
        before = len(w.rows)
        w.rows[:] = [(t, r) for t, r in w.rows if t != tab or keep(r)]
        return before - len(w.rows)
    monkeypatch.setattr(integrations, "delete_rows_where", delete_rows_where)
    monkeypatch.setattr(integrations, "send_email", lambda to, s, b: w.mails.append((to, s, b)) or True)
    monkeypatch.setattr(vobiz_api, "place_call", lambda to, a, h, time_limit=0: w.placed.append((to, a, h)) or "req")
    monkeypatch.setattr(vobiz_api, "hangup", lambda uuid: w.hung.append(uuid))
    monkeypatch.setattr(vobiz_api, "call_cost_inr", lambda uuid: 1.35)
    monkeypatch.setattr(vobiz_api, "balance_inr", lambda: 650.0)
    monkeypatch.setattr(whatsapp, "send_summary",
                        lambda phone, name, s, e, links: w.whatsapp.append((phone, s.get("goal"))) or "SM1")
    monkeypatch.setattr(resources, "find_resources", lambda s, r, usage=None: {
        "videos": [{"title": "Talk", "url": "https://www.youtube.com/watch?v=abcdefghijk", "why": "fits"}],
        "learning": [], "reading": [], "people": [], "people_search": "https://www.linkedin.com/search"})
    monkeypatch.setattr(quality, "score_transcript", lambda t, r="", usage=None: {
        "scores": {k: 4 for k in quality.RUBRIC}, "overall": 4, "issues": [], "best_moment": "good"})
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    return w


def verified_cookie(phone: str) -> dict:
    """Cookie header for a verified number, as /api/verify would set it."""
    from aiohttp import web
    resp = web.Response()
    auth.set_cookie(resp, phone)
    return {auth.COOKIE: resp.cookies[auth.COOKIE].value}
