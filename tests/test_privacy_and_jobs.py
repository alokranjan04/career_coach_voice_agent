"""Data rights (download, delete), retention, scheduled-job auth, and verification cookies."""
import asyncio
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import app as appmod
from agent import auth, config, store
from conftest import verified_cookie

PHONE = "919876543210"


def run(coro):
    return asyncio.run(coro)


async def _client(cookies=None):
    c = TestClient(TestServer(appmod.make_app()), cookies=cookies or {})
    await c.start_server()
    return c


def _seed(world):
    store.save_profile({"phone": PHONE, "name": "Alok", "email": "a@x.com", "resume": "cv",
                        "calls": [{"date": "x", "transcript": "Coach: hi"}]})
    store.record_consent(PHONE, ["coaching_call"], "1.2.3.4", "UA")
    store._write(f"usage/{store.phone_hash(PHONE)}.json", {"free_calls_used": 2})
    store.grant_day_pass(PHONE, "order_1", "pay_1", 49900)
    world.rows += [("Call_Logs", {"Call": "c1", "Phone": PHONE}), ("Call_Logs", {"Call": "c2", "Phone": "910000000000"}),
                   ("Sessions", {"Phone": PHONE}), ("Quality", {"Call": "c1"}), ("Quality", {"Call": "c2"})]


def test_download_needs_verification(world):
    async def go():
        c = await _client()
        try:
            return (await c.get("/api/my-data")).status
        finally:
            await c.close()
    assert run(go()) == 401


def test_download_and_delete(world):
    _seed(world)

    async def go():
        c = await _client(verified_cookie(PHONE))
        try:
            data = json.loads(await (await c.get("/api/my-data")).text())
            deleted = await (await c.post("/api/my-data/delete")).json()
            return data, deleted
        finally:
            await c.close()
    data, deleted = run(go())
    assert data["profile"]["name"] == "Alok" and data["consents"][0]["ip"] == "1.2.3.4"
    assert data["payments"][0]["payment_id"] == "pay_1"
    assert deleted["ok"]
    assert f"profiles/{PHONE}.json" not in world.db
    assert not any(k.startswith(f"consents/{PHONE}/") for k in world.db)
    assert "payments/pay_1.json" in world.db                     # tax record stays
    assert store.free_calls_used(PHONE) == 2                     # deletion doesn't reset free calls
    assert [r["Phone"] for t, r in world.rows if t == "Call_Logs"] == ["910000000000"]
    assert [r["Call"] for t, r in world.rows if t == "Quality"] == ["c2"]
    assert not [r for t, r in world.rows if t == "Sessions"]


def test_retention_cleanup(world, monkeypatch):
    old = time.time() - (config.RETENTION_DAYS + 5) * 86400
    store._write("profiles/911111111111.json", {"phone": "911111111111", "last_active": old, "calls": []})
    store._write("profiles/912222222222.json", {"phone": "912222222222", "last_active": time.time(),
                                                "calls": [{"ended_at": old}, {"ended_at": time.time()}]})
    result = store.retention_cleanup()
    assert result == {"deleted_profiles": 1, "pruned_calls": 1}
    assert "profiles/911111111111.json" not in world.db
    assert len(store.load_profile("912222222222")["calls"]) == 1


def test_cron_rejects_unauthenticated(world):
    async def go():
        c = await _client()
        try:
            return (await c.post("/internal/cron/hourly")).status
        finally:
            await c.close()
    assert run(go()) == 403


def test_cookie_tampering_and_expiry(world, monkeypatch):
    resp = web.Response()
    auth.set_cookie(resp, PHONE)
    value = resp.cookies[auth.COOKIE].value

    class Req:
        def __init__(self, v):
            self.cookies = {auth.COOKIE: v}
    assert auth.verified_phone(Req(value)) == PHONE
    import base64
    phone, issued, sig = base64.urlsafe_b64decode(value).decode().split("|")
    forged = base64.urlsafe_b64encode(f"919999999999|{issued}|{sig}".encode()).decode()
    assert auth.verified_phone(Req(forged)) == ""
    monkeypatch.setattr(config, "SESSION_DAYS", 0)
    time.sleep(1.1)
    assert auth.verified_phone(Req(value)) == ""
