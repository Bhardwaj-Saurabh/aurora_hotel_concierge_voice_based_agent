"""Auth routes (goal.md ADR-018): register/login/logout/change-password/me.

Bodies are parsed by hand from raw bytes — bad JSON must return
400 {"error": "Invalid request body"}, never FastAPI's 422 machinery
(web/auth.js reads `payload.error`). The login limiter runs before any
backend call, keyed (client_ip, email), exactly as before the port.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from aurora.server.cookies import (
    SESSION_COOKIE,
    clear_session_cookie_header,
    session_cookie_header,
    session_ttl_seconds,
)
from aurora.server.deps import (
    client_ip,
    get_login_limiter,
    raw_body,
    resolve_user,
)

router = APIRouter()


def _parse_credentials(body: bytes) -> tuple[dict, JSONResponse | None]:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return {}, JSONResponse({"error": "Invalid request body"}, status_code=400)
    return payload, None


@router.post("/auth/register")
def register(request: Request, body: bytes = Depends(raw_body)):
    payload, error = _parse_credentials(body)
    if error is not None:
        return error
    email = str(payload.get("email", ""))
    password = str(payload.get("password", ""))

    if not get_login_limiter().allow((client_ip(request), email.strip().lower())):
        return JSONResponse({"error": "Too many attempts. Try again later."}, status_code=429)

    from aurora.storage.auth import AuthValidationError, get_auth_backend
    backend = get_auth_backend()
    try:
        user_id = backend.register_user(email, password)
    except AuthValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    token = backend.create_session(user_id, ttl_seconds=session_ttl_seconds())
    return JSONResponse(
        {"ok": True, "email": email.strip().lower()},
        headers={"Set-Cookie": session_cookie_header(token)},
    )


@router.post("/auth/login")
def login(request: Request, body: bytes = Depends(raw_body)):
    payload, error = _parse_credentials(body)
    if error is not None:
        return error
    email = str(payload.get("email", ""))
    password = str(payload.get("password", ""))

    if not get_login_limiter().allow((client_ip(request), email.strip().lower())):
        return JSONResponse({"error": "Too many attempts. Try again later."}, status_code=429)

    from aurora.storage.auth import get_auth_backend
    backend = get_auth_backend()
    user_id = backend.verify_credentials(email, password)
    if user_id is None:
        return JSONResponse({"error": "Invalid email or password"}, status_code=401)

    token = backend.create_session(user_id, ttl_seconds=session_ttl_seconds())
    return JSONResponse(
        {"ok": True, "email": email.strip().lower()},
        headers={"Set-Cookie": session_cookie_header(token)},
    )


@router.post("/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        from aurora.storage.auth import get_auth_backend
        get_auth_backend().revoke_session(token)
    return JSONResponse(
        {"ok": True}, headers={"Set-Cookie": clear_session_cookie_header()},
    )


@router.get("/auth/me")
def me(request: Request):
    user_id = resolve_user(request)
    if user_id is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    from aurora.storage.auth import get_auth_backend
    backend = get_auth_backend()
    email = next((u["email"] for u in backend.list_users() if u["id"] == user_id), None)
    return JSONResponse({"email": email})


@router.post("/auth/change-password")
def change_password(request: Request, body: bytes = Depends(raw_body)):
    user_id = resolve_user(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    payload, error = _parse_credentials(body)
    if error is not None:
        return error
    current = str(payload.get("currentPassword", ""))
    new = str(payload.get("newPassword", ""))

    from aurora.storage.auth import AuthValidationError, get_auth_backend
    backend = get_auth_backend()
    try:
        changed = backend.change_password(user_id, current, new)
    except AuthValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not changed:
        return JSONResponse({"error": "Current password is incorrect"}, status_code=400)
    return JSONResponse({"ok": True})
