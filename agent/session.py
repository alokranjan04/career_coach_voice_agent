"""Bridge one browser voice session (WebSocket) to one Gemini Live session.

Browser protocol (/session):
  client → server  text  {"type":"start","name","email","language","resume"}  (first message)
                   bytes PCM16 LE mono 16 kHz from the mic (resampled in the browser)
                   text  {"type":"stop"}
  server → client  text  {"type":"ready"} / {"type":"transcript","role","text"} /
                         {"type":"interrupted"} / {"type":"ended","reason","saved"} / {"type":"error","message"}
                   bytes PCM16 LE mono 24 kHz from Gemini, played as-is

Same state machine as the telephony template's pipeline (greeting guard, tool guard,
hang up after the goodbye has played); only the audio transport differs.
"""
import asyncio
import base64
import json
import re
import time
import traceback
import uuid

import websockets
from aiohttp import WSMsgType, web

from agent import config, integrations, tools

GREETING_GUARD_MAX_SECS = 20   # release user audio even if the greeting never completes
POST_SAVE_MAX_SECS = 30        # end even if the goodbye never arrives
HANGUP_PAD_SECS = 0.8          # let the browser finish playing the goodbye
AGENT_BYTES_PER_SEC = 24000 * 2
LANGUAGES = {"English", "Hinglish"}
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class Session:
    def __init__(self, name: str, email: str, language: str, resume: str):
        self.sid = uuid.uuid4().hex[:10]
        self.name, self.email, self.language, self.resume = name, email, language, resume
        self.t0 = time.time()
        self.transcript: list[str] = []
        self.heard_buf = ""
        self.said_buf = ""
        self.greeting_done = False
        self.tool_running = False
        self.closing = False
        self.wrap_up_sent = False
        self.end_reason = "user ended"
        self.end_after_turn = False      # a session-ending tool succeeded
        self.end_requested_at = 0.0
        self.audio_since_end = 0         # agent bytes produced after that tool
        self.agent_play_end = 0.0        # wall time when queued agent audio finishes
        self.tools_done: set = set()
        self.summary: dict = {}

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] session={self.sid} | {msg}", flush=True)

    def flush(self) -> list[dict]:
        lines = []
        if self.heard_buf.strip():
            lines.append({"role": "user", "text": self.heard_buf.strip()})
            self.heard_buf = ""
        if self.said_buf.strip():
            lines.append({"role": "agent", "text": self.said_buf.strip()})
            self.said_buf = ""
        for line in lines:
            label = "User" if line["role"] == "user" else "Coach"
            self.transcript.append(f"{label}: {line['text']}")
            self.log(f"{label}: {line['text']}")
        return lines


def _setup_message(s: Session) -> dict:
    agent = config.AGENT
    return {"setup": {
        "model": agent.get("model", "models/gemini-3.8-live"),
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                "voiceName": agent.get("voice", "Aoede")}}},
        },
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "systemInstruction": {"parts": [{"text": config.system_prompt(s.name, s.language, s.resume)}]},
        "tools": config.TOOL_DECLARATIONS,
    }}


def _transcription(data: dict, sc: dict, kind: str) -> str:
    # Field names have varied across API versions; accept all of them.
    for key in (f"{kind}Transcription", f"{kind}AudioTranscription"):
        t = sc.get(key) or data.get(key)
        if t and t.get("text"):
            return t["text"]
    return ""


