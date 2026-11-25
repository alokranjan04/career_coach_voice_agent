"""HTTP entry point.

Pages:  GET / · /privacy · /terms · /my-data
User API (the verified-phone cookie comes from OTP sign-in):
  GET  /api/config               public settings for the page (Firebase, Razorpay key, prices)
  GET  /api/session              which number this browser has verified, and its plan
  POST /api/verify               exchange a Firebase ID token for the verified-phone cookie
  POST /api/logout
  POST /api/call                 form + resume → consent record → entitlement → Vobiz call
  GET  /api/call/{sid}           call status for the page to poll
  POST /api/pay/order            Razorpay order for a day pass
  POST /api/pay/verify           checkout result → signature check → day pass
  GET  /api/my-data              download everything stored for the verified number
  POST /api/my-data/delete       erase it
Webhooks: POST /answer, /hangup (Vobiz) · POST /api/razorpay/webhook
Scheduled (Cloud Scheduler, OIDC-authenticated): POST /internal/cron/{hourly,daily,weekly}
Other: GET /stream (media WebSocket) · GET /health
"""
import asyncio
import json
import re
import sys
import time

from aiohttp import web

from agent import auth, config, integrations, obs, payments, quality, store, telephony, vobiz_api
from agent import whatsapp as wa
from agent.pipeline import call_handler, check_balances
from agent.resume import ResumeError, extract_text

# Windows consoles default to cp1252 and crash on non-ASCII log lines.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

STATIC = config.ROOT / "static"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def _base_url(request: web.Request) -> str:
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    # Behind Cloud Run / ngrok the app sees plain HTTP; trust the proxy headers.
    proto = request.headers.get("X-Forwarded-Proto", "https" if request.secure else "http")
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{proto}://{host}"


