"""Bridge one phone call (telephony media WebSocket) to one Gemini Live session.

Audio:  phone mu-law 8 kHz ─ulaw2lin─▶ PCM16 8 kHz ─ratecv─▶ 16 kHz ─▶ Gemini
        Gemini PCM16 24 kHz ─ratecv─▶ 8 kHz ─lin2ulaw─▶ phone
ratecv keeps filter state between chunks; reusing one state per direction per
call avoids clicks at chunk boundaries. Read the skill's references/architecture.md first.

Each call belongs to a call request (agent/store.py) made from the web page: the
stream URL carries its sid and a one-time token, and the request holds the
user's name, email, resume text and a digest of their previous calls.
"""
import asyncio
import audioop
import base64
import json
import secrets
import time
import traceback
from datetime import datetime

import websockets
from aiohttp import WSMsgType, web

from agent import config, integrations, resources, store, telephony, tools, vobiz_api

GREETING_GUARD_MAX_SECS = 20   # release caller audio even if the greeting never completes
POST_SAVE_MAX_SECS = 30        # hang up even if the goodbye never arrives
HANGUP_PAD_SECS = 0.8          # let the phone finish playing the goodbye


class Call:
    def __init__(self, rec: dict):
        self.sid = rec["sid"]
        self.name, self.email, self.phone = rec["name"], rec["email"], rec["phone"]
        self.resume, self.history = rec["resume"], rec.get("history", "")
        self.call_uuid = rec.get("call_uuid", "")
        self.t0 = time.time()
        self.stream_id = ""
        self.transcript: list[str] = []
        self.heard_buf = ""
        self.said_buf = ""
        self.greeting_done = False
        self.tool_running = False
        self.closing = False
        self.wrap_up_sent = False
        self.end_reason = "caller hung up"
        self.end_after_turn = False      # a call-ending tool succeeded
        self.end_requested_at = 0.0
        self.audio_since_end = 0         # agent bytes produced after that tool
        self.agent_play_end = 0.0        # wall time when queued agent audio finishes
        self.tools_done: set = set()
        self.summary: dict = {}

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] call={self.sid} | {msg}", flush=True)

    def flush(self) -> None:
        if self.heard_buf.strip():
            self.transcript.append(f"Caller: {self.heard_buf.strip()}")
            self.log(f"Caller: {self.heard_buf.strip()}")
            self.heard_buf = ""
        if self.said_buf.strip():
            self.transcript.append(f"Coach: {self.said_buf.strip()}")
            self.log(f"Coach: {self.said_buf.strip()}")
            self.said_buf = ""


def _setup_message(call: Call) -> dict:
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
        "systemInstruction": {"parts": [{"text": config.system_prompt(call.name, call.resume, call.history)}]},
        "tools": config.TOOL_DECLARATIONS,
    }}


def _transcription(data: dict, sc: dict, kind: str) -> str:
    # Field names have varied across API versions; accept all of them.
    for key in (f"{kind}Transcription", f"{kind}AudioTranscription"):
        t = sc.get(key) or data.get(key)
        if t and t.get("text"):
            return t["text"]
    return ""


async def call_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(request)
    phone = telephony.get_provider()
    sid, token = request.query.get("sid", ""), request.query.get("token", "")

    if phone.needs_start:  # e.g. Twilio: need streamSid before we can play audio
        start = await _wait_for_start(ws, phone)
        if start is None:
            await ws.close()
            return ws
        params = start.get("customParameters") or {}
        sid, token = params.get("sid", sid), params.get("token", token)

    rec = await asyncio.to_thread(store.load, sid) if sid else None
    if not rec or not secrets.compare_digest(str(rec.get("token", "")), token) or not rec.get("resume"):
        print(f"[stream] rejected sid={sid!r}: unknown, used or bad token", flush=True)
        await ws.close()
        return ws

    call = Call(rec)
    if phone.needs_start:
        call.stream_id = start.get("streamId", "")
    call.log(f"call started resume_chars={len(call.resume)} history_chars={len(call.history)}")
    await asyncio.to_thread(store.update, call.sid, status="in_call", token="")  # token is single-use
    try:
        async with websockets.connect(config.GEMINI_WS_URL, open_timeout=15, ping_interval=20,
                                      ping_timeout=20, max_size=None) as gem:
            await gem.send(json.dumps(_setup_message(call)))
            first = json.loads(await asyncio.wait_for(gem.recv(), timeout=10))
            if "setupComplete" not in first:
                raise RuntimeError(f"Gemini setup rejected: {first}")
            # Ask the model to speak first (the prompt defines the greeting).
            await gem.send(json.dumps({"realtimeInput": {"text": "[CALL_STARTED]"}}))

            jobs = [asyncio.create_task(_phone_to_gemini(ws, gem, phone, call)),
                    asyncio.create_task(_gemini_to_phone(ws, gem, phone, call)),
                    asyncio.create_task(_watchdog(ws, gem, call))]
            done, pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            for job in pending:
                job.cancel()
            for job in done:
                if job.exception():
                    call.log(f"task error: {job.exception()!r}")
    except websockets.ConnectionClosed as e:
        # Invalid key (1007) and depleted credits (1011) surface here, not at connect.
        call.log(f"Gemini closed the session: code={e.code} reason={e.reason}")
        if not call.end_after_turn:
            call.end_reason = "coach disconnected"
    except Exception:
        call.log("pipeline error:\n" + traceback.format_exc())
        call.end_reason = "error"
    finally:
        call.closing = True
        if not ws.closed:
            await ws.close()
        call.flush()
        call.log(f"call ended after {time.time() - call.t0:.0f}s ({call.end_reason})")
        # aiohttp may cancel this handler once the socket is closed. A thread
        # can't be cancelled, so hang-up and post-call work run as one thread job.
        await asyncio.shield(asyncio.to_thread(_after_call, call))
    return ws


