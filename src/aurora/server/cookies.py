"""Session-cookie header construction (goal.md ADR-018/ADR-020).

The Set-Cookie strings are built by hand — not via a framework helper — so the
exact flags (HttpOnly; SameSite=Strict; Secure toggled by AUTH_COOKIE_SECURE)
are pinned here and asserted by tests, immune to framework-default drift.
"""

from __future__ import annotations

import os

SESSION_COOKIE = "aurora_session"


def cookie_secure() -> bool:
    return os.getenv("AUTH_COOKIE_SECURE", "true").strip().lower() != "false"


def session_ttl_seconds() -> float:
    hours = float(os.getenv("AUTH_SESSION_TTL_HOURS", "24") or 24)
    return hours * 3600


def session_cookie_header(token: str) -> str:
    flags = f"Path=/; HttpOnly; SameSite=Strict; Max-Age={int(session_ttl_seconds())}"
    if cookie_secure():
        flags += "; Secure"
    return f"{SESSION_COOKIE}={token}; {flags}"


def clear_session_cookie_header() -> str:
    flags = "Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    if cookie_secure():
        flags += "; Secure"
    return f"{SESSION_COOKIE}=; {flags}"
