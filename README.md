# Disha — AI career coach (voice)

Users upload a resume in the web app, then talk to Disha in the browser for about
10 minutes. Disha asks questions grounded in the resume, helps the user name one
goal and two or three next steps, then saves a summary to a Google Sheet and emails
it to the user.

Built from the `telephony-voice-agent` skill template (the Mydoot pattern), with the
phone leg replaced by a browser WebSocket.

```
Browser ── POST /api/resume ──▶ Cloud Run (aiohttp) ── text extracted (PDF/DOCX/TXT)
   │  mic PCM16 16 kHz ──WSS /session──▶ agent/session.py ◀──WSS──▶ Gemini Live (gemini-3.8-live)
   ◀── coach PCM16 24 kHz ─────────────┘       │ save_session_summary ─▶ Google Sheet "Sessions"
                                              └ after the session ─▶ summary email · "Session_Logs" row
```

## Files you edit

| File | What it is |
|---|---|
| `agent/prompt.md` | The coach's instructions. `{name}`, `{language}`, `{resume}` are filled per session. |
| `agent/tools.json` | The `save_session_summary` tool the model calls at the end. |
| `agent_config.json` | Name, model, voice, and the sheet tab and columns for the tool. |

## Run locally

```bash
cp .env.example .env        # fill GEMINI_API_KEY; sheet and Gmail are optional locally
pip install -r requirements.txt
python scripts/check_env.py
python app.py               # open http://localhost:8080
python scripts/fake_session.py
```

## Deployment

Cloud Run service `career-coach-voice-agent` in `asia-south1`, project
`project-b2f8fd7f-3661-4447-8b6`, deployed from source like Mydoot:

```bash
gcloud run deploy career-coach-voice-agent --source=. --region=asia-south1 \
  --project=project-b2f8fd7f-3661-4447-8b6 --account=tellyourjourney04@gmail.com
```

Later deploys keep every flag, secret and env var. The first deploy's full settings:
runtime service account `career-coach-runtime@…`, secrets `GEMINI_API_KEY`,
`GMAIL_USER`, `GMAIL_APP_PASSWORD` from Secret Manager, `GOOGLE_SPREADSHEET_ID` env var,
`--timeout=3600 --no-cpu-throttling --min-instances=0 --max-instances=3 --concurrency=20`.

The Google Sheet must be shared as Editor with the runtime service account.

## Limits

- A session is capped at 14 minutes, because Gemini audio sessions end at 15. At 11 minutes the coach is told to recap.
- Sessions are open to anyone with the URL. Each one uses Gemini credits, so add sign-in or rate limits before sharing widely.
