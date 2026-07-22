"""sync_prompt.py  -  push SYSTEM_PROMPT to Opik's Prompt Library (goal.md
ADR-011/019/020, CI automation 2026-07-22).

Runs automatically after every deploy (.github/workflows/ci.yml's
post-deploy-eval job) so Opik's Prompt Library can never silently miss a
prompt edit made in code. `client.create_prompt()` is idempotent on
identical text — verified live: calling it twice with the same string
returns the SAME version, no duplicate; only text that actually differs
from the latest version creates a new one. Safe to run on every deploy
regardless of whether the prompt changed.

Deliberately does NOT promote the new version to the 'production'
environment — that stays the separate, deliberate step (promote_prompt.py),
run manually after reviewing the live-eval results this same CI job produces.
Auto-promoting here would bypass the eval-gated-promotion discipline
ADR-011/019 established.

Usage:
    python -m aurora.ops.sync_prompt
"""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

_PROMPT_NAME = os.getenv("OPIK_PROMPT_NAME", "aurora-system-prompt")


def main() -> None:
    api_key = os.getenv("OPIK_API_KEY", "").strip()
    if not api_key:
        print("OPIK_API_KEY not set — skipping prompt sync.")
        return

    import opik

    from aurora.core.prompts import SYSTEM_PROMPT

    client = opik.Opik()
    prompt = client.create_prompt(name=_PROMPT_NAME, prompt=SYSTEM_PROMPT)
    print(f"{_PROMPT_NAME}: latest version is now {prompt.version} (commit {prompt.commit})")


if __name__ == "__main__":
    main()
