"""Configuration: environment (.env only) plus the agent definition files."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Load ONLY .env. A second file (.env.local etc.) full of placeholders once
# silently replaced real keys in production — keep exactly one source.
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


AGENT = json.loads((ROOT / "agent_config.json").read_text(encoding="utf-8"))
PROMPT_TEMPLATE = (ROOT / AGENT.get("prompt_file", "agent/prompt.md")).read_text(encoding="utf-8")
TOOL_DECLARATIONS = json.loads(
    (ROOT / AGENT.get("tools_file", "agent/tools.json")).read_text(encoding="utf-8"))

GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage"
    ".v1beta.GenerativeService.BidiGenerateContent?key=" + GEMINI_API_KEY
)  # contains the key — never log it

TELEPHONY_PROVIDER = env("TELEPHONY_PROVIDER", "vobiz").lower()
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")
PORT = int(env("PORT", "8080"))

# Post-call research (agent/resources.py): text model with Google Search grounding.
RESEARCH_MODEL = env("RESEARCH_MODEL", "gemini-3.8-flash")
RESEARCH_ENABLED = env("RESEARCH_ENABLED", "1") == "1"

# Outbound calls (Vobiz REST API). FROM must be a Vobiz India number on this account.
VOBIZ_AUTH_ID = env("VOBIZ_AUTH_ID")
VOBIZ_AUTH_TOKEN = env("VOBIZ_AUTH_TOKEN")
VOBIZ_FROM_NUMBER = env("VOBIZ_FROM_NUMBER").lstrip("+")
RING_SECS = int(env("RING_SECS", "45"))

# Pending calls (profile + resume text) live in a private bucket between the web
# request and Vobiz's answer callback, which may reach a different instance.
SESSIONS_BUCKET = env("SESSIONS_BUCKET")
CALL_COOLDOWN_SECS = int(env("CALL_COOLDOWN_SECS", "300"))
MAX_CALLS_PER_DAY = int(env("MAX_CALLS_PER_DAY", "3"))

# Gemini audio sessions are capped at 15 minutes: nudge a recap, then end cleanly.
WRAP_UP_SECS = int(env("WRAP_UP_SECS", "660"))
MAX_CALL_SECS = int(env("MAX_CALL_SECS", "840"))
MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_RESUME_CHARS = 15000

SPREADSHEET_ID = env("GOOGLE_SPREADSHEET_ID")
CALL_LOG_TAB = env("CALL_LOG_TAB", "Call_Logs")

GMAIL_USER = env("GMAIL_USER")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD")


def system_prompt(name: str, resume: str, history: str) -> str:
    # str.replace, not format(): resumes contain braces. {resume} and {history}
    # last, so text inside them is never treated as a placeholder.
    return (PROMPT_TEMPLATE.replace("{name}", name).replace("{history}", history)
            .replace("{resume}", resume))
