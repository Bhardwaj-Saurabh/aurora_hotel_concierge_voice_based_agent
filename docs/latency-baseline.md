# Latency Baseline — Method & Results (Phase 1.5)

Goal: measure where the turn budget goes (target: end-of-turn → first audio under ~800 ms),
then show how `ENDPOINT_SILENCE_MS` trades responsiveness against cutting callers off.

Status: **method ready — results pending a live run** (requires a provider key + microphone).

---

## Method

### Preflight (once)

`pipeline/.env` must have a live provider and telemetry enabled:

```env
PROVIDER=openai            # or groq
OPENAI_API_KEY=...         # or GROQ_API_KEY
TTS_BACKEND=system         # keeps TTS cost at zero; timing still recorded
TELEMETRY_JSONL=../logs/voice-events.jsonl
```

Quiet room, same microphone and distance for every run. Do runs back-to-back so network
conditions are comparable.

### The 5 fixed utterances (same 5, same order, every run)

Speak these exactly — they exercise short/long turns, tools, RAG, and a deliberate
mid-sentence pause (turn 4 is the endpointing probe):

1. "What time is check-in?"
2. "I need a room from August 12 to August 14 for two guests."
3. "What is the cancellation policy?"
4. "I'd like to book the room… *(pause ~0.5 s)* …for Priya Shah at priya@example.com."
5. "What are your room service hours?"

### Runs

```bash
cd pipeline && source ../.venv/bin/activate

# Run A — baseline
ENDPOINT_SILENCE_MS=600 python voice_loop.py     # speak the 5 utterances, then Ctrl-C

# Run B — aggressive endpointing
ENDPOINT_SILENCE_MS=350 python voice_loop.py     # same 5 utterances

# Run C — patient endpointing
ENDPOINT_SILENCE_MS=900 python voice_loop.py     # same 5 utterances
```

Between runs, note qualitative events: was turn 4 **cut off** at the pause? How noticeable
was the **dead air** before the agent replied?

### Extract the numbers

Each turn's timings are printed live and appended to the JSONL. To tabulate the last 15 turns
(3 runs × 5):

```bash
cd .. && python3 - <<'PY'
import json
rows = [json.loads(l) for l in open("logs/voice-events.jsonl")][-15:]
print(f"{'turn':<6}{'stt':>7}{'llm':>7}{'tools':>7}{'tts':>7}{'total':>8}")
for i, r in enumerate(rows, 1):
    t = r["timings"]
    print(f"{i:<6}{t.get('stt',0):>7.0f}{t.get('llm',0):>7.0f}"
          f"{t.get('tools',0):>7.0f}{t.get('tts',0):>7.0f}{r['totalMs']:>8.0f}")
PY
```

---

## Results

*(fill in after the live run — all values in ms)*

### Run A — `ENDPOINT_SILENCE_MS=600` (baseline)

| Turn | STT | LLM | Tools | TTS | Total |
|------|-----|-----|-------|-----|-------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| **median** | | | | | |

Turn 4 cut off at the pause? ☐ yes ☐ no · Perceived dead air: ☐ none ☐ noticeable ☐ bad

### Run B — `ENDPOINT_SILENCE_MS=350`

| Turn | STT | LLM | Tools | TTS | Total |
|------|-----|-----|-------|-----|-------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| **median** | | | | | |

Turn 4 cut off at the pause? ☐ yes ☐ no · Perceived dead air: ☐ none ☐ noticeable ☐ bad

### Run C — `ENDPOINT_SILENCE_MS=900`

| Turn | STT | LLM | Tools | TTS | Total |
|------|-----|-----|-------|-----|-------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| **median** | | | | | |

Turn 4 cut off at the pause? ☐ yes ☐ no · Perceived dead air: ☐ none ☐ noticeable ☐ bad

---

## Analysis

*(answer after filling the tables)*

1. **Dominant stage:** which stage owns the largest share of median total? Is it what you
   expected?
2. **The endpointing tradeoff in numbers:** Run B total minus Run A total is pure endpoint
   savings — did it come at the cost of cutting off turn 4? Run C's extra delay bought what?
3. **Budget check:** median (total − capture) vs the ~800 ms EoT→first-audio target. Pass/fail,
   and which Phase 3.2 streaming change (streaming STT, streaming LLM, incremental TTS) would
   help the dominant stage most?
4. **Baseline for regressions:** these medians are the reference for Phase 2 — booking
   persistence (2.1) and output normalization (2.4) must not move them materially.

Note: `ENDPOINT_SILENCE_MS` itself is *inside* the capture span (the wait for silence), so its
effect shows in perceived responsiveness and the capture timing, not in STT/LLM/TTS. The mock
provider is useless here — its stages are ~0 ms; only a live run measures reality.
