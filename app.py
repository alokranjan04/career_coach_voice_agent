"""HTTP entry point.

GET  /             — web app: upload resume, then talk to the coach
POST /api/resume   — multipart upload; returns the extracted resume text
GET  /session      — voice WebSocket, one per session (agent/session.py)
GET  /health       — readiness / config sanity (no secrets)
"""
import sys

from aiohttp import web

from agent import config
from agent.resume import ResumeError, extract_text
from agent.session import session_handler

# Windows consoles default to cp1252 and crash on non-ASCII log lines.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

STATIC = config.ROOT / "static"


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def upload_resume(request: web.Request) -> web.Response:
    """Stateless: the text goes back to the browser, which sends it with the
    session start message, so upload and session can land on any instance."""
    try:
        form = await request.post()
    except web.HTTPRequestEntityTooLarge:
        return web.json_response({"error": "That file is too large (max 5 MB)."}, status=413)
    upload = form.get("resume")
    if not isinstance(upload, web.FileField):
        return web.json_response({"error": "Please choose a resume file."}, status=400)
    try:
        text = extract_text(upload.filename or "", upload.file.read())
    except ResumeError as e:
        return web.json_response({"error": str(e)}, status=422)
    print(f"[resume] parsed {upload.filename!r}: {len(text)} chars", flush=True)
    return web.json_response({"text": text, "chars": len(text)})


async def health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "agent": config.AGENT.get("name"),
        "model": config.AGENT.get("model"),
        "gemini_key_set": bool(config.GEMINI_API_KEY),
        "sheet_set": bool(config.SPREADSHEET_ID),
        "email_set": bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD),
    })


def make_app() -> web.Application:
    app = web.Application(client_max_size=config.MAX_RESUME_BYTES + 64 * 1024)
    app.router.add_get("/", index)
    app.router.add_post("/api/resume", upload_resume)
    app.router.add_get("/session", session_handler)
    app.router.add_get("/health", health)
    app.router.add_static("/static", STATIC)
    return app


if __name__ == "__main__":
    print(f"{config.AGENT.get('name')} listening on :{config.PORT}", flush=True)
    web.run_app(make_app(), port=config.PORT, print=None)