def _same_origin(request: web.Request) -> bool:
    """Stop other websites from opening sessions on our Gemini key. Non-browser
    clients send no Origin and are allowed (scripts/fake_session.py)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("X-Forwarded-Host") or request.host
    return re.sub(r"^https?://", "", origin).rstrip("/") == host


def _parse_start(raw: str) -> Session:
    msg = json.loads(raw)
    if msg.get("type") != "start":
        raise ValueError("expected a start message")
    name = re.sub(r"[\x00-\x1f<>{}]", "", str(msg.get("name", ""))).strip()[:80]
    email = str(msg.get("email", "")).strip()[:120]
    language = msg.get("language") if msg.get("language") in LANGUAGES else "English"
    resume = str(msg.get("resume", ""))[:config.MAX_RESUME_CHARS]
    if not name:
        raise ValueError("Please enter your name.")
    if not EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address.")
    if len(resume.strip()) < 100:
        raise ValueError("Please upload your resume first.")
    return Session(name, email, language, resume)


async def _send(ws, payload: dict) -> None:
    if not ws.closed:
        await ws.send_str(json.dumps(payload))


async def session_handler(request: web.Request) -> web.WebSocketResponse:
    if not _same_origin(request):
        raise web.HTTPForbidden(text="cross-origin session rejected")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=config.MAX_RESUME_BYTES)
    await ws.prepare(request)

    try:
        first = await ws.receive(timeout=15)
        if first.type != WSMsgType.TEXT:
            raise ValueError("expected a start message")
        s = _parse_start(first.data)
    except (ValueError, TimeoutError, asyncio.TimeoutError) as e:
        await _send(ws, {"type": "error", "message": str(e) or "Session didn't start in time."})
        await ws.close()
        return ws

    s.log(f"session started language={s.language} resume_chars={len(s.resume)}")
    try:
        async with websockets.connect(config.GEMINI_WS_URL, open_timeout=15, ping_interval=20,
                                      ping_timeout=20, max_size=None) as gem:
            await gem.send(json.dumps(_setup_message(s)))
            first = json.loads(await asyncio.wait_for(gem.recv(), timeout=10))
            if "setupComplete" not in first:
                raise RuntimeError(f"Gemini setup rejected: {first}")
            # Ask the model to speak first (the prompt defines the greeting).
            await gem.send(json.dumps({"realtimeInput": {"text": "[SESSION_STARTED]"}}))
            await _send(ws, {"type": "ready"})

            jobs = [asyncio.create_task(_client_to_gemini(ws, gem, s)),
                    asyncio.create_task(_gemini_to_client(ws, gem, s)),
                    asyncio.create_task(_watchdog(ws, gem, s))]
            done, pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            for job in pending:
                job.cancel()
            for job in done:
                if job.exception():
                    s.log(f"task error: {job.exception()!r}")
    except websockets.ConnectionClosed as e:
        # Invalid key (1007) and depleted credits (1011) surface here, not at connect.
        s.log(f"Gemini closed the session: code={e.code} reason={e.reason}")
        if not s.end_after_turn:
            s.end_reason = "coach disconnected"
    except Exception:
        s.log("pipeline error:\n" + traceback.format_exc())
        s.end_reason = "error"
    finally:
        s.closing = True
        for line in s.flush():
            await _send(ws, {"type": "transcript", **line})
        await _send(ws, {"type": "ended", "reason": s.end_reason, "saved": bool(s.summary)})
        if not ws.closed:
            await ws.close()
        s.log(f"session ended after {time.time() - s.t0:.0f}s ({s.end_reason})")
        await asyncio.to_thread(_after_session, s)
    return ws


async def _client_to_gemini(ws, gem, s: Session) -> None:
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            if json.loads(msg.data).get("type") == "stop":
                return
            continue
        if msg.type != WSMsgType.BINARY:
            continue
        # Greeting guard: noise during the greeting would make Gemini interrupt
        # itself. Tool guard: audio during a save can trigger a spurious
        # interrupt that cuts the confirmation.
        if not s.greeting_done and time.time() - s.t0 > GREETING_GUARD_MAX_SECS:
            s.greeting_done = True
        if not s.greeting_done or s.tool_running or s.closing:
            continue
        await gem.send(json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(msg.data).decode(),
            "mimeType": "audio/pcm;rate=16000"}}}))


async def _gemini_to_client(ws, gem, s: Session) -> None:
    async for raw in gem:
        data = json.loads(raw)
        sc = data.get("serverContent", {})

        heard = _transcription(data, sc, "input")
        if heard:
            s.heard_buf += heard
        said = _transcription(data, sc, "output")
        if said:
            if s.heard_buf.strip():
                for line in s.flush():
                    await _send(ws, {"type": "transcript", **line})
            s.said_buf += said

        # One event can carry several parts: forward all of them.
        for part in sc.get("modelTurn", {}).get("parts", []):
            inline = part.get("inlineData")
            if not inline or s.closing:
                continue
            pcm24 = base64.b64decode(inline["data"])
            s.agent_play_end = max(s.agent_play_end, time.time()) + len(pcm24) / AGENT_BYTES_PER_SEC
            if s.end_after_turn:
                s.audio_since_end += len(pcm24)
            if not ws.closed:
                await ws.send_bytes(pcm24)

        if sc.get("interrupted") and not s.end_after_turn:
            # User barged in: the browser drops audio it has queued.
            await _send(ws, {"type": "interrupted"})
            s.agent_play_end = time.time()

        if sc.get("turnComplete"):
            for line in s.flush():
                await _send(ws, {"type": "transcript", **line})
            s.greeting_done = True
            if s.end_after_turn and s.audio_since_end > 0:
                delay = max(0.0, s.agent_play_end - time.time()) + HANGUP_PAD_SECS
                s.log(f"goodbye spoken — ending in {delay:.1f}s")
                s.end_reason = "completed"
                await asyncio.sleep(delay)
                return

        if "toolCall" in data:
            await _run_tools(gem, data["toolCall"], s)

        if "goAway" in data:
            s.log(f"Gemini goAway: {data['goAway']}")


async def _run_tools(gem, tool_call: dict, s: Session) -> None:
    responses = []
    for fc in tool_call.get("functionCalls", []):
        name, args = fc.get("name", ""), fc.get("args") or {}
        s.log(f"tool call {name} {json.dumps(args, ensure_ascii=False)}")
        s.tool_running = True
        try:
            result = await asyncio.to_thread(tools.run, name, args, s, s.tools_done)
        except Exception as e:  # report failure to the model so it can tell the user
            result = {"success": False, "error": str(e)[:300]}
        finally:
            s.tool_running = False
        s.log(f"tool result {name}: {result}")
        if name == "save_session_summary":
            s.summary = s.summary or dict(args)  # email it even if the sheet write failed
        if result.get("success") and tools.ends_call(name) and not s.end_after_turn:
            s.end_after_turn = True
            s.end_requested_at = time.time()
        responses.append({"id": fc.get("id"), "name": name, "response": {"result": result}})
    if responses:
        await gem.send(json.dumps({"toolResponse": {"functionResponses": responses}}))


async def _watchdog(ws, gem, s: Session) -> None:
    while not ws.closed:
        await asyncio.sleep(1)
        elapsed = time.time() - s.t0
        if s.end_after_turn and time.time() - s.end_requested_at > POST_SAVE_MAX_SECS:
            s.log("no goodbye after save — ending")
            s.end_reason = "completed"
            return
        if elapsed > config.WRAP_UP_SECS and not s.wrap_up_sent and not s.end_after_turn:
            s.wrap_up_sent = True
            s.log("time check — asking the coach to recap")
            await gem.send(json.dumps({"realtimeInput": {"text": "[TIME_CHECK]"}}))
        if elapsed > config.MAX_SESSION_SECS:
            s.log("session time limit reached")
            s.end_reason = "time limit"
            return


def _summary_email(s: Session) -> str:
    sm = s.summary
    sections = [("Your goal", "goal"), ("Where you are now", "current_situation"),
                ("Your strengths", "strengths"), ("What's holding you back", "obstacles"),
                ("Resume suggestions", "resume_feedback"), ("Your next steps", "next_steps")]
    body = [f"Hi {s.name},", "",
            "Thanks for your session with Disha, your AI career coach. Here's your summary.", ""]
    for title, key in sections:
        if str(sm.get(key, "")).strip():
            body += [title.upper(), str(sm[key]).strip(), ""]
    body += ["All the best!", "Disha", "",
             "This summary was written by an AI coach. Treat it as guidance, not professional advice."]
    return "\n".join(body)


def _after_session(s: Session) -> None:
    """Summary email to the user and a session log row. Runs after the browser
    disconnected; on Cloud Run give the service always-allocated CPU."""
    text = "\n".join(s.transcript) or "(empty)"
    if s.summary:
        try:
            if integrations.send_email(s.email, "Your career coaching summary", _summary_email(s)):
                s.log("summary email sent")
        except Exception as e:
            s.log(f"email failed: {e!r}")
    if config.SPREADSHEET_ID:
        try:
            integrations.append_row(
                config.SESSION_LOG_TAB,
                ["Start", "Session", "Name", "Email", "Language", "Duration (s)", "Ended", "Saved", "Transcript"],
                [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.t0)), s.sid, s.name, s.email,
                 s.language, round(time.time() - s.t0), s.end_reason,
                 ",".join(sorted(s.tools_done)) or "no", text[:45000]])
        except Exception as e:
            s.log(f"session log failed: {e!r}")
