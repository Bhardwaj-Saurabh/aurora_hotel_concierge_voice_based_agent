# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Aurora is a hotel-reservations **voice agent** built for an FDE workshop. It demonstrates a
progressive build of a production voice cascade:

```
caller audio -> VAD/endpointing -> STT -> AgentRouter -> LLM -> RAG & tools -> TTS
```

The entire agent/tool/RAG/routing/eval/scale path runs **fully offline** with `PROVIDER=mock`
(no network or API key). Live paths add OpenAI or Groq. Everything is Python 3 plus a small
browser client (no JS build step; the LiveKit client library is vendored).

**The mission:** grow this into a production-grade, deployable voice reservation service. The
approved roadmap lives in `goal.md` (5 phases, 20 ADRs, per-phase definitions of done) — it is
**local-only** (gitignored, not in the remote); if it's missing, ask the user for it before
starting roadmap work. Every session should advance a roadmap item, not wander.

## Commands

One installable package (`src/aurora/`, ADR-020) with one venv at the assignment root.
Create/recreate it with:

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[server,worker,audio,dev]"
```

Everything runs from the assignment root — no `cd` into subpackages, no `sys.path` tricks.

**Offline verification (no key required) — the four gates. Run after any change to the agent,
tools, router, RAG, server, or telemetry:**
```bash
python -m aurora.ops.smoke                     # Gate 1: scripted end-to-end through the real Agent
python -m unittest -v tests.test_features tests.test_auth tests.test_rate_limit tests.test_prompt_registry   # Gate 2
python evals/run_evals.py --suite all          # Gate 3: deterministic task + red-team scenarios (mock provider)
python -m unittest -v tests.test_talk_server tests.test_env_loader tests.test_agent_worker tests.test_token_utils  # Gate 4
```

Run a single test: `python -m unittest -v tests.test_features.RouterTests.test_<name>`.
One eval suite with conversation detail: `python evals/run_evals.py --suite core --verbose`
(or `--suite red-team`).

**Run the agent:**
```bash
PROVIDER=mock python -m aurora.voice.loop --text   # type turns, no audio deps — always works
python -m aurora.voice.loop --text                 # live provider (needs .env), text mode
python -m aurora.voice.loop                        # live provider + real mic cascade (needs [audio] extra)
python -m aurora.ops.scale_check --dau 1000000     # capacity calculator, no provider call
```

**Talk server (FastAPI + uvicorn):** `python -m aurora.server` (hard-requires `POSTGRES_HOST`
for the ADR-018 user-auth system). **Room worker:** `python -m aurora.worker dev|start`.
**LiveKit room demo** (three terminals, from the root): `./scripts/start_local_livekit.sh`, then
`python -m aurora.ops.create_room && python -m aurora.server`, then open `http://localhost:5173`.
See [RUNBOOK.md](RUNBOOK.md) Stage 5.

**Config:** `cp config.example.env .env` (root-level, chmod 600 — config check enforces it).
Key vars: `PROVIDER` (`mock`|`openai`|`groq`), `TTS_BACKEND` (`system` = free local `say`,
`provider` = paid cloud TTS), `ENDPOINT_SILENCE_MS`, `TELEMETRY_JSONL`, `TELEMETRY_INCLUDE_CONTENT`.

## Architecture

One package, `src/aurora/`, split by responsibility (ADR-020):

| Subpackage | What lives there |
|---|---|
| `aurora.core` | the brain: `agent.py` (Agent class + turn loop), `tools.py` (TOOLS schemas, routing, `run_tool`), `prompts.py` (SYSTEM_PROMPT fallback), `providers.py`, `router.py`, `knowledge.py` (RAG), `spoken_text.py` |
| `aurora.server` | FastAPI talk server: `app.py` (factory + main), `routes/` (auth/meta/turns), `deps.py` (auth gate + rate limiters), `sessions.py`, `replies.py`, `cookies.py`, `token_utils.py`, packaged `web/` client |
| `aurora.worker` | room-native LiveKit agent worker (ADR-008) |
| `aurora.voice` | local mic/text turn loop (Layer A) |
| `aurora.storage` | `bookings.py` (ADR-007/013), `auth.py` (ADR-018) — Postgres-only, no local-database fallback (ADR-021) |
| `aurora.telemetry` | `traces.py` (TurnTrace JSONL, privacy-by-default), `otel.py` (optional OTLP export) |
| `aurora.config` | `env.py` (.env loader), `check.py` (fail-fast startup validation) |
| `aurora.ops` | CLIs: `smoke`, `load_test`, `slo_report`, `scale_check`, `manage_users`, `promote_prompt`, `create_room`, `create_token` |

**Two-layer split — the key design idea.** The turn loop is provider-agnostic and the brain is
loop-agnostic, so the *same* `Agent` drives text mode, mic mode, the HTTP bridge, and the room
worker with no changes:

- **Layer A — transports** ([aurora/voice/loop.py](src/aurora/voice/loop.py),
  [aurora/server/](src/aurora/server/), [aurora/worker/main.py](src/aurora/worker/main.py)).
