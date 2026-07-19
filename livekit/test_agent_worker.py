"""Offline tests for the room-native agent worker adapter (goal.md 3.1)."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

os.environ.setdefault("PROVIDER", "mock")
os.environ.setdefault("TTS_BACKEND", "print")


def _chat_ctx(*turns: tuple[str, str]):
    from livekit.agents import llm

    ctx = llm.ChatContext.empty()
    for role, text in turns:
        ctx.add_message(role=role, content=text)
    return ctx


def _room_agent():
    import agent_worker
    from agent import Agent
    from providers import make_provider

    class Recording(agent_worker.AuroraRoomAgent):
        def __init__(self):
            super().__init__(Agent(make_provider("mock")), session_id="test-room")
            self.finished_with = []

        def _schedule_finish(self, action):
            self.finished_with.append(action)

    return Recording()


class LatestUserTextTests(unittest.TestCase):
    def test_reads_last_user_message_not_assistant(self):
        import agent_worker

        ctx = _chat_ctx(
            ("user", "What is the pet policy?"),
            ("assistant", "Two dogs are allowed."),
            ("user", "And parking?"),
        )
        self.assertEqual(agent_worker._latest_user_text(ctx), "And parking?")

    def test_empty_context_is_empty_string(self):
        import agent_worker

        self.assertEqual(agent_worker._latest_user_text(_chat_ctx()), "")


class RoomAgentAdapterTests(unittest.TestCase):
    def test_llm_node_answers_through_the_pipeline_brain(self):
        agent = _room_agent()
        reply = asyncio.run(
            agent.llm_node(_chat_ctx(("user", "What is the pet policy?")), [], None)
        )
        self.assertIn("two dogs", reply.lower())
        self.assertEqual(agent.finished_with, [])

    def test_llm_node_schedules_teardown_on_hangup(self):
        agent = _room_agent()
        reply = asyncio.run(agent.llm_node(_chat_ctx(("user", "Goodbye")), [], None))
        self.assertTrue(reply)
        self.assertEqual(agent.finished_with, ["hangup"])

    def test_llm_node_schedules_teardown_on_transfer(self):
        agent = _room_agent()
        asyncio.run(agent.llm_node(_chat_ctx(("user", "Connect me to a person")), [], None))
        self.assertEqual(agent.finished_with, ["transfer"])

    def test_language_switch_flows_through_the_brain(self):
        agent = _room_agent()
        reply = asyncio.run(
            agent.llm_node(_chat_ctx(("user", "Please speak Spanish.")), [], None)
        )
        self.assertIn("Claro", reply)


class WorkerConfigTests(unittest.TestCase):
    def test_mock_provider_is_rejected_for_live_rooms(self):
        import agent_worker

        with self.assertRaises(SystemExit):
            agent_worker._require_live_provider("mock")

    def test_live_providers_accepted(self):
        import agent_worker

        agent_worker._require_live_provider("openai")
        agent_worker._require_live_provider("groq")


if __name__ == "__main__":
    unittest.main()
