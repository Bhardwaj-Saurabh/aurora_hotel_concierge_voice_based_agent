// Public-site chat widget (goal.md, live debugging + frontend pass).
//
// Reuses the same authenticated endpoints as the ops console (talk_server's
// /auth/*, /greeting, /agent, /voice-agent) but skips any visible login: a
// disposable guest account is registered silently on first open, exactly
// like a real site visitor expects from a hotel's chat widget.
//
// Voice capture mirrors the console's continuous auto-VAD (same tuning,
// same barge-in behavior) rather than a manual per-utterance record button:
// click the mic once to start listening naturally, speak whenever you like
// (including interrupting Aurora mid-reply), click again to stop.

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
let opened = false;

// --- playback (voice consistency across turns/languages) ---

// Known female system/browser voice names per language (macOS, Chrome,
// Windows) — mirrors the console's talk.js so the fallback voice never
// switches gender turn to turn or when the session language changes.
const FEMALE_VOICE_HINTS = {
  en: ["samantha", "karen", "moira", "tessa", "victoria", "allison", "ava",
       "susan", "zoe", "nicky", "zira", "hazel", "google us english",
       "google uk english female"],
  es: ["monica", "mónica", "paulina", "marisol", "angelica", "helena",
       "sabina", "google español"],
  fr: ["amelie", "amélie", "audrey", "aurelie", "aurélie", "marie", "julie",
       "hortense", "google français"],
};

let voicesReadyPromise = null;
function waitForVoices() {
  if (!("speechSynthesis" in window)) return Promise.resolve([]);
  const existing = window.speechSynthesis.getVoices();
  if (existing.length) return Promise.resolve(existing);
  if (!voicesReadyPromise) {
    voicesReadyPromise = new Promise((resolve) => {
      const done = () => resolve(window.speechSynthesis.getVoices());
      window.speechSynthesis.addEventListener("voiceschanged", done, { once: true });
      setTimeout(done, 300); // some browsers never fire voiceschanged
    });
  }
  return voicesReadyPromise;
}

// Resolved once per language and reused for the rest of the session, so the
// voice can never drift turn-to-turn or when the language switches.
const chosenVoiceCache = new Map();

async function chooseVoice(locale) {
  if (!("speechSynthesis" in window)) return null;
  const language = locale.toLowerCase().split("-")[0];
  if (chosenVoiceCache.has(language)) return chosenVoiceCache.get(language);

  const voices = await waitForVoices();
  const candidates = voices.filter((v) => v.lang.toLowerCase().startsWith(language));
  const hints = FEMALE_VOICE_HINTS[language] || [];
  const voice = candidates.find(
    (v) => hints.some((hint) => v.name.toLowerCase().includes(hint)),
  ) || candidates[0] || null;

  chosenVoiceCache.set(language, voice); // cache the miss too: never re-search
  return voice;
}

let activeAudio = null;
let playbackToken = 0;
let agentSpeaking = false;
let playbackStartedAt = 0;
let playbackEchoFloor = 0.012;

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

function beginPlayback(token) {
  if (token !== playbackToken) return;
  agentSpeaking = true;
  playbackStartedAt = Date.now();
  playbackEchoFloor = Math.max(noiseFloor, 0.012);
  listenCooldownUntil = playbackStartedAt + tuning.bargeInArmMs;
  setStatus("Aurora is speaking…");
}

function finishPlayback(token) {
  if (token !== playbackToken) return;
  activeAudio = null;
  agentSpeaking = false;
  listenCooldownUntil = Date.now() + 500;
  setStatus(listenStream ? "Listening…" : "Online");
}

async function speakBrowser(text, locale, token) {
  if (!("speechSynthesis" in window) || token !== playbackToken) {
    finishPlayback(token);
    return;
  }
  const voice = await chooseVoice(locale);
  if (token !== playbackToken) {
    finishPlayback(token);
    return;
  }
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = locale;
  utterance.rate = 1.08; // matches the provider-side TTS_SPEED bump
  utterance.pitch = 1.0;
  if (voice) utterance.voice = voice;
  utterance.onstart = () => beginPlayback(token);
  utterance.onend = () => finishPlayback(token);
  utterance.onerror = () => finishPlayback(token);
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
  audio.onplay = () => beginPlayback(token);
  audio.onended = () => finishPlayback(token);
  audio.onerror = fallback;
  audio.play().catch(fallback);
}

// --- transcript UI ---

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

function setInputBusy(busy) {
  textInput.disabled = busy;
  sendButton.disabled = busy;
}

