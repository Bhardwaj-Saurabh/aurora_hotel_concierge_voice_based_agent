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

    bookings = _value(env, "BOOKINGS_DB")
    if bookings and bookings != ":memory:":
        try:
            Path(bookings).expanduser().parent.mkdir(parents=True, exist_ok=True)
            sqlite3.connect(Path(bookings).expanduser().as_posix()).close()
        except (OSError, sqlite3.OperationalError) as exc:
            problems.append(f"BOOKINGS_DB={bookings!r} cannot be opened: {exc}")

    return problems


def require_valid_config(env: Mapping[str, str] | None = None) -> None:
    """Print problems and exit non-zero; call before serving the first turn."""
    problems = validate_config(env)
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
    problems = validate_config()
    if problems:
        print("Configuration problems (fix pipeline/.env):")
        for problem in problems:
            print(f"  - {problem}")
        raise SystemExit(2)
    print("Configuration OK.")


if __name__ == "__main__":
    main()
