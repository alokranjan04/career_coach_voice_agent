# Disha — AI career coach (phone)

Users verify their mobile number once with an SMS OTP, upload a resume, and Disha
calls them through Vobiz. During the roughly 10-minute call, Disha asks questions
grounded in the resume, agrees a goal and a 30-day plan, then emails a summary with
verified internet resources. Each number has a profile,
so returning users only enter their number and Disha continues from the last call.

Built from the `telephony-voice-agent` skill template (the Mydoot pattern), with
outbound calling, memory, payments, compliance and observability added.

```
Browser ─ OTP (Identity Platform) ─▶ /api/verify ─▶ signed cookie: number verified, not asked again
   └─ form + resume ─▶ /api/call ─▶ consent record · spend cap · free calls / day pass (Razorpay)
                                    └─ Vobiz REST: call from +91 80654 81243
Phone answered ─ Vobiz POST /answer ─▶ XML stream ─▶ WSS /stream ─▶ agent/pipeline.py ◀─▶ Gemini Live
After the call ─▶ research (Gemini + Google Search, verified links) ─▶ email
               ─▶ quality score · cost per call · spend ledger · balance checks · profile memory
```

## Plans

First 3 calls free per number (counted when a call connects). Then a ₹499 day pass:
up to 3 calls within 24 hours, paid with UPI, card or netbanking through Razorpay
Checkout. The pass is granted by the checkout callback or the webhook, whichever
arrives first, exactly once.

## Files you edit most

| File | What it is |
|---|---|
| `agent/prompt.md` | The coach's instructions. Changes are blocked from deploying unless the regression test passes. |
| `agent/tools.json` | The `save_session_summary` tool. |
| `agent_config.json` | Name, model, voice, Sheet columns. |
| `agent/quality.py` + `docs/quality-rubric.md` | The quality rubric. |
| `static/privacy.html`, `static/terms.html` | Legal pages: have a lawyer review them. |

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest --ignore=tests/regression                     # offline: fakes for every external service
GEMINI_API_KEY=... python tests/regression/run_regression.py   # real Gemini Live, simulated callers
```

## Deployment

Push to `main`. GitHub Actions (`.github/workflows/deploy.yml`) runs the tests, runs
the prompt regression test when the prompt, tools or rubric changed, then deploys to
Cloud Run from source using keyless Workload Identity Federation, and checks `/health`.

One-time setup (already done; safe to re-run): `bash deploy/setup-gcp.sh` and
`bash deploy/observability.sh`.

Cloud Run service `career-coach-voice-agent`, `asia-south1`, project
`project-b2f8fd7f-3661-4447-8b6`, runtime account `career-coach-runtime@…`.
Settings are kept between deploys and changed with `gcloud run services update`:

| Kind | Names |
|---|---|
| Secrets (Secret Manager) | `GEMINI_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `VOBIZ_AUTH_ID`, `VOBIZ_AUTH_TOKEN`, `SESSION_SECRET`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `TWILIO_AUTH_TOKEN` |
| Env vars | `VOBIZ_FROM_NUMBER`, `SESSIONS_BUCKET`, `PUBLIC_URL`, `GOOGLE_SPREADSHEET_ID`, `FIREBASE_WEB_API_KEY`, `GCP_PROJECT`, `ADMIN_EMAIL`, `RAZORPAY_KEY_ID`, `TWILIO_ACCOUNT_SID`, `TWILIO_WHATSAPP_FROM`, `TWILIO_CONTENT_SID`, plus optional limits and prices in `agent/config.py` |
| Flags | `--timeout=3600 --no-cpu-throttling --min-instances=0 --max-instances=3 --concurrency=20` |

## Observability

- **Logs:** every line is a JSON event (`agent/obs.py`): `call_placed`, `call_started`,
  `call_ended` (duration, cost breakdown, tokens, quality), `payment_captured`,
  `otp_verified`, `alert`, errors. Filter by `labels.call_id` to follow one call.
- **Dashboard:** Cloud Monitoring → "Disha career coach": calls, cost per call, duration,
  quality, payments, errors, alerts.
- **Alerts** (email to the admin): errors, app alerts, a spike in calls, site down.
- **Cost per call** is logged and written to the Call_Logs tab: Vobiz's billed cost,
  Gemini Live tokens (summed per turn), research and quality-review tokens, Google
  Search queries, WhatsApp. Gemini prices are list estimates in `agent/config.py`.

## Spending guardrails

- Global daily cap (`DAILY_SPEND_CAP_INR`, default ₹1,500): new calls are refused for the rest of the IST day, and the admin is emailed.
- Vobiz balance checked after every call and hourly; email below ₹200.
- Gemini: AI Studio has no balance API, so estimated monthly Gemini spend is tracked against `GEMINI_MONTHLY_BUDGET_INR` (email at 80%), Gemini key/credit failures on a call alert immediately, and a ₹5,000/month Cloud Billing budget emails billing admins at 50/90/100%.
- Per number: 5 minutes between calls, 3 calls a day.

## Data protection (DPDP Act)

Privacy policy `/privacy`, terms `/terms`, and `/my-data` (OTP-verified download and
deletion). Consent is recorded for every call request (version, purposes, time, IP,
browser). Profiles and transcripts are deleted 12 months after the last call by the
daily job, which also prunes the Sheet. Payment records keep only a hash and the last
4 digits of the number.

## Quality

See `docs/quality-rubric.md`: every call is scored, a weekly review email samples calls,
and the regression test gates prompt changes.

## WhatsApp

Built but switched off: it turns on only when `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_WHATSAPP_FROM` and an approved `TWILIO_CONTENT_SID` are all set. Until then the
page doesn't offer it and Disha doesn't mention it. Setup: `docs/whatsapp-template.md`.
