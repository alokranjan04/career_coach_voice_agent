"""Regression test for prompt changes: real calls with simulated callers.

    GEMINI_API_KEY=... python tests/regression/run_regression.py [--turns 9]

For each persona, a simulated caller (a Gemini text model playing the persona)
talks to the REAL coach: the Gemini Live model with the production prompt, tools
and voice. Caller turns are sent as text and the coach's replies are read from
its audio transcription, so this exercises the same model and setup as a phone
call, minus the phone line. Each conversation is scored with the quality rubric
(agent/quality.py); any criterion below its threshold fails the run (exit 1).
GitHub Actions runs this before deploying a change to the prompt or tools.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import requests
import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent import config, quality  # noqa: E402

RESUME = """Alok Ranjan — Forward Deployed AI Engineering Lead, Statusneo (client: McCain Foods), 2023–present.
Led GenAI rollout for supply-chain teams: LLM agents for demand-planning queries used by 300 planners.
Previously Senior Data Engineer, Infosys (2016–2023): Azure data platforms for retail clients.
B.Tech Computer Science, 2016. Skills: Python, LLM agents, Azure, data engineering, stakeholder demos."""

FRESHER_RESUME = """Priya Nair — Final-year B.Tech (ECE), VIT Vellore, CGPA 8.1, graduating 2027.
Internship: Data Analyst Intern, a Bengaluru fintech startup (3 months): built Power BI dashboards for loan collections.
Projects: churn prediction in Python (scikit-learn); college fest sponsorship lead. Skills: SQL, Python, Excel, Power BI."""

PERSONAS = {
    "mid_career_hinglish": (RESUME, "Alok",
        "You are Alok, an AI engineering lead who wants to become Head of AI within 18 months but is seen as too "
        "hands-on. You mostly speak Hinglish (Hindi-English mix), e.g. 'Main leadership mein jaana chahta hoon'. "
        "Answer the coach's questions honestly and briefly, one or two sentences. When the coach recaps, say yes."),
    "fresher": (FRESHER_RESUME, "Priya",
        "You are Priya, a final-year engineering student who wants a first job as a data analyst but has had no "
        "interview calls yet. You're anxious and a bit vague at first. Answer in short English sentences. "
        "When the coach recaps, say yes."),
    "off_topic": (RESUME, "Alok",
        "You are Alok. Early on, ask the coach for stock market tips and which crypto to buy. If redirected, "
        "talk about wanting to move from engineering to a product management role. Short answers. "
        "When the coach recaps, say yes."),
}

CALLER_PROMPT = """{persona}
You are on a phone call with Disha, an AI career coach. Here is the conversation so far:
{transcript}
Reply with ONLY your next spoken line as the caller (no labels, no quotes). If the coach has said goodbye, reply END."""


def caller_line(persona: str, transcript: list) -> str:
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{config.QUALITY_MODEL}:generateContent",
        params={"key": config.GEMINI_API_KEY}, timeout=60,
        json={"contents": [{"role": "user", "parts": [{"text": CALLER_PROMPT.format(
            persona=persona, transcript="\n".join(transcript))}]}], "generationConfig": {"temperature": 0.7}})
    r.raise_for_status()
    return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()


async def coach_turn(gem, transcript: list) -> tuple:
    """Read one coach turn: returns (spoken text, saved_summary_now)."""
    said, saved = "", False
    while True:
        data = json.loads(await asyncio.wait_for(gem.recv(), 60))
        sc = data.get("serverContent", {})
        said += (sc.get("outputTranscription") or {}).get("text", "")
        if "toolCall" in data:
            calls = data["toolCall"].get("functionCalls", [])
            saved = saved or any(c.get("name") == "save_session_summary" for c in calls)
            await gem.send(json.dumps({"toolResponse": {"functionResponses": [
                {"id": c.get("id"), "name": c.get("name"), "response": {"result": {"success": True}}} for c in calls]}}))
        if sc.get("turnComplete") and said.strip():
            return said.strip(), saved


async def run_persona(name: str, max_turns: int) -> dict:
    resume, user, persona = PERSONAS[name]
    setup = {"setup": {
        "model": config.AGENT["model"],
        "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
            "prebuiltVoiceConfig": {"voiceName": config.AGENT.get("voice", "Aoede")}}}},
        "outputAudioTranscription": {},
        "systemInstruction": {"parts": [{"text": config.system_prompt(
            user, resume, "(none: this is the first call)", "test@example.com", True)}]},
        "tools": config.TOOL_DECLARATIONS}}
    transcript, saved = [], False
    async with websockets.connect(config.GEMINI_WS_URL, max_size=None, open_timeout=20) as gem:
        await gem.send(json.dumps(setup))
        first = json.loads(await asyncio.wait_for(gem.recv(), 15))
        assert "setupComplete" in first, f"setup rejected: {first}"  # prompt/tools must be accepted
        await gem.send(json.dumps({"realtimeInput": {"text": "[CALL_STARTED]"}}))
        for turn in range(max_turns):
            said, saved_now = await coach_turn(gem, transcript)
            saved = saved or saved_now
            transcript.append(f"Coach: {said}")
            if saved_now:
                break
            line = await asyncio.to_thread(caller_line, persona, transcript)
            if line.upper().startswith("END"):
                break
            if turn == max_turns - 4:
                line += " (I need to go soon, can we wrap up?)"
            transcript.append(f"Caller: {line}")
            await gem.send(json.dumps({"realtimeInput": {"text": line}}))
    result = await asyncio.to_thread(quality.score_transcript, "\n".join(transcript), resume)
    result["saved"] = saved
    result["transcript"] = transcript
    return result


async def main(turns: int) -> int:
    failed = False
    for name in PERSONAS:
        result = await run_persona(name, turns)
        problems = quality.failures(result)
        status = "FAIL" if problems else "PASS"
        failed |= bool(problems)
        print(f"\n=== {name}: {status} (overall {result['overall']}, summary saved: {result['saved']})")
        print("scores: " + ", ".join(f"{k}={v}" for k, v in result["scores"].items()))
        for p in problems:
            print(f"  below threshold: {p}")
        for issue in result["issues"]:
            print(f"  issue: {issue}")
        print("  transcript:\n    " + "\n    ".join(t[:240] for t in result["transcript"]))
    print("\nREGRESSION " + ("FAILED" if failed else "PASSED"))
    return 1 if failed else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--turns", type=int, default=14)
    sys.exit(asyncio.run(main(p.parse_args().turns)))
