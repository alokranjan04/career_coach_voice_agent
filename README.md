# Disha — AI career coach (phone)

Users upload a resume and enter their mobile number on the web page, and Disha
calls them through Vobiz. During the roughly 10-minute call, Disha asks questions
grounded in the resume, helps the user name one goal and agree two or three next
steps, then saves a summary to a Google Sheet and emails it to the user.

Built from the `telephony-voice-agent` skill template (the Mydoot pattern), with
outbound calling added.

```
Browser ── POST /api/call (form + resume) ──▶ Cloud Run (aiohttp)
                                               ├─ resume text → gs://career-coach-sessions-… (1-day delete rule)
                                               └─ Vobiz REST: make call (from +91 80654 81243)
User's phone rings, picks up ── Vobiz POST /answer?sid&token ──▶ XML: open stream
Vobiz ── WSS /stream (mu-law 8 kHz) ──▶ agent/pipeline.py ◀──WSS──▶ Gemini Live (gemini-3.8-live)
                                          │ save_session_summary ─▶ Sheet "Sessions"
                                          └ after the call ─▶ Vobiz hangup · summary email · "Call_Logs" row · resume deleted
```

## Files you edit

| File | What it is |
|---|---|
| `agent/prompt.md` | The coach's instructions. `{name}`, `{language}`, `{resume}` are filled per call. |
| `agent/tools.json` | The `save_session_summary` tool the model calls at the end. |
| `agent_config.json` | Name, model, voice, and the sheet tab and columns for the tool. |

## Run locally

```bash
cp .env.example .env        # fill it; Vobiz needs a public URL (ngrok) to reach /answer
pip install -r requirements.txt
python scripts/check_env.py
python app.py               # open http://localhost:8080
```

`scripts/fake_call.py --sid SID --token TOKEN` simulates the Vobiz media stream for a
call request already in the bucket.

## Deployment

Cloud Run service `career-coach-voice-agent` in `asia-south1`, project
`project-b2f8fd7f-3661-4447-8b6`, deployed from source like Mydoot:

```bash
gcloud run deploy career-coach-voice-agent --source=. --region=asia-south1 \
  --project=project-b2f8fd7f-3661-4447-8b6 --account=tellyourjourney04@gmail.com
```

Later deploys keep every flag, secret and env var. Settings in place:

- Runtime service account `career-coach-runtime@…`, keyless.
- Secrets from Secret Manager: `GEMINI_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `VOBIZ_AUTH_ID`, `VOBIZ_AUTH_TOKEN`.
- Env vars: `VOBIZ_FROM_NUMBER`, `SESSIONS_BUCKET`, `PUBLIC_URL`, `GOOGLE_SPREADSHEET_ID`.
- `--timeout=3600 --no-cpu-throttling --min-instances=0 --max-instances=3 --concurrency=20`.

The Google Sheet must be shared as Editor with the runtime service account. Outgoing
calls set their own answer URL, so the Vobiz number's console app is not changed.

## Limits and safeguards

- A call is capped at 14 minutes, because Gemini audio sessions end at 15. At 11 minutes the coach is told to recap.
- Each number can be called once every 5 minutes and 3 times a day. Only Indian mobile numbers are accepted.
- Answer and stream URLs carry a single-use token, and the resume text is deleted when the call ends.
- Anyone with the page URL can make Disha call any Indian mobile number within those limits. Add sign-in or OTP verification before sharing widely.
