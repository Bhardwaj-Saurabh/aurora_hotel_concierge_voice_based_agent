"""
slo_report.py  -  derive SLO metrics from the JSONL trace stream (goal.md 4.2).

One instrumentation system: every number here comes from the same TurnTrace
events the agent already emits. `--check` turns the report into an alert
primitive — run it in CI or cron and a threshold breach exits non-zero.

    python slo_report.py --input ../logs/voice-events.jsonl
    python slo_report.py --input ... --max-p95-total-ms 800 --max-fallback-rate 0.05
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

_FALLBACK_EVENTS = {"llm.fallback", "stt.fallback", "tts.fallback"}


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(pct / 100.0 * len(ordered)) - 1)
    return ordered[index]


def compute(payloads: list[dict]) -> dict:
    """Aggregate a list of finished TurnTrace payloads into SLO metrics."""
    turns = len(payloads)
    if turns == 0:
        return {"turns": 0}

    totals = [float(p.get("totalMs", 0.0)) for p in payloads]
    llm = [float(p["timings"]["llm"]) for p in payloads if "llm" in p.get("timings", {})]
    stt = [float(p["timings"]["stt"]) for p in payloads if "stt" in p.get("timings", {})]
    event_names = [
        {e.get("name", "") for e in p.get("events", []) or []} for p in payloads
    ]
    actions = [p.get("attributes", {}).get("action") for p in payloads]

    def rate(predicate) -> float:
        return round(sum(1 for x in predicate) / turns, 4)

    report = {
        "turns": turns,
        "sessions": len({p.get("sessionId") for p in payloads}),
        "p50TotalMs": round(_percentile(totals, 50), 1),
        "p95TotalMs": round(_percentile(totals, 95), 1),
        "transferRate": rate(a for a in actions if a == "transfer"),
        "completedCallRate": rate(a for a in actions if a == "hangup"),
        "bargeInRate": rate(n for n in event_names if "turn.cancelled" in n),
        "fillerRate": rate(n for n in event_names if "latency.filler_played" in n),
        "fallbackRate": rate(n for n in event_names if n & _FALLBACK_EVENTS),
    }
    if llm:
        report["p95LlmMs"] = round(_percentile(llm, 95), 1)
    if stt:
        report["p95SttMs"] = round(_percentile(stt, 95), 1)
    return report


def breaches(report: dict, thresholds: dict) -> list[str]:
    """Return one message per SLO threshold the report exceeds."""
    found = []
    for key, limit in thresholds.items():
        value = report.get(key)
        if value is not None and value > limit:
            found.append(f"{key}={value} exceeds the SLO limit of {limit}")
    return found


def _load(path: Path) -> list[dict]:
    payloads = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            payloads.append(json.loads(line))
    return payloads


def main() -> None:
    parser = argparse.ArgumentParser(description="Aurora SLO report from trace JSONL")
    parser.add_argument("--input", default="../logs/voice-events.jsonl")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-p95-total-ms", type=float, dest="p95TotalMs")
    parser.add_argument("--max-transfer-rate", type=float, dest="transferRate")
    parser.add_argument("--max-barge-in-rate", type=float, dest="bargeInRate")
    parser.add_argument("--max-filler-rate", type=float, dest="fillerRate")
    parser.add_argument("--max-fallback-rate", type=float, dest="fallbackRate")
    args = parser.parse_args()

    report = compute(_load(Path(args.input).expanduser()))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Aurora SLO report")
        for key, value in report.items():
            print(f"  {key:<20} {value}")

    thresholds = {
        key: getattr(args, key)
        for key in ("p95TotalMs", "transferRate", "bargeInRate", "fillerRate", "fallbackRate")
        if getattr(args, key) is not None
    }
    found = breaches(report, thresholds)
    for message in found:
        print(f"SLO BREACH: {message}")
    raise SystemExit(1 if found else 0)


if __name__ == "__main__":
    main()
