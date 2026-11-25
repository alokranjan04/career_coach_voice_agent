"""Google Sheets and Gmail. All blocking — call via asyncio.to_thread."""
import json
import smtplib
import time
from email.mime.text import MIMEText

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build

from agent import config

_CREDS: dict = {}
_SHEETS = {"service": None, "built_at": 0.0}
_HEADERS_DONE: set = set()


def google_credentials(scopes: list):
    """(credentials, project). Uses GOOGLE_CREDENTIALS key JSON if set, else the
    runtime identity (Cloud Run service account via ADC). New GCP projects block
    service-account key creation by default, so keyless is the normal path.
    Cached: the ADC lookup is a network call and must not run per request."""
    key = tuple(scopes)
    if key not in _CREDS:
        raw = config.env("GOOGLE_CREDENTIALS")
        if raw:
            info = json.loads(raw)
            _CREDS[key] = (service_account.Credentials.from_service_account_info(
                info, scopes=scopes), info.get("project_id"))
        else:
            _CREDS[key] = google.auth.default(scopes=scopes)
    return _CREDS[key]


def _sheets():
    if not _SHEETS["service"] or time.time() - _SHEETS["built_at"] > 3000:
        creds, _ = google_credentials(["https://www.googleapis.com/auth/spreadsheets"])
        _SHEETS["service"] = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _SHEETS["built_at"] = time.time()
    return _SHEETS["service"]


def append_row(tab: str, header: list, row: list) -> None:
    """Append one row; creates the tab and writes the header if needed."""
    if not config.SPREADSHEET_ID:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID is not set")
    sheets = _sheets().spreadsheets()
    values = sheets.values()
    if tab not in _HEADERS_DONE:
        meta = sheets.get(spreadsheetId=config.SPREADSHEET_ID, fields="sheets.properties.title").execute()
        if tab not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
            sheets.batchUpdate(spreadsheetId=config.SPREADSHEET_ID, body={
                "requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
        existing = (values.get(spreadsheetId=config.SPREADSHEET_ID,
                               range=f"{tab}!1:1").execute().get("values") or [[]])[0]
        # Write the header if the tab is empty, or extend it when columns were
        # added at the end (older rows stay aligned).
        if not existing or (existing != header and header[:len(existing)] == existing):
            values.update(spreadsheetId=config.SPREADSHEET_ID, range=f"{tab}!A1",
                          valueInputOption="RAW", body={"values": [header]}).execute()
        _HEADERS_DONE.add(tab)
    values.append(spreadsheetId=config.SPREADSHEET_ID, range=f"{tab}!A1",
                  valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                  body={"values": [row]}).execute()


def read_rows(tab: str) -> list:
    """All data rows of a tab as dicts keyed by the header row ([] if the tab is missing)."""
    try:
        values = _sheets().spreadsheets().values().get(
            spreadsheetId=config.SPREADSHEET_ID, range=f"{tab}!A:ZZ").execute().get("values", [])
    except Exception:
        return []
    if not values:
        return []
    header = values[0]
    return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in values[1:]]


def delete_rows_where(tab: str, keep) -> int:
    """Delete data rows for which keep(row_dict) is False. Returns rows deleted.
    Used for data-erasure requests and the retention policy."""
    sheets = _sheets().spreadsheets()
    meta = sheets.get(spreadsheetId=config.SPREADSHEET_ID, fields="sheets.properties").execute()
    sheet_id = next((s["properties"]["sheetId"] for s in meta.get("sheets", [])
                     if s["properties"]["title"] == tab), None)
    if sheet_id is None:
        return 0
    rows = read_rows(tab)
    doomed = [i + 1 for i, row in enumerate(rows) if not keep(row)]  # +1 skips the header
    if not doomed:
        return 0
    # Delete bottom-up so earlier indices stay valid.
    requests_ = [{"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                "startIndex": i, "endIndex": i + 1}}}
                 for i in sorted(doomed, reverse=True)]
    sheets.batchUpdate(spreadsheetId=config.SPREADSHEET_ID, body={"requests": requests_}).execute()
    return len(doomed)


def send_email(to: str, subject: str, body: str) -> bool:
    """Send via Gmail SMTP with an app password."""
    if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD and to):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, config.GMAIL_USER, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    return True
