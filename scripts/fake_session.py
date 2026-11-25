"""Simulate the browser side of a session — no mic needed.

    python scripts/fake_session.py                          # ws://localhost:8080/session
    python scripts/fake_session.py wss://<service>/session --secs 15

Sends a start message with a sample resume, then silence as 16 kHz PCM16,
collects the coach's audio and writes it to fake_session_agent.wav. If the
greeting arrives, the server, Gemini key, credits and prompt all work.
"""
import argparse
import asyncio
import json
import wave

import websockets

SAMPLE_RESUME = """Priya Sharma — Software Engineer, Bengaluru
Experience: Infosys, Software Engineer, 2021–present. Built REST APIs in Java and Spring Boot
for a banking client; led migration of a reporting service to AWS, cutting report time by 40%.
Education: B.Tech Computer Science, VIT Vellore, 2021.
Skills: Java, Spring Boot, SQL, AWS, Python basics. Interested in data engineering."""


async def main(url: str, secs: float, email: str) -> None:
    agent = bytearray()
    silence = b"\x00\x00" * 640  # 40 ms at 16 kHz
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "name": "Priya", "email": email,
                                  "language": "English", "resume": SAMPLE_RESUME}))

        async def send_silence():
            for _ in range(int(secs / 0.04)):
                await ws.send(silence)
                await asyncio.sleep(0.04)
            await ws.send(json.dumps({"type": "stop"}))

        async def receive():
            async for msg in ws:
                if isinstance(msg, bytes):
                    agent.extend(msg)
                else:
                    print("server:", msg[:200])

        sender = asyncio.create_task(send_silence())
        try:
            await asyncio.wait_for(receive(), timeout=secs + 10)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        sender.cancel()

    with wave.open("fake_session_agent.wav", "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(bytes(agent))
    seconds = len(agent) / 48000
    print(f"agent audio received: {seconds:.1f}s -> fake_session_agent.wav")
    print("PASS: greeting received" if seconds > 1 else "FAIL: no agent audio — check server logs")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", default="ws://localhost:8080/session")
    p.add_argument("--secs", type=float, default=15)
    p.add_argument("--email", default="test@example.com")
    a = p.parse_args()
    asyncio.run(main(a.url, a.secs, a.email))
