"""Free calls, day passes, Razorpay signature checks, and returning-caller memory."""
import asyncio
import hashlib
import hmac
import json

from aiohttp.test_utils import TestClient, TestServer

import app as appmod
from agent import config, payments, store
from conftest import verified_cookie
from fake_gemini import FakeGemini, run_phone_leg
from test_call_flow import PHONE, form


def run(coro):
    return asyncio.run(coro)


async def _client(cookies=None):
    c = TestClient(TestServer(appmod.make_app()), cookies=cookies or {})
    await c.start_server()
    return c


def _sig(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def test_three_free_calls_then_payment_required(world, monkeypatch):
    async def go():
        async with FakeGemini(drop=True) as url:
            monkeypatch.setattr(config, "GEMINI_WS_URL", url)
            c = await _client(verified_cookie(PHONE))
            try:
                for i in range(3):
                    r = await c.post("/api/call", data=form())
                    assert r.status == 200, await r.text()
                    await run_phone_leg(c, world.placed[-1][1], call_uuid=f"u{i}", seconds=1.5)
                r = await c.post("/api/call", data=form())
                return r.status, await r.json()
            finally:
                await c.close()
    status, body = run(go())
    assert status == 402 and body["payment_required"]
    assert store.free_calls_used(PHONE) == 3


def test_unanswered_call_does_not_use_a_free_call(world):
    async def go():
        c = await _client(verified_cookie(PHONE))
        try:
            await c.post("/api/call", data=form())
        finally:
            await c.close()
    run(go())
    assert store.free_calls_used(PHONE) == 0  # counted only when the call connects


def test_day_pass_via_checkout(world, monkeypatch):
    store._write(f"usage/{store.phone_hash(PHONE)}.json", {"free_calls_used": 3})
    monkeypatch.setattr(payments, "create_order", lambda phone: {"id": "order_1", "amount": 49900})
    monkeypatch.setattr(payments, "fetch_payment", lambda pid: {"status": "captured", "order_id": "order_1",
                                                                "amount": 49900, "method": "upi"})

    async def go():
        c = await _client(verified_cookie(PHONE))
        try:
            order = await (await c.post("/api/pay/order")).json()
            bad = await c.post("/api/pay/verify", json={"razorpay_order_id": "order_1",
                                                        "razorpay_payment_id": "pay_1", "razorpay_signature": "x"})
            good = await c.post("/api/pay/verify", json={
                "razorpay_order_id": "order_1", "razorpay_payment_id": "pay_1",
                "razorpay_signature": _sig(config.RAZORPAY_KEY_SECRET, b"order_1|pay_1")})
            session = await (await c.get("/api/session")).json()
            call = await c.post("/api/call", data=form())
            return order, bad.status, good.status, session, call.status
        finally:
            await c.close()
    order, bad, good, session, call = run(go())
    assert order["order_id"] == "order_1" and order["amount"] == 49900
    assert bad == 400 and good == 200
    assert session["pass_calls_left"] == config.DAY_PASS_CALLS
    assert call == 200
    assert json.loads(world.db["payments/pay_1.json"])["phone_last4"] == PHONE[-4:]


def test_webhook_grants_pass_once(world):
    store.save_order("order_2", PHONE, 49900)
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_2", "order_id": "order_2", "amount": 49900, "method": "upi"}}}}).encode()

    async def go():
        c = await _client()
        try:
            bad = await c.post("/api/razorpay/webhook", data=body, headers={"X-Razorpay-Signature": "nope"})
            sig = _sig(config.RAZORPAY_WEBHOOK_SECRET, body)
            for _ in range(2):  # Razorpay retries: must not grant twice
                ok = await c.post("/api/razorpay/webhook", data=body, headers={"X-Razorpay-Signature": sig})
            return bad.status, ok.status
        finally:
            await c.close()
    assert run(go()) == (400, 200)
    assert len(store.load_profile(PHONE)["passes"]) == 1


def test_returning_caller_needs_only_number(world, monkeypatch):
    fake = FakeGemini()

    async def go():
        async with fake as url:
            monkeypatch.setattr(config, "GEMINI_WS_URL", url)
            c = await _client(verified_cookie(PHONE))
            try:
                assert (await c.post("/api/call", data=form())).status == 200
                await run_phone_leg(c, world.placed[-1][1], seconds=2)
                import aiohttp
                f = aiohttp.FormData()
                for k, v in {"phone": "9876543210", "consent": "yes"}.items():
                    f.add_field(k, v)
                f.add_field("resume", b"", filename="")  # what a browser sends with no file chosen
                r = await c.post("/api/call", data=f)
                assert r.status == 200, await r.text()
                await run_phone_leg(c, world.placed[-1][1], call_uuid="u2", seconds=2)
            finally:
                await c.close()
    run(go())
    second = fake.prompts[1]
    assert "Statusneo" in second                       # resume remembered
    assert "CALL 1" in second and "Head of AI" in second  # previous call remembered
