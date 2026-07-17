# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Aurora is a hotel-reservations **voice agent** built for an FDE workshop. It demonstrates a
progressive build of a production voice cascade:

```
caller audio -> VAD/endpointing -> STT -> AgentRouter -> LLM -> RAG & tools -> TTS
```

The entire agent/tool/RAG/routing/eval/scale path runs **fully offline** with `PROVIDER=mock`
(no network, key, or SDK). Live paths add OpenAI or Groq. There is no build step; everything is
Python 3 plus a small browser client.

**The mission:** grow this into a production-grade, deployable voice reservation service. The
approved roadmap lives in `goal.md` (4 phases, 11 ADRs, per-phase definitions of done) — it is
**local-only** (gitignored, not in the remote); if it's missing, ask the user for it before
starting roadmap work. Every session should advance a roadmap item, not wander.

## Commands

Two Python packages — `pipeline/` (the agent + voice loop) and `livekit/` (the browser room
demo) — share **one venv at the assignment root** (their dependency sets are disjoint except for
an identical `openai` pin). Create/recreate it with:

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r pipeline/requirements.txt -r livekit/requirements.txt
```

Run commands from the relevant subdirectory with the root `.venv` active. (README/RUNBOOK still
describe per-package `python3 -m venv` setups — that also works, but the root venv is canonical.)

**Offline verification (no key required) — run these after any change to the agent, tools, router, RAG, or telemetry:**
```bash
cd pipeline
python3 smoke_test.py                          # scripted end-to-end through the real Agent
python3 -m unittest -v test_features.py        # routing, grounding, telemetry, capacity units

cd ../evals
python3 run_evals.py --suite all               # deterministic task + red-team scenarios (mock provider)
python3 run_evals.py --suite core --verbose    # one suite with conversation detail
python3 run_evals.py --suite red-team --verbose
```

Run a single test: `python3 -m unittest -v test_features.py.RouterTests.test_<name>` (from `pipeline/`).

LiveKit unit tests: `cd livekit && python3 -m unittest -v test_talk_server.py test_env_loader.py`.

**Run the agent:**
```bash
cd pipeline
PROVIDER=mock python3 voice_loop.py --text     # type turns, no audio deps — always works
python3 voice_loop.py --text                   # live provider (needs .env), text mode
python3 voice_loop.py                           # live provider + real mic cascade
python3 scale_check.py --dau 1000000            # capacity calculator, no provider call
```

**LiveKit room demo** (three terminals, from `livekit/`): `./start_local_server.sh`, then
`python create_room.py && python talk_server.py`, then open `http://localhost:5173`. See
[RUNBOOK.md](RUNBOOK.md) Stage 5.

**Config:** `cp pipeline/config.example.env pipeline/.env`. Key vars: `PROVIDER`
(`mock`|`openai`|`groq`), `TTS_BACKEND` (`system` = free local `say`, `provider` = paid cloud TTS),
`ENDPOINT_SILENCE_MS`, `TELEMETRY_JSONL`, `TELEMETRY_INCLUDE_CONTENT`.

## Architecture

**Two-layer split — the key design idea.** The turn loop is provider-agnostic and the brain is
loop-agnostic, so the *same* `Agent` code drives text mode, mic mode, and the LiveKit browser
demo with no changes:

- **Layer A — turn loop** ([pipeline/voice_loop.py](pipeline/voice_loop.py)): mic capture →
  WebRTC VAD endpointing → STT → `Agent` → TTS, with per-stage latency timing. `--text` mode
  skips all audio deps.
- **Layer B — brain** ([pipeline/agent.py](pipeline/agent.py)): an LLM + tool loop
  (`Agent.respond`) over a `Provider`, holding conversation history. Uses OpenAI-style function
  calling (works on both OpenAI and Groq). Loops until the model returns plain text, executing
  tool calls in between; returns `(reply, action)` where `action` is `transfer`/`hangup`.

**Provider adaptor** ([pipeline/providers.py](pipeline/providers.py)): one interface —
`chat` / `transcribe` / `synthesize` — with three backends. `Provider` covers OpenAI and Groq
(same API dialect, only `base_url`/key/model names differ). `MockProvider` is a rule-based,
scripted, zero-dependency stand-in — **when you change tool schemas, guardrails, or the system
prompt, update `MockProvider.chat`'s rule branches too, or the offline tests/evals will drift
from live behavior.** `make_provider()` selects by `PROVIDER`.

**Hybrid tool routing** (the trickiest correctness detail). Tool selection is normally left to
the LLM, but `required_tool_for()` in [agent.py](pipeline/agent.py) force-selects
`search_hotel_knowledge` (via `tool_choice`) for high-confidence policy/amenity phrases *before*
the first model call. This keeps RAG grounding reliable after interruptions or off-topic turns.
It deliberately does **not** route mutation phrases like "cancel my reservation" into policy
search. Phrase lists and fuzzy-match terms live at the top of the file.

