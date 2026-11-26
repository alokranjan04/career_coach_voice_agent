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

from agent import (config, costs, integrations, obs, quality, resources, store, telephony,
                   tools, vobiz_api, whatsapp)

GREETING_GUARD_MAX_SECS = 20   # release caller audio even if the greeting never completes
POST_SAVE_MAX_SECS = 30        # hang up even if the goodbye never arrives
HANGUP_PAD_SECS = 0.8          # let the phone finish playing the goodbye


class Call:
    def __init__(self, rec: dict):
        self.sid = rec["sid"]
        self.name, self.email, self.phone = rec["name"], rec["email"], rec["phone"]
        self.resume, self.history = rec["resume"], rec.get("history", "")
        self.whatsapp = bool(rec.get("whatsapp"))
        self.entitlement = rec.get("entitlement", "free")
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
        self.usage = costs.Usage()

    def log(self, msg: str, event: str = "call_log", severity: str = "INFO", **fields) -> None:
        obs.log(event, msg, severity=severity, call_id=self.sid, **fields)

    def flush(self) -> None:
        if self.heard_buf.strip():
            self.transcript.append(f"Caller: {self.heard_buf.strip()}")
            self.log(f"Caller: {self.heard_buf.strip()}", event="transcript", role="caller")
            self.heard_buf = ""
        if self.said_buf.strip():
            self.transcript.append(f"Coach: {self.said_buf.strip()}")
            self.log(f"Coach: {self.said_buf.strip()}", event="transcript", role="coach")
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
        "systemInstruction": {"parts": [{"text": config.system_prompt(
            call.name, call.resume, call.history, call.email, call.whatsapp)}]},
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
        obs.log("stream_rejected", f"rejected sid={sid!r}: unknown, used or bad token", severity="WARNING")
        await ws.close()
        return ws

    call = Call(rec)
    if phone.needs_start:
        call.stream_id = start.get("streamId", "")
    call.log("call started", event="call_started", entitlement=call.entitlement,
             resume_chars=len(call.resume), history_chars=len(call.history))
    await asyncio.to_thread(store.update, call.sid, status="in_call", token="")  # token is single-use
    # The call connected: count it against the free allowance or the day pass.
    await asyncio.to_thread(store.consume_call, call.phone, call.entitlement)
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
                    call.log(f"task error: {job.exception()!r}", event="call_error", severity="ERROR")
    except websockets.ConnectionClosed as e:
        # Invalid key (1007) and depleted credits (1011) surface here, not at connect.
        call.log(f"Gemini closed the session: code={e.code} reason={e.reason}", event="gemini_closed",
                 severity="WARNING" if call.end_after_turn else "ERROR", code=e.code)
        if not call.end_after_turn:
            call.end_reason = "coach disconnected"
        if e.code in (1007, 1011) and not call.end_after_turn:  # 1007 bad key, 1011 credits depleted
            await asyncio.to_thread(obs.alert, "gemini_credits", "Gemini rejected a call: credits or key",
                                    f"Gemini closed a live call with code {e.code}: {e.reason}\n"
                                    "Top up credits or fix the key in AI Studio; calls are failing until then.",
                                    3600)
    except Exception:
        call.log("pipeline error:\n" + traceback.format_exc(), event="call_error", severity="ERROR")
        call.end_reason = "error"
    finally:
        call.closing = True
        if not ws.closed:
            await ws.close()
        call.flush()
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
        if "usageMetadata" in data:
            call.usage.add_live(data["usageMetadata"])  # one report per turn, summed for billing

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
            call.log(f"Gemini goAway: {data['goAway']}", event="gemini_goaway", severity="WARNING")


