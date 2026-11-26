// Phone verification by voice: Ava's number calls and reads out a 6-digit code.
// Once the server has checked the code it sets a cookie, so a verified number
// isn't asked again on this device. (SMS OTP was dropped: messages weren't
// reaching Indian phones.)

export async function getConfig() {
  const res = await fetch("/api/config", { cache: "no-store" });
  return res.json();
}

export async function getSession() {
  const res = await fetch("/api/session", { cache: "no-store", credentials: "same-origin" });
  return res.json();
}

export function toE164(raw) {
  let d = String(raw || "").replace(/\D/g, "");
  if (d.length === 12 && d.startsWith("91")) d = d.slice(2);
  if (d.length === 11 && d.startsWith("0")) d = d.slice(1);
  return /^[6-9]\d{9}$/.test(d) ? "+91" + d : "";
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

// Places the code call and returns a function that takes the 6-digit code and
// completes verification. (cfg and containerId are unused; kept for callers.)
export async function sendCode(cfg, e164, containerId) {
  await post("/api/otp/send", { phone: e164 });
  return async (code) => (await post("/api/otp/verify", { phone: e164, code })).phone;
}

export function friendlyOtpError(err) {
  return (err && err.message) || "Something went wrong. Please try again.";
}
