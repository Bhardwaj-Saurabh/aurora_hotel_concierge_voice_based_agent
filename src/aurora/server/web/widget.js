// Public-site chat widget (goal.md, live debugging + frontend pass).
//
// Reuses the same authenticated endpoints as the ops console (talk_server's
// /auth/*, /greeting, /agent, /voice-agent) but skips any visible login: a
// disposable guest account is registered silently on first open, exactly
// like a real site visitor expects from a hotel's chat widget. Voice capture
// here is manual start/stop (a mic button), not the console's continuous
// auto-VAD — simpler and more predictable for a first embed.

const widget = document.querySelector("#chat-widget");
const launcher = document.querySelector("#chat-launcher");
const panel = document.querySelector("#chat-panel");
const closeButton = document.querySelector("#chat-close");
const statusEl = document.querySelector("#chat-status");
const logEl = document.querySelector("#chat-log");
const form = document.querySelector("#chat-form");
const textInput = document.querySelector("#chat-text");
const micButton = document.querySelector("#chat-mic");
const sendButton = document.querySelector("#chat-send");

const sessionId = `widget-${crypto.randomUUID()}`;
let turnCounter = 0;
let sessionReadyPromise = null;
let activeAudio = null;
let playbackToken = 0;
let mediaStream = null;
let recorder = null;
let recordedChunks = [];
let opened = false;

function randomGuestCredentials() {
  const id = crypto.randomUUID().replace(/-/g, "").slice(0, 20);
  return {
    email: `guest-${id}@aurora-hotel.widget`,
    password: crypto.randomUUID() + crypto.randomUUID(),
  };
}

function setStatus(text) {
  statusEl.textContent = text;
}

function clearEmpty() {
  logEl.querySelector(".chat-empty")?.remove();
}

function addBubble(role, text, meta = "") {
  clearEmpty();
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  bubble.textContent = text;
  if (meta) {
    const metaEl = document.createElement("span");
    metaEl.className = "chat-bubble-meta";
    metaEl.textContent = meta;
    bubble.appendChild(metaEl);
  }
  logEl.appendChild(bubble);
  logEl.scrollTop = logEl.scrollHeight;
  return bubble;
}

function addPending(text) {
  clearEmpty();
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble agent pending";
  bubble.textContent = text;
  logEl.appendChild(bubble);
  logEl.scrollTop = logEl.scrollHeight;
  return bubble;
}

function stopPlayback() {
  playbackToken += 1;
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  if (activeAudio) {
    activeAudio.onplay = null;
    activeAudio.onended = null;
    activeAudio.onerror = null;
    activeAudio.pause();
    activeAudio.removeAttribute("src");
    activeAudio = null;
  }
}

function speakBrowser(text, locale, token) {
  if (!("speechSynthesis" in window) || token !== playbackToken) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = locale;
  utterance.rate = 1.08; // matches the provider-side TTS_SPEED bump
  window.speechSynthesis.speak(utterance);
}

function speak(text, locale = "en-US", audioBase64 = "", audioContentType = "audio/wav") {
  stopPlayback();
  const token = playbackToken;
  if (!audioBase64) {
    speakBrowser(text, locale, token);
    return;
  }
  const audio = new Audio(`data:${audioContentType};base64,${audioBase64}`);
  activeAudio = audio;
  let fellBack = false;
  const fallback = () => {
    if (fellBack || token !== playbackToken) return;
    fellBack = true;
    activeAudio = null;
    speakBrowser(text, locale, token);
  };
  audio.onended = () => { if (token === playbackToken) activeAudio = null; };
  audio.onerror = fallback;
  audio.play().catch(fallback);
}

function setBusy(busy) {
  textInput.disabled = busy;
  sendButton.disabled = busy;
  micButton.disabled = busy || !navigator.mediaDevices?.getUserMedia;
}

