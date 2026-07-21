"""Focused offline tests for routing, grounding, telemetry, and capacity."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

try:
    # Picks up POSTGRES_* for the live-backend contract tests below; existing
    # env vars (set explicitly on the next two lines) always win over .env.
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

os.environ["PROVIDER"] = "mock"
os.environ.setdefault("TTS_BACKEND", "print")

from aurora.core.agent import Agent, explicit_language_request, required_tool_for
from aurora.core.knowledge import search_hotel_knowledge
from aurora.core.providers import MockProvider, _env_or_default, _mk_tool, make_provider
from aurora.core.router import AgentRouter
from aurora.ops.scale_check import estimate_capacity
from aurora.telemetry.traces import TurnTrace


class RouterTests(unittest.TestCase):
    def test_tool_selected_language_persists(self):
        router = AgentRouter()
        self.assertEqual(router.set_language("es").language, "es")
        self.assertEqual(router.route().language, "es")
        self.assertEqual(router.set_language("en").language, "en")

    def test_language_switch_intent_uses_control_tool(self):
        agent = Agent(make_provider("mock"))
        spanish_trace = TurnTrace(session_id="test", turn_id="spanish")
        agent.respond("Can you please speak in Spanish?", trace=spanish_trace)
        english_trace = TurnTrace(session_id="test", turn_id="english")
        reply, _ = agent.respond("Let me switch back to English.", trace=english_trace)

        self.assertEqual(agent.current_language, "en")
        self.assertIn("continue in English", reply)
        requested = [
            event["attributes"].get("tool")
            for event in english_trace.events
            if event["name"] == "tool.requested"
        ]
        self.assertEqual(requested, ["set_language"])
        self.assertIn(
            "router.language_changed",
            [event["name"] for event in english_trace.events],
        )

    def test_language_change_requires_explicit_target_name(self):
        self.assertTrue(explicit_language_request("Switch back to English", "en"))
        self.assertTrue(explicit_language_request("Por favor, habla español", "es"))
        self.assertFalse(explicit_language_request("¡Gracias!", "es"))

    def test_overeager_language_tool_cannot_change_state(self):
        class OvereagerProvider(MockProvider):
            def chat(self, messages, tools=None, tool_choice=None):
                if messages[-1].get("role") == "user":
                    return _mk_tool("set_language", {"language": "es"})
                return super().chat(messages, tools=tools, tool_choice=tool_choice)

        agent = Agent(OvereagerProvider())
        trace = TurnTrace(session_id="test", turn_id="courtesy")
        agent.respond("¡Gracias!", trace=trace)

        self.assertEqual(agent.current_language, "en")
        self.assertIn(
            "router.language_change_rejected",
            [event["name"] for event in trace.events],
        )


class ProviderConfigurationTests(unittest.TestCase):
    def test_blank_model_override_uses_provider_default(self):
        with patch.dict(os.environ, {"LLM_MODEL": ""}):
            self.assertEqual(_env_or_default("LLM_MODEL", "gpt-4o-mini"), "gpt-4o-mini")

    def test_comment_only_model_override_uses_provider_default(self):
        with patch.dict(os.environ, {"LLM_MODEL": "# example model"}):
            self.assertEqual(_env_or_default("LLM_MODEL", "gpt-4o-mini"), "gpt-4o-mini")

    def test_explicit_model_override_is_preserved(self):
        with patch.dict(os.environ, {"LLM_MODEL": "gpt-4.1-mini"}):
            self.assertEqual(_env_or_default("LLM_MODEL", "gpt-4o-mini"), "gpt-4.1-mini")


class RetrievalTests(unittest.TestCase):
    def test_english_policy_returns_precise_source(self):
        result = search_hotel_knowledge("What is the cancellation policy?")
        self.assertEqual(result["sources"], ["hotel_policies.md#Cancellation"])

    def test_spanish_query_expands_to_english_knowledge(self):
        result = search_hotel_knowledge("¿Cuál es la política de mascotas?")
        self.assertEqual(result["sources"], ["hotel_policies.md#Pets"])

    def test_policy_intent_requires_grounding_tool(self):
        self.assertEqual(
            required_tool_for("What does the cancellation policy look like?"),
            "search_hotel_knowledge",
        )

    def test_cancellation_action_is_not_misrouted_to_rag(self):
        self.assertIsNone(required_tool_for("Please cancel my reservation"))

    def test_noisy_spanish_pet_policy_transcript_routes_to_rag(self):
        self.assertEqual(
            required_tool_for("Fiol es la politista di maskotas."),
            "search_hotel_knowledge",
        )

    def test_forced_tool_choice_is_sent_on_first_model_call(self):
        class RecordingProvider(MockProvider):
            def __init__(self):
                super().__init__()
                self.tool_choices = []

            def chat(self, messages, tools=None, tool_choice=None):
                self.tool_choices.append(tool_choice)
                return super().chat(messages, tools=tools, tool_choice=tool_choice)

        provider = RecordingProvider()
        agent = Agent(provider)
        trace = TurnTrace(session_id="test", turn_id="forced-rag")
        reply, _ = agent.respond("What is the cancellation policy?", trace=trace)

        self.assertIn("6:00 PM", reply)
        self.assertEqual(
            provider.tool_choices[0],
            {"type": "function", "function": {"name": "search_hotel_knowledge"}},
        )
        self.assertIsNone(provider.tool_choices[1])
        self.assertIn("tool.route_selected", [event["name"] for event in trace.events])


def _stream_chunk(content=None, tool_calls=None):
    from types import SimpleNamespace as NS
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))])


def _tool_call_delta(index, call_id=None, name=None, arguments=None):
    from types import SimpleNamespace as NS
    return NS(index=index, id=call_id, function=NS(name=name, arguments=arguments))


class StreamingRespondTests(unittest.TestCase):
    def test_content_streams_incrementally_with_first_token_event(self):
        class StreamingProvider(MockProvider):
            def stream_chat(self, messages, tools=None, tool_choice=None):
                yield _stream_chunk(content="You can cancel ")
                yield _stream_chunk(content="until 6 PM.")

        agent = Agent(StreamingProvider())
        trace = TurnTrace(session_id="t", turn_id="stream-1")
        pieces = list(agent.respond_stream("Hello", trace=trace))
        self.assertEqual(pieces, ["You can cancel ", "until 6 PM."])
        self.assertIn("llm.first_token", [e["name"] for e in trace.events])
        self.assertIsNone(agent.last_action)

    def test_streamed_tool_calls_assemble_execute_then_stream_reply(self):
        class ToolStreamingProvider(MockProvider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def stream_chat(self, messages, tools=None, tool_choice=None):
                self.calls += 1
                if self.calls == 1:
                    yield _stream_chunk(tool_calls=[_tool_call_delta(
                        0, call_id="call_1", name="search_hotel_knowledge",
                        arguments='{"query": "pet',
                    )])
                    yield _stream_chunk(tool_calls=[_tool_call_delta(
                        0, arguments=' policy"}',
                    )])
                else:
                    yield _stream_chunk(content="Two dogs are ")
                    yield _stream_chunk(content="allowed per room.")

        agent = Agent(ToolStreamingProvider())
        trace = TurnTrace(session_id="t", turn_id="stream-2")
        reply = "".join(agent.respond_stream("What is the pet policy?", trace=trace))
        self.assertEqual(reply, "Two dogs are allowed per room.")
        requested = [
            e["attributes"].get("tool") for e in trace.events
            if e["name"] == "tool.requested"
        ]
        self.assertIn("search_hotel_knowledge", requested)
        self.assertIn("hotel_policies.md#Pets", agent.last_sources[0])

    def test_mid_stream_failure_yields_spoken_fallback(self):
        class DyingStreamProvider(MockProvider):
            def stream_chat(self, messages, tools=None, tool_choice=None):
                yield _stream_chunk(content="Let me ")
                raise ConnectionError("stream died")

        agent = Agent(DyingStreamProvider())
        trace = TurnTrace(session_id="t", turn_id="stream-3")
        pieces = list(agent.respond_stream("Hello", trace=trace))
        self.assertIn("trouble", "".join(pieces).lower())
        self.assertIn("llm.fallback", [e["name"] for e in trace.events])

    def test_respond_joins_the_stream_for_non_streaming_callers(self):
        # MockProvider has no stream_chat: the fallback path must behave as before.
        agent = Agent(MockProvider())
        reply, action = agent.respond("What is the pet policy?")
        self.assertIn("two dogs", reply.lower())
        self.assertIsNone(action)


class OtelExportTests(unittest.TestCase):
    def _finished_payload(self):
        trace = TurnTrace(session_id="s-1", turn_id="t-1")
        with trace.span("stt", model="whisper-1"):
            pass
        with trace.span("llm", model="gpt-4o-mini"):
            pass
        trace.event("llm.first_token", ttftMs=210.0)
        trace.event("caller.transcript", text="secret words from the caller")
        trace.attributes.update({"language": "en", "provider": "openai"})
        return trace.finish(action="hangup", sources=["hotel_policies.md#Pets"])

    def _export(self, payload):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        from aurora.telemetry.otel import export_payload

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        export_payload(payload, tracer_provider=provider)
        return exporter.get_finished_spans()

    def test_turn_maps_to_root_and_stage_spans(self):
        spans = self._export(self._finished_payload())
        names = {s.name for s in spans}
        self.assertIn("voice.turn", names)
        self.assertIn("voice.stt", names)
        self.assertIn("voice.llm", names)
        root = next(s for s in spans if s.name == "voice.turn")
        self.assertEqual(root.attributes["voice.session_id"], "s-1")
        self.assertEqual(root.attributes["voice.action"], "hangup")
        stage = next(s for s in spans if s.name == "voice.llm")
        self.assertEqual(stage.parent.span_id, root.context.span_id)
        self.assertGreaterEqual(root.end_time, root.start_time)

    def test_notable_events_land_on_the_root_span(self):
        spans = self._export(self._finished_payload())
        root = next(s for s in spans if s.name == "voice.turn")
        self.assertIn("llm.first_token", {e.name for e in root.events})

    def test_redaction_survives_export(self):
        spans = self._export(self._finished_payload())
        blob = repr(spans)
        self.assertNotIn("secret words", blob)   # content omitted before export


class OtlpHeadersParsingTests(unittest.TestCase):
    """TELEMETRY_OTLP_HEADERS (goal.md ADR-019) — generic so telemetry_otel.py
    stays vendor-neutral (ADR-009); Opik just happens to need three of them
    (Authorization, projectName, Comet-Workspace)."""

    def test_empty_string_yields_no_headers(self):
        from aurora.telemetry.otel import _parse_otlp_headers
        self.assertEqual(_parse_otlp_headers(""), {})

    def test_parses_multiple_key_value_pairs(self):
        from aurora.telemetry.otel import _parse_otlp_headers
        raw = "Authorization=secret-key,projectName=aurora-hotel,Comet-Workspace=my-workspace"
        self.assertEqual(_parse_otlp_headers(raw), {
            "Authorization": "secret-key",
            "projectName": "aurora-hotel",
            "Comet-Workspace": "my-workspace",
        })

    def test_tolerates_surrounding_whitespace(self):
        from aurora.telemetry.otel import _parse_otlp_headers
        raw = " Authorization = secret-key , projectName = aurora-hotel "
        self.assertEqual(_parse_otlp_headers(raw), {
            "Authorization": "secret-key",
            "projectName": "aurora-hotel",
        })

    def test_value_may_itself_contain_an_equals_sign(self):
        from aurora.telemetry.otel import _parse_otlp_headers
        self.assertEqual(
            _parse_otlp_headers("Authorization=abc=def"),
            {"Authorization": "abc=def"},
        )

    def test_malformed_entry_without_equals_is_skipped(self):
        from aurora.telemetry.otel import _parse_otlp_headers
        self.assertEqual(_parse_otlp_headers("not-well-formed,projectName=x"), {"projectName": "x"})


class SloReportTests(unittest.TestCase):
    def _payloads(self):
        def turn(total, action=None, events=(), timings=None):
            return {
                "sessionId": "s", "turnId": "t", "totalMs": total,
                "timings": timings or {"llm": total / 2},
                "attributes": {"action": action},
                "events": [{"name": n, "offsetMs": 0.0, "attributes": {}} for n in events],
            }

        return [
            turn(400),
            turn(500, events=("latency.filler_played",)),
            turn(600, action="transfer"),
            turn(700, events=("turn.cancelled",)),
            turn(3000, action="hangup", events=("llm.fallback",)),
        ]

    def test_report_computes_percentiles_and_rates(self):
        from aurora.ops.slo_report import compute

        report = compute(self._payloads())
        self.assertEqual(report["turns"], 5)
        self.assertEqual(report["p50TotalMs"], 600)
        self.assertEqual(report["p95TotalMs"], 3000)
        self.assertAlmostEqual(report["transferRate"], 0.2)
        self.assertAlmostEqual(report["bargeInRate"], 0.2)
        self.assertAlmostEqual(report["fillerRate"], 0.2)
        self.assertAlmostEqual(report["fallbackRate"], 0.2)

    def test_check_mode_flags_slo_breaches(self):
        from aurora.ops.slo_report import breaches

        report = {"p95TotalMs": 3000, "transferRate": 0.2, "fallbackRate": 0.2}
        found = breaches(report, {"p95TotalMs": 800, "transferRate": 0.5})
        self.assertEqual(len(found), 1)
        self.assertIn("p95TotalMs", found[0])
        self.assertEqual(breaches(report, {"p95TotalMs": 5000}), [])


class BargeInCancellationTests(unittest.TestCase):
    def _many_chunk_provider(self):
        class ManyChunks(MockProvider):
            def __init__(self):
                super().__init__()
                self.chunks_served = 0
                self.stream_closed = False

            def stream_chat(self, messages, tools=None, tool_choice=None):
                try:
                    for i in range(10):
                        self.chunks_served += 1
                        yield _stream_chunk(content=f"piece{i} ")
                finally:
                    self.stream_closed = True

        return ManyChunks()

    def test_cancel_mid_stream_truncates_history_to_what_was_spoken(self):
        import threading

        provider = self._many_chunk_provider()
        agent = Agent(provider)
        cancel = threading.Event()
        trace = TurnTrace(session_id="t", turn_id="barge-1")
        heard = []
        for piece in agent.respond_stream("Hello", trace=trace, cancel=cancel):
            heard.append(piece)
            if len(heard) == 2:
                cancel.set()          # caller starts talking over the agent
        self.assertEqual(heard, ["piece0 ", "piece1 "])
        self.assertTrue(provider.stream_closed)          # stop paying for tokens
        self.assertLess(provider.chunks_served, 10)
        self.assertEqual(agent.messages[-1]["role"], "assistant")
        self.assertEqual(agent.messages[-1]["content"], "piece0 piece1")
        self.assertIn("turn.cancelled", [e["name"] for e in trace.events])
        self.assertIsNone(agent.last_action)

    def test_cancel_before_first_model_call_makes_no_request(self):
        import threading

        provider = self._many_chunk_provider()
        agent = Agent(provider)
        cancel = threading.Event()
        cancel.set()
        pieces = list(agent.respond_stream("Hello", cancel=cancel))
        self.assertEqual(pieces, [])
        self.assertEqual(provider.chunks_served, 0)

    def test_cancel_mid_tool_batch_keeps_history_openai_valid(self):
        import threading

        class TwoToolProvider(MockProvider):
            def __init__(self):
                super().__init__()
                self.turns = 0

            def stream_chat(self, messages, tools=None, tool_choice=None):
                self.turns += 1
                if self.turns == 1:
                    yield _stream_chunk(tool_calls=[
                        _tool_call_delta(0, call_id="c1", name="search_hotel_knowledge",
                                         arguments='{"query": "pets"}'),
                        _tool_call_delta(1, call_id="c2", name="get_room_service_hours",
                                         arguments="{}"),
                    ])
                else:
                    yield _stream_chunk(content="reply")

        provider = TwoToolProvider()
        agent = Agent(provider)
        cancel = threading.Event()
        trace = TurnTrace(session_id="t", turn_id="barge-2")

        original = agent._run_tool_calls

        def cancel_after_first(tool_calls, tr, user_text, cancel_event=None):
            cancel.set()  # interruption arrives while tools are running
            return original(tool_calls, tr, user_text, cancel_event)

        agent._run_tool_calls = cancel_after_first
        pieces = list(agent.respond_stream("pets and room service?", trace=trace, cancel=cancel))
        self.assertEqual(pieces, [])                     # never spoke
        self.assertEqual(provider.turns, 1)              # no follow-up LLM call
        tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
        tool_call_ids = {"c1", "c2"}
        self.assertEqual({m["tool_call_id"] for m in tool_messages[-2:]}, tool_call_ids)

    def test_unset_cancel_event_changes_nothing(self):
        import threading

        provider = self._many_chunk_provider()
        agent = Agent(provider)
        pieces = list(agent.respond_stream("Hello", cancel=threading.Event()))
        self.assertEqual(len(pieces), 10)


class KnowledgeSnapshotTests(unittest.TestCase):
    def _snapshot_root(self):
        import json
        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp())
        for stamp, policy in (
            ("2026-01-01", "Self-parking costs $10 per night."),
            ("2026-02-02", "Self-parking costs $99 per night."),
        ):
            snapshot = root / stamp
            snapshot.mkdir()
            (snapshot / "hotel_policies.md").write_text(
                f"# Policies\n\n## Parking\n\n{policy}\n", encoding="utf-8"
            )
            (snapshot / "manifest.json").write_text(
                json.dumps({"snapshot": stamp, "files": ["hotel_policies.md"]}),
                encoding="utf-8",
            )
        return root

    def test_latest_snapshot_wins(self):
        from aurora.core.knowledge import KnowledgeBase
        kb = KnowledgeBase(self._snapshot_root())
        self.assertEqual(kb.snapshot, "2026-02-02")
        self.assertIn("$99", kb.search("parking")[0]["text"])

    def test_pinned_snapshot_rolls_back(self):
        from aurora.core.knowledge import KnowledgeBase
        kb = KnowledgeBase(self._snapshot_root(), snapshot="2026-01-01")
        self.assertEqual(kb.snapshot, "2026-01-01")
        self.assertIn("$10", kb.search("parking")[0]["text"])

    def test_invalid_pin_raises_clearly(self):
        from aurora.core.knowledge import KnowledgeBase
        with self.assertRaises(ValueError):
            KnowledgeBase(self._snapshot_root(), snapshot="1999-01-01")

    def test_manifest_is_authoritative(self):
        from aurora.core.knowledge import KnowledgeBase
        root = self._snapshot_root()
        (root / "2026-02-02" / "rogue.md").write_text(
            "## Secret\n\nRogue unreviewed content about parking.\n", encoding="utf-8"
        )
        kb = KnowledgeBase(root)
        self.assertFalse(any(c["source"] == "rogue.md" for c in kb.chunks))

    def test_loose_files_still_work_without_snapshots(self):
        import tempfile
        from pathlib import Path
        from aurora.core.knowledge import KnowledgeBase

        root = Path(tempfile.mkdtemp())
        (root / "hotel_policies.md").write_text(
            "# Policies\n\n## Parking\n\nSelf-parking costs $28 per night.\n",
            encoding="utf-8",
        )
        kb = KnowledgeBase(root)
        self.assertEqual(kb.snapshot, "unversioned")
        self.assertIn("$28", kb.search("parking")[0]["text"])

    def test_repo_knowledge_loads_from_a_snapshot(self):
        from aurora.core.knowledge import KNOWLEDGE_BASE
        self.assertNotEqual(KNOWLEDGE_BASE.snapshot, "unversioned")


class LatencyFillerTests(unittest.TestCase):
    def test_filler_plays_when_turn_exceeds_threshold(self):
        import time
        from aurora.voice.loop import LatencyFiller

        spoken = []
        trace = TurnTrace(session_id="t", turn_id="slow")
        filler = LatencyFiller(spoken.append, threshold_ms=30)
        filler.start(trace, "en")
        time.sleep(0.12)                      # the "slow tool call"
        filler.stop()
        self.assertEqual(spoken, ["Thanks for waiting, I'm working on that for you."])
        self.assertTrue(filler.played)
        events = [e["name"] for e in trace.events]
        self.assertIn("latency.filler_played", events)

    def test_filler_skipped_when_turn_is_fast(self):
        from aurora.voice.loop import LatencyFiller

        spoken = []
        trace = TurnTrace(session_id="t", turn_id="fast")
        filler = LatencyFiller(spoken.append, threshold_ms=200)
        filler.start(trace, "en")
        filler.stop()                          # turn finished immediately
        self.assertEqual(spoken, [])
        self.assertFalse(filler.played)
        self.assertNotIn("latency.filler_played", [e["name"] for e in trace.events])

    def test_filler_speaks_session_language(self):
        import time
        from aurora.voice.loop import LatencyFiller

        spoken = []
        trace = TurnTrace(session_id="t", turn_id="slow-fr")
        filler = LatencyFiller(spoken.append, threshold_ms=30)
        filler.start(trace, "fr")
        time.sleep(0.12)
        filler.stop()
        self.assertEqual(spoken, ["Merci de patienter, je m'en occupe."])

    def test_filler_disabled_with_zero_threshold(self):
        import time
        from aurora.voice.loop import LatencyFiller

        spoken = []
        trace = TurnTrace(session_id="t", turn_id="disabled")
        filler = LatencyFiller(spoken.append, threshold_ms=0)
        filler.start(trace, "en")
        time.sleep(0.05)
        filler.stop()
        self.assertEqual(spoken, [])


class SpokenTextTests(unittest.TestCase):
    def test_markdown_bullets_and_emphasis_stripped(self):
        from aurora.core.spoken_text import normalize_spoken_text
        raw = "- **Check-in** is at 3 PM\n- Check-out is at `11 AM`"
        self.assertEqual(
            normalize_spoken_text(raw),
            "Check-in is at 3 PM Check-out is at 11 AM",
        )

    def test_numbered_lists_and_headers_stripped(self):
        from aurora.core.spoken_text import normalize_spoken_text
        raw = "## Hours\n1. Breakfast at 6:30 AM\n2. Dinner at 5 PM"
        self.assertEqual(
            normalize_spoken_text(raw),
            "Hours Breakfast at 6:30 AM Dinner at 5 PM",
        )

    def test_em_dashes_become_commas_but_word_hyphens_survive(self):
        from aurora.core.spoken_text import normalize_spoken_text
        raw = "Parking — $28 per night — includes in-room check-in"
        self.assertEqual(
            normalize_spoken_text(raw),
            "Parking, $28 per night, includes in-room check-in",
        )

    def test_markdown_links_keep_only_the_label(self):
        from aurora.core.spoken_text import normalize_spoken_text
        self.assertEqual(
            normalize_spoken_text("See [our policies](https://example.com/p)."),
            "See our policies.",
        )

    def test_whitespace_collapsed(self):
        from aurora.core.spoken_text import normalize_spoken_text
        self.assertEqual(
            normalize_spoken_text("Hello   there\n\n  caller"),
            "Hello there caller",
        )

    def test_chunk_short_text_is_single_chunk(self):
        from aurora.core.spoken_text import chunk_text
        self.assertEqual(chunk_text("Welcome to Aurora."), ["Welcome to Aurora."])
        self.assertEqual(chunk_text(""), [])

    def test_chunk_splits_at_sentence_boundaries_under_limit(self):
        from aurora.core.spoken_text import chunk_text
        text = " ".join(f"Sentence number {i} is here." for i in range(1, 21))
        chunks = chunk_text(text, max_chars=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 80 for c in chunks))
        self.assertTrue(all(c.endswith(".") for c in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_chunk_hard_splits_oversized_sentence(self):
        from aurora.core.spoken_text import chunk_text
        text = "word " * 100
        chunks = chunk_text(text.strip(), max_chars=40)
        self.assertTrue(all(len(c) <= 40 for c in chunks))

    def test_speak_normalizes_before_tts(self):
        from aurora.voice.loop import speak

        class RecordingTTSProvider(MockProvider):
            def __init__(self):
                super().__init__()
                self.spoken = []

            def synthesize(self, text):
                self.spoken.append(text)
                return None

        provider = RecordingTTSProvider()
        speak(provider, "**Booking confirmed** — code `AH-4827`")
        self.assertEqual(provider.spoken, ["Booking confirmed, code AH-4827"])


class PostgresConfigCheckTests(unittest.TestCase):
    def test_no_postgres_host_is_fine(self):
        from aurora.config.check import validate_config
        self.assertEqual(validate_config({"PROVIDER": "mock"}), [])

    def test_postgres_host_requires_user_password_db(self):
        from aurora.config.check import validate_config
        problems = validate_config({"PROVIDER": "mock", "POSTGRES_HOST": "db.example.com"})
        self.assertTrue(any("POSTGRES_USER" in p for p in problems))
        self.assertTrue(any("POSTGRES_PASSWORD" in p for p in problems))
        self.assertTrue(any("POSTGRES_DB" in p for p in problems))

    def test_fully_configured_postgres_passes(self):
        from aurora.config.check import validate_config
        problems = validate_config({
            "PROVIDER": "mock",
            "POSTGRES_HOST": "db.example.com",
            "POSTGRES_USER": "u",
            "POSTGRES_PASSWORD": "p",
            "POSTGRES_DB": "d",
        })
        self.assertEqual(problems, [])

    def test_bad_postgres_port_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({
            "PROVIDER": "mock",
            "POSTGRES_HOST": "db.example.com",
            "POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p", "POSTGRES_DB": "d",
            "POSTGRES_PORT": "not-a-port",
        })
        self.assertTrue(any("POSTGRES_PORT" in p for p in problems))


class EnvFilePermissionsTests(unittest.TestCase):
    def test_world_readable_env_file_flagged(self):
        import tempfile
        from pathlib import Path
        from aurora.config.check import check_env_file_permissions

        with tempfile.NamedTemporaryFile(suffix=".env", delete=False) as f:
            path = Path(f.name)
        try:
            path.chmod(0o644)
            problems = check_env_file_permissions(path)
            self.assertTrue(any("readable" in p.lower() or "permissions" in p.lower()
                               for p in problems))
        finally:
            path.unlink()

    def test_owner_only_env_file_passes(self):
        import tempfile
        from pathlib import Path
        from aurora.config.check import check_env_file_permissions

        with tempfile.NamedTemporaryFile(suffix=".env", delete=False) as f:
            path = Path(f.name)
        try:
            path.chmod(0o600)
            self.assertEqual(check_env_file_permissions(path), [])
        finally:
            path.unlink()

    def test_missing_env_file_is_not_a_problem(self):
        from pathlib import Path
        from aurora.config.check import check_env_file_permissions
        self.assertEqual(check_env_file_permissions(Path("/nonexistent/.env")), [])


class ConfigCheckTests(unittest.TestCase):
    def test_mock_provider_needs_no_keys(self):
        from aurora.config.check import validate_config
        self.assertEqual(validate_config({"PROVIDER": "mock"}), [])

    def test_bad_livekit_token_ttl_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({
            "PROVIDER": "mock", "LIVEKIT_TOKEN_TTL_MINUTES": "not-a-number",
        })
        self.assertTrue(any("LIVEKIT_TOKEN_TTL_MINUTES" in p for p in problems))

    def test_invalid_provider_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({"PROVIDER": "banana"})
        self.assertTrue(any("PROVIDER" in p for p in problems))

    def test_malformed_otlp_headers_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({
            "PROVIDER": "mock", "TELEMETRY_OTLP_HEADERS": "no-equals-sign-here",
        })
        self.assertTrue(any("TELEMETRY_OTLP_HEADERS" in p for p in problems))

    def test_well_formed_otlp_headers_pass(self):
        from aurora.config.check import validate_config
        problems = validate_config({
            "PROVIDER": "mock",
            "TELEMETRY_OTLP_HEADERS": "Authorization=key,projectName=aurora-hotel",
        })
        self.assertFalse(any("TELEMETRY_OTLP_HEADERS" in p for p in problems))

    def test_opik_api_key_without_the_package_installed_is_flagged(self):
        from aurora.config.check import validate_config
        with patch.dict("sys.modules", {"opik": None}):
            problems = validate_config({"PROVIDER": "mock", "OPIK_API_KEY": "fake-key"})
        self.assertTrue(any("OPIK_API_KEY" in p for p in problems))

    def test_no_opik_api_key_is_fine_without_the_package(self):
        from aurora.config.check import validate_config
        with patch.dict("sys.modules", {"opik": None}):
            problems = validate_config({"PROVIDER": "mock"})
        self.assertFalse(any("OPIK_API_KEY" in p for p in problems))

    def test_live_provider_requires_matching_key(self):
        from aurora.config.check import validate_config
        problems = validate_config({"PROVIDER": "openai"})
        self.assertTrue(any("OPENAI_API_KEY" in p for p in problems))
        problems = validate_config({"PROVIDER": "groq", "GROQ_API_KEY": "gsk-x"})
        self.assertEqual(problems, [])

    def test_unwritable_telemetry_path_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({
            "PROVIDER": "mock",
            "TELEMETRY_JSONL": "/dev/null/nested/voice-events.jsonl",
        })
        self.assertTrue(any("TELEMETRY_JSONL" in p for p in problems))

    def test_unopenable_bookings_db_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({
            "PROVIDER": "mock",
            "BOOKINGS_DB": "/dev/null/nested/bookings.db",
        })
        self.assertTrue(any("BOOKINGS_DB" in p for p in problems))

    def test_bad_numeric_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({"PROVIDER": "mock", "ENDPOINT_SILENCE_MS": "fast"})
        self.assertTrue(any("ENDPOINT_SILENCE_MS" in p for p in problems))

    def test_bad_tts_backend_flagged(self):
        from aurora.config.check import validate_config
        problems = validate_config({"PROVIDER": "mock", "TTS_BACKEND": "cloud"})
        self.assertTrue(any("TTS_BACKEND" in p for p in problems))


class FailureFallbackTests(unittest.TestCase):
    class FailingChatProvider(MockProvider):
        def chat(self, *args, **kwargs):
            raise ConnectionError("provider unreachable")

    def test_llm_failure_returns_spoken_fallback_not_crash(self):
        agent = Agent(self.FailingChatProvider())
        trace = TurnTrace(session_id="t", turn_id="fail-1")
        reply, action = agent.respond("I need a room for two.", trace=trace)
        self.assertIn("trouble", reply.lower())
        self.assertIsNone(action)
        self.assertIn("llm.fallback", [e["name"] for e in trace.events])

    def test_repeated_llm_failures_transfer_to_human(self):
        agent = Agent(self.FailingChatProvider())
        agent.respond("Hello?")
        reply, action = agent.respond("Hello, are you there?")
        self.assertEqual(action, "transfer")
        self.assertIn("front desk", reply.lower())

    def test_llm_success_resets_failure_count(self):
        agent = Agent(MockProvider())
        agent._consecutive_llm_failures = 1
        agent.respond("What is the pet policy?")
        self.assertEqual(agent._consecutive_llm_failures, 0)

    def test_llm_failure_fallback_speaks_session_language(self):
        agent = Agent(MockProvider())
        agent.respond("Please speak Spanish.")
        agent.provider = self.FailingChatProvider()
        reply, action = agent.respond("Hola, ¿sigue ahí?")
        self.assertIn("repetirlo", reply)
        self.assertIsNone(action)

    def test_tts_failure_falls_back_without_crashing(self):
        from aurora.voice.loop import speak

        class FailingTTSProvider(MockProvider):
            def synthesize(self, text):
                raise RuntimeError("tts down")

        trace = TurnTrace(session_id="t", turn_id="tts-1")
        with patch.dict(os.environ, {"SYSTEM_TTS_CMD": "true"}):
            speak(FailingTTSProvider(), "Hello caller", trace=trace)  # must not raise
        self.assertIn("tts.fallback", [e["name"] for e in trace.events])

    def test_stt_failure_reprompts_once_then_transfers(self):
        from aurora.voice.loop import stt_failure_response

        message, transfer = stt_failure_response(1, "en")
        self.assertIn("trouble", message.lower())
        self.assertFalse(transfer)
        message, transfer = stt_failure_response(2, "fr")
        self.assertIn("réception", message)
        self.assertTrue(transfer)


class _BookingBackendContractTests:
    """Behavioral contract every BookingBackend must satisfy. Not a TestCase
    itself (no test runner discovery) — mixed into one concrete class per
    backend so both implementations are proven identical (goal.md ADR-013)."""

    def _backend(self):
        raise NotImplementedError

    def _details(self, **overrides):
        details = {
            "session_id": "session-a",
            "check_in": "August 12",
            "check_out": "August 14",
            "guests": 2,
            "room_type": "standard",
            "guest_name": "Priya Shah",
            "contact": "priya@example.com",
        }
        details.update(overrides)
        return details

    def test_identical_retry_returns_same_confirmation(self):
        backend = self._backend()
        first = backend.create_booking(**self._details())
        replay = backend.create_booking(**self._details())
        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first.confirmation_id, replay.confirmation_id)

    def test_distinct_bookings_get_unique_confirmations(self):
        backend = self._backend()
        first = backend.create_booking(**self._details())
        second = backend.create_booking(**self._details(guest_name="John Doe",
                                                        contact="john@example.com"))
        self.assertTrue(second.created)
        self.assertNotEqual(first.confirmation_id, second.confirmation_id)

    def test_confirmation_id_is_non_guessable_and_phone_safe(self):
        # goal.md ADR-014: no sequential counter (guessable/enumerable); a
        # fixed-format random code instead, using an alphabet with confusable
        # characters removed (0/O, 1/I/L) so it can be spoken and heard
        # correctly over a phone call.
        from aurora.storage.bookings import _CONFIRMATION_ALPHABET, _CONFIRMATION_CODE_LENGTH
        backend = self._backend()
        confirmation = backend.create_booking(**self._details()).confirmation_id
        self.assertTrue(confirmation.startswith("AH-"))
        code = confirmation[len("AH-"):]
        self.assertEqual(len(code), _CONFIRMATION_CODE_LENGTH)
        self.assertTrue(set(code) <= set(_CONFIRMATION_ALPHABET))
        self.assertNotIn("0", code)
        self.assertNotIn("1", code)

    def test_many_confirmations_have_no_collisions(self):
        backend = self._backend()
        codes = {
            backend.create_booking(**self._details(
                guest_name=f"Guest {i}", contact=f"guest{i}@example.com",
            )).confirmation_id
            for i in range(25)
        }
        self.assertEqual(len(codes), 25)

    def test_checkout_before_checkin_rejected(self):
        from aurora.storage.bookings import BookingValidationError
        backend = self._backend()
        with self.assertRaises(BookingValidationError):
            backend.create_booking(**self._details(check_in="August 14",
                                                   check_out="August 12"))

    def test_guests_over_capacity_rejected(self):
        from aurora.storage.bookings import BookingValidationError
        backend = self._backend()
        with self.assertRaises(BookingValidationError):
            backend.create_booking(**self._details(guests=6, room_type="standard"))

    def test_zero_guests_rejected(self):
        from aurora.storage.bookings import BookingValidationError
        backend = self._backend()
        with self.assertRaises(BookingValidationError):
            backend.create_booking(**self._details(guests=0))

    def test_unparseable_dates_are_accepted_leniently(self):
        backend = self._backend()
        record = backend.create_booking(**self._details(check_in="next Tuesday",
                                                        check_out="the Thursday after"))
        self.assertTrue(record.created)


class SqliteBookingBackendTests(_BookingBackendContractTests, unittest.TestCase):
    def _backend(self):
        from aurora.storage.bookings import SqliteBookingBackend
        return SqliteBookingBackend(":memory:")

    def test_confirmation_collision_retries_with_a_new_code(self):
        from aurora.storage.bookings import SqliteBookingBackend
        codes = iter(["AH-AAAAAA", "AH-AAAAAA", "AH-BBBBBB"])
        backend = SqliteBookingBackend(":memory:", id_generator=lambda: next(codes))
        first = backend.create_booking(**self._details())
        self.assertEqual(first.confirmation_id, "AH-AAAAAA")
        second = backend.create_booking(**self._details(
            guest_name="Other Guest", contact="other@example.com",
        ))
        self.assertEqual(second.confirmation_id, "AH-BBBBBB")


def _postgres_env_configured() -> bool:
    return bool(os.getenv("POSTGRES_HOST", "").strip())


@unittest.skipUnless(_postgres_env_configured(), "POSTGRES_HOST not configured; skipping live Postgres tests")
class PostgresBookingBackendTests(_BookingBackendContractTests, unittest.TestCase):
    """Runs for real against the configured Postgres instance (goal.md ADR-013).

    Uses a disposable, uniquely-named table — never the production `bookings`
    table — dropped and recreated per test for the same isolation SQLite's
    `:memory:` gives for free.
    """

    TABLE = "bookings_contract_test"

    def _backend(self, id_generator=None):
        from aurora.storage.bookings import PostgresBookingBackend
        kwargs = dict(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.environ["POSTGRES_DB"],
            table_name=self.TABLE,
        )
        if id_generator is not None:
            kwargs["id_generator"] = id_generator
        backend = PostgresBookingBackend(**kwargs)
        self.addCleanup(backend.close)
        return backend

    def test_confirmation_collision_retries_with_a_new_code(self):
        # Proves our retry logic against the REAL psycopg exception type, not
        # an assumption about what Postgres raises.
        codes = iter(["AH-AAAAAA", "AH-AAAAAA", "AH-BBBBBB"])
        backend = self._backend(id_generator=lambda: next(codes))
        first = backend.create_booking(**self._details())
        self.assertEqual(first.confirmation_id, "AH-AAAAAA")
        second = backend.create_booking(**self._details(
            guest_name="Other Guest", contact="other@example.com",
        ))
        self.assertEqual(second.confirmation_id, "AH-BBBBBB")

    def setUp(self):
        # Start each test from a clean, empty table (mirrors SQLite's fresh
        # :memory: db per test) so e.g. "first confirmation is AH-4827" holds.
        from aurora.storage.bookings import PostgresBookingBackend
        probe = PostgresBookingBackend(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.environ["POSTGRES_DB"],
            table_name=self.TABLE,
        )
        probe.reset_for_tests()
        probe.close()


class TelemetryTests(unittest.TestCase):
    def test_tool_and_language_events_are_visible(self):
        agent = Agent(make_provider("mock"))
        trace = TurnTrace(session_id="test", turn_id="policy")
        reply, action = agent.respond("What is the pet policy?", trace=trace)
        payload = trace.finish(action=action, sources=agent.last_sources)
        event_names = [event["name"] for event in payload["events"]]
        requested_tools = [
            event["attributes"].get("tool")
            for event in payload["events"]
            if event["name"] == "tool.requested"
        ]
        self.assertIn("two dogs", reply)
        self.assertIn("retrieval.completed", event_names)
        self.assertEqual(requested_tools, ["search_hotel_knowledge"])
        self.assertEqual(payload["attributes"]["language"], "en")

    def test_sensitive_tool_arguments_are_redacted(self):
        trace = TurnTrace(session_id="test", turn_id="redaction")
        trace.event("tool.requested", arguments={
            "guest_name": "Priya Shah",
            "contact": "priya@example.com",
            "check_in": "August 12",
        })
        attributes = trace.events[0]["attributes"]["arguments"]
        self.assertEqual(attributes["guest_name"], "[REDACTED]")
        self.assertEqual(attributes["contact"], "[REDACTED]")
        self.assertEqual(attributes["check_in"], "August 12")


class ScaleTests(unittest.TestCase):
    def test_one_million_dau_example(self):
        result = estimate_capacity(
            dau=1_000_000,
            calls_per_dau=0.25,
            duration_minutes=4,
            turns_per_minute=3,
            peak_factor=8,
            sessions_per_worker=40,
            headroom=0.30,
            cost_per_minute=0,
        )
        self.assertAlmostEqual(result["peakConcurrency"], 5555.6)
        self.assertEqual(result["workers"], 181)


class LoadTestPercentileTests(unittest.TestCase):
    """load_test.py (goal.md 4.3) drives real network calls against a live
    deployment — not exercised here — but its pure percentile math is."""

    def test_percentile_on_empty_list_is_zero(self):
        from aurora.ops.load_test import _percentile
        self.assertEqual(_percentile([], 0.95), 0.0)

    def test_median_of_five_values(self):
        from aurora.ops.load_test import _percentile
        self.assertEqual(_percentile([1, 2, 3, 4, 5], 0.5), 3)

    def test_p95_picks_a_high_but_not_the_max_value_for_a_large_sample(self):
        from aurora.ops.load_test import _percentile
        values = list(range(1, 101))  # 1..100
        self.assertEqual(_percentile(values, 0.95), 95)


if __name__ == "__main__":
    unittest.main()
