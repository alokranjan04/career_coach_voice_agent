"""Runs Gemini tool calls. Each tool in agent_config.json["tools"] names a
handler here plus its sheet tab, column order and whether it ends the session."""
from datetime import datetime, timedelta, timezone

from agent import config, integrations

TZ = timezone(timedelta(minutes=int(config.AGENT.get("utc_offset_minutes", 330))))


def save_record(tool_name: str, spec: dict, session, args: dict) -> dict:
    """Append the tool's arguments as one sheet row, in the configured column order."""
    columns = spec.get("columns") or sorted(args)
    header = ["Timestamp", "Session", "Name", "Email"] + columns
    row = [datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), session.sid, session.name, session.email]
    row += [str(args.get(c, "")) for c in columns]
    integrations.append_row(spec.get("sheet_tab", tool_name), header, row)
    return {"success": True}


HANDLERS = {"save_record": save_record}


def spec_for(name: str) -> dict:
    return config.AGENT.get("tools", {}).get(name, {})


def ends_call(name: str) -> bool:
    return bool(spec_for(name).get("ends_call"))


def run(name: str, args: dict, session, already_done: set) -> dict:
    """Execute a tool call. Repeated calls of a session-ending tool return success
    without writing again — the model sometimes retries, and a duplicate row is
    worse than a skipped one."""
    spec = spec_for(name)
    handler = HANDLERS.get(spec.get("handler", ""))
    if handler is None:
        return {"success": False, "error": f"unknown tool {name}"}
    if spec.get("ends_call") and name in already_done:
        return {"success": True, "note": "already saved"}
    result = handler(name, spec, session, args)
    if result.get("success"):
        already_done.add(name)
    return result
