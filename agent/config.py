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

PORT = int(env("PORT", "8080"))

# Gemini audio sessions are capped at 15 minutes: nudge a recap, then end cleanly.
WRAP_UP_SECS = int(env("WRAP_UP_SECS", "660"))
MAX_SESSION_SECS = int(env("MAX_SESSION_SECS", "840"))
MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_RESUME_CHARS = 15000

SPREADSHEET_ID = env("GOOGLE_SPREADSHEET_ID")
SESSION_LOG_TAB = env("SESSION_LOG_TAB", "Session_Logs")

GMAIL_USER = env("GMAIL_USER")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD")


def system_prompt(name: str, language: str, resume: str) -> str:
    # str.replace, not format(): resumes contain braces.
    return (PROMPT_TEMPLATE.replace("{name}", name).replace("{language}", language)
            .replace("{resume}", resume))
