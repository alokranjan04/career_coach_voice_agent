"""Verify every configured value for real before deploying or calling.

    python scripts/check_env.py

Checks: placeholders, Gemini Live session (catches invalid key AND depleted
prepaid credits — a model-list request passes even when calls will fail),
Gmail SMTP login, Google Sheet access. Prints only lengths/prefixes, never secrets.
"""
import asyncio
import json
import smtplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import config  # noqa: E402

PLACEHOLDER_HINTS = ("your_", "xxx", "changeme", "<", "placeholder", "example")
ok = True


def report(name: str, passed: bool, detail: str = "") -> None:
    global ok
    ok &= passed
    print(f"{'PASS' if passed else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")


def check_placeholders() -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_SPREADSHEET_ID", "GMAIL_USER", "GMAIL_APP_PASSWORD"):
        value = config.env(name)
        if value and any(h in value.lower() for h in PLACEHOLDER_HINTS):
            report(name, False, f"looks like a placeholder (len={len(value)})")
    extra = [p.name for p in config.ROOT.glob(".env.*") if p.name != ".env.example"]
    if extra:
        print(f"WARN  other env files present {extra} — the app ignores them; "
              "do not build deploy settings from them")


async def check_gemini() -> None:
    import websockets
    if not config.GEMINI_API_KEY:
        return report("Gemini", False, "GEMINI_API_KEY not set")
    try:
        async with websockets.connect(config.GEMINI_WS_URL, open_timeout=15) as ws:
            await ws.send(json.dumps({"setup": {
                "model": config.AGENT.get("model"),
                "generationConfig": {"responseModalities": ["AUDIO"]}}}))
            first = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if "setupComplete" not in first:
                return report("Gemini", False, f"setup rejected: {first}")
            await ws.send(json.dumps({"realtimeInput": {"text": "Say hello."}}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
            report("Gemini Live", "serverContent" in reply, "session answered" if "serverContent" in reply else str(reply)[:200])
    except Exception as e:  # 1007 invalid key / 1011 credits depleted arrive here
        report("Gemini Live", False, str(e)[:300])


def check_smtp() -> None:
    if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD):
        return print("SKIP  Gmail (not configured)")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        report("Gmail SMTP login", True, config.GMAIL_USER)
    except Exception as e:  # "Connection unexpectedly closed" = revoked app password
        report("Gmail SMTP login", False, f"{type(e).__name__}: {e}")


def check_sheet() -> None:
    if not config.SPREADSHEET_ID:
        return print("SKIP  Google Sheet (not configured)")
    try:
        from agent.integrations import _sheets
        meta = _sheets().spreadsheets().get(spreadsheetId=config.SPREADSHEET_ID).execute()
        report("Google Sheet", True, meta["properties"]["title"])
    except Exception as e:  # 403 = not shared with this identity
        report("Google Sheet", False, f"{str(e)[:250]} (locally this uses your gcloud ADC "
               "identity; on Cloud Run it is the service account)")


if __name__ == "__main__":
    check_placeholders()
    asyncio.run(check_gemini())
    check_smtp()
    check_sheet()
    sys.exit(0 if ok else 1)
