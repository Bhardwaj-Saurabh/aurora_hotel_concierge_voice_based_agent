"""
voice_loop.py  -  the turn loop (Layer A).

    mic -> VAD endpointing -> STT -> Agent -> TTS -> speakers

with per-stage latency timing so the room can SEE where the ~800ms turn budget
goes. Provider (Groq/OpenAI) is chosen in .env; see providers.py.

Modes:
    python voice_loop.py          # real mic
    python voice_loop.py --text   # type your turn (no audio deps / no mic)  -  always works
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import threading
import uuid

from agent import (
    Agent,
    FALLBACK_RETRY_MESSAGES,
    FALLBACK_TRANSFER_MESSAGES,
    FILLER_MESSAGES,
)
from providers import make_provider
from spoken_text import normalize_spoken_text
from telemetry import TurnTrace, format_trace, write_trace

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass  # .env is optional; env vars still work. Keeps the offline mock zero-install.

# Env is parsed lazily (not at import) so config_check can report a malformed
# value as one clear startup message instead of an import-time traceback.

def _sample_rate() -> int:
    return int(os.getenv("SAMPLE_RATE", "16000"))


# --- Audio (imported lazily so --text mode needs no audio libs) ---

def record_utterance(trace: TurnTrace) -> bytes:
    """Capture mic until the caller pauses (VAD endpointing). Returns 16-bit PCM."""
    import sounddevice as sd
    import webrtcvad

    SAMPLE_RATE = _sample_rate()
    VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "2"))
    ENDPOINT_SILENCE_MS = int(os.getenv("ENDPOINT_SILENCE_MS", "600"))

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    frame_ms = 30
    frame_len = int(SAMPLE_RATE * frame_ms / 1000)     # samples per frame
    silence_frames_needed = ENDPOINT_SILENCE_MS // frame_ms

    frames: list[bytes] = []
    started = False
    trailing_silence = 0

    print("  (listening: speak, then pause)")
    trace.event("vad.listening", aggressiveness=VAD_AGGRESSIVENESS)
    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=frame_len,
                           dtype="int16", channels=1) as stream:
        while True:
            block, _ = stream.read(frame_len)
            frame = bytes(block)
            if len(frame) < frame_len * 2:             # short tail frame
                continue
            speech = vad.is_speech(frame, SAMPLE_RATE)
            if speech:
                if not started:
                    trace.event("vad.speech_started")
                started = True
                trailing_silence = 0
                frames.append(frame)
            elif started:
                trailing_silence += 1
                frames.append(frame)
                if trailing_silence >= silence_frames_needed:
                    trace.event(
                        "vad.endpoint_detected",
                        endpointSilenceMs=ENDPOINT_SILENCE_MS,
                    )
                    break
    return b"".join(frames)


def play_wav_bytes(wav: bytes) -> None:
    """Play WAV bytes via the configured local audio player."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        f.write(wav)
        f.flush()
        subprocess.run([os.getenv("AUDIO_PLAYER_CMD", "afplay"), f.name], check=False)


def speak(provider, text: str, trace: TurnTrace | None = None) -> None:
    """Speak `text`: cloud TTS returns audio, or the provider handles playback.

    A TTS failure never crashes the call (goal.md 2.2): fall back to the local
    system voice; the text is already printed either way. Text is normalized
    first (goal.md 2.4) so markdown never reaches a voice.
    """
    text = normalize_spoken_text(text)
    print(f"agent> {text}")
    try:
        audio = provider.synthesize(text)
    except Exception as exc:
        if trace:
            trace.event("tts.fallback", errorType=type(exc).__name__)
        subprocess.run([os.getenv("SYSTEM_TTS_CMD", "say"), text], check=False)
        return
    if audio:
        play_wav_bytes(audio)


