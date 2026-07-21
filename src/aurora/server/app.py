"""Serve a tiny browser client for testing local LiveKit audio.

Run this after `./start_local_server.sh`, then open http://localhost:5173.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import warnings
from io import BytesIO
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt

from aurora.config.env import load_env_files
from aurora.core.spoken_text import normalize_spoken_text

HOST = os.getenv("TALK_HOST", "localhost")
PORT = int(os.getenv("TALK_PORT", "5173"))
# Static web client ships inside the package (goal.md ADR-020).
WEB_ROOT = Path(__file__).resolve().parent

_session_registry_lock = threading.Lock()
_agent_sessions: dict[tuple[int, str], object] = {}
_session_locks: dict[tuple[int, str], threading.Lock] = {}

GREETING = "Thanks for calling Aurora Hotel reservations. How can I help?"

# --- auth (goal.md ADR-018): closes ADR-015's documented /token & /agent gap ---

_SESSION_COOKIE = "aurora_session"
_cost_limiter = None
_login_limiter = None
_limiter_lock = threading.Lock()


def _cookie_secure() -> bool:
    return os.getenv("AUTH_COOKIE_SECURE", "true").strip().lower() != "false"


def _session_ttl_seconds() -> float:
    hours = float(os.getenv("AUTH_SESSION_TTL_HOURS", "24") or 24)
    return hours * 3600


def _session_cookie_header(token: str) -> str:
    flags = f"Path=/; HttpOnly; SameSite=Strict; Max-Age={int(_session_ttl_seconds())}"
    if _cookie_secure():
        flags += "; Secure"
    return f"{_SESSION_COOKIE}={token}; {flags}"


def _clear_session_cookie_header() -> str:
    flags = "Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    if _cookie_secure():
        flags += "; Secure"
    return f"{_SESSION_COOKIE}=; {flags}"


def _client_ip(handler) -> str:
    # Fly's edge sets this; a raw socket peer address is the local-dev fallback.
    return handler.headers.get("Fly-Client-IP") or handler.client_address[0]


def _request_cookie(handler, name: str) -> str | None:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    from http.cookies import SimpleCookie
    jar = SimpleCookie()
    jar.load(raw)
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _get_cost_limiter():
    """Post-auth cost limiter, keyed by user_id (goal.md ADR-018)."""
    global _cost_limiter
    with _limiter_lock:
        if _cost_limiter is None:
            from aurora.rate_limit import SlidingWindowRateLimiter
            limit = int(os.getenv("AUTH_RATE_LIMIT_PER_HOUR", "20") or 20)
            _cost_limiter = SlidingWindowRateLimiter(limit=limit, window_seconds=3600)
        return _cost_limiter


def _get_login_limiter():
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


def _load_env_files() -> None:
    load_env_files((Path.cwd() / ".env",))


def _agent_provider_name() -> str:
    return os.getenv("PROVIDER", "mock").lower()


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


def _new_agent():
    from aurora.core.agent import Agent
    from aurora.core.providers import make_provider

    return Agent(make_provider(_agent_provider_name()))


def _get_session(key: tuple[int, str]):
    with _session_registry_lock:
        if key not in _agent_sessions:
            _agent_sessions[key] = _new_agent()
            _session_locks[key] = threading.Lock()
        return _agent_sessions[key], _session_locks[key]


def _reset_session(key: tuple[int, str]) -> None:
    with _session_registry_lock:
        _agent_sessions.pop(key, None)
        _session_locks.pop(key, None)


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
        "provider": getattr(agent.provider, "name", _agent_provider_name()),
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
    agent, lock = _get_session(key)
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
    agent, lock = _get_session(key)
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
    agent, lock = _get_session(key)
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


def _token(identity: str, name: str, room: str) -> str:
    if _livekit_api_secret() == "secret":
        warnings.filterwarnings("ignore", category=jwt.InsecureKeyLengthWarning)
    from aurora.server.token_utils import mint_token
    return mint_token(
        api_key=_livekit_api_key(), api_secret=_livekit_api_secret(),
        identity=identity, name=name, room=room,
    )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/web/index.html"
            return super().do_GET()
        if parsed.path == "/state":
            return self._send_json({
                "livekitRoom": _livekit_room(),
                "livekitUrl": _livekit_url(),
                "agentProvider": _agent_provider_name(),
                "languages": _supported_languages(),
            })
        if parsed.path == "/auth/me":
            return self._handle_me()
        if parsed.path != "/token":
            return super().do_GET()

        user_id = self._require_auth()
        if user_id is None:
            return

        query = parse_qs(parsed.query)
        requested_identity = query.get("identity", ["caller-demo"])[0]
        name = query.get("name", [requested_identity])[0]
        room = query.get("room", [_livekit_room()])[0]
        # Identity is server-derived from the authenticated user (goal.md
        # ADR-018) — the caller-supplied label is kept as a display suffix
        # (this demo intentionally opens two role participants, "caller-demo"
        # and "aurora-agent", per browser session) but can never be forged to
        # collide with another authenticated user's identity.
        identity = f"{requested_identity}-u{user_id}"

        payload = {
            "url": _livekit_url(),
            "room": room,
            "identity": identity,
            "token": _token(identity, name, room),
        }
        self._send_json(payload)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/auth/register":
            return self._handle_register()
        if parsed.path == "/auth/login":
            return self._handle_login()
        if parsed.path == "/auth/logout":
            return self._handle_logout()
        if parsed.path == "/auth/change-password":
            return self._handle_change_password()

        user_id = self._require_auth()
        if user_id is None:
            return
        session_id = self.headers.get("X-Session-ID", "browser-demo")
        key = (user_id, session_id)
        turn_id = self.headers.get("X-Turn-ID")
        if parsed.path == "/reset":
            _reset_session(key)
            return self._send_json({"reset": True, "sessionId": session_id})
        if parsed.path == "/greeting":
            try:
                return self._send_json(_greeting_reply(key))
            except Exception as exc:
                return self._send_json({"error": str(exc)}, status=500)
        if parsed.path == "/voice-agent":
            return self._handle_voice_agent(key, turn_id)
        if parsed.path != "/agent":
            self.send_error(404, "File not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            payload = json.loads(body or b"{}")
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("Missing text")
            response = _agent_reply(text, key, turn_id)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json(response)

    def _handle_voice_agent(self, key: tuple[int, str], turn_id: str | None) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            audio = self.rfile.read(length)
            if not audio:
                raise ValueError("Missing audio")
            response = _voice_agent_reply(
                audio,
                self.headers.get("Content-Type", ""),
                key,
                turn_id,
                self.headers.get("X-Barge-In", "false").lower() == "true",
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json(response)

    def _send_json(self, payload: dict, status: int = 200, extra_headers=None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # --- auth (goal.md ADR-018) ---

    def _resolve_authenticated_user(self) -> int | None:
        token = _request_cookie(self, _SESSION_COOKIE)
        if not token:
            return None
        from aurora.storage.auth import get_auth_backend
        return get_auth_backend().resolve_session(token)

    def _require_auth(self) -> int | None:
        """Gate for /token, /agent, /voice-agent, /greeting, /reset — the
        cost-incurring / session-establishing routes (goal.md ADR-018, closing
        ADR-015's documented gap). Writes the error response itself."""
        user_id = self._resolve_authenticated_user()
        if user_id is None:
            self._send_json({"error": "Authentication required"}, status=401)
            return None
        if not _get_cost_limiter().allow(user_id):
            self._send_json({"error": "Rate limit exceeded. Try again later."}, status=429)
            return None
        return user_id

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        return json.loads(body or b"{}")

    def _handle_register(self) -> None:
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            return self._send_json({"error": "Invalid request body"}, status=400)
        email = str(payload.get("email", ""))
        password = str(payload.get("password", ""))

        if not _get_login_limiter().allow((_client_ip(self), email.strip().lower())):
            return self._send_json({"error": "Too many attempts. Try again later."}, status=429)

        from aurora.storage.auth import AuthValidationError, get_auth_backend
        backend = get_auth_backend()
        try:
            user_id = backend.register_user(email, password)
        except AuthValidationError as exc:
            return self._send_json({"error": str(exc)}, status=400)

        token = backend.create_session(user_id, ttl_seconds=_session_ttl_seconds())
        self._send_json(
            {"ok": True, "email": email.strip().lower()},
            extra_headers=[("Set-Cookie", _session_cookie_header(token))],
        )

    def _handle_login(self) -> None:
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            return self._send_json({"error": "Invalid request body"}, status=400)
        email = str(payload.get("email", ""))
        password = str(payload.get("password", ""))

        if not _get_login_limiter().allow((_client_ip(self), email.strip().lower())):
            return self._send_json({"error": "Too many attempts. Try again later."}, status=429)

        from aurora.storage.auth import get_auth_backend
        backend = get_auth_backend()
        user_id = backend.verify_credentials(email, password)
        if user_id is None:
            return self._send_json({"error": "Invalid email or password"}, status=401)

        token = backend.create_session(user_id, ttl_seconds=_session_ttl_seconds())
        self._send_json(
            {"ok": True, "email": email.strip().lower()},
            extra_headers=[("Set-Cookie", _session_cookie_header(token))],
        )

    def _handle_logout(self) -> None:
        token = _request_cookie(self, _SESSION_COOKIE)
        if token:
            from aurora.storage.auth import get_auth_backend
            get_auth_backend().revoke_session(token)
        self._send_json(
            {"ok": True}, extra_headers=[("Set-Cookie", _clear_session_cookie_header())],
        )

    def _handle_me(self) -> None:
        user_id = self._resolve_authenticated_user()
        if user_id is None:
            return self._send_json({"error": "Not authenticated"}, status=401)
        from aurora.storage.auth import get_auth_backend
        backend = get_auth_backend()
        email = next((u["email"] for u in backend.list_users() if u["id"] == user_id), None)
        self._send_json({"email": email})

    def _handle_change_password(self) -> None:
        user_id = self._resolve_authenticated_user()
        if user_id is None:
            return self._send_json({"error": "Authentication required"}, status=401)
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            return self._send_json({"error": "Invalid request body"}, status=400)
        current = str(payload.get("currentPassword", ""))
        new = str(payload.get("newPassword", ""))

        from aurora.storage.auth import AuthValidationError, get_auth_backend
        backend = get_auth_backend()
        try:
            changed = backend.change_password(user_id, current, new)
        except AuthValidationError as exc:
            return self._send_json({"error": str(exc)}, status=400)
        if not changed:
            return self._send_json({"error": "Current password is incorrect"}, status=400)
        self._send_json({"ok": True})


def main() -> None:
    _load_env_files()
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Open http://{HOST}:{PORT}")
    print(f"LiveKit URL: {_livekit_url()}")
    print(f"Room: {_livekit_room()}")
    print(f"Agent provider: {_agent_provider_name()}")
    print(f"TTS backend: {os.getenv('TTS_BACKEND', 'provider').lower()}")
    print("Use the two panes for LiveKit audio. Use the conversation panel for the hotel agent.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
