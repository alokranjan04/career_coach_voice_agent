"""Simulate the telephony side of a call — no phone needed.

    python scripts/fake_call.py ws://localhost:8080/stream --sid SID --token TOKEN
    python scripts/fake_call.py wss://<service>/stream --sid SID --token TOKEN --secs 15

SID and TOKEN must belong to a call request in the sessions bucket
(calls/<sid>.json with "token" and "resume" set). Sends silence as mu-law 8 kHz media frames (Vobiz format, or Twilio with
--twilio), collects the agent's audio and writes it to fake_call_agent.wav.
If the greeting arrives, the server, Gemini key, credits and prompt all work.
"""
import argparse
import asyncio
import audioop
import base64
import json
import wave

import websockets


async def main(url: str, secs: float, twilio: bool, sid: str, token: str) -> None:
    query = f"?sid={sid}&token={token}"
    agent = bytearray()
    silence = audioop.lin2ulaw(b"\x00\x00" * 160, 2)  # 20 ms
    async with websockets.connect(url + ("" if twilio else query), max_size=None) as ws:
        if twilio:
            await ws.send(json.dumps({"event": "start", "start": {
                "streamSid": "MZfake", "customParameters": {"sid": sid, "token": token}}}))

        async def send_silence():
            for _ in range(int(secs / 0.02)):
                frame = {"event": "media", "media": {"payload": base64.b64encode(silence).decode()}}
                if twilio:
                    frame["streamSid"] = "MZfake"
                await ws.send(json.dumps(frame))
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"event": "stop"}))

        async def receive():
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("event") in ("playAudio", "media"):
                    agent.extend(audioop.ulaw2lin(base64.b64decode(msg["media"]["payload"]), 2))

        sender = asyncio.create_task(send_silence())
        try:
            await asyncio.wait_for(receive(), timeout=secs + 5)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        sender.cancel()

    with wave.open("fake_call_agent.wav", "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(bytes(agent))
    seconds = len(agent) / 16000
    print(f"agent audio received: {seconds:.1f}s -> fake_call_agent.wav")
    print("PASS: greeting received" if seconds > 1 else "FAIL: no agent audio — check server logs")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", default="ws://localhost:8080/stream")
    p.add_argument("--secs", type=float, default=12)
    p.add_argument("--twilio", action="store_true")
    p.add_argument("--sid", required=True)
    p.add_argument("--token", required=True)
    a = p.parse_args()
    asyncio.run(main(a.url, a.secs, a.twilio, a.sid, a.token))
