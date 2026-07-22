"""Meta routes: the web client shell, /state, and /token (goal.md ADR-015/018).

GET /state stays dependency-free and unauthenticated — Fly's health check
(fly.talk-server.toml) polls it with no cookie; it must succeed on a cold
start before any DB or provider is touched.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from aurora.server.deps import require_user
from aurora.server.sessions import agent_provider_name

router = APIRouter()

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def _supported_languages() -> list[str]:
    """Derive from the router so /state can never drift from the agent."""
    from aurora.core.router import LANGUAGES
    return sorted(LANGUAGES)


def _livekit_url() -> str:
    raw = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    if raw.startswith("http://"):
        return "ws://" + raw[len("http://"):]
    if raw.startswith("https://"):
        return "wss://" + raw[len("https://"):]
    return raw


def _livekit_api_key() -> str:
    return os.getenv("LIVEKIT_API_KEY", "devkey")


def _livekit_api_secret() -> str:
    return os.getenv("LIVEKIT_API_SECRET", "secret")


def _livekit_room() -> str:
    return os.getenv("LIVEKIT_ROOM", "aurora-demo-room")


def _token(identity: str, name: str, room: str) -> str:
    if _livekit_api_secret() == "secret":
        warnings.filterwarnings("ignore", category=jwt.InsecureKeyLengthWarning)
    from aurora.server.token_utils import mint_token
    return mint_token(
        api_key=_livekit_api_key(), api_secret=_livekit_api_secret(),
        identity=identity, name=name, room=room,
    )


@router.get("/")
def index():
    """Public marketing site with the embedded voice/chat widget."""
    return FileResponse(WEB_DIR / "site.html")


@router.get("/console")
def console():
    """Internal ops console: the two-party LiveKit room demo + telemetry
    timeline used for development and the workshop (formerly served at /)."""
    return FileResponse(WEB_DIR / "index.html")


@router.get("/state")
def state():
    return JSONResponse({
        "livekitRoom": _livekit_room(),
        "livekitUrl": _livekit_url(),
        "agentProvider": agent_provider_name(),
        "languages": _supported_languages(),
    })


@router.get("/token")
def token(request: Request, user_id: int = Depends(require_user)):
    query = request.query_params
    requested_identity = query.get("identity", "caller-demo")
    name = query.get("name", requested_identity)
    room = query.get("room", _livekit_room())
    # Identity is server-derived from the authenticated user (goal.md
    # ADR-018) — the caller-supplied label is kept as a display suffix
    # (this demo intentionally opens two role participants, "caller-demo"
    # and "aurora-agent", per browser session) but can never be forged to
    # collide with another authenticated user's identity.
    identity = f"{requested_identity}-u{user_id}"

    return JSONResponse({
        "url": _livekit_url(),
        "room": room,
        "identity": identity,
        "token": _token(identity, name, room),
    })