function setSessionReady(ready) {
  setInputBusy(!ready);
  micButton.disabled = !ready || !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

// --- session bootstrap ---

function randomGuestCredentials() {
  const id = crypto.randomUUID().replace(/-/g, "").slice(0, 20);
  return {
    email: `guest-${id}@aurora-hotel.widget`,
    password: crypto.randomUUID() + crypto.randomUUID(),
  };
}

async function ensureSession() {
  if (sessionReadyPromise) return sessionReadyPromise;
  sessionReadyPromise = (async () => {
    setStatus("Connecting…");
    let email = null;
    try {
      const me = await fetch("/auth/me");
      if (me.ok) email = (await me.json()).email;
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
    setSessionReady(true);
    setStatus("Online");

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
  setSessionReady(false);
  ensureSession()
    .then(() => textInput.focus())
    .catch((error) => {
      setStatus("Connection issue");
      addBubble("agent", error.message, "error");
      setInputBusy(false);
    });
}

function closeWidget() {
  opened = false;
  widget.dataset.state = "closed";
  launcher.setAttribute("aria-expanded", "false");
  disarmMic(); // don't keep the microphone hot while the panel is hidden
}

launcher.addEventListener("click", () => (opened ? closeWidget() : openWidget()));
closeButton.addEventListener("click", closeWidget);

// --- turn sending ---

let agentBusy = false;

async function sendText(text) {
  if (!text.trim()) return;
  addBubble("caller", text);
  textInput.value = "";
  agentBusy = true;
  setInputBusy(true);
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
    agentBusy = false;
    setInputBusy(false);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendText(textInput.value);
});

async function sendAudio(blob, wasBargeIn) {
  agentBusy = true;
  const pending = addPending(wasBargeIn ? "Aurora heard you — one second…" : "Aurora is listening…");
  const turnId = `turn-${++turnCounter}`;
  try {
    const response = await fetch("/voice-agent", {
      method: "POST",
      headers: {
        "Content-Type": blob.type || "audio/webm",
        "X-Session-ID": sessionId,
        "X-Turn-ID": turnId,
        "X-Barge-In": String(Boolean(wasBargeIn)),
      },
      body: blob,
    });
    const payload = await response.json();
    pending.remove();
    if (!response.ok) throw new Error(payload.error || "That message didn't go through.");
    if (payload.ignored) {
      return;
    }
    addBubble("caller", payload.transcript || "(voice message)");
    addBubble("agent", payload.reply);
    speak(payload.reply, payload.locale || "en-US", payload.audioBase64 || "", payload.audioContentType || "audio/wav");
  } catch (error) {
    pending.remove();
    addBubble("agent", error.message, "error");
  } finally {
    agentBusy = false;
    setStatus(listenStream ? (agentSpeaking ? "Aurora is speaking…" : "Listening…") : "Online");
  }
}

// --- continuous voice capture (mirrors the console's auto-VAD + barge-in) ---

let listenStream = null;
let audioContext = null;
let analyser = null;
let vadFrame = null;
let recorder = null;
let recordedChunks = [];
let recordingStartedAt = 0;
let lastSpeechAt = 0;
let speechCandidateAt = 0;
let bargeCandidateAt = 0;
let bargeRecordingCandidate = false;
let listenCooldownUntil = 0;
let noiseFloor = 0.008;
let smoothedLevel = 0;
let discardRecording = false;
let lastEndpointAt = 0;
let pendingBargeInTurn = false;
let currentTurnWasBargeIn = false;

const tuning = {
  endpointSilenceMs: 650,
  sensitivity: 3.2,
  minTurnMs: 500,
  speechConfirmationMs: 110,
  bargeInConfirmationMs: 200,
  bargeInArmMs: 450,
  maxTurnMs: 20000,
};

function audioLevel() {
  if (!analyser) return 0;
  const samples = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(samples);
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  return Math.sqrt(sum / samples.length);
}

function thresholds() {
  const start = Math.min(0.09, Math.max(0.012, noiseFloor * tuning.sensitivity));
  return {
    start,
    end: Math.max(0.008, start * 0.58),
    barge: Math.min(0.12, Math.max(0.024, start * 1.55, playbackEchoFloor * 1.8)),
  };
}

function startTurnRecording(isBargeIn = false) {
  if (!listenStream || recorder || agentBusy) return;
  recordedChunks = [];
  discardRecording = false;
  recorder = new MediaRecorder(listenStream);
  currentTurnWasBargeIn = isBargeIn || pendingBargeInTurn;
  pendingBargeInTurn = false;
  recordingStartedAt = Date.now();
  lastSpeechAt = recordingStartedAt;
  micButton.dataset.recording = "true";
  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) recordedChunks.push(event.data);
  };
  recorder.onstop = () => {
    const shouldDiscard = discardRecording;
    const mimeType = recorder.mimeType || "audio/webm";
    const blob = new Blob(recordedChunks, { type: mimeType });
    recorder = null;
    recordedChunks = [];
    micButton.dataset.recording = "false";
    const wasBargeIn = currentTurnWasBargeIn;
    currentTurnWasBargeIn = false;
    if (shouldDiscard || blob.size < 800) {
      if (listenStream) setStatus(agentSpeaking ? "Aurora is speaking…" : "Listening…");
      return;
    }
    sendAudio(blob, wasBargeIn);
  };
  recorder.start(100);
  setStatus("Listening to you…");
}

function stopTurnRecording(discard = false) {
  if (!recorder || recorder.state === "inactive") return;
  discardRecording = discard;
  recorder.stop();
}

function interruptAgent() {
  if (!agentSpeaking) return;
  stopPlayback();
  agentSpeaking = false;
  listenCooldownUntil = Date.now() + 80;
  setStatus("Listening…");
}

function vadLoop() {
  if (!listenStream) return;
  const now = Date.now();
  const rawLevel = audioLevel();
  smoothedLevel = (smoothedLevel * 0.72) + (rawLevel * 0.28);
  const limit = thresholds();

  if (!recorder && !agentSpeaking && !agentBusy && smoothedLevel < limit.start) {
    noiseFloor = (noiseFloor * 0.985) + (rawLevel * 0.015);
  }

  if (agentSpeaking) {
    const playbackAge = now - playbackStartedAt;
    if (playbackAge < tuning.bargeInArmMs) {
      playbackEchoFloor = (playbackEchoFloor * 0.88) + (smoothedLevel * 0.12);
      bargeCandidateAt = 0;
    } else if (smoothedLevel > limit.barge) {
      if (!bargeCandidateAt) {
        bargeCandidateAt = now;
        bargeRecordingCandidate = true;
        startTurnRecording(true);
        lastSpeechAt = now;
      }
      if (now - bargeCandidateAt >= tuning.bargeInConfirmationMs) {
        bargeRecordingCandidate = false;
        interruptAgent();
        pendingBargeInTurn = false;
        lastSpeechAt = now;
        bargeCandidateAt = 0;
      }
    } else {
      if (bargeRecordingCandidate) {
        stopTurnRecording(true);
        bargeRecordingCandidate = false;
      }
      bargeCandidateAt = 0;
      playbackEchoFloor = (playbackEchoFloor * 0.995) + (smoothedLevel * 0.005);
    }
  } else if (!agentBusy && now > listenCooldownUntil) {
    if (!recorder) {
      if (smoothedLevel > limit.start) {
        speechCandidateAt = speechCandidateAt || now;
        if (now - speechCandidateAt >= tuning.speechConfirmationMs) {
          startTurnRecording();
          speechCandidateAt = 0;
        }
      } else {
        speechCandidateAt = 0;
      }
    } else {
      if (smoothedLevel > limit.end) lastSpeechAt = now;
      const duration = now - recordingStartedAt;
      const endpointReached = duration >= tuning.minTurnMs
        && now - lastSpeechAt >= tuning.endpointSilenceMs;
      if (endpointReached || duration >= tuning.maxTurnMs) {
        lastEndpointAt = Date.now();
        stopTurnRecording();
      }
    }
  }

  vadFrame = requestAnimationFrame(vadLoop);
}

async function armMic() {
  if (listenStream || micButton.disabled) return;
  try {
    listenStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch {
    addBubble("agent", "Microphone access was denied — you can still type a message.", "error");
    return;
  }
  audioContext = new AudioContext();
  const source = audioContext.createMediaStreamSource(listenStream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);
  micButton.dataset.armed = "true";
  micButton.title = "Listening naturally — click to stop";
  setStatus("Calibrating…");
  vadFrame = requestAnimationFrame(vadLoop);
  await new Promise((resolve) => setTimeout(resolve, 650));
  if (listenStream) setStatus(agentSpeaking ? "Aurora is speaking…" : "Listening…");
}

function disarmMic() {
  if (vadFrame) cancelAnimationFrame(vadFrame);
  vadFrame = null;
  if (recorder && recorder.state !== "inactive") {
    discardRecording = true;
    recorder.stop();
  }
  listenStream?.getTracks().forEach((track) => track.stop());
  listenStream = null;
  audioContext?.close();
  audioContext = null;
  analyser = null;
  bargeCandidateAt = 0;
  speechCandidateAt = 0;
  bargeRecordingCandidate = false;
  micButton.dataset.armed = "false";
  micButton.dataset.recording = "false";
  micButton.title = "Click to talk naturally";
  if (opened) setStatus("Online");
}

micButton.addEventListener("click", () => {
  if (listenStream) {
    disarmMic();
  } else {
    armMic();
  }
});

if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
  micButton.disabled = true;
  micButton.title = "Voice capture isn't supported in this browser — type instead.";
}

setSessionReady(false);
