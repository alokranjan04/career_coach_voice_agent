// Web client: send the form and resume to /api/call, then poll the call's status.
const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;
const POLL_LIMIT_MS = 20 * 60 * 1000;

const MESSAGES = {
  calling: ["ringing", "Calling you now. Please pick up when your phone rings."],
  answered: ["speaking", "Connecting you to Disha…"],
  in_call: ["speaking", "You're on the call with Disha."],
  completed: ["", "Call complete. Your summary is on its way to your inbox."],
  ended: ["", "The call has ended. If it ended early, you can request another call in a few minutes."],
  no_answer: ["", "We couldn't reach you. Check your phone is on and try again in a few minutes."],
  failed: ["", "We couldn't place the call. Please try again."],
};

function setStatus(el, text, isError = false) {
  el.textContent = text;
  el.classList.toggle("error", isError);
}

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

$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = $("start");
  const status = $("setup-status");
  button.disabled = true;
  setStatus(status, "Reading your resume and placing the call…");
  try {
    const res = await fetch("/api/call", { method: "POST", body: new FormData(e.target) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
    setStatus(status, "");
    $("setup").hidden = true;
    $("calling").hidden = false;
    show("calling");
    poll(data.sid);
  } catch (err) {
    setStatus(status, err.message, true);
  } finally {
    button.disabled = false;
  }
});

$("again").addEventListener("click", () => {
  $("calling").hidden = true;
  $("setup").hidden = false;
});
