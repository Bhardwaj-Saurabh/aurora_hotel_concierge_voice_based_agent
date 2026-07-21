"""FastAPI dependencies: auth gate + rate limiters (goal.md ADR-018/ADR-020).

Response-shape contract (pinned by tests, consumed by web/auth.js + talk.js):
every error here is `{"error": <message>}` with the same status codes the
stdlib server used — 401 "Authentication required", 429 for both limiters.
ApiError + its handler in app.py keep FastAPI's default `{"detail": ...}`
shape out of the API entirely.
"""

from __future__ import annotations

import os
import threading

from fastapi import Request


class ApiError(Exception):
    """Carries (status, message) to the app-level handler -> {"error": message}."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_cost_limiter = None
_login_limiter = None
_limiter_lock = threading.Lock()


def get_cost_limiter():
    """Post-auth cost limiter, keyed by user_id (goal.md ADR-018)."""
    global _cost_limiter
    with _limiter_lock:
        if _cost_limiter is None:
            from aurora.rate_limit import SlidingWindowRateLimiter
            limit = int(os.getenv("AUTH_RATE_LIMIT_PER_HOUR", "20") or 20)
            _cost_limiter = SlidingWindowRateLimiter(limit=limit, window_seconds=3600)
        return _cost_limiter


def get_login_limiter():
    """Pre-auth brute-force limiter, keyed by (client_ip, email) — the cost
    limiter above gives zero protection to an unauthenticated attacker."""
    global _login_limiter
    with _limiter_lock:
        if _login_limiter is None:
            from aurora.rate_limit import SlidingWindowRateLimiter
            limit = int(os.getenv("AUTH_LOGIN_RATE_LIMIT", "5") or 5)
            _login_limiter = SlidingWindowRateLimiter(limit=limit, window_seconds=900)
        return _login_limiter


def _reset_rate_limiters_for_tests() -> None:
    global _cost_limiter, _login_limiter
    with _limiter_lock:
        _cost_limiter = None
        _login_limiter = None


def client_ip(request: Request) -> str:
    # Fly's edge sets this; the socket peer address is the local-dev fallback.
    return request.headers.get("Fly-Client-IP") or (
        request.client.host if request.client else ""
    )


def resolve_user(request: Request) -> int | None:
    """Soft auth: cookie -> user_id, or None. Consumes no rate-limit budget
    (used by /auth/me and /auth/change-password, exactly as before)."""
    from aurora.server.cookies import SESSION_COOKIE

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    from aurora.storage.auth import get_auth_backend
    return get_auth_backend().resolve_session(token)


def require_user(request: Request) -> int:
    """Gate for /token, /agent, /voice-agent, /greeting, /reset — the
    cost-incurring / session-establishing routes (goal.md ADR-018, closing
    ADR-015's documented gap). Auth first, then the cost limiter."""
    user_id = resolve_user(request)
    if user_id is None:
        raise ApiError(401, "Authentication required")
    if not get_cost_limiter().allow(user_id):
        raise ApiError(429, "Rate limit exceeded. Try again later.")
    return user_id


async def raw_body(request: Request) -> bytes:
    """Raw request body for sync endpoints (no Pydantic request models by
    design — see app.py's contract note)."""
    return await request.body()