class LatencyFiller:
    """Speak a short filler if the turn's thinking exceeds a threshold (goal.md 2.5).

    Perceived latency is managed, not just measured: past `threshold_ms` of
    silence the caller hears "One moment." in the session language while the
    LLM/tools keep working. Every firing emits `latency.filler_played` — the
    filler rate is an SLO early-warning signal (fillers spiking = pipeline
    slowing before p95 shows it).
    """

    def __init__(self, speak_fn, threshold_ms: int | None = None):
        self.threshold_ms = (
            threshold_ms if threshold_ms is not None
            else int(os.getenv("LATENCY_FILLER_MS", "1200"))
        )
        self._speak = speak_fn
        self._timer: threading.Timer | None = None
        self.played = False

    def start(self, trace: TurnTrace, language: str) -> None:
        self.played = False
        if self.threshold_ms <= 0:
            return  # disabled

        def _fire() -> None:
            self.played = True
            trace.event(
                "latency.filler_played",
                thresholdMs=self.threshold_ms,
                language=language,
            )
            self._speak(FILLER_MESSAGES.get(language, FILLER_MESSAGES["en"]))

        self._timer = threading.Timer(self.threshold_ms / 1000, _fire)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def stt_failure_response(consecutive_failures: int, language: str) -> tuple[str, bool]:
    """Fallback for a failed transcription: (spoken message, should_transfer).

    First failure re-prompts the caller; the second hands off to a human
    (goal.md 2.2: never dead-end a caller).
    """
    if consecutive_failures >= 2:
        return (
            FALLBACK_TRANSFER_MESSAGES.get(language, FALLBACK_TRANSFER_MESSAGES["en"]),
            True,
        )
    return (
        FALLBACK_RETRY_MESSAGES.get(language, FALLBACK_RETRY_MESSAGES["en"]),
        False,
    )


# --- The loop ---

def run(text_mode: bool) -> None:
    provider = make_provider()
    agent = Agent(provider)
    session_id = f"cli-{uuid.uuid4().hex[:10]}"
    print(f"Provider: {provider.name} | LLM: {provider.llm_model}")
    print("Call started. Say/type 'goodbye' or Ctrl-C to hang up.\n")

    speak(provider, "Thanks for calling Aurora Hotel reservations. How can I help?")

    stt_failures = 0
    while True:
        try:
            if text_mode:
                user_text = input("you> ")
                trace = TurnTrace(session_id=session_id)
                trace.event("input.text")
            else:
                trace = TurnTrace(session_id=session_id)
                with trace.span("capture"):
                    pcm = record_utterance(trace)
                try:
                    with trace.span("stt", model=getattr(provider, "stt_model", "unknown")):
                        user_text = provider.transcribe(pcm, _sample_rate())
                    stt_failures = 0
                except Exception as exc:
                    stt_failures += 1
                    trace.event(
                        "stt.fallback",
                        errorType=type(exc).__name__,
                        consecutiveFailures=stt_failures,
                    )
                    message, should_transfer = stt_failure_response(
                        stt_failures, agent.current_language
                    )
                    speak(provider, message, trace)
                    write_trace(trace.finish(action="transfer" if should_transfer else None))
                    if should_transfer:
                        print("[transferring to front desk: SIP REFER to front-desk]")
                        break
                    continue
                print(f"you> {user_text}")
            if not user_text.strip():
                continue

            filler = LatencyFiller(lambda text: speak(provider, text, trace))
            filler.start(trace, agent.current_language)
            try:
                reply, action = agent.respond(user_text, trace=trace)
            finally:
                filler.stop()

            with trace.span("tts", model=getattr(provider, "tts_model", "unknown")):
                speak(provider, reply, trace)

            payload = trace.finish(action=action, sources=agent.last_sources)
            write_trace(payload)
            print(format_trace(payload))
            print()

            if action == "hangup":
                print("[call ended: SIP BYE]")
                break
            if action == "transfer":
                print("[transferring to front desk: SIP REFER to front-desk]")
                break

        except (EOFError, KeyboardInterrupt):
            print("\n[caller hung up: SIP BYE]")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Workshop voice loop")
    parser.add_argument("--text", action="store_true",
                        help="type turns instead of speaking (no mic / no audio deps)")
    args = parser.parse_args()
    from config_check import require_valid_config
    require_valid_config()  # fail fast, before the first turn (goal.md 2.3)
    run(text_mode=args.text)


if __name__ == "__main__":
    main()
