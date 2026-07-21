# Aurora Voice Agent — Runbook

Operational guide for running, verifying, demoing, and operating Aurora. Every command here
has been verified against the current code. Architecture and design rationale live in
`README.md`; the roadmap and ADRs live in `goal.md` (local-only).

---

## 1. Quick Reference

| Task | Command (from the assignment root) |
|---|---|
| Set up once | `uv venv --python 3.12 && source .venv/bin/activate && uv pip install -r pipeline/requirements.txt -r livekit/requirements.txt` |
| Validate config | `cd pipeline && python config_check.py` |
| Full offline gates | see §3 (smoke + unit tests + evals + livekit tests) |
| Talk to Aurora (offline, typed) | `cd pipeline && PROVIDER=mock python voice_loop.py --text` |
| Talk to Aurora (live, mic) | `cd pipeline && python voice_loop.py` |
| Browser demo (HTTP bridge) | §5.1 — three terminals |
| Room-native agent worker | §5.2 — `python agent_worker.py dev` |
| Run in Docker | §5.3 — two images: `docker build -f Dockerfile.talk-server ...` and `-f Dockerfile.worker ...` |
| SLO report | `cd pipeline && python slo_report.py --input ../logs/voice-events.jsonl` |
| Capacity estimate | `cd pipeline && python scale_check.py --dau 1000000` |
| SIP/IVR simulations | `cd mocks && python demo_call.py` / `python ivr_menu_mock.py` |

---

## 2. Setup

One virtualenv at the assignment root serves both packages (`pipeline/` and `livekit/`):

```bash
cd FDE/Assignment_2_voice_agent
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r pipeline/requirements.txt -r livekit/requirements.txt
cp pipeline/config.example.env pipeline/.env
```

Key `.env` settings:

```env
PROVIDER=mock                  # mock (offline) | openai | groq
OPENAI_API_KEY=                # only the selected provider's key is required
TTS_BACKEND=system             # system = free local voice; provider = cloud TTS ($)
TELEMETRY_JSONL=../logs/voice-events.jsonl
BOOKINGS_DB=../logs/bookings.db    # blank = in-memory (bookings vanish on restart)
ENDPOINT_SILENCE_MS=600        # pause that ends a caller turn
LATENCY_FILLER_MS=1200         # "One moment." if a turn thinks longer; 0 disables
KNOWLEDGE_SNAPSHOT=            # blank = newest knowledge/YYYY-MM-DD/; set to roll back
TELEMETRY_OTLP_ENDPOINT=       # optional OTel collector (see §7.2)
```

**Validate before running** — config mistakes fail fast with one clear message instead of a
mid-call stack trace:

```bash
cd pipeline && python config_check.py
```

Real-mic mode additionally needs PortAudio: `brew install portaudio`. Text mode, evals, the
browser demo, and the worker need no audio libraries on this machine.

---

## 3. Offline Verification Gates

Four suites, all running on the mock provider — no key, no network, < 15 s total. They are
**green at every commit** and enforced by CI (`.github/workflows/ci.yml`) on every push/PR:

```bash
source .venv/bin/activate
(cd pipeline && python smoke_test.py)                                # scripted end-to-end
(cd pipeline && python -m unittest -v test_features.py)              # 68 unit tests
(cd evals && python run_evals.py --suite all)                        # 19 scenarios: core + red-team
(cd livekit && python -m unittest -v test_talk_server.py test_env_loader.py test_agent_worker.py)
```

Run a single eval suite with conversation detail:

```bash
cd evals
python run_evals.py --suite core --verbose
python run_evals.py --suite red-team --verbose
```

**The working rule (EDD):** any change to agent behavior — prompt, tools, guardrails, routing,
knowledge, MockProvider — starts by writing the eval that pins the new behavior, proving it
fails, then implementing. Never weaken an eval to make it pass.

---

## 4. Talking to Aurora (CLI)

### 4.1 Offline text mode — always works

