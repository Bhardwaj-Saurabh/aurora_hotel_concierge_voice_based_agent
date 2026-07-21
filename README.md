# Assignment 2: Aurora Hotel Voice Agent

Aurora is a practical hotel-reservations voice agent built for an FDE workshop. The project starts with a deterministic text agent and progressively adds a live model, business tools, local retrieval, multilingual routing, microphone audio, turn detection, telemetry, LiveKit rooms, evaluation, and capacity planning.

The core cascade is:

```text
caller audio -> VAD and endpointing -> STT -> AgentRouter -> LLM -> RAG and tools -> TTS
```

## Capabilities

- Hotel availability and mock booking tools
- Hotel-only conversational guardrails
- Local policy RAG using SQLite FTS5
- English and Spanish session routing
- Mock, OpenAI, and Groq provider modes
- Local microphone capture with WebRTC VAD
- Browser VAD with adaptive noise calibration and playback barge-in
- Per-turn structured telemetry and a browser trace timeline
- Local LiveKit room with caller and agent participants
- Deterministic task evaluation and red-team suites
- Zero-cost capacity calculator for DAU and concurrency planning
- SIP and IVR simulations for telephony mapping

## Project Structure

```text
Assignment_2_voice_agent/
|-- README.md
|-- RUNBOOK.md
|-- knowledge/
|   `-- 2026-07-19/            # date-stamped snapshot; newest loads by default
|       |-- manifest.json      # authoritative file list for the snapshot
|       `-- hotel_policies.md
|-- evals/
|   |-- core.json
|   |-- red_team.json
|   `-- run_evals.py
|-- pyproject.toml             # one installable package: pip install -e .
|-- src/aurora/
|   |-- core/                  # brain: agent, tools, prompts, providers, router, RAG
|   |-- server/                # FastAPI talk server + packaged browser client (web/)
|   |-- worker/                # room-native LiveKit agent worker
|   |-- voice/                 # local mic/text turn loop
|   |-- storage/               # bookings + user auth (SQLite/Postgres)
|   |-- telemetry/             # JSONL traces + optional OTel export
|   |-- config/                # .env loader + fail-fast config check
|   `-- ops/                   # smoke, load_test, slo_report, scale_check, ...
|-- tests/                     # all unit/integration tests (run from the root)
|-- scripts/
|   `-- start_local_livekit.sh
`-- mocks/
    |-- demo_call.py
    |-- ivr_menu_mock.py
    `-- sip-ivr-call-flow.md
```

## Quick Start Without An API Key

The complete agent, tool, RAG, routing, evaluation, and scale paths run without network access or paid requests.

```bash
cd FDE/Assignment_2_voice_agent
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[server,worker,dev]"
python -m aurora.ops.smoke
python -m unittest -v tests.test_features
PROVIDER=mock python -m aurora.voice.loop --text
```

Try these turns:

```text
What is the weather?
What is the cancellation policy?
I need a room from August 12 to August 14 for two guests.
Please speak Spanish.
¿Cuál es la política de mascotas?
Connect me to the front desk.
```

## OpenAI Setup

```bash
cd FDE/Assignment_2_voice_agent
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[server,worker,audio,dev]"
cp config.example.env .env && chmod 600 .env
```

Set the following values in `.env`:

```env
PROVIDER=openai
OPENAI_API_KEY=your_key_here
TTS_BACKEND=system
TELEMETRY_JSONL=logs/voice-events.jsonl
```

Verify the live model before adding audio:

```bash
python -m aurora.voice.loop --text
```

Run the local microphone cascade:

```bash
python -m aurora.voice.loop
```

The terminal reports capture, STT, routing, retrieval, LLM, tool, TTS, and total turn timing. `TTS_BACKEND=system` uses the macOS voice and avoids cloud TTS cost during rehearsal.

Set `TTS_BACKEND=provider` to use the selected provider's configured TTS model and voice. Provider TTS incurs audio-generation cost.

## Groq Setup

The provider adapter uses the same tool-calling interface for OpenAI and Groq.

```env
PROVIDER=groq
GROQ_API_KEY=your_key_here
TTS_BACKEND=system
```

The commands remain the same.

## Local LiveKit Demo

The room demo uses the same root install (the browser's LiveKit client library is vendored
inside the package — no npm step). Use three terminals from the assignment root.

Terminal 1 starts the self-contained LiveKit development server:

```bash
./scripts/start_local_livekit.sh
```

Terminal 2 creates the room and starts the browser application:

```bash
source .venv/bin/activate
python -m aurora.ops.create_room
python -m aurora.server
```

Open `http://localhost:5173`, click **Start call**, allow microphone access, and speak naturally. The browser automatically joins the caller and Aurora participants, detects caller turns, displays grounding sources, and shows stage telemetry.

The LiveKit bridge honors `TTS_BACKEND` from `.env`. With `provider`, the server synthesizes WAV audio using `TTS_MODEL` and `TTS_VOICE`, and the UI labels the response with the selected voice. With `system` or `mock`, the browser uses its installed speech voice.

