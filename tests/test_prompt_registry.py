"""Tests for the Opik-backed prompt registry (goal.md Phase 4.5, ADR-011/019).

No live Opik account needed: opik.Opik is monkeypatched everywhere except the
one deliberately-skipped live smoke test. get_system_prompt() must NEVER
raise — a registry hiccup degrades to the caller-supplied local fallback,
never a dead call (goal.md 2.2 failure-handling principle).
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from aurora.prompt_registry import get_system_prompt

LOCAL_FALLBACK = "You are a friendly phone reservations agent."


class NoApiKeyTests(unittest.TestCase):
    def test_missing_api_key_returns_local_fallback_without_importing_opik(self):
        with patch.dict(os.environ, {"OPIK_API_KEY": ""}, clear=False):
            with patch("aurora.prompt_registry._opik_client") as client_factory:
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, LOCAL_FALLBACK)
        self.assertEqual(version, "local")
        client_factory.assert_not_called()


class OpikConfiguredTests(unittest.TestCase):
    def _fake_prompt(self, text: str, version: str):
        prompt = MagicMock()
        prompt.prompt = text
        prompt.version = version
        return prompt

    def test_production_environment_prompt_is_used_when_present(self):
        client = MagicMock()
        client.get_prompt.return_value = self._fake_prompt("Opik-managed prompt text", "v3")
        with patch.dict(os.environ, {"OPIK_API_KEY": "fake-key"}, clear=False):
            with patch("aurora.prompt_registry._opik_client", return_value=client):
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, "Opik-managed prompt text")
        self.assertEqual(version, "opik:v3")
        client.get_prompt.assert_called_once()
        self.assertEqual(client.get_prompt.call_args.kwargs.get("environment"), "production")

    def test_falls_back_to_latest_when_no_production_tag_exists(self):
        client = MagicMock()
        client.get_prompt.side_effect = [None, self._fake_prompt("Latest untagged prompt", "v1")]
        with patch.dict(os.environ, {"OPIK_API_KEY": "fake-key"}, clear=False):
            with patch("aurora.prompt_registry._opik_client", return_value=client):
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, "Latest untagged prompt")
        self.assertEqual(version, "opik:v1")
        self.assertEqual(client.get_prompt.call_count, 2)

    def test_no_prompt_found_at_all_falls_back_to_local(self):
        client = MagicMock()
        client.get_prompt.return_value = None
        with patch.dict(os.environ, {"OPIK_API_KEY": "fake-key"}, clear=False):
            with patch("aurora.prompt_registry._opik_client", return_value=client):
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, LOCAL_FALLBACK)
        self.assertEqual(version, "local-fallback")

    def test_client_exception_falls_back_to_local_and_never_raises(self):
        client = MagicMock()
        client.get_prompt.side_effect = RuntimeError("network is down")
        with patch.dict(os.environ, {"OPIK_API_KEY": "fake-key"}, clear=False):
            with patch("aurora.prompt_registry._opik_client", return_value=client):
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, LOCAL_FALLBACK)
        self.assertEqual(version, "local-fallback")

    def test_client_construction_failure_falls_back_to_local(self):
        with patch.dict(os.environ, {"OPIK_API_KEY": "fake-key"}, clear=False):
            with patch("aurora.prompt_registry._opik_client", side_effect=RuntimeError("bad config")):
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, LOCAL_FALLBACK)
        self.assertEqual(version, "local-fallback")


class VersionOverrideTests(unittest.TestCase):
    """promote_prompt.py (goal.md ADR-011/019 promotion gate) pins an exact
    candidate version for an eval run — bypassing the environment tag lookup
    entirely, since the candidate is (by definition) not yet tagged."""

    def test_override_fetches_the_exact_pinned_version(self):
        client = MagicMock()
        prompt = MagicMock()
        prompt.prompt = "Candidate v5 text"
        prompt.version = "v5"
        client.get_prompt.return_value = prompt
        env = {"OPIK_API_KEY": "fake-key", "OPIK_PROMPT_VERSION_OVERRIDE": "v5"}
        with patch.dict(os.environ, env, clear=False):
            with patch("aurora.prompt_registry._opik_client", return_value=client):
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, "Candidate v5 text")
        self.assertEqual(version, "opik:v5")
        client.get_prompt.assert_called_once_with(name="aurora-system-prompt", version="v5")

    def test_override_to_a_nonexistent_version_falls_back_to_local_not_latest(self):
        client = MagicMock()
        client.get_prompt.return_value = None
        env = {"OPIK_API_KEY": "fake-key", "OPIK_PROMPT_VERSION_OVERRIDE": "v999"}
        with patch.dict(os.environ, env, clear=False):
            with patch("aurora.prompt_registry._opik_client", return_value=client):
                text, version = get_system_prompt(LOCAL_FALLBACK)
        self.assertEqual(text, LOCAL_FALLBACK)
        self.assertEqual(version, "local-fallback")
        client.get_prompt.assert_called_once()  # never fell through to a second (latest) call


if __name__ == "__main__":
    unittest.main()
