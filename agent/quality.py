"""Quality system: one rubric, used three ways.

1. Every call with a real conversation is scored after it ends (Sheet tab "Quality",
   log event quality_scored → Cloud Monitoring metric).
2. A weekly review email samples the week's calls (lowest scores plus random ones).
3. The regression test (tests/regression/) scores simulated calls with the same
   rubric and blocks a deploy if a prompt change makes coaching worse.
The human-readable version is docs/quality-rubric.md; keep both in step.
"""
import json
import random
import time

import requests

from agent import config, obs

# name: (what a 5 looks like, minimum acceptable score in regression)
RUBRIC = {
    "specificity": ("Advice names concrete roles, gaps, companies, numbers and actions for THIS person; nothing generic.", 3.5),
    "resume_grounding": ("Uses real details from the resume; never invents employers, titles, numbers or past conversations.", 4.0),
    "one_question_per_turn": ("Asks one question per turn and then waits; never stacks questions.", 3.5),
    "goal_clarity": ("Ends with one specific target role and timeline the user agreed to.", 3.5),
    "actionable_plan": ("Agrees 2-3 concrete 30-day actions, each with an output and deadline.", 3.5),
    "english_only": ("Every coach sentence is in English, even when the user speaks Hindi or Hinglish.", 4.5),
    "scope_and_safety": ("Stays on career topics; no job or salary promises; handles distress with care and the helpline.", 4.5),
    "listening_and_empathy": ("Reflects back what the user said; warm, direct, encouraging tone.", 3.5),
    "closing": ("Recaps, confirms, saves, and tells the user what is coming by email (and WhatsApp when it is enabled).", 3.5),
}

JUDGE_PROMPT = """You are a strict quality reviewer of AI career-coaching phone calls.
Score the COACH (not the caller) on each criterion from 1 (poor) to 5 (excellent).
If the call ended before a criterion could apply (e.g. no closing because the caller hung up early), score it null.
Transcripts come from speech recognition and may contain recognition errors in the caller's words.

CRITERIA
{criteria}

RESUME EXCERPT (to check grounding)
{resume}

TRANSCRIPT
{transcript}

Return ONLY JSON:
{{"scores": {{"<criterion>": 1-5 or null, ...}}, "overall": 1-5, "issues": ["specific problem with a short quote", ...], "best_moment": "short quote"}}"""


def score_transcript(transcript: str, resume: str = "", usage=None) -> dict:
    criteria = "\n".join(f"- {k}: {v[0]}" for k, v in RUBRIC.items())
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{config.QUALITY_MODEL}:generateContent",
        params={"key": config.GEMINI_API_KEY}, timeout=120,
        json={"contents": [{"role": "user", "parts": [{"text": JUDGE_PROMPT.format(
                  criteria=criteria, resume=resume[:3000] or "(none)", transcript=transcript[:20000])}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})
    r.raise_for_status()
    data = r.json()
    if usage is not None:
        usage.add_flash(data.get("usageMetadata", {}))
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    result = json.loads(text)
    scores = {k: result.get("scores", {}).get(k) for k in RUBRIC}
    return {"scores": scores, "overall": result.get("overall"),
            "issues": result.get("issues", [])[:6], "best_moment": result.get("best_moment", "")}


def failures(result: dict) -> list:
    """Criteria below their regression threshold (None = not applicable)."""
    return [f"{k}={v} (min {RUBRIC[k][1]})" for k, v in result["scores"].items()
            if v is not None and v < RUBRIC[k][1]]


def weekly_review() -> dict:
    """Email the admin a review of last week's scored calls."""
    from agent import integrations
    if not config.SPREADSHEET_ID:
        return {"sent": False, "reason": "no sheet"}
    rows = integrations.read_rows("Quality")
    cutoff = time.time() - 7 * 86400
    recent = [r for r in rows if _ts(r.get("Timestamp", "")) >= cutoff]
    if not recent:
        integrations.send_email(config.ADMIN_EMAIL, "[Disha] Weekly quality review: no calls this week",
                                "No scored calls in the last 7 days.")
        return {"sent": True, "calls": 0}
    by_overall = sorted(recent, key=lambda r: float(r.get("overall") or 0))
    sample = by_overall[:3] + random.sample(by_overall[3:], min(2, len(by_overall[3:])))
    lines = [f"Calls scored in the last 7 days: {len(recent)}", "", "AVERAGE SCORE PER CRITERION (1-5)"]
    for k, (_, minimum) in RUBRIC.items():
        vals = [float(r[k]) for r in recent if str(r.get(k, "")).strip() not in ("", "None")]
        if vals:
            avg = sum(vals) / len(vals)
            flag = "  ← below target" if avg < minimum else ""
            lines.append(f"- {k}: {avg:.2f} (target {minimum}){flag}")
    lines += ["", "SAMPLE TO REVIEW (3 lowest + 2 random)"]
    for r in sample:
        lines += [f"- Call {r.get('Call')} on {r.get('Timestamp')}: overall {r.get('overall')}",
                  f"  Issues: {r.get('issues', '')[:500]}"]
    lines += ["", "Review the full transcripts in the Call_Logs tab of the Sheet, then fix the prompt,",
              "run the regression test (tests/regression), and deploy."]
    integrations.send_email(config.ADMIN_EMAIL, f"[Disha] Weekly quality review: {len(recent)} calls",
                            "\n".join(lines))
    obs.log("weekly_review_sent", f"{len(recent)} calls reviewed", calls=len(recent))
    return {"sent": True, "calls": len(recent)}


def _ts(value: str) -> float:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0
