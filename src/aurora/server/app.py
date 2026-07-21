"""Aurora talk server — FastAPI app factory + entrypoint (goal.md ADR-020).

Serves the browser client (packaged web/), LiveKit token minting, the auth
routes (ADR-018), and the HTTP turn bridge (/agent, /voice-agent). Run with
`python -m aurora.server` (or the `aurora-server` console script).

Response-shape contract (pinned by tests/test_talk_server.py, consumed by
web/auth.js + web/talk.js): every error body is `{"error": <message>}` with
the pre-port status codes — deliberately NO Pydantic request models anywhere,
so FastAPI's 422 validation machinery can never fire; bodies are parsed by
hand from raw bytes inside each route.

Single-process only: the session registry and rate limiters are in-process
memory (sessions.py/deps.py), so main() runs uvicorn with exactly one worker.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from aurora.config.env import load_env_files
from aurora.server.deps import ApiError, _reset_rate_limiters_for_tests  # noqa: F401 (test seam)
from aurora.server.replies import (  # noqa: F401 (facade for tests + back-compat)
    GREETING,
    _agent_reply,
    _browser_tts_payload,
    _finish_response,
    _greeting_reply,
    _is_probable_playback_echo,
    _voice_agent_reply,
)
from aurora.server.routes.meta import _supported_languages  # noqa: F401 (test seam)
from aurora.server.sessions import agent_provider_name as _agent_provider_name

HOST = os.getenv("TALK_HOST", "localhost")
PORT = int(os.getenv("TALK_PORT", "5173"))
WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app() -> FastAPI:
    # No docs/openapi routes: the pre-port server exposed exactly its own
    # endpoints and nothing else — keep the public surface identical.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    from aurora.server.routes.auth import router as auth_router
    from aurora.server.routes.meta import router as meta_router
    from aurora.server.routes.turns import router as turns_router

    app.include_router(meta_router)
    app.include_router(auth_router)
    app.include_router(turns_router)
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    return app


def main() -> None:
    load_env_files((Path.cwd() / ".env",))
    os.environ.setdefault(
        "TELEMETRY_JSONL",
        str(Path.cwd() / "logs" / "voice-events.jsonl"),
    )
    from aurora.config.check import require_valid_config
    require_valid_config()  # fail fast, before the first call (goal.md 2.3)
    if not os.getenv("POSTGRES_HOST", "").strip():
        raise SystemExit(
            "POSTGRES_HOST is not set. talk-server requires the Postgres-backed user-auth "
            "system (goal.md ADR-018) to protect /token, /agent, /voice-agent, /greeting, "
            "and /reset — set POSTGRES_* in .env (see config.example.env)."
        )

    from aurora.server.routes.meta import _livekit_room, _livekit_url

    print(f"Open http://{HOST}:{PORT}")
    print(f"LiveKit URL: {_livekit_url()}")
    print(f"Room: {_livekit_room()}")
    print(f"Agent provider: {_agent_provider_name()}")
    print(f"TTS backend: {os.getenv('TTS_BACKEND', 'provider').lower()}")
    print("Use the two panes for LiveKit audio. Use the conversation panel for the hotel agent.")

    import uvicorn

    # workers=1 is load-bearing (in-process session registry + limiters).
    uvicorn.run(create_app(), host=HOST, port=PORT, workers=1, log_level="info")


if __name__ == "__main__":
    main()
