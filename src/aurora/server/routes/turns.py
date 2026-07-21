"""Turn routes: /reset, /greeting, /agent, /voice-agent (goal.md ADR-020).

All four are auth-gated (ADR-018). Error contract pinned by tests and
web/talk.js: any turn failure — including a bad JSON body on /agent — returns
500 {"error": str(exc)}, exactly as the stdlib server did. Endpoints are sync
`def`s so FastAPI runs them on its threadpool (the ThreadingHTTPServer
equivalent); the per-session lock in sessions.py serializes same-session turns.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from aurora.server.deps import raw_body, require_user
from aurora.server.replies import _agent_reply, _greeting_reply, _voice_agent_reply
from aurora.server.sessions import reset_session

router = APIRouter()


def _session_key(request: Request, user_id: int) -> tuple[int, str]:
    return (user_id, request.headers.get("X-Session-ID", "browser-demo"))


@router.post("/reset")
def reset(request: Request, user_id: int = Depends(require_user)):
    key = _session_key(request, user_id)
    reset_session(key)
    return JSONResponse({"reset": True, "sessionId": key[1]})


@router.post("/greeting")
def greeting(request: Request, user_id: int = Depends(require_user)):
    key = _session_key(request, user_id)
    try:
        return JSONResponse(_greeting_reply(key))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/agent")
def agent_turn(
    request: Request,
    user_id: int = Depends(require_user),
    body: bytes = Depends(raw_body),
):
    key = _session_key(request, user_id)
    turn_id = request.headers.get("X-Turn-ID")
    try:
        payload = json.loads(body or b"{}")
        text = str(payload.get("text", "")).strip()
        if not text:
            raise ValueError("Missing text")
        response = _agent_reply(text, key, turn_id)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(response)


@router.post("/voice-agent")
def voice_agent_turn(
    request: Request,
    user_id: int = Depends(require_user),
    body: bytes = Depends(raw_body),
):
    key = _session_key(request, user_id)
    turn_id = request.headers.get("X-Turn-ID")
    try:
        if not body:
            raise ValueError("Missing audio")
        response = _voice_agent_reply(
            body,
            request.headers.get("Content-Type", ""),
            key,
            turn_id,
            request.headers.get("X-Barge-In", "false").lower() == "true",
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(response)
