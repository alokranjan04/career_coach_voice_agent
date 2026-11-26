# WhatsApp summary template (Twilio)

Business-initiated WhatsApp messages need a template approved by Meta.

1. Twilio Console → Messaging → Content Template Builder → Create new.
2. Name `career_call_summary`, language English, type Text, category **Utility**.
3. Body:

```
Hi {{1}}, here's a summary of your coaching call with Ava.

Your goal: {{2}}

Your 30-day plan: {{3}}

Full details, resume tips and hand-picked resources are in your email ({{4}}).
Top picks: {{5}}

These links are only to guide you, not promotions. Reply STOP to opt out.
```

4. Sample values for approval: `Alok` · `Head of AI within 18 months` ·
   `1) Quantify the McCain rollout savings by Sunday 2) Publish a case study by the 15th` ·
   `a…@gmail.com` · `https://www.youtube.com/watch?v=...`
5. Submit for WhatsApp approval. When it's approved, copy its Content SID (starts with `HX`).
6. Set the service's secrets and env vars: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
   (Secret Manager), `TWILIO_WHATSAPP_FROM` (for example `whatsapp:+91XXXXXXXXXX`, your
   approved WhatsApp sender), `TWILIO_CONTENT_SID`.

Template variables can't contain line breaks, so `agent/whatsapp.py` flattens them.
Users opt in with the WhatsApp checkbox on the form; it is recorded in their consent record.
