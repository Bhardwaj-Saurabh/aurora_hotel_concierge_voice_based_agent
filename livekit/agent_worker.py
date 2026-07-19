"""
agent_worker.py  -  room-native Aurora agent (goal.md 3.1, ADR-008).

The worker joins the LiveKit room as a real participant: it subscribes to the
caller's audio track, runs VAD/turn detection server-side (Silero), transcribes
with the configured provider, and publishes Aurora's replies as a TTS audio
track. This replaces the HTTP turn bridge for room-native calls; the bridge
(`talk_server.py` /voice-agent) remains the documented fallback.

The pipeline `Agent` stays the single brain (ADR-002): the framework's LLM slot
is overridden with `llm_node`, which feeds the caller's transcript to
`Agent.respond` and returns the reply text. Tools, routing, guardrails,
grounding, telemetry, and evals are untouched — the worker is transport.

Run (needs a live provider for STT/TTS; the mock cannot hear or speak):

    source ../.venv/bin/activate
    python agent_worker.py dev        # against ./start_local_server.sh
    python agent_worker.py start      # production mode

STT and TTS reuse the provider presets from pipeline/providers.py — Groq and
OpenAI both speak the OpenAI API dialect, so one plugin covers both via
base_url, exactly like the pipeline's own adaptor.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PIPELINE_ROOT = ROOT.parent / "pipeline"
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from livekit import agents
from livekit.agents import AgentSession, JobContext, WorkerOptions, cli, get_job_context
from livekit.plugins import openai as openai_plugin
from livekit.plugins import silero

from agent import Agent as AuroraBrain  # noqa: E402  (pipeline)
from env_loader import load_env_files  # noqa: E402
from providers import DEFAULT_STT_PROMPT, PRESETS, make_provider  # noqa: E402
from telemetry import TurnTrace, write_trace  # noqa: E402

GREETING = "Thanks for calling Aurora Hotel reservations. How can I help?"
_STREAM_END = object()  # sentinel closing the brain→TTS delta queue


def _require_live_provider(name: str) -> None:
    """A live room needs real STT/TTS; the mock provider can neither hear nor speak."""
    if name not in ("openai", "groq"):
        print(
            f"PROVIDER={name!r} cannot drive a live room: the worker needs real "
            "STT and TTS. Set PROVIDER=openai or PROVIDER=groq (with its API key) "
            "in pipeline/.env. The offline mock path lives in voice_loop.py --text."
        )
        raise SystemExit(2)


def _latest_user_text(chat_ctx) -> str:
    """The framework's chat context carries the transcript; the brain owns history."""
    for item in reversed(chat_ctx.items):
        if getattr(item, "role", None) == "user" and item.text_content:
            return item.text_content
    return ""


class AuroraRoomAgent(agents.Agent):
    """Adapter: the framework's LLM slot delegates to the pipeline brain."""

    def __init__(self, brain: AuroraBrain, session_id: str):
        # The brain owns the real system prompt; the framework never calls an LLM.
        super().__init__(instructions="You are Aurora. (Handled by the pipeline brain.)")
        self._brain = brain
        self._session_id = session_id
        self._turn = 0

    async def llm_node(self, chat_ctx, tools, model_settings):
        """Stream the brain's reply into the framework's TTS (goal.md 3.2).

        `respond_stream` yields content deltas as the provider streams them;
        the framework sentence-batches into TTS, so first audio starts before
        the full reply exists. Markdown is filtered by the session's default
        tts_text_transforms.
        """
        user_text = _latest_user_text(chat_ctx)
        self._turn += 1
        trace = TurnTrace(session_id=self._session_id, turn_id=f"turn-{self._turn}")
        trace.event("input.room_audio")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel = threading.Event()  # barge-in signal into the brain (goal.md 3.3)

        def put(item) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # event loop already gone (cancelled turn during shutdown)

        def produce() -> None:
            # The producer owns the trace: it is the last to touch it whether
            # the turn completes, fails, or is cancelled by a barge-in.
            try:
                for piece in self._brain.respond_stream(
                    user_text, trace=trace, cancel=cancel
                ):
                    put(piece)
            except Exception as exc:  # respond_stream falls back internally
                put(exc)
            finally:
                write_trace(trace.finish(
                    action=self._brain.last_action,
                    sources=self._brain.last_sources,
                ))
                put(_STREAM_END)

        producer = loop.run_in_executor(None, produce)
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        except (asyncio.CancelledError, GeneratorExit):
            # Framework barge-in: stop the brain too — no zombie turn keeps
            # consuming provider tokens or running tools for a reply nobody
            # will hear.
            cancel.set()
            raise
        await producer
        action = self._brain.last_action
        if action in ("transfer", "hangup"):
            self._schedule_finish(action)

    def _schedule_finish(self, action: str) -> None:
        try:
            session = self.session
        except RuntimeError:
            return  # not attached to a live session (unit tests, teardown races)
        asyncio.create_task(_finish_call(session, action))


async def _finish_call(session: AgentSession, action: str) -> None:
    """Let the goodbye finish playing, then end the call (SIP BYE semantics).

    Distributed transfer (SIP REFER to a human) arrives with Phase 3.4; until
    then both actions tear the room down after the spoken handoff line.
    """
    speech = session.current_speech
    if speech is not None:
        await speech.wait_for_playout()
    await get_job_context().delete_room()


def _stt_tts_from_presets(provider_name: str):
    preset = PRESETS[provider_name]
    api_key = os.getenv(preset["api_key_env"], "")
    stt = openai_plugin.STT(
        model=os.getenv("STT_MODEL", "").strip() or preset["stt_model"],
        prompt=os.getenv("STT_PROMPT", "").strip() or DEFAULT_STT_PROMPT,
        base_url=preset["base_url"],
        api_key=api_key,
    )
    tts = openai_plugin.TTS(
        model=os.getenv("TTS_MODEL", "").strip() or preset["tts_model"],
        voice=os.getenv("TTS_VOICE", "").strip() or preset["tts_voice"],
        base_url=preset["base_url"],
        api_key=api_key,
    )
    return stt, tts


async def entrypoint(ctx: JobContext) -> None:
    provider_name = os.getenv("PROVIDER", "mock").lower()
    _require_live_provider(provider_name)

    brain = AuroraBrain(make_provider(provider_name))
    stt, tts = _stt_tts_from_presets(provider_name)
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=stt,
        tts=tts,
    )
    await session.start(
        agent=AuroraRoomAgent(brain, session_id=f"room-{ctx.room.name}"),
        room=ctx.room,
    )
    await session.say(GREETING)


def main() -> None:
    load_env_files((PIPELINE_ROOT / ".env", ROOT / ".env"))
    os.environ.setdefault(
        "TELEMETRY_JSONL",
        str(ROOT.parent / "logs" / "voice-events.jsonl"),
    )
    from config_check import require_valid_config
    require_valid_config()
    _require_live_provider(os.getenv("PROVIDER", "mock").lower())
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    main()
