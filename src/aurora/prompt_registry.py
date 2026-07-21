"""prompt_registry.py  -  Opik-backed prompt registry (goal.md Phase 4.5, ADR-011/019).

`get_system_prompt()` is the seam agent.py calls instead of using its
`SYSTEM_PROMPT` constant directly. Takes the constant as `local_fallback`
(never imports it — avoids a circular import with agent.py, and keeps this
module independently testable).

Resolution order:
    1. OPIK_API_KEY unset -> (local_fallback, "local"). opik is never even
       imported in this path, same lazy-dependency style as bookings.py's
       psycopg import.
    2. OPIK_API_KEY set -> fetch the prompt tagged for the `production`
       environment (the promotion target `promote_prompt.py` writes to after
       the eval suite passes against a candidate version). If no version
       carries that tag yet, fall back to the latest version. If neither
       exists, or ANYTHING raises (network, auth, misconfiguration), fall
       back to local_fallback with a distinct "local-fallback" label — this
       function must never raise; a registry hiccup must never dead-end a
       call (goal.md 2.2).

Opik's own client caches get_prompt() results for OPIK_PROMPT_CACHE_TTL_SECONDS
(default 300s) at the module level inside the SDK, so no separate caching
layer is added here — constructing a fresh client per call still benefits
from it.
"""

from __future__ import annotations

import os

_PROMPT_NAME = os.getenv("OPIK_PROMPT_NAME", "aurora-system-prompt")
_PRODUCTION_ENVIRONMENT = "production"


def _opik_client():
    import opik
    return opik.Opik()


def get_system_prompt(local_fallback: str) -> tuple[str, str]:
    api_key = os.getenv("OPIK_API_KEY", "").strip()
    if not api_key:
        return local_fallback, "local"

    version_override = os.getenv("OPIK_PROMPT_VERSION_OVERRIDE", "").strip()

    try:
        client = _opik_client()
        if version_override:
            # promote_prompt.py's eval-gate run: pin an exact candidate that
            # is (by definition) not yet tagged for any environment — never
            # fall through to "latest" here, a missing pin is a real error.
            prompt = client.get_prompt(name=_PROMPT_NAME, version=version_override)
        else:
            prompt = client.get_prompt(name=_PROMPT_NAME, environment=_PRODUCTION_ENVIRONMENT)
            if prompt is None:
                prompt = client.get_prompt(name=_PROMPT_NAME)
        if prompt is None:
            return local_fallback, "local-fallback"
        return prompt.prompt, f"opik:{prompt.version}"
    except Exception:
        return local_fallback, "local-fallback"
