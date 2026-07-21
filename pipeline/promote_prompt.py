"""promote_prompt.py  -  the eval-gated promotion mechanism ADR-011 requires
(goal.md Phase 4.5, ADR-019).

"Promotion" means tagging one Opik prompt version for the `production`
environment — the only version get_system_prompt() will use at runtime
(pipeline/prompt_registry.py). A version is never hand-tagged; it only
becomes `production` after the full offline eval suite passes against it,
exactly like every other agent-behavior change in this project (the `edd`
skill's hard rule, applied here to prompt edits made through Opik's UI
instead of a code diff).

Usage (needs OPIK_API_KEY / OPIK_WORKSPACE / OPIK_PROJECT_NAME configured):
    python promote_prompt.py --version v5
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

_PROMPT_NAME = os.getenv("OPIK_PROMPT_NAME", "aurora-system-prompt")
_EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"


def run_eval_gate(version: str) -> bool:
    """Run the full offline eval suite with `version` pinned as the system
    prompt, on the mock provider (same gate every agent-behavior change goes
    through). Returns True iff every scenario passes."""
    env = dict(os.environ)
    env["PROVIDER"] = "mock"
    env["OPIK_PROMPT_VERSION_OVERRIDE"] = version
    result = subprocess.run(
        [sys.executable, "run_evals.py", "--suite", "all", "--verbose"],
        cwd=_EVALS_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


def promote(version: str) -> None:
    import opik

    client = opik.Opik()
    client.set_prompt_environments(_PROMPT_NAME, ["production"], version=version)
    print(f"Promoted {_PROMPT_NAME} {version} to the production environment.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval-gated prompt promotion (goal.md 4.5)")
    parser.add_argument("--version", required=True, help='Opik version to promote, e.g. "v5"')
    args = parser.parse_args()

    print(f"Running the offline eval suite against candidate {args.version}...")
    if not run_eval_gate(args.version):
        print(f"\nGates FAILED for {args.version} — not promoted.", file=sys.stderr)
        return 1

    print(f"\nGates PASSED for {args.version}.")
    promote(args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