- **Layer B — brain** ([aurora/core/agent.py](src/aurora/core/agent.py)): an LLM + tool loop
  (`Agent.respond` / `respond_stream`) over a `Provider`, holding conversation history. Uses
  OpenAI-style function calling (works on both OpenAI and Groq). Loops until the model returns
  plain text, executing tool calls in between; returns `(reply, action)` where `action` is
  `transfer`/`hangup`.

**Provider adaptor** ([aurora/core/providers.py](src/aurora/core/providers.py)): one interface —
`chat` / `transcribe` / `synthesize` — with three backends. `Provider` covers OpenAI and Groq
(same API dialect, only `base_url`/key/model names differ). `MockProvider` is a rule-based,
scripted stand-in — **when you change tool schemas, guardrails, or the system prompt, update
`MockProvider.chat`'s rule branches too, or the offline tests/evals will drift from live
behavior.** `make_provider()` selects by `PROVIDER`.

**Hybrid tool routing** (the trickiest correctness detail). Tool selection is normally left to
the LLM, but `required_tool_for()` in [aurora/core/tools.py](src/aurora/core/tools.py)
force-selects `search_hotel_knowledge` (via `tool_choice`) for high-confidence policy/amenity
phrases *before* the first model call. This keeps RAG grounding reliable after interruptions or
off-topic turns. It deliberately does **not** route mutation phrases like "cancel my
reservation" into policy search. Phrase lists and fuzzy-match terms live in the same file.

**Grounding boundaries** — different kinds of truth use different mechanisms:
- Policies/amenities → local RAG (`search_hotel_knowledge`, read-only, cites sources)
- Availability & rates → `check_availability` tool (dynamic operational truth)
- Booking → `create_booking` tool (auditable state mutation)
- Language → `set_language` control tool (validated session state)
- Transfer/hangup → control `action` returned to the loop (→ SIP REFER/BYE)

Room availability/rates in `run_tool()` are a static mock catalog. Bookings themselves are real:
[aurora/storage/bookings.py](src/aurora/storage/bookings.py) persists them to Postgres
(ADR-007/013/020 — no local-database fallback) with a random, non-guessable confirmation ID
(ADR-014) — never a sequential counter.

**Language routing** ([aurora/core/router.py](src/aurora/core/router.py) + `set_language`
handling in `agent.py`): `AgentRouter` holds validated session state. The LLM proposes a switch,
but `explicit_language_request()` **gates** it — the caller's utterance must literally name the
target language, so a courtesy word like "¡Gracias!" does not flip the session. Rejected switches
emit `router.language_change_rejected` and leave state unchanged.

**RAG** ([aurora/core/knowledge.py](src/aurora/core/knowledge.py)): indexes Markdown sections
from the root `knowledge/` dir (versioned snapshots; `KNOWLEDGE_DIR` env overrides the location,
containers set `/app/knowledge`) into an in-memory SQLite FTS5 table (pure-Python lexical
fallback if FTS5 is unavailable). Includes query expansion across languages. Sources are cited
as `file.md#Section`.

**Telemetry** ([aurora/telemetry/traces.py](src/aurora/telemetry/traces.py)): `TurnTrace`
collects ordered events + stage timings per turn via `.span()`/`.event()`.
**Privacy-by-default**: keys in `_SENSITIVE_KEYS` (guest name, contact) are redacted and
`_CONTENT_KEYS` (transcript, query, result, text) are omitted unless
`TELEMETRY_INCLUDE_CONTENT=true`. Traces append to `TELEMETRY_JSONL` (default
`logs/voice-events.jsonl`, git-ignored). Optional OTLP export
([aurora/telemetry/otel.py](src/aurora/telemetry/otel.py)) is vendor-neutral; Opik is
configuration (ADR-019).

**Talk server** ([aurora/server/](src/aurora/server/)): a FastAPI app (ADR-020) that serves the
packaged browser client, mints LiveKit room tokens, and bridges HTTP turns to the `Agent`.
Auth (ADR-018) is a `Depends(require_user)` gate; conversation sessions are keyed
`(user_id, X-Session-ID)` with one lock per session. **Response-shape contract:** every error
body is `{"error": …}` with hand-parsed request bodies — deliberately no Pydantic request
models, so FastAPI's 422 machinery can never fire (web/auth.js + talk.js read `payload.error`).
`/state` stays unauthenticated (Fly's health check). Single-process only (in-memory sessions +
limiters): uvicorn runs with `workers=1`. It is not a room-native worker — that's
`aurora.worker`, which subscribes to LiveKit audio tracks directly. The browser
([aurora/server/web/](src/aurora/server/web/)) does VAD, endpointing, and playback barge-in
client-side.

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
                              record ADR-021+ in goal.md §5 BEFORE building on it.
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
- Work the phases in order. The next open item in goal.md §4 is the default
  "what should we do next" answer.

## Conventions

- **Faithful reporting:** if smoke / unittest / evals fail, say so with the output; don't
  claim a change works until the offline suite passes.
- Keep spoken replies short and TTS-friendly (the system prompt enforces this — respect it when
  editing guardrails).
- Use `TTS_BACKEND=system` and `PROVIDER=mock` during development to avoid cloud cost.
- Never commit `.env`, venvs, `logs/*.jsonl`, or anything under `private/`.
