"""Tests for the user-auth backend (goal.md ADR-018).

Closes ADR-015's documented, previously-accepted gap: talk-server's /token
and /agent endpoints had no caller authentication. SqliteAuthBackend exists
for tests only (goal.md ADR-018 deliberately does NOT extend bookings.py's
file-durability affordance to credentials/sessions) — PostgresAuthBackend is
the only backend ever used outside a test run.
"""

from __future__ import annotations

import os
import time
import unittest

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass


class _AuthBackendContractTests:
    """Behavioral contract every auth backend must satisfy (mirrors bookings.py's
    _BookingBackendContractTests mixin, goal.md ADR-007/018)."""

    def _backend(self):
        raise NotImplementedError

    def test_register_then_login_succeeds(self):
        backend = self._backend()
        user_id = backend.register_user("guest@example.com", "correct horse battery")
        self.assertIsNotNone(user_id)
        logged_in_id = backend.verify_credentials("guest@example.com", "correct horse battery")
        self.assertEqual(logged_in_id, user_id)

    def test_duplicate_email_is_rejected(self):
        from aurora.storage.auth import AuthValidationError
        backend = self._backend()
        backend.register_user("guest@example.com", "correct horse battery")
        with self.assertRaises(AuthValidationError):
            backend.register_user("guest@example.com", "a different password")

    def test_wrong_password_is_rejected(self):
        backend = self._backend()
        backend.register_user("guest@example.com", "correct horse battery")
        self.assertIsNone(backend.verify_credentials("guest@example.com", "wrong password"))

    def test_unknown_email_is_rejected(self):
        backend = self._backend()
        self.assertIsNone(backend.verify_credentials("nobody@example.com", "whatever"))

    def test_password_too_short_is_rejected(self):
        from aurora.storage.auth import AuthValidationError
        backend = self._backend()
        with self.assertRaises(AuthValidationError):
            backend.register_user("guest@example.com", "short")

    def test_malformed_email_is_rejected(self):
        from aurora.storage.auth import AuthValidationError
        backend = self._backend()
        with self.assertRaises(AuthValidationError):
            backend.register_user("not-an-email", "correct horse battery")

    def test_session_created_and_resolved_to_the_right_user(self):
        backend = self._backend()
        user_id = backend.register_user("guest@example.com", "correct horse battery")
        token = backend.create_session(user_id, ttl_seconds=3600)
        self.assertEqual(backend.resolve_session(token), user_id)

    def test_expired_session_is_rejected(self):
        backend = self._backend()
        user_id = backend.register_user("guest@example.com", "correct horse battery")
        token = backend.create_session(user_id, ttl_seconds=0)
        time.sleep(0.01)
        self.assertIsNone(backend.resolve_session(token))

    def test_unknown_token_is_rejected(self):
        backend = self._backend()
        self.assertIsNone(backend.resolve_session("not-a-real-token"))

    def test_logout_invalidates_the_session(self):
        backend = self._backend()
        user_id = backend.register_user("guest@example.com", "correct horse battery")
        token = backend.create_session(user_id, ttl_seconds=3600)
        backend.revoke_session(token)
        self.assertIsNone(backend.resolve_session(token))

    def test_disabled_user_cannot_log_in(self):
        backend = self._backend()
        user_id = backend.register_user("guest@example.com", "correct horse battery")
        backend.set_active(user_id, False)
        self.assertIsNone(backend.verify_credentials("guest@example.com", "correct horse battery"))

    def test_disabled_user_existing_sessions_stop_resolving(self):
        backend = self._backend()
        user_id = backend.register_user("guest@example.com", "correct horse battery")
        token = backend.create_session(user_id, ttl_seconds=3600)
        backend.set_active(user_id, False)
        self.assertIsNone(backend.resolve_session(token))

    def test_change_password_requires_current_password(self):
        backend = self._backend()
        user_id = backend.register_user("guest@example.com", "correct horse battery")
        self.assertFalse(backend.change_password(user_id, "wrong current", "new password 2"))
        self.assertTrue(backend.change_password(user_id, "correct horse battery", "new password 2"))
        self.assertIsNone(backend.verify_credentials("guest@example.com", "correct horse battery"))
        self.assertEqual(
            backend.verify_credentials("guest@example.com", "new password 2"), user_id,
        )


class SqliteAuthBackendTests(_AuthBackendContractTests, unittest.TestCase):
    def _backend(self):
        from aurora.storage.auth import SqliteAuthBackend
        return SqliteAuthBackend(":memory:")


def _postgres_env_configured() -> bool:
    return bool(os.getenv("POSTGRES_HOST", "").strip())


@unittest.skipUnless(_postgres_env_configured(), "POSTGRES_HOST not configured; skipping live Postgres tests")
class PostgresAuthBackendTests(_AuthBackendContractTests, unittest.TestCase):
    """Runs for real against the configured Postgres instance (goal.md ADR-018).

    Uses disposable, uniquely-named tables — never the production auth_users/
    auth_sessions tables — dropped and recreated per test.
    """

    USERS_TABLE = "auth_users_contract_test"
    SESSIONS_TABLE = "auth_sessions_contract_test"

    def _backend(self):
        from aurora.storage.auth import PostgresAuthBackend
        backend = PostgresAuthBackend(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.environ["POSTGRES_DB"],
            users_table=self.USERS_TABLE,
            sessions_table=self.SESSIONS_TABLE,
        )
        self.addCleanup(backend.close)
        return backend

    def setUp(self):
        from aurora.storage.auth import PostgresAuthBackend
        probe = PostgresAuthBackend(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.environ["POSTGRES_DB"],
            users_table=self.USERS_TABLE,
            sessions_table=self.SESSIONS_TABLE,
        )
        probe.reset_for_tests()
        probe.close()


class TimingSafeLoginTests(unittest.TestCase):
    """goal.md ADR-018: unknown-email and wrong-password failures must look
    the same from the outside — no early-return before a hash comparison.
    Not a wall-clock timing assertion (too flaky in CI); instead proves the
    structural guarantee: an unknown email still runs a real hash verify."""

    def test_unknown_email_still_runs_a_hash_verification(self):
        from unittest.mock import patch
        from aurora.storage import auth
        from aurora.storage.auth import SqliteAuthBackend

        backend = SqliteAuthBackend(":memory:")
        backend.register_user("guest@example.com", "correct horse battery")

        with patch.object(auth.PasswordHasher, "verify", wraps=auth.PasswordHasher().verify) as spy:
            self.assertIsNone(backend.verify_credentials("nobody@example.com", "wrong password"))
            self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