async function ensureSession() {
  if (sessionReadyPromise) return sessionReadyPromise;
  sessionReadyPromise = (async () => {
    setStatus("Connecting…");
    let email = null;
    try {
      const me = await fetch("/auth/me");
      if (me.ok) {
        email = (await me.json()).email;
      }
    } catch {
      // fall through to registration
    }
    if (!email) {
      const guest = randomGuestCredentials();
      const response = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(guest),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not start a chat session");
      email = payload.email;
    }
    setStatus("Online");
    setBusy(false);

    try {
      const greetingResponse = await fetch("/greeting", {
        method: "POST",
        headers: { "X-Session-ID": sessionId },
      });
      const greeting = await greetingResponse.json();
      if (!greetingResponse.ok) throw new Error(greeting.error || "Greeting failed");
      addBubble("agent", greeting.reply);
      speak(
        greeting.reply,
        greeting.locale || "en-US",
        greeting.audioBase64 || "",
        greeting.audioContentType || "audio/wav",
      );
    } catch {
      addBubble("agent", "Thanks for calling Aurora Hotel reservations. How can I help?");
    }
    return email;
  })();
  return sessionReadyPromise;
}

function openWidget() {
  opened = true;
  widget.dataset.state = "open";
  launcher.setAttribute("aria-expanded", "true");
  setBusy(true);
  ensureSession()
    .then(() => textInput.focus())
    .catch((error) => {
      setStatus("Connection issue");
      addBubble("agent", error.message, "error");
      setBusy(false);
    });
}

function closeWidget() {
  opened = false;
  widget.dataset.state = "closed";
  launcher.setAttribute("aria-expanded", "false");
}

launcher.addEventListener("click", () => (opened ? closeWidget() : openWidget()));
closeButton.addEventListener("click", closeWidget);

async function sendText(text) {
  if (!text.trim()) return;
  addBubble("caller", text);
  textInput.value = "";
  setBusy(true);
  const pending = addPending("Aurora is thinking…");
  try {
    const response = await fetch("/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Session-ID": sessionId },
      body: JSON.stringify({ text }),
    });
    const payload = await response.json();
    pending.remove();
    if (!response.ok) throw new Error(payload.error || "That message didn't go through.");
    addBubble("agent", payload.reply);
    speak(payload.reply, payload.locale || "en-US", payload.audioBase64 || "", payload.audioContentType || "audio/wav");
  } catch (error) {
    pending.remove();
    addBubble("agent", error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function sendAudio(blob) {
  setBusy(true);
  const pending = addPending("Transcribing your message…");
  const turnId = `turn-${++turnCounter}`;
  try {
    const response = await fetch("/voice-agent", {
      method: "POST",
      headers: {
        "Content-Type": blob.type || "audio/webm",
        "X-Session-ID": sessionId,
        "X-Turn-ID": turnId,
      },
      body: blob,
    });
    const payload = await response.json();
    pending.remove();
    if (!response.ok) throw new Error(payload.error || "That message didn't go through.");
    if (payload.ignored) {
      setBusy(false);
      return;
    }
    addBubble("caller", payload.transcript || "(voice message)");
    addBubble("agent", payload.reply);
    speak(payload.reply, payload.locale || "en-US", payload.audioBase64 || "", payload.audioContentType || "audio/wav");
  } catch (error) {
    pending.remove();
    addBubble("agent", error.message, "error");
  } finally {
    setBusy(false);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendText(textInput.value);
});

async function startRecording() {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch {
    addBubble("agent", "Microphone access was denied — you can still type a message.", "error");
    return;
  }
  recordedChunks = [];
  recorder = new MediaRecorder(mediaStream);
  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) recordedChunks.push(event.data);
  };
  recorder.onstop = () => {
    mediaStream?.getTracks().forEach((track) => track.stop());
    mediaStream = null;
    const blob = new Blob(recordedChunks, { type: recorder.mimeType || "audio/webm" });
    recordedChunks = [];
    micButton.dataset.recording = "false";
    if (blob.size > 800) sendAudio(blob);
  };
  recorder.start();
  micButton.dataset.recording = "true";
  setStatus("Listening…");
}

function stopRecording() {
  if (recorder && recorder.state !== "inactive") recorder.stop();
  setStatus("Online");
}

micButton.addEventListener("click", () => {
  if (micButton.dataset.recording === "true") {
    stopRecording();
  } else {
    startRecording();
  }
});

if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
  micButton.disabled = true;
  micButton.title = "Voice capture isn't supported in this browser — type instead.";
}

setBusy(true);