def _indian_mobile(raw: str) -> str:
    """Return 91XXXXXXXXXX for a valid Indian mobile number, else ""."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return "91" + digits if re.fullmatch(r"[6-9]\d{9}", digits) else ""


def _client_ip(request: web.Request) -> str:
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()) or (request.remote or "")


def _error(message: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _page(name: str):
    async def handler(request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / name)
    return handler


# ---------- session and verification ----------

async def public_config(request: web.Request) -> web.Response:
    return web.json_response({
        "firebase": {"apiKey": config.FIREBASE_WEB_API_KEY,
                     "authDomain": f"{config.GCP_PROJECT}.firebaseapp.com", "projectId": config.GCP_PROJECT},
        "razorpay_key_id": config.RAZORPAY_KEY_ID,
        "free_calls": config.FREE_CALLS, "day_pass_rupees": config.DAY_PASS_PAISE // 100,
        "day_pass_calls": config.DAY_PASS_CALLS, "day_pass_hours": config.DAY_PASS_HOURS,
        "whatsapp_available": bool(wa.enabled()), "consent_version": config.CONSENT_VERSION})


async def session(request: web.Request) -> web.Response:
    phone = auth.verified_phone(request)
    if not phone:
        return web.json_response({"phone": ""})
    profile = await asyncio.to_thread(store.load_profile, phone)
    p = store.active_pass(profile)
    return web.json_response({
        "phone": "+" + phone, "has_profile": bool(profile.get("resume")),
        "free_calls_left": max(0, config.FREE_CALLS - await asyncio.to_thread(store.free_calls_used, phone)),
        "pass_calls_left": (config.DAY_PASS_CALLS - p.get("calls_used", 0)) if p else 0,
        "pass_expires_at": p.get("expires_at") if p else None})


async def verify(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        e164 = await asyncio.to_thread(auth.verify_id_token, str(body.get("idToken", "")))
    except Exception as e:
        obs.log("otp_failed", f"ID token rejected: {e!r}", severity="WARNING")
        return _error("Verification failed. Please request a new code.", 401)
    phone = _indian_mobile(e164)
    if not phone:
        return _error("Only Indian mobile numbers are supported.", 400)
    profile = await asyncio.to_thread(store.load_profile, phone)
    if profile:
        profile["verified_at"] = profile.get("verified_at") or time.time()
        await asyncio.to_thread(store.save_profile, profile)
    obs.log("otp_verified", "phone verified", phone_last4=phone[-4:])
    resp = web.json_response({"phone": "+" + phone})
    auth.set_cookie(resp, phone)
    return resp


async def logout(request: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie(auth.COOKIE, path="/")
    return resp


# ---------- calls ----------

async def request_call(request: web.Request) -> web.Response:
    try:
        form = await request.post()
    except web.HTTPRequestEntityTooLarge:
        return _error("That file is too large (max 5 MB).", 413)
    phone = _indian_mobile(str(form.get("phone", "")))
    if not phone:
        return _error("Please enter a valid 10-digit Indian mobile number.")
    if auth.verified_phone(request) != phone:
        return _error("Please verify your number with the OTP first.", 401, needs_verification=True)
    if form.get("consent") != "yes":
        return _error("Please tick the consent box.")

    # Returning users only give their number: everything else falls back to their
    # saved profile. The call only goes to the verified number.
    profile = await asyncio.to_thread(store.load_profile, phone)
    name = re.sub(r"[\x00-\x1f<>{}]", "", str(form.get("name", ""))).strip()[:80] or profile.get("name", "")
    email = str(form.get("email", "")).strip()[:120] or profile.get("email", "")
    upload = form.get("resume")
    resume = profile.get("resume", "")
    if isinstance(upload, web.FileField) and upload.filename:
        try:
            resume = extract_text(upload.filename, upload.file.read())
        except ResumeError as e:
            return _error(str(e), 422)
    if not resume:
        return _error("We don't have a resume for this number yet. Please upload it.")
    if not name:
        return _error("Please enter your name.")
    if not EMAIL_RE.match(email):
        return _error("Please enter a valid email address.")

    # Spend guardrail first: it protects every account.
    spent = await asyncio.to_thread(store.spend_today)
    if spent >= config.DAILY_SPEND_CAP_INR:
        obs.log("spend_cap_refused", "call refused: daily spend cap reached", severity="WARNING", spent_inr=spent)
        return _error("Disha is fully booked for today. Please try again tomorrow.", 503)
    kind = await asyncio.to_thread(store.entitlement, phone, profile)
    if kind == "payment_required":
        return _error(f"You've used your {config.FREE_CALLS} free calls. A day pass is "
                      f"₹{config.DAY_PASS_PAISE // 100} for up to {config.DAY_PASS_CALLS} calls.",
                      402, payment_required=True)
    try:
        await asyncio.to_thread(store.check_and_record_call, phone)
    except store.RateLimited as e:
        return _error(str(e), 429)

    whatsapp = form.get("whatsapp") == "yes" and bool(wa.enabled())  # off unless Twilio is fully set up
    purposes = ["coaching_call", "resume_and_call_memory", "email_summary"] + (["whatsapp_summary"] if whatsapp else [])
    await asyncio.to_thread(store.record_consent, phone, purposes, _client_ip(request),
                            request.headers.get("User-Agent", ""))
    if resume != profile.get("resume"):
        profile["resume_updated"] = time.time()
    profile.update(phone=phone, name=name, email=email, resume=resume, whatsapp_opt_in=whatsapp)
    await asyncio.to_thread(store.save_profile, profile)
    rec = await asyncio.to_thread(store.create, profile, kind)
    base = _base_url(request)
    try:
        await asyncio.to_thread(
            vobiz_api.place_call, phone,
            f"{base}/answer?sid={rec['sid']}&token={rec['token']}",
            f"{base}/hangup?sid={rec['sid']}")
    except vobiz_api.CallError as e:
        await asyncio.to_thread(store.update, rec["sid"], status="failed", resume="", token="")
        obs.log("call_place_failed", str(e), severity="ERROR", call_id=rec["sid"])
        return _error(str(e), 502)
    obs.log("call_placed", "outbound call placed", call_id=rec["sid"], phone_last4=phone[-4:],
            entitlement=kind, resume_chars=len(resume), previous_calls=len(profile.get("calls", [])))
    return web.json_response({"sid": rec["sid"]})


async def call_status(request: web.Request) -> web.Response:
    rec = await asyncio.to_thread(store.load, request.match_info["sid"])
    if not rec:
        return _error("Unknown call.", 404)
    return web.json_response({k: rec.get(k) for k in ("status", "ended_reason", "summary_saved")})


async def answer(request: web.Request) -> web.Response:
    phone = telephony.get_provider()
    form = await request.post()
    sid, token = request.query.get("sid", ""), request.query.get("token", "")
    rec = await asyncio.to_thread(store.load, sid) if sid else None
    if not rec or not token or rec.get("token") != token:
        obs.log("answer_rejected", f"unknown call sid={sid!r}, hanging up", severity="WARNING")
        return web.Response(text='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                            content_type="text/xml")
    await asyncio.to_thread(store.update, sid, status="answered", call_uuid=str(form.get("CallUUID", "")))
    obs.log("call_answered", "call answered", call_id=sid)
    ws_url = re.sub(r"^http", "ws", _base_url(request)) + "/stream"
    return web.Response(text=phone.answer_xml(ws_url, {"sid": sid, "token": token}), content_type="text/xml")


async def hangup(request: web.Request) -> web.Response:
    form = await request.post()
    sid = request.query.get("sid", "")
    rec = await asyncio.to_thread(store.load, sid) if sid else None
    cause = str(form.get("HangupCause", ""))
    if rec and rec.get("status") in ("calling", "answered"):
        # The stream never started: unanswered, rejected, or busy.
        await asyncio.to_thread(store.update, sid, status="no_answer", resume="", token="", ended_reason=cause)
        obs.log("call_not_connected", f"call not connected: {cause}", call_id=sid, hangup_cause=cause)
    return web.Response(text="OK")


# ---------- payments ----------

async def pay_order(request: web.Request) -> web.Response:
    phone = auth.verified_phone(request)
    if not phone:
        return _error("Please verify your number first.", 401, needs_verification=True)
    if not payments.enabled():
        return _error("Payments aren't set up yet.", 503)
    try:
        order = await asyncio.to_thread(payments.create_order, phone)
    except payments.PaymentError as e:
        obs.log("payment_order_failed", str(e), severity="ERROR")
        return _error("Couldn't start the payment. Please try again.", 502)
    await asyncio.to_thread(store.save_order, order["id"], phone, order["amount"])
    profile = await asyncio.to_thread(store.load_profile, phone)
    return web.json_response({"order_id": order["id"], "amount": order["amount"], "currency": "INR",
                              "key_id": config.RAZORPAY_KEY_ID,
                              "prefill": {"name": profile.get("name", ""), "email": profile.get("email", ""),
                                          "contact": "+" + phone}})


async def pay_verify(request: web.Request) -> web.Response:
    phone = auth.verified_phone(request)
    body = await request.json()
    order_id = str(body.get("razorpay_order_id", ""))
    payment_id = str(body.get("razorpay_payment_id", ""))
    if not payments.checkout_signature_ok(order_id, payment_id, str(body.get("razorpay_signature", ""))):
        obs.log("payment_signature_bad", "checkout signature mismatch", severity="WARNING")
        return _error("Payment couldn't be verified.", 400)
    order = await asyncio.to_thread(store.load_order, order_id)
    if not order or order["phone"] != phone:
        return _error("Payment doesn't match this number.", 400)
    payment = await asyncio.to_thread(payments.fetch_payment, payment_id)
    if payment.get("status") not in ("captured", "authorized") or payment.get("order_id") != order_id:
        return _error("Payment isn't complete yet.", 402)
    if await asyncio.to_thread(store.grant_day_pass, phone, order_id, payment_id, int(payment.get("amount", 0))):
        obs.log("payment_captured", "day pass bought", phone_last4=phone[-4:], amount_paise=payment.get("amount"),
                method=payment.get("method"), source="checkout")
    return web.json_response({"ok": True})


async def razorpay_webhook(request: web.Request) -> web.Response:
    raw = await request.read()
    if not payments.webhook_signature_ok(raw, request.headers.get("X-Razorpay-Signature", "")):
        obs.log("webhook_signature_bad", "Razorpay webhook signature mismatch", severity="WARNING")
        return web.Response(status=400)
    event = json.loads(raw)
    if event.get("event") in ("payment.captured", "order.paid"):
        payment = event.get("payload", {}).get("payment", {}).get("entity", {})
        order = await asyncio.to_thread(store.load_order, str(payment.get("order_id", "")))
        if order and await asyncio.to_thread(store.grant_day_pass, order["phone"], order["order_id"],
                                             payment.get("id", ""), int(payment.get("amount", 0))):
            obs.log("payment_captured", "day pass bought", phone_last4=order["phone"][-4:],
                    amount_paise=payment.get("amount"), method=payment.get("method"), source="webhook")
    return web.Response(text="OK")


# ---------- data rights (DPDP Act) ----------

async def my_data(request: web.Request) -> web.Response:
    phone = auth.verified_phone(request)
    if not phone:
        return _error("Please verify your number first.", 401, needs_verification=True)
    data = await asyncio.to_thread(store.export_user_data, phone)
    obs.log("data_exported", "user downloaded their data", phone_last4=phone[-4:])
    return web.Response(text=json.dumps(data, ensure_ascii=False, indent=2, default=str),
                        content_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=disha-my-data.json"})


def _erase_sheet_rows(phone: str) -> int:
    if not config.SPREADSHEET_ID:
        return 0
    call_ids = {r.get("Call") for r in integrations.read_rows(config.CALL_LOG_TAB) if r.get("Phone") == phone}
    n = integrations.delete_rows_where(config.CALL_LOG_TAB, lambda r: r.get("Phone") != phone)
    n += integrations.delete_rows_where("Sessions", lambda r: r.get("Phone") != phone)
    n += integrations.delete_rows_where("Quality", lambda r: r.get("Call") not in call_ids)
    return n


async def my_data_delete(request: web.Request) -> web.Response:
    phone = auth.verified_phone(request)
    if not phone:
        return _error("Please verify your number first.", 401, needs_verification=True)
    result = await asyncio.to_thread(store.delete_user_data, phone)
    result["sheet_rows_deleted"] = await asyncio.to_thread(_erase_sheet_rows, phone)
    obs.log("data_erased", "user data erased on request", phone_last4=phone[-4:], **result)
    resp = web.json_response({"ok": True, **result})
    resp.del_cookie(auth.COOKIE, path="/")
    return resp


# ---------- scheduled jobs ----------

def _scheduler_ok(request: web.Request) -> bool:
    """Only Cloud Scheduler's service account (OIDC token) may run jobs."""
    import google.auth.transport.requests
    from google.oauth2 import id_token as gid
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    try:
        claims = gid.verify_oauth2_token(token, google.auth.transport.requests.Request(),
                                         audience=config.PUBLIC_URL)
        return claims.get("email") == config.SCHEDULER_SA and claims.get("email_verified")
    except Exception:
        return False