async def _wait_for_start(ws, phone, timeout: float = 10.0):
    try:
        async with asyncio.timeout(timeout):
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    kind, payload = phone.parse(json.loads(msg.data))
                    if kind == "start":
                        return payload
    except TimeoutError:
        pass
    return None


async def _phone_to_gemini(ws, gem, phone, call: Call) -> None:
    up_state = None
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        kind, payload = phone.parse(json.loads(msg.data))
        if kind == "start":
            call.stream_id = payload.get("streamId", call.stream_id)
            continue
        if kind == "stop":
            return
        if kind != "media":
            continue
        # Greeting guard: line noise during the greeting would make Gemini
        # interrupt itself. Tool guard: audio during a save can trigger a
        # spurious interrupt that cuts the confirmation.
        if not call.greeting_done and time.time() - call.t0 > GREETING_GUARD_MAX_SECS:
            call.greeting_done = True
        if not call.greeting_done or call.tool_running or call.closing:
            continue
        pcm8 = audioop.ulaw2lin(payload, 2)
        pcm16, up_state = audioop.ratecv(pcm8, 2, 1, 8000, 16000, up_state)
        await gem.send(json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(pcm16).decode(), "mimeType": "audio/pcm;rate=16000"}}}))


async def _gemini_to_phone(ws, gem, phone, call: Call) -> None:
    down_state = None
    async for raw in gem:
        data = json.loads(raw)
        sc = data.get("serverContent", {})

        heard = _transcription(data, sc, "input")
        if heard:
            call.heard_buf += heard
        said = _transcription(data, sc, "output")
        if said:
            if call.heard_buf.strip():
                call.flush()
            call.said_buf += said

        # One event can carry several parts: play all of them.
        for part in sc.get("modelTurn", {}).get("parts", []):
            inline = part.get("inlineData")
            if not inline or call.closing:
                continue
            pcm8, down_state = audioop.ratecv(
                base64.b64decode(inline["data"]), 2, 1, 24000, 8000, down_state)
            now = time.time()
            call.agent_play_end = max(call.agent_play_end, now) + len(pcm8) / 16000
            if call.end_after_turn:
                call.audio_since_end += len(pcm8)
            if not ws.closed:
                await ws.send_str(json.dumps(phone.play(audioop.lin2ulaw(pcm8, 2), call.stream_id)))

        if sc.get("interrupted") and not call.end_after_turn:
            # Caller barged in: stop what the phone has queued.
            if not ws.closed:
                await ws.send_str(json.dumps(phone.clear(call.stream_id)))
            call.agent_play_end = time.time()

        if sc.get("turnComplete"):
            call.flush()
            call.greeting_done = True
            if call.end_after_turn and call.audio_since_end > 0:
                delay = max(0.0, call.agent_play_end - time.time()) + HANGUP_PAD_SECS
                call.log(f"goodbye spoken — hanging up in {delay:.1f}s")
                call.end_reason = "completed"
                await asyncio.sleep(delay)
                return

        if "toolCall" in data:
            await _run_tools(gem, data["toolCall"], call)

        if "goAway" in data:
            call.log(f"Gemini goAway: {data['goAway']}")


