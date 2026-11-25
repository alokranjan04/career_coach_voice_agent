// Web client: upload the resume, then stream mic audio to /session and play
// the coach's 24 kHz PCM replies gaplessly. Protocol: see agent/session.py.
const $ = (id) => document.getElementById(id);
const AGENT_RATE = 24000;

let ws = null;
let mic = null;        // { stream, ctx, node }
let player = null;     // { ctx, nextTime, sources }
let ended = false;

function setStatus(el, text, isError = false) {
  el.textContent = text;
  el.classList.toggle("error", isError);
}

function addLine(role, text) {
  const li = document.createElement("li");
  li.className = role;
  const who = document.createElement("b");
  who.textContent = role === "agent" ? "Disha" : "You";
  li.append(who, document.createTextNode(text));
  $("transcript").append(li);
  li.scrollIntoView({ block: "nearest" });
}

function setOrb(state) {
  $("orb").className = "orb " + state;
}

// ---------- playback ----------
function playChunk(arrayBuffer) {
  const pcm = new Int16Array(arrayBuffer);
  const buffer = player.ctx.createBuffer(1, pcm.length, AGENT_RATE);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
  const src = player.ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(player.ctx.destination);
  const startAt = Math.max(player.nextTime, player.ctx.currentTime + 0.05);
  src.start(startAt);
  player.nextTime = startAt + buffer.duration;
  player.sources.add(src);
  setOrb("speaking");
  src.onended = () => {
    player.sources.delete(src);
    if (player.sources.size === 0 && !ended) {
      setOrb("listening");
      setStatus($("talk-status"), "Listening… speak whenever you're ready.");
    }
  };
}

function stopPlayback() {
  if (!player) return;
  for (const src of player.sources) {
    try { src.stop(); } catch (_) { /* already stopped */ }
  }
  player.sources.clear();
  player.nextTime = 0;
}

// ---------- mic ----------
async function startMic() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule("/static/mic-worklet.js");
  const node = new AudioWorkletNode(ctx, "mic-capture");
  node.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(e.data);
  };
  const mute = ctx.createGain();
  mute.gain.value = 0;  // keep the worklet pulled by the graph without playing the mic back
  ctx.createMediaStreamSource(stream).connect(node).connect(mute).connect(ctx.destination);
  mic = { stream, ctx, node };
}

function stopMic() {
  if (!mic) return;
  mic.stream.getTracks().forEach((t) => t.stop());
  mic.ctx.close();
  mic = null;
}

// ---------- session ----------
function finish(message, isError = false) {
  if (ended) return;
  ended = true;
  stopMic();
  // Let queued audio (the goodbye) finish, then release the speaker.
  const remaining = player ? Math.max(0, player.nextTime - player.ctx.currentTime) : 0;
  setTimeout(() => { if (player) player.ctx.close(); player = null; }, remaining * 1000 + 300);
  setOrb("");
  setStatus($("talk-status"), message, isError);
  $("stop").hidden = true;
  $("again").hidden = false;
}

function handleMessage(msg) {
  switch (msg.type) {
    case "ready":
      setStatus($("talk-status"), "Disha is joining…");
      break;
    case "transcript":
      addLine(msg.role, msg.text);
      break;
    case "interrupted":
      stopPlayback();
      setOrb("listening");
      break;
    case "ended":
      finish(msg.saved
        ? "Session complete. Your summary is on its way to your inbox."
        : msg.reason === "time limit"
          ? "We've reached the time limit for one session."
          : "Session ended.");
      break;
    case "error":
      finish(msg.message, true);
      break;
  }
}

async function startSession(profile) {
  ended = false;
  $("transcript").replaceChildren();
  $("stop").hidden = false;
  $("again").hidden = true;
  $("setup").hidden = true;
  $("talk").hidden = false;
  setStatus($("talk-status"), "Allow microphone access to begin…");

  // Create the speaker context inside the click handler so autoplay rules allow it.
  player = { ctx: new AudioContext({ sampleRate: AGENT_RATE }), nextTime: 0, sources: new Set() };
  try {
    await startMic();
  } catch (err) {
    finish("Microphone access is needed to talk with Disha. Allow it in your browser and try again.", true);
    return;
  }

  setStatus($("talk-status"), "Connecting…");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}/session`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({ type: "start", ...profile }));
  ws.onmessage = (e) => {
    if (typeof e.data === "string") handleMessage(JSON.parse(e.data));
    else if (player && !ended) playChunk(e.data);
  };
  ws.onclose = () => finish("Session ended.");
  ws.onerror = () => finish("Lost connection to the coach. Please try again.", true);
}

$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = $("start");
  const status = $("setup-status");
  button.disabled = true;
  setStatus(status, "Reading your resume…");
  try {
    const body = new FormData();
    body.append("resume", $("resume").files[0]);
    const res = await fetch("/api/resume", { method: "POST", body });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Couldn't upload your resume. Please try again.");
    setStatus(status, "");
    await startSession({
      name: $("name").value.trim(),
      email: $("email").value.trim(),
      language: $("language").value,
      resume: data.text,
    });
  } catch (err) {
    setStatus(status, err.message, true);
  } finally {
    button.disabled = false;
  }
});

$("stop").addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
  setStatus($("talk-status"), "Ending session…");
});

$("again").addEventListener("click", () => {
  $("talk").hidden = true;
  $("setup").hidden = false;
});
