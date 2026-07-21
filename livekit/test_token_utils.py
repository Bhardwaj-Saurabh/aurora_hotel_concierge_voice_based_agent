"""Tests for least-privilege LiveKit token minting (goal.md ADR-015)."""

from __future__ import annotations

import os
import unittest

import jwt as pyjwt


def _decode(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False})


class TokenGrantsTests(unittest.TestCase):
    def _grants(self, room="test-room"):
        from token_utils import build_video_grants
        return build_video_grants(room)

    def test_caller_can_join_publish_and_subscribe(self):
        grants = self._grants()
        self.assertTrue(grants.room_join)
        self.assertTrue(grants.can_publish)
        self.assertTrue(grants.can_subscribe)
        self.assertEqual(grants.room, "test-room")

    def test_admin_and_management_grants_are_explicitly_denied(self):
        grants = self._grants()
        self.assertFalse(grants.room_create)
        self.assertFalse(grants.room_admin)
        self.assertFalse(grants.room_list)
        self.assertFalse(grants.room_record)
        self.assertFalse(grants.ingress_admin)
        self.assertFalse(grants.recorder)
        self.assertFalse(grants.can_manage_agent_session)

    def test_unused_capabilities_are_explicitly_denied(self):
        # The browser never uses LiveKit data channels or metadata updates;
        # deny by default rather than relying on the SDK's own defaults.
        grants = self._grants()
        self.assertFalse(grants.can_publish_data)
        self.assertFalse(grants.can_update_own_metadata)
        self.assertFalse(grants.hidden)


class TokenTtlTests(unittest.TestCase):
    def test_default_ttl_is_one_hour(self):
        from token_utils import token_ttl
        with_env_cleared = {k: v for k, v in os.environ.items() if k != "LIVEKIT_TOKEN_TTL_MINUTES"}
        import unittest.mock as mock
        with mock.patch.dict(os.environ, with_env_cleared, clear=True):
            self.assertEqual(token_ttl().total_seconds(), 60 * 60)

    def test_ttl_configurable_via_env(self):
        from token_utils import token_ttl
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"LIVEKIT_TOKEN_TTL_MINUTES": "15"}):
            self.assertEqual(token_ttl().total_seconds(), 15 * 60)

    def test_minted_token_actually_expires_around_the_configured_ttl(self):
        from token_utils import mint_token
        token = mint_token(
            api_key="devkey", api_secret="secret-at-least-32-characters-long!!",
            identity="caller-demo", name="Caller Demo", room="test-room",
            ttl_minutes=30,
        )
        payload = _decode(token)
        self.assertIn("exp", payload)
        # iat isn't always set by the SDK; just check exp is ~30 minutes out
        # from "now" within a generous tolerance for test execution time.
        import time
        self.assertAlmostEqual(payload["exp"], time.time() + 30 * 60, delta=30)

    def test_minted_token_carries_least_privilege_grants(self):
        from token_utils import mint_token
        token = mint_token(
            api_key="devkey", api_secret="secret-at-least-32-characters-long!!",
            identity="caller-demo", name="Caller Demo", room="test-room",
        )
        payload = _decode(token)
        grants = payload["video"]
        self.assertTrue(grants["roomJoin"])
        self.assertFalse(grants["roomCreate"])
        self.assertFalse(grants["roomAdmin"])


if __name__ == "__main__":
    unittest.main()
