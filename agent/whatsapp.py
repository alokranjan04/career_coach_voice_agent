"""WhatsApp summary through Twilio, using an approved Content template.

Business-initiated WhatsApp messages must use a template approved by Meta.
Create it in Twilio Console → Messaging → Content Template Builder (category
UTILITY) with the text in docs/whatsapp-template.md, then set TWILIO_CONTENT_SID.
Template variables can't contain line breaks, so values are flattened. Blocking."""
import json
import re

import requests

from agent import config


def enabled() -> bool:
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN
                and config.TWILIO_WHATSAPP_FROM and config.TWILIO_CONTENT_SID)


def _flat(text: str, limit: int) -> str:
    text = re.sub(r"\s*\n\s*", " · ", str(text or "").strip())
    text = re.sub(r"\s{4,}", "   ", text)
    return (text[:limit - 1] + "…") if len(text) > limit else (text or "-")


def send_summary(phone: str, name: str, summary: dict, email: str, top_links: list) -> str:
    """Send the summary template; returns Twilio's message SID."""
    variables = {
        "1": _flat(name, 60),
        "2": _flat(summary.get("goal", ""), 200),
        "3": _flat(summary.get("next_steps", ""), 500),
        "4": _flat(config.mask_email(email), 60),
        "5": _flat("  ".join(top_links[:2]) or "see your email", 300),
    }
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json",
        auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), timeout=20,
        data={"From": config.TWILIO_WHATSAPP_FROM, "To": f"whatsapp:+{phone}",
              "ContentSid": config.TWILIO_CONTENT_SID, "ContentVariables": json.dumps(variables)})
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio {r.status_code}: {r.text[:300]}")
    return r.json().get("sid", "")
