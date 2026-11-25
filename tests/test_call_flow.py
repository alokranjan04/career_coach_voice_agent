"""End-to-end call flow against fakes: verified number → call request → Vobiz
answer → media stream → Gemini → save → goodbye → hang-up → post-call work."""
import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

import app as appmod
from agent import config
from conftest import verified_cookie
from fake_gemini import FakeGemini, run_phone_leg

PHONE = "919876543210"
RESUME = b"Forward Deployed AI Engineering Lead at Statusneo for McCain Foods. GenAI, Python. " * 4


def form(**over) -> aiohttp.FormData:
    values = {"phone": "98765 43210", "name": "Alok", "email": "alok@example.com", "consent": "yes",
              "whatsapp": "yes", **over}
    f = aiohttp.FormData()
    for k, v in values.items():
        if v is not None:
            f.add_field(k, v)
    f.add_field("resume", RESUME, filename="cv.txt")
    return f


def run(coro):
    return asyncio.run(coro)


async def _client(cookies=None) -> TestClient:
    client = TestClient(TestServer(appmod.make_app()), cookies=cookies or {})
    await client.start_server()
    return client


def test_unverified_number_is_asked_for_otp(world):
    async def go():
        c = await _client()
        try:
            r = await c.post("/api/call", data=form())
            return r.status, await r.json()
        finally:
            await c.close()
    status, body = run(go())
    assert status == 401 and body["needs_verification"]
    assert world.placed == []


def test_cookie_for_another_number_is_not_enough(world):
    async def go():
        c = await _client(verified_cookie("919999999999"))
        try:
            return (await c.post("/api/call", data=form())).status
        finally:
            await c.close()
    assert run(go()) == 401


def test_full_call(world, monkeypatch):
    async def go():
        async with FakeGemini() as url:
            monkeypatch.setattr(config, "GEMINI_WS_URL", url)
            c = await _client(verified_cookie(PHONE))
            try:
                r = await c.post("/api/call", data=form())
                assert r.status == 200, await r.text()
                sid = (await r.json())["sid"]
                to, answer_url, _ = world.placed[-1]
                assert to == PHONE
                audio = await run_phone_leg(c, answer_url)
                status = await (await c.get(f"/api/call/{sid}")).json()
                return sid, audio, status
            finally:
                await c.close()
    sid, audio, status = run(go())
    assert audio > 0
    assert status == {"status": "completed", "ended_reason": "completed", "summary_saved": True}
    assert world.hung == ["uuid-1"]
    rec = json.loads(world.db[f"calls/{sid}.json"])
    assert rec["resume"] == "" and rec["token"] == ""          # cleared after the call
    # Email with the summary and the not-a-promotion note, and WhatsApp
    to, subject, body = world.mails[-1]
    assert to == "alok@example.com" and "Head of AI" in body and "not promotions" in body
    assert world.whatsapp == [(PHONE, "Head of AI in 18 months")]
    # Rows: summary, quality score, call log with cost
    tabs = [t for t, _ in world.rows]
    assert tabs.count("Sessions") == 1 and tabs.count("Quality") == 1 and tabs.count("Call_Logs") == 1
    call_log = next(r for t, r in world.rows if t == "Call_Logs")
    assert call_log["Plan"] == "free" and call_log["Vobiz (₹)"] == 1.35 and call_log["Cost (₹)"] > 1.35
    # Spend ledger and memory
    day = next(json.loads(v) for k, v in world.db.items() if k.startswith("spend/"))
    assert day["calls"] == 1 and day["total_inr"] > 1.35
    profile = json.loads(world.db[f"profiles/{PHONE}.json"])
    assert profile["calls"][0]["summary"]["goal"] == "Head of AI in 18 months"
    # Consent record written
    assert any(k.startswith(f"consents/{PHONE}/") for k in world.db)


def test_prompt_tells_user_about_email_and_whatsapp(world, monkeypatch):
    fake = FakeGemini()

    async def go():
        async with fake as url:
            monkeypatch.setattr(config, "GEMINI_WS_URL", url)
            c = await _client(verified_cookie(PHONE))
            try:
                await c.post("/api/call", data=form())
                await run_phone_leg(c, world.placed[-1][1], seconds=2)
            finally:
                await c.close()
    run(go())
    prompt = fake.prompts[0]
    assert "a…@example.com" in prompt and "WhatsApp" in prompt and "{delivery}" not in prompt


def test_spend_cap_refuses_new_calls(world, monkeypatch):
    monkeypatch.setattr(config, "DAILY_SPEND_CAP_INR", 10.0)
    from agent import store
    store.add_spend({"vobiz": 11.0})

    async def go():
        c = await _client(verified_cookie(PHONE))
        try:
            return (await c.post("/api/call", data=form())).status
        finally:
            await c.close()
    assert run(go()) == 503
    assert world.placed == []


def test_unanswered_call_is_marked_no_answer(world):
    async def go():
        c = await _client(verified_cookie(PHONE))
        try:
            sid = (await (await c.post("/api/call", data=form())).json())["sid"]
            hangup_url = world.placed[-1][2]
            await c.post(hangup_url.replace("http://testserver", ""), data={"HangupCause": "NO_ANSWER"})
            return await (await c.get(f"/api/call/{sid}")).json()
        finally:
            await c.close()
    assert run(go())["status"] == "no_answer"


def test_bad_answer_token_hangs_up(world):
    async def go():
        c = await _client(verified_cookie(PHONE))
        try:
            await c.post("/api/call", data=form())
            answer = world.placed[-1][1].replace("http://testserver", "")
            return await (await c.post(answer.split("&token=")[0] + "&token=WRONG")).text()
        finally:
            await c.close()
    assert "<Hangup/>" in run(go())
