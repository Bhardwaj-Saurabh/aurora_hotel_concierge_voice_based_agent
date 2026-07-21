"""Turn-reply builders for the talk server (goal.md ADR-020 split).

Bodies are unchanged from the pre-FastAPI server: these functions were already
transport-independent (they take a session key and return a dict), which is
what made the framework swap a pure re-wiring. The response dict shape is
consumed verbatim by web/talk.js — do not rename keys casually.
"""

from __future__ import annotations

import base64
from io import BytesIO

from aurora.core.spoken_text import normalize_spoken_text
from aurora.server.sessions import agent_provider_name, get_session

GREETING = "Thanks for calling Aurora Hotel reservations. How can I help?"


def _trace(session_id: str, turn_id: str | None = None):
    from aurora.telemetry.traces import TurnTrace

    return TurnTrace(session_id=session_id, turn_id=turn_id)


def _finish_response(agent, trace, reply: str, action: str | None, **extra) -> dict:
    from aurora.telemetry.traces import write_trace

    reply = normalize_spoken_text(reply)  # browser TTS speaks this verbatim (goal.md 2.4)
    sources = extra.pop("response_sources", agent.last_sources)
    payload = trace.finish(action=action, sources=sources)
    write_trace(payload)
    return {
        "reply": reply,
        "action": action,
        "provider": getattr(agent.provider, "name", agent_provider_name()),
        "model": getattr(agent.provider, "llm_model", "unknown"),
        "language": agent.current_language,
        "locale": agent.current_locale,
        "sources": sources,
        "trace": payload,
        **extra,
    }


def _browser_tts_payload(agent, trace, text: str) -> dict:
    """Return provider audio for the browser or select its local voice fallback."""
    text = normalize_spoken_text(text)  # never synthesize markdown (goal.md 2.4)
    provider = agent.provider
    backend = getattr(provider, "tts_backend", "provider")
    if backend != "provider" or getattr(provider, "name", "") == "mock":
        return {"ttsBackend": "browser"}

    model = getattr(provider, "tts_model", "unknown")
    voice = getattr(provider, "tts_voice", "unknown")
    try:
        with trace.span("tts", model=model, voice=voice):
            audio = provider.synthesize(text)
    except Exception as exc:
        trace.event("tts.fallback", errorType=type(exc).__name__)
        return {"ttsBackend": "browser", "ttsFallback": True}

    if not audio:
        trace.event("tts.fallback", errorType="EmptyAudio")
        return {"ttsBackend": "browser", "ttsFallback": True}
    return {
        "ttsBackend": "provider",
        "ttsModel": model,
        "ttsVoice": voice,
        "audioContentType": "audio/wav",
        "audioBase64": base64.b64encode(audio).decode("ascii"),
    }


def _greeting_reply(key: tuple[int, str]) -> dict:
    agent, lock = get_session(key)
    trace = _trace(key[1], "greeting")
    trace.event("greeting.requested")
    with lock:
        tts = _browser_tts_payload(agent, trace, GREETING)
    return _finish_response(
        agent,
        trace,
        GREETING,
        None,
        response_sources=[],
        **tts,
    )


def _agent_reply(text: str, key: tuple[int, str], turn_id: str | None) -> dict:
    agent, lock = get_session(key)
    trace = _trace(key[1], turn_id)
    trace.event("input.text")
    with lock:
        reply, action = agent.respond(text, trace=trace)
        tts = _browser_tts_payload(agent, trace, reply)
    return _finish_response(agent, trace, reply, action, **tts)


def _voice_agent_reply(
    audio: bytes,
    content_type: str,
    key: tuple[int, str],
    turn_id: str | None,
    was_barge_in: bool,
) -> dict:
    agent, lock = get_session(key)
    trace = _trace(key[1], turn_id)
    trace.event("audio.received", bytes=len(audio), contentType=content_type)
    if was_barge_in:
        trace.event("barge_in.turn_started")
    with lock:
        if getattr(agent.provider, "name", "") == "mock":
            with trace.span("stt", model=getattr(agent.provider, "stt_model", "unknown")):
                transcript = agent.provider.transcribe(b"")
        else:
            audio_file = BytesIO(audio)
            if "mp4" in content_type:
                audio_file.name = "caller.mp4"
            elif "ogg" in content_type:
                audio_file.name = "caller.ogg"
            else:
                audio_file.name = "caller.webm"
            with trace.span("stt", model=getattr(agent.provider, "stt_model", "unknown")):
                transcription_args = {
                    "model": agent.provider.stt_model,
                    "file": audio_file,
                    "response_format": "text",
                }
                stt_prompt = getattr(agent.provider, "stt_prompt", "")
                if stt_prompt:
                    transcription_args["prompt"] = stt_prompt
                stt = agent.provider.client.audio.transcriptions.create(**transcription_args)
            transcript = (stt if isinstance(stt, str) else stt.text).strip()
        if was_barge_in and _is_probable_playback_echo(transcript):
            trace.event("barge_in.echo_suppressed", transcript=transcript)
            return _finish_response(
                agent,
                trace,
                "",
                None,
                transcript=transcript,
                sttModel=getattr(agent.provider, "stt_model", "unknown"),
                ignored=True,
                ignoreReason="probable_playback_echo",
                response_sources=[],
            )
        reply, action = agent.respond(transcript, trace=trace)
        tts = _browser_tts_payload(agent, trace, reply)
    return _finish_response(
        agent,
        trace,
        reply,
        action,
        transcript=transcript,
        sttModel=getattr(agent.provider, "stt_model", "unknown"),
        **tts,
    )


def _is_probable_playback_echo(transcript: str) -> bool:
    normalized = " ".join(
        transcript.lower().replace("'", "").replace(".", "").replace(",", "").split()
    )
    return normalized in {
        "all right",
        "alright",
        "thanks",
        "thank you",
        "youre welcome",
        "your welcome",
    }