async def _run_tools(gem, tool_call: dict, call: Call) -> None:
    responses = []
    for fc in tool_call.get("functionCalls", []):
        name, args = fc.get("name", ""), fc.get("args") or {}
        call.log(f"tool call {name} {json.dumps(args, ensure_ascii=False)}")
        call.tool_running = True
        try:
            result = await asyncio.to_thread(tools.run, name, args, call, call.tools_done)
        except Exception as e:  # report failure to the model so it can tell the caller
            result = {"success": False, "error": str(e)[:300]}
        finally:
            call.tool_running = False
        call.log(f"tool result {name}: {result}")
        if name == "save_session_summary":
            call.summary = call.summary or dict(args)  # email it even if the sheet write failed
        if result.get("success") and tools.ends_call(name) and not call.end_after_turn:
            call.end_after_turn = True
            call.end_requested_at = time.time()
        responses.append({"id": fc.get("id"), "name": name, "response": {"result": result}})
    if responses:
        await gem.send(json.dumps({"toolResponse": {"functionResponses": responses}}))


async def _watchdog(ws, gem, call: Call) -> None:
    while not ws.closed:
        await asyncio.sleep(1)
        elapsed = time.time() - call.t0
        if call.end_after_turn and time.time() - call.end_requested_at > POST_SAVE_MAX_SECS:
            call.log("no goodbye after save — hanging up")
            call.end_reason = "completed"
            return
        if elapsed > config.WRAP_UP_SECS and not call.wrap_up_sent and not call.end_after_turn:
            call.wrap_up_sent = True
            call.log("time check — asking the coach to recap")
            await gem.send(json.dumps({"realtimeInput": {"text": "[TIME_CHECK]"}}))
        if elapsed > config.MAX_CALL_SECS:
            call.log("call time limit reached")
            call.end_reason = "time limit"
            return


def _summary_email(call: Call, resources_text: str) -> str:
    sm = call.summary
    sections = [("Your goal", "goal"), ("Target role", "target_role"),
                ("Where you are now", "current_situation"), ("Your strengths", "strengths"),
                ("Biggest gaps to close", "skill_gaps"), ("What's holding you back", "obstacles"),
                ("Resume suggestions", "resume_feedback"), ("Your 30-day plan", "next_steps")]
    body = [f"Hi {call.name},", "",
            "Thanks for your call with Disha, your AI career coach. Here's your summary.", ""]
    for title, key in sections:
        if str(sm.get(key, "")).strip():
            body += [title.upper(), str(sm[key]).strip(), ""]
    if resources_text:
        body += [resources_text]
    body += ["All the best!", "Disha", "",
             "This summary was written by an AI coach. Treat it as guidance, not professional advice."]
    return "\n".join(body)


def _after_call(call: Call) -> None:
    """Hang up, then summary email, call log row, and dropping the resume text.
    On Cloud Run give the service always-allocated CPU."""
    try:
        # keepCallAlive keeps the line open after the stream; end it explicitly.
        vobiz_api.hangup(call.call_uuid)
    except Exception as e:
        call.log(f"hangup failed: {e!r}")
    text = "\n".join(call.transcript) or "(empty)"
    found = {}
    if call.summary:
        try:
            # Internet research for the goal; only verified links survive.
            found = resources.find_resources(call.summary, call.resume)
        except Exception as e:
            call.log(f"research failed: {e!r}")
        try:
            body = _summary_email(call, resources.email_section(found) if found else "")
            if integrations.send_email(call.email, "Your career coaching summary and resources", body):
                call.log("summary email sent")
        except Exception as e:
            call.log(f"email failed: {e!r}")
    if config.SPREADSHEET_ID:
        try:
            integrations.append_row(
                config.CALL_LOG_TAB,
                ["Start", "Call", "Name", "Email", "Phone", "Duration (s)", "Ended", "Saved", "Transcript"],
                [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(call.t0)), call.sid, call.name, call.email,
                 call.phone, round(time.time() - call.t0), call.end_reason,
                 ",".join(sorted(call.tools_done)) or "no", text[:45000]])
        except Exception as e:
            call.log(f"call log failed: {e!r}")
    try:
        # Remember this call so the next one can pick up where it left off.
        store.append_call_to_profile(call.phone, {
            "date": datetime.now(tools.TZ).strftime("%d %b %Y, %I:%M %p"),
            "duration": round(time.time() - call.t0), "ended": call.end_reason,
            "summary": call.summary, "transcript": "\n".join(call.transcript),
            "resources_sent": [f"{i.get('title') or i.get('name')}: {i['url']}"
                               for k in ("videos", "learning", "reading", "people") for i in found.get(k, [])]})
    except Exception as e:
        call.log(f"profile update failed: {e!r}")
    try:
        store.update(call.sid, status="completed" if call.summary else "ended", resume="", history="",
                     summary_saved=bool(call.summary), ended_reason=call.end_reason)
    except Exception as e:
        call.log(f"status update failed: {e!r}")
