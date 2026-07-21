"""Per-user conversation session registry (goal.md ADR-018/ADR-020).

Sessions are keyed (user_id, session_id) so a client-supplied X-Session-ID can
never reach another authenticated user's conversation state. One lock per
session serializes turns within a conversation while distinct sessions run
concurrently. In-process memory — the server must run as a single process
(uvicorn workers=1, pinned in app.py).
"""

from __future__ import annotations

import os
import threading

_session_registry_lock = threading.Lock()
_agent_sessions: dict[tuple[int, str], object] = {}
_session_locks: dict[tuple[int, str], threading.Lock] = {}


def agent_provider_name() -> str:
    return os.getenv("PROVIDER", "mock").lower()


def _new_agent():
    from aurora.core.agent import Agent
    from aurora.core.providers import make_provider

    return Agent(make_provider(agent_provider_name()))


def get_session(key: tuple[int, str]):
    with _session_registry_lock:
        if key not in _agent_sessions:
            _agent_sessions[key] = _new_agent()
            _session_locks[key] = threading.Lock()
        return _agent_sessions[key], _session_locks[key]


def reset_session(key: tuple[int, str]) -> None:
    with _session_registry_lock:
        _agent_sessions.pop(key, None)
        _session_locks.pop(key, None)
