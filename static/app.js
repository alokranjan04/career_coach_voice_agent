// Web client: verify the number once (OTP), pay if the free calls are used up,
// send the form and resume to /api/call, then poll the call's status.
import { friendlyOtpError, getConfig, getSession, sendCode, toE164 } from "/static/otp.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;
const POLL_LIMIT_MS = 20 * 60 * 1000;

const MESSAGES = {
  calling: ["ringing", "Calling you now. Please pick up when your phone rings."],
  answered: ["speaking", "Connecting you to Disha…"],
  in_call: ["speaking", "You're on the call with Disha."],
  completed: ["", "Call complete. Your summary is on its way to your inbox (and WhatsApp, if you chose it)."],
  ended: ["", "The call has ended. If it ended early, you can request another call in a few minutes."],
  no_answer: ["", "We couldn't reach you. Check your phone is on and try again in a few minutes."],
  failed: ["", "We couldn't place the call. Please try again."],
};

let cfg = null;
let session = { phone: "" };

function setStatus(el, text, isError = false) {
  el.textContent = text;
  el.classList.toggle("error", isError);
}

function showPanel(name) {
  $("setup-form").hidden = name !== "form";
  $("otp").hidden = name !== "otp";
  $("pay").hidden = name !== "pay";
}

function renderSession() {
  const v = $("verified");
  if (session.phone) {
    v.hidden = false;
    v.textContent = `✓ ${session.phone} is verified on this device.`;
    if (!$("phone").value) $("phone").value = session.phone.slice(3);
    const plan = $("plan");
    plan.hidden = false;
    plan.textContent = session.pass_calls_left > 0
      ? `Day pass active: ${session.pass_calls_left} call(s) left.`
      : session.free_calls_left > 0
        ? `${session.free_calls_left} free call(s) left.`
        : `Free calls used. A day pass is ₹${cfg.day_pass_rupees} for up to ${cfg.day_pass_calls} calls in ${cfg.day_pass_hours} hours.`;
  } else {
    v.hidden = true;
  }
}

// ---------- call status ----------
function show(status) {
  const [orb, text] = MESSAGES[status] || MESSAGES.calling;
  $("orb").className = "orb " + orb;
  setStatus($("call-status"), text, status === "failed" || status === "no_answer");
  const done = !["calling", "answered", "in_call"].includes(status);
  $("again").hidden = !done;
  return done;
}

async function poll(sid) {
  const started = Date.now();
  while (Date.now() - started < POLL_LIMIT_MS) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    try {
      const res = await fetch(`/api/call/${sid}`, { cache: "no-store" });
      if (res.ok && show((await res.json()).status)) return;
    } catch (_) { /* network blip: keep polling */ }
  }
  show("ended");
}

// ---------- steps ----------
async function requestCall() {
  setStatus($("setup-status"), "Reading your resume and placing the call…");
  const res = await fetch("/api/call", { method: "POST", body: new FormData($("setup-form")), credentials: "same-origin" });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && data.needs_verification) return startOtp();
  if (res.status === 402 && data.payment_required) return startPayment(data.error);
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  setStatus($("setup-status"), "");
  showPanel("none");
  $("setup").hidden = true;
  $("calling").hidden = false;
  show("calling");
  poll(data.sid);
}

let confirmCode = null;

async function startOtp() {
  const e164 = toE164($("phone").value);
  if (!e164) throw new Error("Please enter a valid 10-digit Indian mobile number.");
  setStatus($("setup-status"), "Sending a code to your phone…");
  confirmCode = await sendCode(cfg, e164, "recaptcha");
  setStatus($("setup-status"), "");
  $("otp-number").textContent = e164;
  $("otp-code").value = "";
  setStatus($("otp-status"), "");
  showPanel("otp");
  $("otp-code").focus();
}

async function startPayment(message) {
  $("pay-text").textContent = message;
  setStatus($("pay-status"), "");
  showPanel("pay");
}

async function pay() {
  const orderRes = await fetch("/api/pay/order", { method: "POST", credentials: "same-origin" });
  const order = await orderRes.json().catch(() => ({}));
  if (!orderRes.ok) throw new Error(order.error || "Couldn't start the payment.");
  await new Promise((resolve, reject) => {
    const rzp = new window.Razorpay({
      key: order.key_id, order_id: order.order_id, amount: order.amount, currency: order.currency,
      name: "Disha Career Coach", description: `Day pass: up to ${cfg.day_pass_calls} coaching calls`,
      prefill: order.prefill, theme: { color: "#2f6f5e" },
      handler: async (resp) => {
        const res = await fetch("/api/pay/verify", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify(resp),
        });
        const data = await res.json().catch(() => ({}));
        res.ok ? resolve() : reject(new Error(data.error || "Payment couldn't be verified."));
      },
      modal: { ondismiss: () => reject(new Error("Payment cancelled.")) },
    });
    rzp.on("payment.failed", (e) => reject(new Error(e.error?.description || "Payment failed.")));
    rzp.open();
  });
}

// ---------- events ----------
$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = $("start");
  if (!$("consent").checked) return setStatus($("setup-status"), "Please tick the consent box.", true);
  button.disabled = true;
  try {
    const e164 = toE164($("phone").value);
    if (!e164) throw new Error("Please enter a valid 10-digit Indian mobile number.");
    if (session.phone !== e164) await startOtp();  // verified numbers skip this
    else await requestCall();
  } catch (err) {
    showPanel("form");
    setStatus($("setup-status"), friendlyOtpError(err), true);
  } finally {
    button.disabled = false;
  }
});

$("otp-verify").addEventListener("click", async () => {
  const code = $("otp-code").value.trim();
  if (!/^\d{6}$/.test(code)) return setStatus($("otp-status"), "Please enter the 6-digit code.", true);
  $("otp-verify").disabled = true;
  setStatus($("otp-status"), "Verifying…");
  try {
    await confirmCode(code);
    session = await getSession();
    renderSession();
    showPanel("form");
    await requestCall();  // verified: place the call straight away
  } catch (err) {
    setStatus($("otp-status"), friendlyOtpError(err), true);
  } finally {
    $("otp-verify").disabled = false;
  }
});

$("otp-cancel").addEventListener("click", () => showPanel("form"));

$("pay-button").addEventListener("click", async () => {
  $("pay-button").disabled = true;
  try {
    await pay();
    setStatus($("pay-status"), "Payment received. Placing your call…");
    session = await getSession();
    renderSession();
    showPanel("form");
    await requestCall();
  } catch (err) {
    setStatus($("pay-status"), err.message, true);
  } finally {
    $("pay-button").disabled = false;
  }
});

$("again").addEventListener("click", async () => {
  $("calling").hidden = true;
  $("setup").hidden = false;
  showPanel("form");
  session = await getSession();
  renderSession();
});

(async () => {
  [cfg, session] = await Promise.all([getConfig(), getSession()]);
  renderSession();
})();