**Grounding boundaries** — different kinds of truth use different mechanisms:
- Policies/amenities → local RAG (`search_hotel_knowledge`, read-only, cites sources)
- Availability & rates → `check_availability` tool (dynamic operational truth)
- Booking → `create_booking` tool (auditable state mutation)
- Language → `set_language` control tool (validated session state)
- Transfer/hangup → control `action` returned to the loop (→ SIP REFER/BYE)

The mock tools in `run_tool()` return hardcoded rooms and a fixed confirmation ID (`AH-4827`) —
swap for real backends in production.

**Language routing** ([pipeline/router.py](pipeline/router.py) + `set_language` handling in
`agent.py`): `AgentRouter` holds validated `en`/`es` session state. The LLM proposes a switch,
but `explicit_language_request()` **gates** it — the caller's utterance must literally name the
target language, so a courtesy word like "¡Gracias!" does not flip the session. Rejected switches
emit `router.language_change_rejected` and leave state unchanged.

**RAG** ([pipeline/knowledge.py](pipeline/knowledge.py)): indexes Markdown sections from
`knowledge/*.md` into an in-memory SQLite FTS5 table (with a pure-Python lexical fallback if
FTS5 is unavailable). Includes English↔Spanish query expansion. Sources are cited as
`file.md#Section`.

**Telemetry** ([pipeline/telemetry.py](pipeline/telemetry.py)): `TurnTrace` collects ordered
events + stage timings per turn via `.span()`/`.event()`. **Privacy-by-default**: keys in
`_SENSITIVE_KEYS` (guest name, contact) are redacted and `_CONTENT_KEYS` (transcript, query,
result, text) are omitted unless `TELEMETRY_INCLUDE_CONTENT=true`. Traces append to
`TELEMETRY_JSONL` (default `logs/voice-events.jsonl`, git-ignored).

**LiveKit boundary** ([livekit/talk_server.py](livekit/talk_server.py)): a stdlib HTTP server
that mints LiveKit room tokens and reuses the pipeline `Agent` per session (keyed by
`X-Session-ID`, one lock per session). It imports from `pipeline/` by inserting it on
`sys.path`. **It is not yet a room-native agent worker** — it processes completed browser audio
via `/voice-agent` rather than subscribing to a LiveKit audio track. The browser
([livekit/web/](livekit/web/)) does VAD, endpointing, and playback barge-in client-side.

**Evals** ([evals/](evals/)): `run_evals.py` drives JSON scenarios (`core.json`, `red_team.json`)
through the real `Agent` on the mock provider, asserting expected `tools`, `action`, `language`,
`sourceContains`, `contains`, and `forbid` text. Add a red-team case *before* changing prompts or
tools so behavior changes have an explicit acceptance criterion.

## How to work: the goal loop (mandatory)

Four project skills in `.claude/skills/` turn the goal.md roadmap into a working discipline.
Every task follows this loop — the skills are the steps, in this order:

```
1. ORIENT   roadmap-guard  →  map the task to a goal.md phase item ("📍 Phase X.Y — …");
                              off-plan work gets flagged: amend the plan or defer, never
                              silently build it. Respect phase order; make skips explicit.
2. DECIDE   adr            →  if the task involves a significant technical choice (new
                              dependency, schema, interface, deviation from an ADR),
                              record ADR-012+ in goal.md §5 BEFORE building on it.
3. BUILD    edd            →  for ANY agent-behavior change (prompt, TOOLS, run_tool,
                              guardrails, routing, knowledge/, MockProvider): write the
                              eval FIRST, run it, prove it FAILS, then mock parity, then
                              the live path, then green. Pure refactors skip to step 4.
4. VERIFY   gates          →  all four offline suites, per-gate results with real output.
                              Red gates = not done. Never claim otherwise.
5. CLOSE    roadmap-guard  →  check the item against its phase's acceptance criteria
                              (goal.md §4/§7), mark it done in goal.md with a date,
                              then commit (short single-line message) and push.
```

Standing rules that override convenience:
- **Never weaken, delete, or loosen an eval to make it pass** — changed acceptance criteria are
  a product decision for the user.
- **Never write the eval after the implementation and call it EDD.**
- **Never silently contradict an ADR** — supersede it explicitly via the `adr` skill.
- Work the phases in order (Phase 1 → 4). The next open item in goal.md §4 is the default
  "what should we do next" answer.

## Conventions

- **Faithful reporting:** if smoke_test / unittest / evals fail, say so with the output; don't
  claim a change works until the offline suite passes.
- Keep spoken replies short and TTS-friendly (the system prompt enforces this — respect it when
  editing guardrails).
- Use `TTS_BACKEND=system` and `PROVIDER=mock` during development to avoid cloud cost.
- Never commit `.env`, venvs, `logs/*.jsonl`, or anything under `private/`.