def _retention_job() -> dict:
    result = store.retention_cleanup()
    if config.SPREADSHEET_ID:
        cutoff = time.time() - config.RETENTION_DAYS * 86400
        old = lambda r, col: quality._ts(r.get(col, "")) and quality._ts(r.get(col, "")) < cutoff
        result["sheet_rows_deleted"] = sum([
            integrations.delete_rows_where(config.CALL_LOG_TAB, lambda r: not old(r, "Start")),
            integrations.delete_rows_where("Sessions", lambda r: not old(r, "Timestamp")),
            integrations.delete_rows_where("Quality", lambda r: not old(r, "Timestamp"))])
    return result


async def cron(request: web.Request) -> web.Response:
    if not _scheduler_ok(request):
        return web.Response(status=403)
    job = request.match_info["job"]
    if job == "hourly":
        await asyncio.to_thread(check_balances)
        result = {"spend_today_inr": await asyncio.to_thread(store.spend_today)}
    elif job == "daily":
        result = await asyncio.to_thread(_retention_job)
    elif job == "weekly":
        result = await asyncio.to_thread(quality.weekly_review)
    else:
        return web.Response(status=404)
    obs.log("cron_done", f"{job} job done", job=job, result=result)
    return web.json_response(result)


async def health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "agent": config.AGENT.get("name"),
        "model": config.AGENT.get("model"),
        "gemini_key_set": bool(config.GEMINI_API_KEY),
        "vobiz_set": bool(config.VOBIZ_AUTH_ID and config.VOBIZ_AUTH_TOKEN and config.VOBIZ_FROM_NUMBER),
        "sessions_bucket_set": bool(config.SESSIONS_BUCKET),
        "sheet_set": bool(config.SPREADSHEET_ID),
        "email_set": bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD),
        "otp_set": bool(config.FIREBASE_WEB_API_KEY and config.SESSION_SECRET),
        "payments_set": payments.enabled(),
        "whatsapp_set": bool(wa.enabled()),
    })


