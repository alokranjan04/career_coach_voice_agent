"""Observability: structured logs that Cloud Logging parses, plus throttled
admin alerts.

Every log line is one JSON object on stdout. Cloud Run turns `severity` and
`message` into the log entry's fields and keeps the rest as jsonPayload, which
the log-based metrics and alert policies in deploy/observability.sh filter on
(jsonPayload.event, jsonPayload.cost_inr, ...).
"""
import json
import sys
import time


def log(event: str, message: str = "", severity: str = "INFO", **fields) -> None:
    entry = {"severity": severity, "event": event, "message": message or event, **fields}
    if "call_id" in fields:
        entry["logging.googleapis.com/labels"] = {"call_id": str(fields["call_id"])}
    print(json.dumps(entry, ensure_ascii=False, default=str), file=sys.stdout, flush=True)


def alert(key: str, subject: str, body: str, every_secs: int = 6 * 3600) -> bool:
    """Log an ALERT and email the admin, at most once per `every_secs` per key.
    The throttle lives in the bucket so it holds across instances."""
    from agent import config, integrations, store
    log("alert", subject, severity="ALERT", alert_key=key, detail=body[:2000])
    try:
        last = store.get_json(f"alerts/{key}.json") or {}
        if time.time() - last.get("sent_at", 0) < every_secs:
            return False
        store.put_json(f"alerts/{key}.json", {"sent_at": time.time(), "subject": subject})
        return integrations.send_email(config.ADMIN_EMAIL, f"[Disha alert] {subject}", body)
    except Exception as e:
        log("alert_failed", f"could not send alert {key}: {e!r}", severity="ERROR")
        return False
