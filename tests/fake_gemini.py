"""A stand-in Gemini Live server and a stand-in Vobiz phone leg for tests."""
import asyncio
import audioop
import base64
import html
import json
import re

import websockets


def audio_part(samples: int = 2400) -> dict:
    return {"inlineData": {"data": base64.b64encode(b"\x10\x00" * samples).decode()}}


class FakeGemini:
    """Greets, then on the first caller audio saves a summary (unless drop=True,
    in which case it closes the session like a dropped call), then says goodbye.
    Sends usageMetadata each turn like the real service."""

    def __init__(self, drop: bool = False):
        self.drop = drop
        self.prompts = []
        self.server = None

    async def handler(self, ws):
        setup = json.loads(await ws.recv())
        self.prompts.append(setup["setup"]["systemInstruction"]["parts"][0]["text"])
        await ws.send(json.dumps({"setupComplete": {}}))
        usage = {"usageMetadata": {"promptTokenCount": 1000, "responseTokenCount": 100,
                                   "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 800},
                                                           {"modality": "AUDIO", "tokenCount": 200}],
                                   "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 100}]}}
        heard = False
        async for raw in ws:
            m = json.loads(raw)
            if m.get("realtimeInput", {}).get("text") == "[CALL_STARTED]":
                await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "Hi, this is Disha."},
                                                            "modelTurn": {"parts": [audio_part()]}}}))
                await ws.send(json.dumps({"serverContent": {"turnComplete": True}, **usage}))
            elif "audio" in m.get("realtimeInput", {}) and not heard:
                heard = True
                await ws.send(json.dumps({"serverContent": {"inputTranscription": {"text": "I want to lead AI teams"}}}))
                if self.drop:
                    return
                await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "Got it. Let me recap."},
                                                            "modelTurn": {"parts": [audio_part(800)]}}}))
                await ws.send(json.dumps({"serverContent": {"inputTranscription": {"text": "Yes, that's right."}}}))
                await ws.send(json.dumps({"toolCall": {"functionCalls": [{"id": "1", "name": "save_session_summary", "args": {
                    "current_situation": "AI lead", "goal": "Head of AI in 18 months", "target_role": "Head of AI",
                    "obstacles": "visibility", "next_steps": "1. Publish a case study", "follow_up_wanted": "yes"}}]}}))
            elif "toolResponse" in m:
                await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "All the best!"},
                                                            "modelTurn": {"parts": [audio_part(1200)]}}}))
                await ws.send(json.dumps({"serverContent": {"turnComplete": True}, **usage}))

    async def __aenter__(self):
        self.server = await websockets.serve(self.handler, "127.0.0.1", 0)
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def __aexit__(self, *exc):
        self.server.close()


async def run_phone_leg(client, answer_url: str, call_uuid: str = "uuid-1", seconds: float = 5) -> int:
    """Play Vobiz: POST the answer URL, open the stream it returns, send silence.
    Returns the number of agent audio bytes received."""
    path = lambda url: re.sub(r"^\w+://[^/]+", "", url)  # PUBLIC_URL host → test server
    xml = await (await client.post(path(answer_url), data={"CallUUID": call_uuid})).text()
    ws_url = html.unescape(re.search(r">(ws[^<]+)<", xml).group(1))
    ws = await client.ws_connect(path(ws_url))
    silence = base64.b64encode(audioop.lin2ulaw(b"\x00\x00" * 160, 2)).decode()
    received = 0

    async def pump():
        nonlocal received
        async for msg in ws:
            data = json.loads(msg.data)
            if data.get("event") == "playAudio":
                received += len(base64.b64decode(data["media"]["payload"]))

    async def speak():
        for _ in range(int(seconds / 0.02)):
            if ws.closed:
                return
            await ws.send_str(json.dumps({"event": "media", "media": {"payload": silence}}))
            await asyncio.sleep(0.02)
        await ws.close()

    await asyncio.wait_for(asyncio.gather(pump(), speak()), seconds + 10)
    await asyncio.sleep(0.5)  # let the post-call thread finish
    return received