@web.middleware
async def security_headers(request: web.Request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


def make_app() -> web.Application:
    app = web.Application(client_max_size=config.MAX_RESUME_BYTES + 64 * 1024, middlewares=[security_headers])
    app.router.add_get("/", _page("index.html"))
    app.router.add_get("/privacy", _page("privacy.html"))
    app.router.add_get("/terms", _page("terms.html"))
    app.router.add_get("/my-data", _page("my-data.html"))
    app.router.add_get("/api/config", public_config)
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/verify", verify)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/call", request_call)
    app.router.add_get("/api/call/{sid}", call_status)
    app.router.add_post("/api/pay/order", pay_order)
    app.router.add_post("/api/pay/verify", pay_verify)
    app.router.add_post("/api/razorpay/webhook", razorpay_webhook)
    app.router.add_get("/api/my-data", my_data)
    app.router.add_post("/api/my-data/delete", my_data_delete)
    app.router.add_post("/answer", answer)
    app.router.add_post("/hangup", hangup)
    app.router.add_get("/stream", call_handler)
    app.router.add_post("/internal/cron/{job}", cron)
    app.router.add_get("/health", health)
    app.router.add_static("/static", STATIC)
    return app


if __name__ == "__main__":
    obs.log("startup", f"{config.AGENT.get('name')} listening on :{config.PORT}")
    web.run_app(make_app(), port=config.PORT, print=None)
