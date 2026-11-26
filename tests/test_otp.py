"""Voice OTP: request a code call, the call reads the code once, verify it once."""
import asyncio
import re

from aiohttp.test_utils import TestClient, TestServer

import app as appmod
from agent import auth, otp

PHONE = "919876543210"


def run(coro):
    return asyncio.run(coro)


async def _client():
    c = TestClient(TestServer(appmod.make_app()))
    await c.start_server()
    return c


def _path(url: str) -> str:
    return re.sub(r"^\w+://[^/]+", "", url)


def test_voice_otp_flow(world):
    async def go():
        c = await _client()
        try:
            sent = await c.post("/api/otp/send", json={"phone": "98765 43210"})
            assert sent.status == 200, await sent.text()
            to, answer_url, _ = world.placed[-1]
            assert to == PHONE and "k=" in answer_url and otp.TTL_SECS > 0
            xml = await (await c.post(_path(answer_url))).text()
            code = "".join(re.findall(r"\d", xml.split("is:")[1].split(".")[0]))
            again = await (await c.post(_path(answer_url))).text()  # answer URL is single-use
            wrong = await c.post("/api/otp/verify", json={"phone": "9876543210", "code": "000000" if code != "000000" else "111111"})
            right = await c.post("/api/otp/verify", json={"phone": "9876543210", "code": code})
            cookie = right.cookies.get(auth.COOKIE)
            reuse = await c.post("/api/otp/verify", json={"phone": "9876543210", "code": code})
            return xml, code, again, wrong.status, right.status, cookie, reuse.status
        finally:
            await c.close()
    xml, code, again, wrong, right, cookie, reuse = run(go())
    assert len(code) == 6 and 'language="en-IN"' in xml and "<Hangup/>" in xml
    assert "<Speak" not in again and "<Hangup/>" in again
    assert wrong == 400 and right == 200 and cookie is not None
    assert reuse == 400  # a used code can't verify again
    assert any(k.startswith("spend/") for k in world.db)  # code calls count toward spend


def test_code_never_in_urls(world):
    async def go():
        c = await _client()
        try:
            await c.post("/api/otp/send", json={"phone": "9876543210"})
        finally:
            await c.close()
    run(go())
    code = world.db and __import__("json").loads(world.db[f"otp/{PHONE}.json"])["code"]
    _, answer_url, hangup_url = world.placed[-1]
    assert code not in answer_url and code not in hangup_url


def test_rate_limits(world, monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(otp.time, "time", lambda: clock["t"])

    async def send():
        c = await _client()
        try:
            r = await c.post("/api/otp/send", json={"phone": "9876543210"})
            return r.status
        finally:
            await c.close()
    assert run(send()) == 200
    assert run(send()) == 429            # 30-second cooldown
    clock["t"] += 40
    assert run(send()) == 200
    clock["t"] += 40
    assert run(send()) == 200
    clock["t"] += 40
    assert run(send()) == 429            # 3 per hour per number


def test_too_many_wrong_attempts(world):
    async def go():
        c = await _client()
        try:
            await c.post("/api/otp/send", json={"phone": "9876543210"})
            statuses = []
            for _ in range(otp.MAX_ATTEMPTS + 1):
                r = await c.post("/api/otp/verify", json={"phone": "9876543210", "code": "abcdef"})
                statuses.append(r.status)
            return statuses
        finally:
            await c.close()
    statuses = run(go())
    assert statuses[:otp.MAX_ATTEMPTS] == [400] * otp.MAX_ATTEMPTS and statuses[-1] == 429
