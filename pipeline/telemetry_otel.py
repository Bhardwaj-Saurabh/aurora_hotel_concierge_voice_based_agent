"""
telemetry_otel.py  -  optional OpenTelemetry export of Aurora turn traces
(goal.md 4.2, ADR-009).

The vendor-neutral JSONL schema stays the source of truth; this is a thin
mapping layer: one finished TurnTrace payload becomes one OTel trace — a
`voice.turn` root span, a child span per pipeline stage, and the notable
events attached to the root. Redaction already happened inside TurnTrace, so
nothing sensitive can reach an export backend.

Enable by setting TELEMETRY_OTLP_ENDPOINT (an OTLP/HTTP collector URL) and
installing the exporter:

    pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
"""

from __future__ import annotations

import json
import os
import threading

_ROOT_ATTR_KEYS = ("language", "locale", "provider", "model", "action")
_provider_lock = threading.Lock()
_provider = None
_warned = False


def _parse_otlp_headers(raw: str) -> dict:
    """Parse TELEMETRY_OTLP_HEADERS ("Key1=Value1,Key2=Value2") into a dict
    for OTLPSpanExporter(headers=...). Generic on purpose (goal.md ADR-019) —
    this module stays vendor-neutral (ADR-009); Opik just happens to require
    three headers (Authorization, projectName, Comet-Workspace), but nothing
    here knows that."""
    headers = {}
    for entry in (raw or "").split(","):
        if "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def _to_ns(started_at: float, offset_ms: float) -> int:
    return int((started_at + offset_ms / 1000.0) * 1_000_000_000)


def _clean_attributes(attributes: dict) -> dict:
    """OTel attributes allow str/bool/int/float and lists of those."""
    cleaned = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            cleaned[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, bool, int, float)) for item in value
        ):
            cleaned[key] = list(value)
        else:
            cleaned[key] = json.dumps(value, ensure_ascii=True)
    return cleaned


def export_payload(payload: dict, tracer_provider) -> None:
    """Map one finished TurnTrace payload onto OTel spans."""
    from opentelemetry import trace as trace_api

    tracer = tracer_provider.get_tracer("aurora.voice")
    started_at = float(payload.get("startedAt", 0.0))
    total_ms = float(payload.get("totalMs", 0.0))
    attributes = payload.get("attributes", {}) or {}

    root_attributes = {
        "voice.session_id": payload.get("sessionId", ""),
        "voice.turn_id": payload.get("turnId", ""),
        "voice.trace_id": payload.get("traceId", ""),
        "voice.total_ms": total_ms,
    }
    for key in _ROOT_ATTR_KEYS:
        if attributes.get(key) is not None:
            root_attributes[f"voice.{key}"] = attributes[key]
    sources = attributes.get("sources")
    if sources:
        root_attributes["voice.sources"] = list(sources)

    root = tracer.start_span(
        "voice.turn",
        start_time=_to_ns(started_at, 0.0),
        attributes=_clean_attributes(root_attributes),
    )
    root_context = trace_api.set_span_in_context(root)

    timings = payload.get("timings", {}) or {}
    events = payload.get("events", []) or []

    # Reconstruct stage spans from X.started / X.completed event pairs.
    reconstructed: set[str] = set()
    open_starts: dict[str, float] = {}
    for event in events:
        name = event.get("name", "")
        offset = float(event.get("offsetMs", 0.0))
        if name.endswith(".started") and name[: -len(".started")] in timings:
            open_starts[name[: -len(".started")]] = offset
        elif name.endswith(".completed") and name[: -len(".completed")] in open_starts:
            stage = name[: -len(".completed")]
            child = tracer.start_span(
                f"voice.{stage}",
                context=root_context,
                start_time=_to_ns(started_at, open_starts.pop(stage)),
                attributes=_clean_attributes(event.get("attributes", {})),
            )
            child.end(end_time=_to_ns(started_at, offset))
            reconstructed.add(stage)

    # Stages timed without span events (e.g. streamed llm uses set_timing):
    # anchor at turn start with the recorded duration.
    for stage, duration in timings.items():
        if stage in reconstructed:
            continue
        child = tracer.start_span(
            f"voice.{stage}", context=root_context, start_time=_to_ns(started_at, 0.0)
        )
        child.end(end_time=_to_ns(started_at, float(duration)))

    # Notable events (everything that isn't span plumbing) go on the root.
    for event in events:
        name = event.get("name", "")
        if name.endswith(".started") or name.endswith(".completed"):
            continue
        root.add_event(
            name,
            attributes=_clean_attributes(event.get("attributes", {})),
            timestamp=_to_ns(started_at, float(event.get("offsetMs", 0.0))),
        )

    root.end(end_time=_to_ns(started_at, total_ms))


def maybe_export(payload: dict) -> None:
    """Export when TELEMETRY_OTLP_ENDPOINT is configured; silent no-op otherwise."""
    endpoint = os.getenv("TELEMETRY_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    provider = _otlp_provider(endpoint)
    if provider is not None:
        export_payload(payload, provider)


def _otlp_provider(endpoint: str):
    global _provider, _warned
    with _provider_lock:
        if _provider is not None:
            return _provider
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            if not _warned:
                _warned = True
                print(
                    "TELEMETRY_OTLP_ENDPOINT is set but the OTLP exporter is not "
                    "installed; skipping export. pip install opentelemetry-sdk "
                    "opentelemetry-exporter-otlp-proto-http"
                )
            return None
        headers = _parse_otlp_headers(os.getenv("TELEMETRY_OTLP_HEADERS", ""))
        provider = TracerProvider(
            resource=Resource.create({"service.name": "aurora-voice-agent"})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers or None))
        )
        _provider = provider
        return provider
