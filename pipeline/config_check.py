"""
config_check.py  -  fail-fast startup validation (goal.md 2.3).

A configuration mistake should be one clear message at startup, not a stack
trace in the middle of a caller's sentence. `validate_config()` is pure (takes
the env, returns problems) so it is trivially testable; entry points call it
before serving the first turn, and it runs standalone:

    python config_check.py
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Mapping

VALID_PROVIDERS = ("mock", "groq", "openai")
VALID_TTS_BACKENDS = ("provider", "system", "print")
_KEY_ENVS = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}
_NUMERIC_VARS = (
    ("ENDPOINT_SILENCE_MS", int),
    ("VAD_AGGRESSIVENESS", int),
    ("SAMPLE_RATE", int),
    ("PROVIDER_TIMEOUT_S", float),
    ("PROVIDER_MAX_RETRIES", int),
    ("LATENCY_FILLER_MS", int),
    ("LIVEKIT_TOKEN_TTL_MINUTES", int),
    ("AUTH_SESSION_TTL_HOURS", float),
    ("AUTH_RATE_LIMIT_PER_HOUR", int),
    ("AUTH_LOGIN_RATE_LIMIT", int),
)


def _value(env: Mapping[str, str], name: str) -> str:
    """Read like providers._env_or_default: blank or comment-only means unset."""
    raw = (env.get(name) or "").strip()
    return "" if raw.startswith("#") else raw


def validate_config(env: Mapping[str, str] | None = None) -> list[str]:
    """Return a list of human-readable configuration problems (empty = good)."""
    env = os.environ if env is None else env
    problems: list[str] = []

    provider = _value(env, "PROVIDER").lower() or "groq"
    if provider not in VALID_PROVIDERS:
        problems.append(
            f"PROVIDER={provider!r} is not one of {', '.join(VALID_PROVIDERS)}."
        )
    else:
        key_env = _KEY_ENVS.get(provider)
        if key_env and not _value(env, key_env):
            problems.append(
                f"PROVIDER={provider} requires {key_env} in pipeline/.env "
                "(or use PROVIDER=mock for the offline path)."
            )

    tts_backend = _value(env, "TTS_BACKEND").lower()
    if tts_backend and tts_backend not in VALID_TTS_BACKENDS:
        problems.append(
            f"TTS_BACKEND={tts_backend!r} is not one of {', '.join(VALID_TTS_BACKENDS)}."
        )

    for name, caster in _NUMERIC_VARS:
        raw = _value(env, name)
        if raw:
            try:
                caster(raw)
            except ValueError:
                problems.append(f"{name}={raw!r} is not a valid number.")

    vad = _value(env, "VAD_AGGRESSIVENESS")
    if vad.isdigit() and not 0 <= int(vad) <= 3:
        problems.append(f"VAD_AGGRESSIVENESS={vad} must be between 0 and 3.")

    telemetry = _value(env, "TELEMETRY_JSONL")
    if telemetry:
        path = Path(telemetry).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            problems.append(f"TELEMETRY_JSONL={telemetry!r} is not writable: {exc}")

    otlp = _value(env, "TELEMETRY_OTLP_ENDPOINT")
    if otlp:
        try:
            import opentelemetry.exporter.otlp.proto.http.trace_exporter  # noqa: F401
            import opentelemetry.sdk.trace  # noqa: F401
        except ImportError:
            problems.append(
                "TELEMETRY_OTLP_ENDPOINT is set but the exporter is missing: "
                "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http"
            )

    otlp_headers = _value(env, "TELEMETRY_OTLP_HEADERS")
    if otlp_headers:
        bad_entries = [entry for entry in otlp_headers.split(",") if "=" not in entry]
        if bad_entries:
            problems.append(
                f"TELEMETRY_OTLP_HEADERS has a malformed entry (no '='): {bad_entries[0]!r}. "
                "Expected \"Key1=Value1,Key2=Value2\" (goal.md ADR-019)."
            )

    if _value(env, "OPIK_API_KEY"):
        try:
            import opik  # noqa: F401
        except ImportError:
            problems.append(
                "OPIK_API_KEY is set but the opik package is not installed: pip install opik "
                "(goal.md ADR-019) — otherwise the prompt registry silently falls back to the "
                "local prompt on every call."
            )

    pin = _value(env, "KNOWLEDGE_SNAPSHOT")
    if pin:
        from knowledge import KNOWLEDGE_ROOT, _snapshot_dirs
        if not any(d.name == pin for d in _snapshot_dirs(KNOWLEDGE_ROOT)):
            available = ", ".join(d.name for d in _snapshot_dirs(KNOWLEDGE_ROOT)) or "none"
            problems.append(
                f"KNOWLEDGE_SNAPSHOT={pin!r} does not exist (available: {available})."
            )

    bookings = _value(env, "BOOKINGS_DB")
    if bookings and bookings != ":memory:":
        try:
            Path(bookings).expanduser().parent.mkdir(parents=True, exist_ok=True)
            sqlite3.connect(Path(bookings).expanduser().as_posix()).close()
        except (OSError, sqlite3.OperationalError) as exc:
            problems.append(f"BOOKINGS_DB={bookings!r} cannot be opened: {exc}")

    postgres_host = _value(env, "POSTGRES_HOST")
    if postgres_host:
        for required in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            if not _value(env, required):
                problems.append(
                    f"POSTGRES_HOST is set but {required} is missing (goal.md ADR-013)."
                )
        port = _value(env, "POSTGRES_PORT")
        if port:
            try:
                int(port)
            except ValueError:
                problems.append(f"POSTGRES_PORT={port!r} is not a valid number.")
        try:
            import psycopg  # noqa: F401
        except ImportError:
            problems.append(
                "POSTGRES_HOST is set but psycopg is missing: "
                "pip install \"psycopg[binary]>=3.1\""
            )

    return problems


def check_env_file_permissions(path: Path) -> list[str]:
    """Flag a .env file readable/writable by anyone but its owner (goal.md
    ADR-016). A real secret manager injects env vars directly and never
    touches this check; this only protects the local-dev/.env file path."""
    if not path.exists():
        return []
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return [
            f"{path} is readable by group/others (mode {oct(mode)[2:]}); "
            f"it holds real secrets. Run: chmod 600 {path}"
        ]
    return []


def require_valid_config(env: Mapping[str, str] | None = None) -> None:
    """Print problems and exit non-zero; call before serving the first turn."""
    problems = validate_config(env)
    problems += check_env_file_permissions(Path(__file__).resolve().parent / ".env")
    if not problems:
        return
    print("Configuration problems (fix pipeline/.env):")
    for problem in problems:
        print(f"  - {problem}")
    raise SystemExit(2)


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ModuleNotFoundError:
        pass
    require_valid_config()  # includes the .env file-permission check
    print("Configuration OK.")


if __name__ == "__main__":
    main()
