"""Create a LiveKit room join token for a caller or agent participant.

Optional:
    LIVEKIT_API_KEY
    LIVEKIT_API_SECRET
    LIVEKIT_ROOM

Example:
    python create_token.py --identity caller-demo --name "Caller Demo"
    python create_token.py --identity aurora-agent --name "Aurora Agent"
"""

from __future__ import annotations

import argparse
import os
import warnings
from pathlib import Path

import jwt

from env_loader import load_env_files
from token_utils import mint_token

LOCAL_DEFAULTS = {
    "LIVEKIT_API_KEY": "devkey",
    "LIVEKIT_API_SECRET": "secret",
    "LIVEKIT_ROOM": "aurora-demo-room",
}


def _load_env_files() -> None:
    root = Path(__file__).resolve().parents[1]
    load_env_files((root / "pipeline" / ".env", root / "livekit" / ".env"))


def _setting(name: str) -> str:
    return os.getenv(name, LOCAL_DEFAULTS[name])


def main() -> None:
    _load_env_files()
    parser = argparse.ArgumentParser(description="Create a LiveKit room join token")
    parser.add_argument("--identity", required=True, help="Unique participant identity")
    parser.add_argument("--name", default=None, help="Human-readable participant name")
    parser.add_argument("--room", default=_setting("LIVEKIT_ROOM"))
    args = parser.parse_args()

    if _setting("LIVEKIT_API_SECRET") == LOCAL_DEFAULTS["LIVEKIT_API_SECRET"]:
        warnings.filterwarnings("ignore", category=jwt.InsecureKeyLengthWarning)

    token = mint_token(
        api_key=_setting("LIVEKIT_API_KEY"), api_secret=_setting("LIVEKIT_API_SECRET"),
        identity=args.identity, name=args.name or args.identity, room=args.room,
    )

    print(token)


if __name__ == "__main__":
    main()