The browser exposes two workshop controls:

- **Endpoint silence** changes how long a pause must be before a turn is committed.
- **Speech sensitivity** changes the adaptive speech threshold relative to the measured noise floor.

Speak while Aurora is playing a response to demonstrate playback barge-in. The browser cancels speech output, records the interruption, and opens the next caller turn.

### LiveKit Boundary

Two agent transports exist:

- **HTTP turn bridge** (`aurora.server`, the FastAPI talk server, default demo path): the browser records a completed
  turn and POSTs audio to `/voice-agent`; browser or provider TTS speaks the reply.
- **Room-native agent worker** (`aurora.worker`): the agent joins the room as a participant,
  subscribes to the caller's audio track, runs Silero VAD/turn detection server-side, and
  publishes Aurora's replies as a TTS audio track. The same `Agent` powers both — the
  worker only replaces the transport.

Run the worker against the local server (requires a live provider; the mock cannot hear or speak):

```bash
source .venv/bin/activate
LIVEKIT_URL=ws://localhost:7880 LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=secret \
python -m aurora.worker dev
```

Then join the room from the browser app; the worker is dispatched automatically.

Remaining production extensions: streaming STT/LLM/TTS (Phase 3.2), distributed barge-in
cancellation (3.3), and SIP dispatch (3.4).

## Grounding And Tools

Aurora uses different boundaries for different kinds of truth:

| Information | Mechanism | Reason |
|-------------|-----------|--------|
| Policies, parking, pets, breakfast, accessibility | Local RAG | Read-oriented knowledge with source evidence |
| Availability and room rates | Tool call | Dynamic operational truth |
| Booking creation | Tool call | Auditable state mutation |
| Language switching | `set_language` control tool | Validated session state and matching TTS locale |
| Transfer and hangup | Control action | Runtime and telephony behavior |

The local retriever indexes Markdown sections with SQLite FTS5. It includes English and Spanish query expansion while keeping the source document unchanged.

Aurora uses hybrid tool routing. High-confidence policy and amenity phrases select `search_hotel_knowledge` in application code before the first model call. Other tool decisions remain automatic. This keeps retrieval reliable after interruptions or off-topic turns without routing a request such as `cancel my reservation` into policy search.

## Telemetry

Each turn carries a session ID, turn ID, trace ID, provider, model, language, stage timings, tool arguments, tool results, sources, action, and ordered runtime events.

Raw transcript and response content are omitted by default, and sensitive tool fields such as guest name and contact details are redacted. Set `TELEMETRY_INCLUDE_CONTENT=true` only for controlled local debugging with non-sensitive data.

The LiveKit server writes JSONL traces to:

```text
logs/voice-events.jsonl
```

The path is ignored by Git. Set `TELEMETRY_JSONL` to change or disable the destination.

Important production measures include endpoint delay, STT latency, LLM time to first token, tool latency, TTS time to first audio, end-of-turn to first audio, interruption latency, task completion, critical entity accuracy, transfer rate, and cost per successful outcome.

## Evaluation And Red Teaming

Run all deterministic scenarios:

```bash
python evals/run_evals.py --suite all
```

Run one suite with conversation details:

```bash
python evals/run_evals.py --suite core --verbose
python evals/run_evals.py --suite red-team --verbose
```

The suites verify expected tools, actions, languages, sources, allowed text, and forbidden text. The red-team set covers prompt injection, policy fabrication, privacy, structured tool input, and guardrails after a language switch.

## Scale Check

The calculator converts product assumptions into peak concurrency and service demand without calling a provider:

```bash
python -m aurora.ops.scale_check --dau 1000000
```

Default assumptions are 0.25 calls per DAU, four minutes per call, three turns per minute, an 8x peak factor, 40 sessions per worker, and 30 percent headroom. Change every assumption before using the result as a capacity plan.

Example with a blended variable cost:

```bash
python -m aurora.ops.scale_check --dau 1000000 --cost-per-minute 0.035
```

## Telephony Mapping

```text
PSTN caller -> carrier -> SIP trunk -> SBC or SIP edge -> LiveKit room -> agent -> tools
```

Run the local signaling demonstrations:

```bash
cd FDE/Assignment_2_voice_agent/mocks
python3 demo_call.py
python3 demo_call.py --transfer
python3 ivr_menu_mock.py
```

The mock maps booking completion to SIP BYE and human escalation to SIP REFER. A real phone deployment also requires a carrier or telephony provider, an internet-reachable SIP edge, codec and media negotiation, security policy, dispatch rules, and a room-native agent worker.

## Safety And Cost

- Keep `.env`, virtual environments, telemetry logs, and private workshop materials out of Git.
- Do not enable raw telemetry content for real customer conversations without an approved privacy and retention policy.
- Use mock mode for rehearsal, evaluation, and scale exercises.
- Use system TTS while developing to avoid cloud TTS charges.
- Treat booking tools as mock systems until authentication, validation, idempotency, persistence, and audit controls are added.