```bash
cd pipeline
PROVIDER=mock python voice_loop.py --text
```

Turns worth trying:

```text
What is the cancellation policy?          → grounded RAG answer + source
What are your room service hours?         → get_room_service_hours tool
I need a room from August 12 to August 14 for two guests.
Book it for Priya Shah at priya@example.com.     → AH-<random code>, persisted
Book it again for Priya Shah at priya@example.com. → "already confirmed", same ID (idempotent)
Should I take out a loan to pay for my stay?     → polite guardrail redirect
Please speak French. / Quelle est la politique d'annulation ?
Merci !                                   → must NOT switch languages
Connect me to the front desk.             → transfer (SIP REFER)
Goodbye                                   → hangup (SIP BYE)
```

### 4.2 Live voice mode (mic)

Set `PROVIDER=openai` or `groq` with its key, then:

```bash
python voice_loop.py
```

Per-turn telemetry prints capture/STT/routing/retrieval/LLM/tools/TTS timings. With a
streaming provider the `llm` figure is **time-to-first-token**. Slow turns (> `LATENCY_FILLER_MS`)
say "One moment." in the session language instead of leaving dead air.

### 4.3 Latency baseline experiment

Full protocol, fixed utterances, results template, and extraction snippet:
`docs/latency-baseline.md` (3 runs × 5 turns across `ENDPOINT_SILENCE_MS` 600/350/900).

---

## 5. Serving Aurora

### 5.1 Browser demo — HTTP turn bridge (default demo path)

Three terminals:

```bash
# T1 — local LiveKit server (Docker)
cd livekit && ./start_local_server.sh

# T2 — room + web app
cd livekit && source ../.venv/bin/activate
python create_room.py
python talk_server.py

# Browser
open http://localhost:5173     # Start call → allow mic
```

The browser does VAD/endpointing and barge-in; completed turns POST to `/voice-agent`.
Verify: both participants join, policy questions show grounding sources, the language badge
follows `en/es/fr`, and interrupting Aurora mid-reply records a barge-in without a feedback
loop.

### 5.2 Room-native agent worker (production path)

The agent joins the room as a participant: server-side Silero VAD, streaming LLM deltas into
incremental TTS, and real barge-in cancellation (interruption stops the provider stream and
tool work, not just playback). Requires a live provider — the mock can neither hear nor speak:

```bash
# with T1's local server running, and PROVIDER=openai|groq in pipeline/.env
cd livekit && source ../.venv/bin/activate
LIVEKIT_URL=ws://localhost:7880 LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=secret \
python agent_worker.py dev
```

Join the room from the browser app; the worker is dispatched automatically. Expected log:
`registered worker` on startup, then one trace per turn in the JSONL. On hangup/transfer the
worker lets the goodbye finish playing, then deletes the room.

### 5.3 Docker

Two images, because they're two different deployables (a web-facing service vs. a LiveKit job
worker with no public API) — see `goal.md` ADR-012.

**Talk server** (HTTP bridge — §5.1):

```bash
docker build -f Dockerfile.talk-server -t aurora-talk-server .
docker run --rm -p 5173:5173 \
  -e PROVIDER=mock \
  -e LIVEKIT_URL=ws://host.docker.internal:7880 \
  aurora-talk-server
curl http://localhost:5173/state
```

`python:3.12-slim`, non-root, installs only `livekit/requirements-server.txt` — no Silero/VAD
weight, since the browser does capture on this path.

**Room-native worker** (§5.2) — needs a live provider and a reachable LiveKit server:

```bash
docker build -f Dockerfile.worker -t aurora-agent-worker .
docker run --rm -p 8081:8081 \
  -e PROVIDER=openai -e OPENAI_API_KEY=... \
  -e LIVEKIT_URL=wss://your-project.livekit.cloud \
  -e LIVEKIT_API_KEY=... -e LIVEKIT_API_SECRET=... \
  aurora-agent-worker
curl http://localhost:8081/    # "OK" once connected to LiveKit — the health probe
```

