// Phone OTP with Firebase Auth (Identity Platform). Once the server has checked
// the token it sets a cookie, so a verified number is not asked again.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import { getAuth, RecaptchaVerifier, signInWithPhoneNumber, signOut } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";

let auth = null;
let verifier = null;

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

function init(cfg) {
  if (auth) return;
  auth = getAuth(initializeApp(cfg.firebase));
  auth.useDeviceLanguage();
}

// Sends the SMS and returns a function that takes the 6-digit code and
// completes verification on our server.
export async function sendCode(cfg, e164, containerId) {
  init(cfg);
  if (!verifier) verifier = new RecaptchaVerifier(auth, containerId, { size: "invisible" });
  const confirmation = await signInWithPhoneNumber(auth, e164, verifier);
  return async (code) => {
    const cred = await confirmation.confirm(code);
    const idToken = await cred.user.getIdToken();
    const res = await fetch("/api/verify", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idToken }),
    });
    const data = await res.json().catch(() => ({}));
    await signOut(auth).catch(() => {});  // our cookie is the session from here on
    if (!res.ok) throw new Error(data.error || "Verification failed.");
    return data.phone;
  };
}

export function friendlyOtpError(err) {
  const code = (err && err.code) || "";
  if (code.includes("invalid-verification-code")) return "That code isn't right. Please check and try again.";
  if (code.includes("code-expired")) return "That code has expired. Please request a new one.";
  if (code.includes("too-many-requests")) return "Too many attempts. Please wait a while and try again.";
  if (code.includes("invalid-phone-number")) return "Please enter a valid Indian mobile number.";
  return (err && err.message) || "Something went wrong. Please try again.";
}