async def _run_tools(gem, tool_call: dict, call: Call) -> None:
    responses = []
    for fc in tool_call.get("functionCalls", []):
        name, args = fc.get("name", ""), fc.get("args") or {}
        call.log(f"tool call {name}", event="tool_call", tool=name, args=args)
        call.tool_running = True
        try:
            result = await asyncio.to_thread(tools.run, name, args, call, call.tools_done)
        except Exception as e:  # report failure to the model so it can tell the caller
            result = {"success": False, "error": str(e)[:300]}
        finally:
            call.tool_running = False
        call.log(f"tool result {name}: {result}", event="tool_result", tool=name,
                 severity="INFO" if result.get("success") else "ERROR")
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
            call.log("no goodbye after save — hanging up", severity="WARNING")
            call.end_reason = "completed"
            return
        if elapsed > config.WRAP_UP_SECS and not call.wrap_up_sent and not call.end_after_turn:
            call.wrap_up_sent = True
            call.log("time check — asking the coach to recap")
            await gem.send(json.dumps({"realtimeInput": {"text": "[TIME_CHECK]"}}))
        if elapsed > config.MAX_CALL_SECS:
            call.log("call time limit reached", severity="WARNING")
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
             "This summary was written by an AI coach. Treat it as guidance, not professional advice.",
             f"Your data and rights: {config.SITE_URL}/my-data · Privacy policy: {config.SITE_URL}/privacy"]
    return "\n".join(body)


