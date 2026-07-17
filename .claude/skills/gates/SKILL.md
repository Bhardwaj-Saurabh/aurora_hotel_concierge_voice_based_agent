---
name: gates
description: Run all of Aurora's offline verification gates (smoke test, pipeline unit tests, core + red-team eval suites, livekit unit tests) and report per-gate pass/fail with real output. Use before every commit, after any change, and whenever asked "does it still work".
---

# Gates — Offline Verification Suite

Aurora's standing rule (goal.md Principle 6): these gates are green at every commit. They run
fully offline on the mock provider — no key, no network, < 10 seconds total. There is no excuse
to skip them.

## Run

From the assignment root, using the root `.venv`:

```bash
source .venv/bin/activate
(cd pipeline && python smoke_test.py)
(cd pipeline && python -m unittest -v test_features.py)
(cd evals && python run_evals.py --suite all)
(cd livekit && python -m unittest -v test_talk_server.py test_env_loader.py)
```

Run all four even if an early one fails — the full failure picture beats a truncated one.

## Report

Present a table:

| Gate | Result |
|---|---|
| smoke_test | PASS / FAIL |
| pipeline unittest | PASS / FAIL |
| evals (core + red-team) | PASS / FAIL (score X/N) |
| livekit unittest | PASS / FAIL |

## Rules

- **On any failure: show the actual failing output** (the failing case IDs / assertion messages),
  state plainly that gates are red, and do not claim the work is done. Never summarize a failure
  away.
- Never "fix" a red gate by weakening a test or eval — that is a product decision for the user
  (see the edd skill's hard rules).
- A `pkg_resources is deprecated` warning from webrtcvad is cosmetic — ignore it; it is not a
  failure.
- If `.venv` is missing, recreate it: `uv venv --python 3.12 && uv pip install -r
  pipeline/requirements.txt -r livekit/requirements.txt`.
