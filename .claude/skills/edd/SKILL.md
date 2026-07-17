---
name: edd
description: Evaluation-Driven Development loop for Aurora. MANDATORY for any change to agent behavior — system prompt, tools, guardrails, routing, language handling, retrieval, or MockProvider — in pipeline/agent.py, pipeline/providers.py, pipeline/router.py, pipeline/knowledge.py, or knowledge/*.md. Write the eval FIRST, prove it fails, enforce mock parity, then implement and run gates.
---

# EDD — Evaluation-Driven Development

You are working on Aurora (goal.md, Principles 1–2). A behavior change without a pre-written
acceptance criterion is a regression waiting for a customer. Follow this loop **in order** —
do not reorder, do not skip.

## Step 0 — Classify the change

- **Behavior change** (what the agent says/does: prompt wording, tool schemas, `run_tool`,
  guardrails, routing phrases, language handling, retrieval, MockProvider rules) → full loop below.
- **Pure refactor** (no observable behavior change) → skip to Step 5; the gates *are* the proof
  nothing changed.
- Unsure → treat as behavior change.

## Step 1 — Write the eval FIRST (red)

Add or extend a case in `evals/core.json` (task behavior) or `evals/red_team.json`
(injection/fabrication/privacy/guardrail attacks) **before touching any implementation**.

Case schema (consumed by `evals/run_evals.py`):

```json
{
  "id": "area.short_name",
  "description": "One sentence: the behavior this pins.",
  "turns": [
    {
      "user": "caller utterance",
      "expect": {
        "contains": "substring required in reply (case-insensitive)",
        "forbid": ["substrings that must NOT appear in reply"],
        "tools": ["exact ordered list of tool.requested names; [] asserts NO tools"],
        "action": "transfer | hangup",
        "language": "en | es",
        "sourceContains": "substring required in a grounding source, e.g. hotel_policies.md#Pets"
      }
    }
  ]
}
```

All `expect` keys are optional — assert only what defines the behavior. Multi-turn cases share
one Agent/session (see `router.language_switch` in core.json as the model).

**Prove it fails:** run `python run_evals.py --suite <core|red-team> --verbose` from `evals/`
and confirm the new case FAILS for the right reason. An eval that passes before the change is
written proves nothing. Report the red output.

## Step 2 — Mock parity (same commit, always)

The eval suite runs on `MockProvider` (`pipeline/providers.py`). Any behavior you want from the
live model must exist as a mock rule too, or the suite tests fiction:

- New tool → branch in `MockProvider.chat` + result handling for the `role == "tool"` path.
- New guardrail → extend `_mock_off_topic` / relevant matcher with the same vocabulary (EN + ES,
  and FR once Phase 1.4 lands).
- New language / routing phrase → mirror in mock keyword branches AND live lists
  (`_KNOWLEDGE_INTENT_PHRASES`, `_LANGUAGE_NAMES`).

## Step 3 — Implement the live path

Now change `SYSTEM_PROMPT` / `TOOLS` / `run_tool` / router / knowledge as needed. Keep replies
short and spoken-friendly (no markdown, no bullets — they get read aloud).

## Step 4 — Green

Re-run the failed suite with `--verbose`. The new case must PASS and every pre-existing case must
still pass.

## Step 5 — Full gates

Invoke the `gates` skill (or run all four suites). Do not report the task complete until all
gates are green. Report actual output, not a summary of hope.

## Hard rules

- **Never weaken, delete, or loosen an existing eval to make it pass.** If an eval seems wrong,
  stop and ask the user — changing an acceptance criterion is a product decision, not a fix.
- **Never write the eval after the implementation** and present it as EDD. If you realize the
  eval was skipped, say so and backfill it with a deliberately broken implementation check
  (revert, confirm red, restore).
- One behavior → one focused case. Don't cram unrelated assertions into a giant case.
- If the change maps to a goal.md phase item, say which one (roadmap-guard skill covers this).