def _step(call: Call, name: str, fn, *args, **kwargs):
    """Run one post-call step; a failure is logged and doesn't stop the others."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        call.log(f"{name} failed: {e!r}", event=f"{name}_failed", severity="ERROR")
        return None


def _after_call(call: Call) -> None:
    """Hang up, then research, email, WhatsApp, quality score, cost, spend ledger,
    balance check and memory. On Cloud Run give the service always-allocated CPU."""
    duration = round(time.time() - call.t0)
    # keepCallAlive keeps the line open after the stream; end it explicitly.
    _step(call, "hangup", vobiz_api.hangup, call.call_uuid)
    text = "\n".join(call.transcript) or "(empty)"

    found, wa_sid, wa_cost = {}, "", 0.0
    if call.summary:
        found = _step(call, "research", resources.find_resources, call.summary, call.resume, call.usage) or {}
        body = _summary_email(call, resources.email_section(found) if found else "")
        if _step(call, "email", integrations.send_email, call.email,
                 "Your career coaching summary and resources", body):
            call.log("summary email sent", event="email_sent")
        if call.whatsapp and whatsapp.enabled():
            top = [i["url"] for k in ("videos", "learning") for i in found.get(k, [])]
            wa_sid = _step(call, "whatsapp", whatsapp.send_summary, call.phone, call.name,
                           call.summary, call.email, top) or ""
            if wa_sid:
                wa_cost = config.PRICE_WHATSAPP_INR
                call.log("whatsapp summary sent", event="whatsapp_sent", twilio_sid=wa_sid)

    q = None
    if len(call.transcript) >= 4 and config.GEMINI_API_KEY:
        q = _step(call, "quality", quality.score_transcript, text, call.resume, call.usage)
        if q:
            call.log("call scored", event="quality_scored", overall=q.get("overall"), scores=q["scores"])
            if config.SPREADSHEET_ID:
                _step(call, "quality_row", integrations.append_row, "Quality",
                      ["Timestamp", "Call", "overall", *quality.RUBRIC, "issues", "best_moment"],
                      [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(call.t0)), call.sid,
                       q.get("overall"), *[q["scores"].get(k) for k in quality.RUBRIC],
                       " | ".join(q.get("issues", [])), q.get("best_moment", "")])

    # Cost: Vobiz's billed figure (it can lag a few seconds), else the tariff estimate.
    vobiz = -1.0
    for _ in range(3):
        vobiz = _step(call, "vobiz_cost", vobiz_api.call_cost_inr, call.call_uuid) or -1.0
        if vobiz >= 0:
            break
        time.sleep(4)
    vobiz_source = "billed" if vobiz >= 0 else "estimate"
    if vobiz < 0:
        vobiz = costs.vobiz_estimate_inr(duration)
    cost = {"vobiz": round(vobiz, 4), "gemini_live": round(call.usage.live_inr(), 4),
            "gemini_research_quality": round(call.usage.flash_inr(), 4), "whatsapp": wa_cost}
    total = round(sum(cost.values()), 2)
    call.log(f"call ended after {duration}s ({call.end_reason}), cost ₹{total}", event="call_ended",
             duration_secs=duration, end_reason=call.end_reason, saved=bool(call.summary),
             entitlement=call.entitlement, cost_inr=total, cost_breakdown_inr=cost,
             vobiz_cost_source=vobiz_source, tokens=call.usage.tokens(),
             quality_overall=(q or {}).get("overall"))

    if config.SPREADSHEET_ID:
        _step(call, "call_log_row", integrations.append_row, config.CALL_LOG_TAB,
              ["Start", "Call", "Name", "Email", "Phone", "Duration (s)", "Ended", "Saved", "Transcript",
               "Plan", "Cost (₹)", "Vobiz (₹)", "Gemini live (₹)", "Gemini research+QA (₹)", "Quality"],
              [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(call.t0)), call.sid, call.name, call.email,
               call.phone, duration, call.end_reason, ",".join(sorted(call.tools_done)) or "no",
               text[:45000], call.entitlement, total, cost["vobiz"], cost["gemini_live"],
               cost["gemini_research_quality"], (q or {}).get("overall", "")])

    day = _step(call, "spend_ledger", store.add_spend, cost) or {}
    if day.get("total_inr", 0) >= config.DAILY_SPEND_CAP_INR:
        obs.alert("daily_cap", f"Daily spend cap reached: ₹{day['total_inr']:.0f}",
                  f"Estimated spend today is ₹{day['total_inr']:.2f} across {day.get('calls')} calls, "
                  f"reaching the cap of ₹{config.DAILY_SPEND_CAP_INR:.0f}. New calls are refused until "
                  "midnight IST. Raise DAILY_SPEND_CAP_INR to allow more.", 12 * 3600)
    check_balances()

    _step(call, "profile_update", store.append_call_to_profile, call.phone, {
        "date": datetime.now(tools.TZ).strftime("%d %b %Y, %I:%M %p"),
        "duration": duration, "ended": call.end_reason,
        "summary": call.summary, "transcript": text if call.transcript else "",
        "resources_sent": [f"{i.get('title') or i.get('name')}: {i['url']}"
                           for k in ("videos", "learning", "reading", "people") for i in found.get(k, [])]})
    _step(call, "status_update", store.update, call.sid,
          status="completed" if call.summary else "ended", resume="", history="",
          summary_saved=bool(call.summary), ended_reason=call.end_reason)


def check_balances() -> None:
    """Low-balance alerts: Vobiz from its balance API; Gemini from this month's
    estimated spend against the budget (AI Studio has no balance API)."""
    try:
        bal = vobiz_api.balance_inr()
        obs.log("vobiz_balance", f"Vobiz balance ₹{bal:.2f}", balance_inr=bal)
        if bal < config.VOBIZ_LOW_BALANCE_INR:
            obs.alert("vobiz_low_balance", f"Vobiz balance low: ₹{bal:.2f}",
                      f"Vobiz available balance is ₹{bal:.2f}, below ₹{config.VOBIZ_LOW_BALANCE_INR:.0f}. "
                      "Calls fail when it runs out. Recharge in the Vobiz console.")
    except Exception as e:
        obs.log("vobiz_balance_failed", f"balance check failed: {e!r}", severity="WARNING")
    try:
        month = store.gemini_spend_this_month()
        if month >= 0.8 * config.GEMINI_MONTHLY_BUDGET_INR:
            obs.alert("gemini_budget", f"Gemini spend at ₹{month:.0f} this month",
                      f"Estimated Gemini spend this month is ₹{month:.2f}, over 80% of the "
                      f"₹{config.GEMINI_MONTHLY_BUDGET_INR:.0f} budget. Check prepaid credits in AI Studio "
                      "(aistudio.google.com → Billing).", 24 * 3600)
    except Exception as e:
        obs.log("gemini_budget_failed", f"budget check failed: {e!r}", severity="WARNING")