No `EXPOSE`d application port — the worker registers with LiveKit and receives room dispatches;
port 8081 is only the orchestrator health/liveness endpoint. Run a pool of these; scale by
concurrent call volume, not HTTP traffic.

CI builds and boot-verifies both images independently (`container-talk-server`,
`container-worker`); the worker job spins up a real local LiveKit server to prove registration.

---

## 6. Behavior Under Failure

Verified by unit tests; every fallback emits a trace event.

| Failure | Behavior |
|---|---|
| LLM call fails (timeout/network) | Spoken fallback in the session language: "I'm having trouble… say that again?" (`llm.fallback`) |
| Two consecutive failed turns | "…connecting you to the front desk." + transfer action (`failure.transfer`) |
| STT fails | Re-prompt once, transfer on the second consecutive failure (`stt.fallback`) |
| Provider TTS fails | Local system voice fallback; the call never crashes (`tts.fallback`) |
| Mid-stream provider death | Partial reply + spoken fallback; history keeps only what was heard |
| Barge-in during a reply | Provider stream closed (no zombie tokens), running tool never interrupted, remaining tools get synthetic responses, history truncates to what was spoken (`turn.cancelled`) |
| Bad `.env` | Startup exits with every problem listed; nothing serves |
| Booking retried | Same confirmation ID, "already confirmed", no duplicate row |

Transport-level resilience: `PROVIDER_TIMEOUT_S=30`, `PROVIDER_MAX_RETRIES=1`.

---

## 7. Operations

### 7.1 Telemetry

Every turn appends a redacted trace to `TELEMETRY_JSONL` (default `logs/voice-events.jsonl`,
git-ignored). Guest name/contact are redacted; transcript/reply text is omitted unless
`TELEMETRY_INCLUDE_CONTENT=true` (local debugging only — never with real callers).

```bash
tail -n 1 logs/voice-events.jsonl | python3 -m json.tool
```

### 7.2 OpenTelemetry export

Set `TELEMETRY_OTLP_ENDPOINT` (e.g. `http://localhost:4318/v1/traces`) and install the wire
exporter (`pip install opentelemetry-exporter-otlp-proto-http`). Each turn becomes one OTel
trace: a `voice.turn` root span, per-stage child spans, notable events on the root. Redaction
happens before export. `config_check.py` flags a configured endpoint with a missing exporter.

**Sending traces to Opik Cloud** (goal.md ADR-019): `TELEMETRY_OTLP_HEADERS` is a generic
`Key1=Value1,Key2=Value2` env var (this module stays vendor-neutral) — Opik needs three:

```bash
TELEMETRY_OTLP_ENDPOINT=https://www.comet.com/opik/api/v1/private/otel/v1/traces
TELEMETRY_OTLP_HEADERS=Authorization=<OPIK_API_KEY>,projectName=<project>,Comet-Workspace=<your-real-workspace-slug>
```

Verified live, not assumed from docs: the endpoint without the `/v1/traces` suffix 404s, and
`Comet-Workspace` must be your actual workspace slug (visible in the Opik dashboard URL) —
`"default"` authenticates fine for the Prompt Library API but silently 500s on trace ingestion.

### 7.2.1 Prompt registry — Opik Cloud

`SYSTEM_PROMPT` (`pipeline/agent.py`) still ships as a Python constant and remains the fallback
of record. With `OPIK_API_KEY` set, `pipeline/prompt_registry.py` fetches the version tagged for
Opik's `production` environment instead (falling back to Opik's latest version, then to the local
constant, on any miss or error — this never dead-ends a call). Every trace records a
`promptVersion` attribute (`"local"`, `"local-fallback"`, or `"opik:vN"`).

**Promotion is eval-gated, not manual** — a version only becomes `production` after it passes the
full offline eval suite:

