"""HTTP entry point.

GET  /                 — web app: upload resume and phone number, then get a call
POST /api/call         — validate the form, store the request, place the Vobiz call
GET  /api/call/{sid}   — call status for the web page to poll
POST /answer           — Vobiz answer URL; returns XML that opens the media stream
POST /hangup           — Vobiz hangup URL; marks unanswered calls
GET  /stream           — media WebSocket, one per call (agent/pipeline.py)
GET  /health           — readiness / config sanity (no secrets)
"""
import asyncio
import re
import sys
import time

from aiohttp import web

from agent import config, store, telephony, vobiz_api
from agent.pipeline import call_handler
from agent.resume import ResumeError, extract_text

# Windows consoles default to cp1252 and crash on non-ASCII log lines.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

STATIC = config.ROOT / "static"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def _base_url(request: web.Request) -> str:
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    # Behind Cloud Run / ngrok the app sees plain HTTP; trust the proxy headers.
    proto = request.headers.get("X-Forwarded-Proto", "https" if request.secure else "http")
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{proto}://{host}"


def _indian_mobile(raw: str) -> str:
    """Return 91XXXXXXXXXX for a valid Indian mobile number, else ""."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return "91" + digits if re.fullmatch(r"[6-9]\d{9}", digits) else ""


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def request_call(request: web.Request) -> web.Response:
    try:
        form = await request.post()
    except web.HTTPRequestEntityTooLarge:
        return _error("That file is too large (max 5 MB).", 413)
    phone = _indian_mobile(str(form.get("phone", "")))
    if not phone:
        return _error("Please enter a valid 10-digit Indian mobile number.")
    if form.get("consent") != "yes":
        return _error("Please tick the consent box.")

    # Returning users only give their number: everything else falls back to their
    # saved profile. The page never shows profile data, and the call only goes
    # to that number, so entering someone else's number reveals nothing.
    profile = await asyncio.to_thread(store.load_profile, phone)
    name = re.sub(r"[\x00-\x1f<>{}]", "", str(form.get("name", ""))).strip()[:80] or profile.get("name", "")
    email = str(form.get("email", "")).strip()[:120] or profile.get("email", "")
    upload = form.get("resume")
    resume = profile.get("resume", "")
    if isinstance(upload, web.FileField) and upload.filename:
        try:
            resume = extract_text(upload.filename, upload.file.read())
        except ResumeError as e:
            return _error(str(e), 422)
    if not resume:
        return _error("We don't have a resume for this number yet. Please upload it.")
    if not name:
        return _error("Please enter your name.")
    if not EMAIL_RE.match(email):
        return _error("Please enter a valid email address.")

    try:
        await asyncio.to_thread(store.check_and_record_call, phone)
    except store.RateLimited as e:
        return _error(str(e), 429)
    if resume != profile.get("resume"):
        profile["resume_updated"] = time.time()
    profile.update(phone=phone, name=name, email=email, resume=resume)
    await asyncio.to_thread(store.save_profile, profile)
    rec = await asyncio.to_thread(store.create, profile)
    base = _base_url(request)
    try:
        await asyncio.to_thread(
            vobiz_api.place_call, phone,
            f"{base}/answer?sid={rec['sid']}&token={rec['token']}",
            f"{base}/hangup?sid={rec['sid']}")
    except vobiz_api.CallError as e:
        await asyncio.to_thread(store.update, rec["sid"], status="failed", resume="", token="")
        return _error(str(e), 502)
    print(f"[call] placed sid={rec['sid']} to=…{phone[-4:]} resume_chars={len(resume)} "
          f"previous_calls={len(profile.get('calls', []))}", flush=True)
    return web.json_response({"sid": rec["sid"]})


async def call_status(request: web.Request) -> web.Response:
    rec = await asyncio.to_thread(store.load, request.match_info["sid"])
    if not rec:
        return _error("Unknown call.", 404)
    return web.json_response({k: rec.get(k) for k in ("status", "ended_reason", "summary_saved")})


async def answer(request: web.Request) -> web.Response:
    phone = telephony.get_provider()
    form = await request.post()
    sid, token = request.query.get("sid", ""), request.query.get("token", "")
    rec = await asyncio.to_thread(store.load, sid) if sid else None
    if not rec or not token or rec.get("token") != token:
        print(f"[answer] unknown call sid={sid!r} — hanging up", flush=True)
        return web.Response(text='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                            content_type="text/xml")
    await asyncio.to_thread(store.update, sid, status="answered",
                            call_uuid=str(form.get("CallUUID", "")))
    print(f"[answer] sid={sid} answered", flush=True)
    ws_url = re.sub(r"^http", "ws", _base_url(request)) + "/stream"
    return web.Response(text=phone.answer_xml(ws_url, {"sid": sid, "token": token}),
                        content_type="text/xml")


async def hangup(request: web.Request) -> web.Response:
    form = await request.post()
    sid = request.query.get("sid", "")
    rec = await asyncio.to_thread(store.load, sid) if sid else None
    if rec and rec.get("status") in ("calling", "answered"):
        # The stream never started: unanswered, rejected, or busy.
        await asyncio.to_thread(store.update, sid, status="no_answer", resume="", token="",
                                ended_reason=str(form.get("HangupCause", "")))
    print(f"[hangup] sid={sid} cause={form.get('HangupCause', '')}", flush=True)
    return web.Response(text="OK")


async def health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "agent": config.AGENT.get("name"),
        "model": config.AGENT.get("model"),
        "gemini_key_set": bool(config.GEMINI_API_KEY),
        "vobiz_set": bool(config.VOBIZ_AUTH_ID and config.VOBIZ_AUTH_TOKEN and config.VOBIZ_FROM_NUMBER),
        "sessions_bucket_set": bool(config.SESSIONS_BUCKET),
        "sheet_set": bool(config.SPREADSHEET_ID),
        "email_set": bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD),
    })


def make_app() -> web.Application:
    app = web.Application(client_max_size=config.MAX_RESUME_BYTES + 64 * 1024)
    app.router.add_get("/", index)
    app.router.add_post("/api/call", request_call)
    app.router.add_get("/api/call/{sid}", call_status)
    app.router.add_post("/answer", answer)
    app.router.add_post("/hangup", hangup)
    app.router.add_get("/stream", call_handler)
    app.router.add_get("/health", health)
    app.router.add_static("/static", STATIC)
    return app


if __name__ == "__main__":
    print(f"{config.AGENT.get('name')} listening on :{config.PORT}", flush=True)
    web.run_app(make_app(), port=config.PORT, print=None)
