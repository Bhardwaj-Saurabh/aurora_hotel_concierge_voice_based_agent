"""Run the same core.json/red_team.json scenarios against the REAL configured
LLM (goal.md, live debugging pass, 2026-07-22) and log the results to Opik as
a proper Experiment — not the offline mock gate (evals/run_evals.py), which
proves grading logic and MockProvider parity but cannot tell you whether the
actual model reliably calls create_booking / search_hotel_knowledge / etc.
That gap is exactly what surfaced live: users found booking/RAG tools
sometimes silently unused, and an off-topic guardrail misfiring on in-scope
requests — both intermittent, temperature-driven failures the deterministic
mock substrate structurally cannot see (ADR-004's own documented risk).

Reuses run_evals.py's scenario loading and grading (`_check`, `run_case`,
`load_cases`) unchanged — same criteria, same JSON files, so a live failure
here is directly comparable to the offline gate, not a parallel definition of
correctness. `run_case()` takes an optional `provider_name`, defaulting to
"mock" for the gate; this script passes the real one.

Booking safety (goal.md ADR-021: Postgres-only, no local fallback): a
disposable, uniquely-named table is injected via
set_booking_backend_for_tests, so a scenario that completes create_booking
can never write a row into the production bookings table (same carefulness
as load_test.py's read-only-question choice, goal.md Phase 4.3).

Usage:
    python evals/run_live_evals.py --suite all --trials 3
    python evals/run_live_evals.py --suite red-team --trials 5 --local-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

# Captured before importing run_evals: that module unconditionally sets
# PROVIDER=mock at import time (it's the offline gate's own substrate,
# ADR-004) — importing it here would silently clobber the real provider
# this script exists to exercise.
_REQUESTED_PROVIDER = os.getenv("PROVIDER", "").strip().lower()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_evals import load_cases, run_case  # noqa: E402

# run_evals's own import already injected a disposable "bookings_gate_test"
# backend as a side effect — re-inject with a distinct table name so a live
# run never shares state with an offline gate run that happens concurrently.
from aurora.storage.bookings import (  # noqa: E402
    new_disposable_backend_for_offline_gates,
    set_booking_backend_for_tests,
)
set_booking_backend_for_tests(
    new_disposable_backend_for_offline_gates("bookings_live_eval_test")
)

_DATASET_NAME = "aurora-live-eval-scenarios"
_DEFAULT_PROJECT = os.getenv("OPIK_EVAL_PROJECT_NAME", "aurora-hotel-evals")


def _task(item: dict) -> dict:
    case = json.loads(item["case_json"])
    ok, failures = run_case(case, verbose=False, provider_name=item["provider_name"])
    return {"passed": ok, "failure_detail": "; ".join(failures) if failures else ""}


def _build_dataset(client, cases: list[dict], provider_name: str, project_name: str):
    # A dataset's own project (not evaluate()'s deprecated project_name param)
    # decides where its experiment traces land — keep eval traffic out of the
    # "aurora-hotel" project real callers' traces live in.
    dataset = client.get_or_create_dataset(
        name=_DATASET_NAME,
        description=(
            "Aurora core.json/red_team.json scenarios (same JSON, same grading "
            "as the offline mock gate, evals/run_evals.py) run against the real "
            "configured provider instead of MockProvider. goal.md live "
            "debugging pass, 2026-07-22."
        ),
        project_name=project_name,
    )
    items = [
        {
            "case_id": case["id"],
            "description": case["description"],
            "case_json": json.dumps(case),
            "provider_name": provider_name,
        }
        for case in cases
    ]
    dataset.insert(items)  # Opik de-dupes identical items across runs
    return dataset


def _scenario_passed_metric():
    from opik.evaluation.metrics import base_metric, score_result

    class ScenarioPassed(base_metric.BaseMetric):
        def __init__(self):
            super().__init__(name="scenario_passed")

        def score(self, passed: bool = False, failure_detail: str = "", **_ignored):
            return score_result.ScoreResult(
                name=self.name,
                value=1.0 if passed else 0.0,
                reason=failure_detail or "all assertions passed",
            )

    return ScenarioPassed()


def _run_local(cases: list[dict], trials: int, provider_name: str) -> bool:
    total_trials = 0
    total_passed = 0
    for case in cases:
        outcomes = []
        for _ in range(trials):
            ok, failures = run_case(case, verbose=False, provider_name=provider_name)
            outcomes.append(ok)
            if not ok:
                for failure in failures:
                    print(f"      {failure}")
        passed = sum(outcomes)
        total_trials += trials
        total_passed += passed
        status = "PASS" if passed == trials else ("FLAKY" if passed else "FAIL")
        print(f"{status:6} {case['id']}: {passed}/{trials} trials — {case['description']}")
    print(
        f"\nOverall: {total_passed}/{total_trials} trials passed across "
        f"{len(cases)} scenarios (provider={provider_name})."
    )
    return total_passed == total_trials


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate Aurora against the real configured LLM and log to Opik"
    )
    parser.add_argument("--suite", choices=("core", "red-team", "all"), default="all")
    parser.add_argument("--trials", type=int, default=3, help="Repeats per scenario (sampling is non-deterministic)")
    parser.add_argument("--local-only", action="store_true", help="Print a report; skip Opik logging entirely")
    parser.add_argument("--project", default=_DEFAULT_PROJECT)
    args = parser.parse_args()

    provider_name = _REQUESTED_PROVIDER
    if provider_name in ("", "mock"):
        raise SystemExit(
            "PROVIDER must be a live backend (openai or groq) for a live evaluation — "
            "set PROVIDER in .env. The deterministic mock gate is evals/run_evals.py."
        )

    cases = load_cases(args.suite)

    if args.local_only or not os.getenv("OPIK_API_KEY", "").strip():
        if not args.local_only:
            print(
                "OPIK_API_KEY is not set — printing a local report only "
                "(pass --local-only to silence this notice)."
            )
        all_passed = _run_local(cases, args.trials, provider_name)
        raise SystemExit(0 if all_passed else 1)

    import opik

    client = opik.Opik()
    dataset = _build_dataset(client, cases, provider_name, args.project)
    result = opik.evaluate(
        dataset=dataset,
        task=_task,
        scoring_metrics=[_scenario_passed_metric()],
        trial_count=args.trials,
        experiment_name_prefix=f"aurora-live-eval-{args.suite}",
        experiment_config={
            "provider": provider_name,
            "llm_model": os.getenv("LLM_MODEL", ""),
            "llm_temperature": os.getenv("LLM_TEMPERATURE", ""),
            "suite": args.suite,
            "trial_count": args.trials,
        },
        verbose=1,
    )
    print(result)


if __name__ == "__main__":
    main()