```bash
cd pipeline
python promote_prompt.py --version v5
```

This runs `evals/run_evals.py --suite all` with that exact candidate pinned
(`OPIK_PROMPT_VERSION_OVERRIDE`), and only tags it for `production` in Opik if every scenario
passes — the same eval-first discipline every other agent-behavior change goes through, applied
to prompt edits made in Opik's UI instead of a code diff.

### 7.3 SLO report — the alert primitive

```bash
cd pipeline
python slo_report.py --input ../logs/voice-events.jsonl
python slo_report.py --input ../logs/voice-events.jsonl \
  --max-p95-total-ms 800 --max-fallback-rate 0.05 --max-transfer-rate 0.3
```

Reports p50/p95 total, p95 LLM (TTFT) and STT, and transfer / completed-call / barge-in /
filler / fallback rates. Any `--max-*` breach exits non-zero — run it in CI or cron. Watch
`fillerRate` first: fillers spike before p95 does.

### 7.4 Bookings

Two backends, same interface (`get_booking_backend()` in `pipeline/bookings.py`), both idempotent
via session + normalized-details keys:

**SQLite** (default) — `BOOKINGS_DB` set → a durable file; blank → in-memory. Single instance only:

```bash
sqlite3 logs/bookings.db 'SELECT confirmation_id, guest_name, check_in, check_out FROM bookings;'
```

**Postgres** (goal.md ADR-013) — set `POSTGRES_HOST`/`PORT`/`USER`/`PASSWORD`/`DB` to make this
the active backend instead; needed the moment more than one process writes bookings (a worker
pool, ADR-012), since idempotency across separate SQLite files can't be guaranteed. Atomicity
comes from `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` at the database level, not an
application lock. `POSTGRES_SSLMODE` defaults to `prefer` — hosted free-tier providers often
don't actually support `require` despite what their docs claim; trust the live connection error
over the marketing page. Inspect:

```bash
psql "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB?sslmode=prefer" \
  -c "SET search_path TO \"$POSTGRES_USER\", public; SELECT id, guest_name, check_in, check_out FROM bookings;"
```

Confirmation IDs are a random, non-guessable 6-character code (`AH-` + an alphabet with
confusable characters removed — no `0/O`, `1/I/L`), so they can be spoken and heard correctly
over the phone (goal.md ADR-014).

### 7.5 Knowledge snapshots

Policies live in date-stamped snapshots; the newest loads by default and the manifest is
authoritative (unlisted files are not indexed).

**Publish a policy change:**

```bash
cp -r knowledge/2026-07-19 knowledge/2026-08-01      # copy newest snapshot
# edit knowledge/2026-08-01/hotel_policies.md, update manifest.json if files changed
cd evals && python run_evals.py --suite all           # grounding evals must stay green
```

**Roll back a bad edit** — one line in `pipeline/.env`:

```env
KNOWLEDGE_SNAPSHOT=2026-07-19
```

An invalid pin is rejected at startup with the available snapshots listed.

### 7.6 Capacity planning

```bash
cd pipeline && python scale_check.py --dau 1000000 --cost-per-minute 0.035
```

Change every assumption before treating the output as a plan; replace assumptions with
measured load-test numbers (goal.md 4.3) before production.

### 7.7 Secrets management

`.env` files are a **local-dev convenience only** — never commit one, and check its permissions:

```bash
cd pipeline && python config_check.py    # now flags a world/group-readable .env
chmod 600 .env                            # if flagged
```

In production, don't use a `.env` file at all. Inject the exact same env vars
(`OPENAI_API_KEY`, `POSTGRES_PASSWORD`, `LIVEKIT_API_SECRET`, …) via your platform's secret
manager (AWS Secrets Manager, HashiCorp Vault, Doppler, Kubernetes Secrets, …) — every entry
point reads them with plain `os.getenv()`, so nothing in the app needs to know or care where a
value came from. `pipeline/config.example.env` documents every var but must never carry real
values.

