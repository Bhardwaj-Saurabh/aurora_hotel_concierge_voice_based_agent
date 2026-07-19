"""Focused offline tests for routing, grounding, telemetry, and capacity."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ["PROVIDER"] = "mock"
os.environ.setdefault("TTS_BACKEND", "print")

from agent import Agent, explicit_language_request, required_tool_for
from knowledge import search_hotel_knowledge
from providers import MockProvider, _env_or_default, _mk_tool, make_provider
from router import AgentRouter
from scale_check import estimate_capacity
from telemetry import TurnTrace


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
        from knowledge import KnowledgeBase
        kb = KnowledgeBase(self._snapshot_root())
        self.assertEqual(kb.snapshot, "2026-02-02")
        self.assertIn("$99", kb.search("parking")[0]["text"])

    def test_pinned_snapshot_rolls_back(self):
        from knowledge import KnowledgeBase
        kb = KnowledgeBase(self._snapshot_root(), snapshot="2026-01-01")
        self.assertEqual(kb.snapshot, "2026-01-01")
        self.assertIn("$10", kb.search("parking")[0]["text"])

    def test_invalid_pin_raises_clearly(self):
        from knowledge import KnowledgeBase
        with self.assertRaises(ValueError):
            KnowledgeBase(self._snapshot_root(), snapshot="1999-01-01")

    def test_manifest_is_authoritative(self):
        from knowledge import KnowledgeBase
        root = self._snapshot_root()
        (root / "2026-02-02" / "rogue.md").write_text(
            "## Secret\n\nRogue unreviewed content about parking.\n", encoding="utf-8"
        )
        kb = KnowledgeBase(root)
        self.assertFalse(any(c["source"] == "rogue.md" for c in kb.chunks))

    def test_loose_files_still_work_without_snapshots(self):
        import tempfile
        from pathlib import Path
        from knowledge import KnowledgeBase

        root = Path(tempfile.mkdtemp())
        (root / "hotel_policies.md").write_text(
            "# Policies\n\n## Parking\n\nSelf-parking costs $28 per night.\n",
            encoding="utf-8",
        )
        kb = KnowledgeBase(root)
        self.assertEqual(kb.snapshot, "unversioned")
        self.assertIn("$28", kb.search("parking")[0]["text"])

    def test_repo_knowledge_loads_from_a_snapshot(self):
        from knowledge import KNOWLEDGE_BASE
        self.assertNotEqual(KNOWLEDGE_BASE.snapshot, "unversioned")


class LatencyFillerTests(unittest.TestCase):
    def test_filler_plays_when_turn_exceeds_threshold(self):
        import time
        from voice_loop import LatencyFiller

        spoken = []
        trace = TurnTrace(session_id="t", turn_id="slow")
        filler = LatencyFiller(spoken.append, threshold_ms=30)
        filler.start(trace, "en")
        time.sleep(0.12)                      # the "slow tool call"
        filler.stop()
        self.assertEqual(spoken, ["One moment."])
        self.assertTrue(filler.played)
        events = [e["name"] for e in trace.events]
        self.assertIn("latency.filler_played", events)

    def test_filler_skipped_when_turn_is_fast(self):
        from voice_loop import LatencyFiller

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
        from voice_loop import LatencyFiller

        spoken = []
        trace = TurnTrace(session_id="t", turn_id="slow-fr")
        filler = LatencyFiller(spoken.append, threshold_ms=30)
        filler.start(trace, "fr")
        time.sleep(0.12)
        filler.stop()
        self.assertEqual(spoken, ["Un instant."])

    def test_filler_disabled_with_zero_threshold(self):
        import time
        from voice_loop import LatencyFiller

        spoken = []
        trace = TurnTrace(session_id="t", turn_id="disabled")
        filler = LatencyFiller(spoken.append, threshold_ms=0)
        filler.start(trace, "en")
        time.sleep(0.05)
        filler.stop()
        self.assertEqual(spoken, [])


class SpokenTextTests(unittest.TestCase):
    def test_markdown_bullets_and_emphasis_stripped(self):
        from spoken_text import normalize_spoken_text
        raw = "- **Check-in** is at 3 PM\n- Check-out is at `11 AM`"
        self.assertEqual(
            normalize_spoken_text(raw),
            "Check-in is at 3 PM Check-out is at 11 AM",
        )

    def test_numbered_lists_and_headers_stripped(self):
        from spoken_text import normalize_spoken_text
        raw = "## Hours\n1. Breakfast at 6:30 AM\n2. Dinner at 5 PM"
        self.assertEqual(
            normalize_spoken_text(raw),
            "Hours Breakfast at 6:30 AM Dinner at 5 PM",
        )

    def test_em_dashes_become_commas_but_word_hyphens_survive(self):
        from spoken_text import normalize_spoken_text
        raw = "Parking — $28 per night — includes in-room check-in"
        self.assertEqual(
            normalize_spoken_text(raw),
            "Parking, $28 per night, includes in-room check-in",
        )

    def test_markdown_links_keep_only_the_label(self):
        from spoken_text import normalize_spoken_text
        self.assertEqual(
            normalize_spoken_text("See [our policies](https://example.com/p)."),
            "See our policies.",
        )

    def test_whitespace_collapsed(self):
        from spoken_text import normalize_spoken_text
        self.assertEqual(
            normalize_spoken_text("Hello   there\n\n  caller"),
            "Hello there caller",
        )

    def test_chunk_short_text_is_single_chunk(self):
        from spoken_text import chunk_text
        self.assertEqual(chunk_text("Welcome to Aurora."), ["Welcome to Aurora."])
        self.assertEqual(chunk_text(""), [])

    def test_chunk_splits_at_sentence_boundaries_under_limit(self):
        from spoken_text import chunk_text
        text = " ".join(f"Sentence number {i} is here." for i in range(1, 21))
        chunks = chunk_text(text, max_chars=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 80 for c in chunks))
        self.assertTrue(all(c.endswith(".") for c in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_chunk_hard_splits_oversized_sentence(self):
        from spoken_text import chunk_text
        text = "word " * 100
        chunks = chunk_text(text.strip(), max_chars=40)
        self.assertTrue(all(len(c) <= 40 for c in chunks))

    def test_speak_normalizes_before_tts(self):
        from voice_loop import speak

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


class ConfigCheckTests(unittest.TestCase):
    def test_mock_provider_needs_no_keys(self):
        from config_check import validate_config
        self.assertEqual(validate_config({"PROVIDER": "mock"}), [])

    def test_invalid_provider_flagged(self):
        from config_check import validate_config
        problems = validate_config({"PROVIDER": "banana"})
        self.assertTrue(any("PROVIDER" in p for p in problems))

    def test_live_provider_requires_matching_key(self):
        from config_check import validate_config
        problems = validate_config({"PROVIDER": "openai"})
        self.assertTrue(any("OPENAI_API_KEY" in p for p in problems))
        problems = validate_config({"PROVIDER": "groq", "GROQ_API_KEY": "gsk-x"})
        self.assertEqual(problems, [])

    def test_unwritable_telemetry_path_flagged(self):
        from config_check import validate_config
        problems = validate_config({
            "PROVIDER": "mock",
            "TELEMETRY_JSONL": "/dev/null/nested/voice-events.jsonl",
        })
        self.assertTrue(any("TELEMETRY_JSONL" in p for p in problems))

    def test_unopenable_bookings_db_flagged(self):
        from config_check import validate_config
        problems = validate_config({
            "PROVIDER": "mock",
            "BOOKINGS_DB": "/dev/null/nested/bookings.db",
        })
        self.assertTrue(any("BOOKINGS_DB" in p for p in problems))

    def test_bad_numeric_flagged(self):
        from config_check import validate_config
        problems = validate_config({"PROVIDER": "mock", "ENDPOINT_SILENCE_MS": "fast"})
        self.assertTrue(any("ENDPOINT_SILENCE_MS" in p for p in problems))

    def test_bad_tts_backend_flagged(self):
        from config_check import validate_config
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
        from voice_loop import speak

        class FailingTTSProvider(MockProvider):
            def synthesize(self, text):
                raise RuntimeError("tts down")

        trace = TurnTrace(session_id="t", turn_id="tts-1")
        with patch.dict(os.environ, {"SYSTEM_TTS_CMD": "true"}):
            speak(FailingTTSProvider(), "Hello caller", trace=trace)  # must not raise
        self.assertIn("tts.fallback", [e["name"] for e in trace.events])

    def test_stt_failure_reprompts_once_then_transfers(self):
        from voice_loop import stt_failure_response

        message, transfer = stt_failure_response(1, "en")
        self.assertIn("trouble", message.lower())
        self.assertFalse(transfer)
        message, transfer = stt_failure_response(2, "fr")
        self.assertIn("réception", message)
        self.assertTrue(transfer)


class BookingBackendTests(unittest.TestCase):
    def _backend(self):
        from bookings import SqliteBookingBackend
        return SqliteBookingBackend(":memory:")

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

    def test_first_confirmation_matches_workshop_id(self):
        # Deterministic sequence keeps the smoke test and demo story stable.
        backend = self._backend()
        self.assertEqual(backend.create_booking(**self._details()).confirmation_id, "AH-4827")

    def test_checkout_before_checkin_rejected(self):
        from bookings import BookingValidationError
        backend = self._backend()
        with self.assertRaises(BookingValidationError):
            backend.create_booking(**self._details(check_in="August 14",
                                                   check_out="August 12"))

    def test_guests_over_capacity_rejected(self):
        from bookings import BookingValidationError
        backend = self._backend()
        with self.assertRaises(BookingValidationError):
            backend.create_booking(**self._details(guests=6, room_type="standard"))

    def test_zero_guests_rejected(self):
        from bookings import BookingValidationError
        backend = self._backend()
        with self.assertRaises(BookingValidationError):
            backend.create_booking(**self._details(guests=0))

    def test_unparseable_dates_are_accepted_leniently(self):
        backend = self._backend()
        record = backend.create_booking(**self._details(check_in="next Tuesday",
                                                        check_out="the Thursday after"))
        self.assertTrue(record.created)


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


if __name__ == "__main__":
    unittest.main()
