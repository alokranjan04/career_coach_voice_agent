// Web client: verify the number once (OTP), pay if the free calls are used up,
// send the form (resume and/or profile) to /api/call, then follow the call live.
import { friendlyOtpError, getConfig, getSession, sendCode, toE164 } from "/static/otp.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;
const POLL_LIMIT_MS = 20 * 60 * 1000;

const MESSAGES = {
  calling: ["ringing", "Calling you now… please pick up when your phone rings."],
  answered: ["speaking", "Connecting you to Ava…"],
  in_call: ["speaking", "You're on the call with Ava."],
  completed: ["done", "Great call! Your summary, plan and resources are on their way to your inbox."],
  ended: ["", "The call has ended. If it ended early, you can request another call in a few minutes."],
  no_answer: ["", "We couldn't reach you. Check your phone is on and try again in a few minutes."],
  failed: ["", "We couldn't place the call. Please try again."],
};
const TIMELINE = { calling: 0, answered: 1, in_call: 1, completed: 3, ended: 2, no_answer: 0, failed: 0 };

let cfg = null;
let session = { phone: "" };

function setStatus(el, text, isError = false) {
  el.textContent = text;
  el.classList.toggle("error", isError);
}

function busy(button, on) {
  button.disabled = on;
  button.classList.toggle("loading", on);
}

function showPanel(name) {
  $("setup-form").hidden = name !== "form";
  $("otp").hidden = name !== "otp";
  $("pay").hidden = name !== "pay";
}

function renderSession() {
  const v = $("verified");
  const plan = $("plan");
  if (!session.phone) { v.hidden = true; plan.hidden = true; return; }
  v.hidden = false;
  v.textContent = `✓ ${session.phone} is verified on this device${session.first_name ? ` · welcome back, ${session.first_name}` : ""}`;
  if (!$("phone").value) $("phone").value = session.phone.slice(3);
  plan.hidden = false;
  plan.textContent = session.pass_calls_left > 0
    ? `Day pass active · ${session.pass_calls_left} call(s) left`
    : session.free_calls_left > 0
      ? `${session.free_calls_left} free call${session.free_calls_left === 1 ? "" : "s"} left`
      : `Free calls used · day pass ₹${cfg.day_pass_rupees} for ${cfg.day_pass_calls} calls`;
}

// ---------- background tabs and resume drop zone ----------
function selectTab(which) {
  for (const t of ["resume", "details"]) {
    const on = t === which;
    $(`tab-${t}`).classList.toggle("active", on);
    $(`tab-${t}`).setAttribute("aria-selected", String(on));
    $(`panel-${t}`).hidden = !on;
  }
}
$("tab-resume").addEventListener("click", () => selectTab("resume"));
$("tab-details").addEventListener("click", () => selectTab("details"));

const dz = $("dropzone");
function showFile() {
  const file = $("resume").files[0];
  dz.classList.toggle("has-file", Boolean(file));
  $("dz-title").textContent = file ? `✓ ${file.name}` : "Drop your resume here or browse";
}
$("resume").addEventListener("change", showFile);
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (!file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  $("resume").files = dt.files;
  showFile();
});

// ---------- call status ----------
function show(status) {
  const [orb, text] = MESSAGES[status] || MESSAGES.calling;
  $("orb").className = "orb " + orb;
  setStatus($("call-status"), text, status === "failed" || status === "no_answer");
  const reached = TIMELINE[status] ?? 0;
  [...$("timeline").children].forEach((li, i) => {
    li.classList.toggle("done", i < reached);
    li.classList.toggle("active", i === reached && reached < 3 && !["ended", "no_answer", "failed"].includes(status));
  });
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
  setStatus($("setup-status"), "Preparing your call…");
  const res = await fetch("/api/call", { method: "POST", body: new FormData($("setup-form")), credentials: "same-origin" });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && data.needs_verification) return startOtp();
  if (res.status === 402 && data.payment_required) return startPayment(data.error);
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  setStatus($("setup-status"), "");
  showPanel("none");
  $("setup").hidden = true;
  $("calling").hidden = false;
  $("calling").scrollIntoView({ behavior: "smooth", block: "center" });
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
      name: "Tell Your Journey", description: `Day pass: up to ${cfg.day_pass_calls} coaching calls with Ava`,
      prefill: order.prefill, theme: { color: "#4f46e5" },
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
  const button = $("start-btn");
  if (!$("consent").checked) return setStatus($("setup-status"), "Please tick the consent box.", true);
  busy(button, true);
  try {
    const e164 = toE164($("phone").value);
    if (!e164) throw new Error("Please enter a valid 10-digit Indian mobile number.");
    if (session.phone !== e164) await startOtp();  // verified numbers skip this
    else await requestCall();
  } catch (err) {
    showPanel("form");
    setStatus($("setup-status"), friendlyOtpError(err), true);
  } finally {
    busy(button, false);
  }
});

$("otp-code").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, 6);
  if (e.target.value.length === 6) $("otp-verify").click();
});

$("otp-verify").addEventListener("click", async () => {
  const code = $("otp-code").value.trim();
  if (!/^\d{6}$/.test(code)) return setStatus($("otp-status"), "Please enter the 6-digit code.", true);
  busy($("otp-verify"), true);
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
    busy($("otp-verify"), false);
  }
});

$("otp-cancel").addEventListener("click", () => showPanel("form"));

$("pay-button").addEventListener("click", async () => {
  busy($("pay-button"), true);
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
    busy($("pay-button"), false);
  }
});

$("again").addEventListener("click", async () => {
  $("calling").hidden = true;
  $("setup").hidden = false;
  showPanel("form");
  session = await getSession();
  renderSession();
});

// ---------- scroll reveal ----------
const io = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
}, { threshold: 0.12 }) : null;
document.querySelectorAll(".reveal").forEach((el, i) => {
  el.style.transitionDelay = `${(i % 4) * 70}ms`;
  io ? io.observe(el) : el.classList.add("in");
});

(async () => {
  [cfg, session] = await Promise.all([getConfig(), getSession()]);
  if (cfg.whatsapp_available) {  // only offered when WhatsApp sending is set up
    $("whatsapp-option").hidden = false;
    $("whatsapp").checked = true;
  }
  renderSession();
})();