### 7.8 User auth (talk-server)

Closes ADR-015's documented gap: `/token`, `/agent`, `/voice-agent`, `/greeting`, and `/reset`
now require a logged-in session (goal.md ADR-018). `/state` stays open (Fly's health check and
the page's pre-login provider badge both need it).

- **Registration is self-service and open** — anyone can create an account at `/auth/register`.
  There is no email verification and no password-reset-by-email (both would need an
  email-sending dependency this project doesn't have).
- **Revocation is CLI-only**, no admin web UI:
  ```bash
  cd pipeline
  python manage_users.py list
  python manage_users.py disable someone@example.com   # immediately invalidates their sessions
  python manage_users.py enable someone@example.com
  ```
- **Requires Postgres** — `talk_server.py` refuses to start without `POSTGRES_HOST` set; unlike
  bookings.py, there is no file-backed SQLite fallback for credentials/sessions in production.
- **Two independent rate limiters**, both in-memory/single-process (reset on restart/deploy;
  would need a shared store if ever scaled past one machine): `AUTH_RATE_LIMIT_PER_HOUR`
  (post-auth, per user — the cost-incurring routes) and `AUTH_LOGIN_RATE_LIMIT` (pre-auth, per
  `client IP, email` — protects `/auth/login` and `/auth/register` themselves).
- Set `AUTH_COOKIE_SECURE=false` only for local `http://localhost` dev; Fly always serves https.

---

## 8. Demo Script (condensed)

A 20-minute walkthrough of the full system, offline until step 5:

1. **Gates** (§3) — "the whole behavior surface is pinned: 19 scenarios, 68 tests."
2. **Text session** (§4.1) — grounding with sources, room service tool, **idempotent
   double-booking**, financial-advice guardrail, French round-trip ending with "Merci !"
   not switching.
3. **Failure theater** — stop your network mid-live-call (or show
   `FailureFallbackTests`): re-prompt → transfer, never silence.
4. **SLO report** (§7.3) over the traces the demo just generated.
5. **Live voice** (§4.2 or §5.2) — same brain, now with ears and a mouth; point out TTFT
   and the latency filler on a slow turn; interrupt Aurora mid-sentence to show barge-in.
6. **Rollback** (§7.5) — pin last week's policy snapshot, restart, ask the same question.

---

## 9. Troubleshooting

| Symptom | Resolution |
|---|---|
| Startup prints "Configuration problems" | Fix the listed `pipeline/.env` entries; `python config_check.py` re-checks |
| Missing provider key | Run with `PROVIDER=mock`, or set the key for the selected provider |
| `sounddevice` fails | `brew install portaudio`, or use `--text` mode |
| Browser can't connect | Keep `start_local_server.sh` running (port 7880) |
| Worker won't start with mock | Expected: a live room needs real STT/TTS — set `PROVIDER=openai|groq` |
| Worker registered but silent on join | Provider key invalid — check T2 logs for STT/TTS auth errors |
| Mock ignores what you type/say | Mock STT returns scripted phrases by design; the mock LLM is rule-based |
| Turn cuts off mid-sentence | Raise `ENDPOINT_SILENCE_MS` (e.g. 900) |
| Replies feel slow | Lower `ENDPOINT_SILENCE_MS` carefully; check `fillerRate` and p95 TTFT in the SLO report |
| Provider TTS errors | `TTS_BACKEND=system` and restart; the loop also falls back automatically |
| `pkg_resources is deprecated` warning | Cosmetic (webrtcvad); the `setuptools<81` pin handles it |
| OTel endpoint set but nothing exports | Install `opentelemetry-exporter-otlp-proto-http`; config_check flags this |
| Evals fail after a knowledge edit | The eval is doing its job — fix the snapshot or update the eval *as a deliberate product decision* |
| Live service dies during a demo | Fall back to `PROVIDER=mock --text`; the architecture story survives |
