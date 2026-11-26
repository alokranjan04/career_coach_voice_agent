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
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")  # run.app address: Vobiz callbacks, media stream, scheduler audience
SITE_URL = (env("SITE_URL") or PUBLIC_URL).rstrip("/")  # address people see, e.g. https://coach.tellyourjourney.com
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
ADMIN_EMAIL = env("ADMIN_EMAIL", "ranjanalok2032@gmail.com")  # alerts and weekly quality review

# ---------- phone verification (Identity Platform / Firebase Auth) ----------
GCP_PROJECT = env("GCP_PROJECT", "project-b2f8fd7f-3661-4447-8b6")
FIREBASE_WEB_API_KEY = env("FIREBASE_WEB_API_KEY")  # browser key, restricted by referrer
SESSION_SECRET = env("SESSION_SECRET")  # signs the "verified phone" cookie
SESSION_DAYS = int(env("SESSION_DAYS", "180"))  # a verified number isn't asked again for this long

# ---------- plans and payments (Razorpay) ----------
FREE_CALLS = int(env("FREE_CALLS", "3"))
DAY_PASS_PAISE = int(env("DAY_PASS_PAISE", "49900"))  # ₹499
DAY_PASS_CALLS = int(env("DAY_PASS_CALLS", "3"))
DAY_PASS_HOURS = int(env("DAY_PASS_HOURS", "24"))
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = env("RAZORPAY_WEBHOOK_SECRET")

# ---------- WhatsApp (Twilio) ----------
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = env("TWILIO_WHATSAPP_FROM")  # e.g. whatsapp:+14155238886
TWILIO_CONTENT_SID = env("TWILIO_CONTENT_SID")  # approved summary template (HX...)

# ---------- data protection (DPDP Act) ----------
CONSENT_VERSION = "2026-11-25"
RETENTION_DAYS = int(env("RETENTION_DAYS", "365"))  # profiles and transcripts after last activity

# ---------- spend guardrails (INR) ----------
DAILY_SPEND_CAP_INR = float(env("DAILY_SPEND_CAP_INR", "1500"))
GEMINI_MONTHLY_BUDGET_INR = float(env("GEMINI_MONTHLY_BUDGET_INR", "5000"))
VOBIZ_LOW_BALANCE_INR = float(env("VOBIZ_LOW_BALANCE_INR", "200"))
USD_INR = float(env("USD_INR", "88"))
# Gemini list prices, USD per million tokens. Estimates from third-party listings:
# check them against Google's pricing page and override here.
PRICE_LIVE_TEXT_IN = float(env("PRICE_LIVE_TEXT_IN", "0.75"))
PRICE_LIVE_AUDIO_IN = float(env("PRICE_LIVE_AUDIO_IN", "3.0"))
PRICE_LIVE_TEXT_OUT = float(env("PRICE_LIVE_TEXT_OUT", "4.5"))
PRICE_LIVE_AUDIO_OUT = float(env("PRICE_LIVE_AUDIO_OUT", "12.0"))
PRICE_FLASH_IN = float(env("PRICE_FLASH_IN", "0.5"))
PRICE_FLASH_OUT = float(env("PRICE_FLASH_OUT", "3.0"))
PRICE_SEARCH_PER_QUERY = float(env("PRICE_SEARCH_PER_QUERY", "0.014"))
PRICE_WHATSAPP_INR = float(env("PRICE_WHATSAPP_INR", "0.15"))
VOBIZ_RATE_PER_MIN_INR = float(env("VOBIZ_RATE_PER_MIN_INR", "0.45"))

# ---------- scheduled jobs ----------
SCHEDULER_SA = env("SCHEDULER_SA", f"career-coach-scheduler@{GCP_PROJECT}.iam.gserviceaccount.com")
QUALITY_MODEL = env("QUALITY_MODEL", "gemini-3.8-flash")


def mask_email(email: str) -> str:
    user, _, domain = email.partition("@")
    return f"{user[:1]}…@{domain}" if domain else ""


def system_prompt(name: str, resume: str, history: str, email: str = "", whatsapp: bool = False) -> str:
    # str.replace, not format(): resumes contain braces. {resume} and {history}
    # last, so text inside them is never treated as a placeholder.
    channels = f"your email, {mask_email(email)}" if email else "your email"
    if whatsapp:
        channels += ", and a short version on WhatsApp"
    return (PROMPT_TEMPLATE.replace("{name}", name).replace("{delivery}", channels)
            .replace("{history}", history).replace("{resume}", resume))
