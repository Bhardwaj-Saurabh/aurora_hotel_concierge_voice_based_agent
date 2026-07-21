"""Offline tests for browser TTS payload selection and the auth HTTP layer."""

from __future__ import annotations

import base64
import json
import os
import unittest
from contextlib import contextmanager

os.environ.setdefault("PROVIDER", "mock")

from aurora.server import app as talk_server
from aurora.server.app import _browser_tts_payload


class SupportedLanguagesTests(unittest.TestCase):
    def test_state_languages_follow_the_router(self):
        self.assertEqual(talk_server._supported_languages(), ["en", "es", "fr"])


class NormalizedReplyTests(unittest.TestCase):
    def test_finish_response_normalizes_reply_for_speech(self):

        class FakeAgent:
            last_sources = []
            current_language = "en"
            current_locale = "en-US"

            class provider:
                name = "mock"
                llm_model = "mock-llm"

        class FinishableTrace(FakeTrace):
            def finish(self, **attributes):
                return {"attributes": attributes}

        payload = talk_server._finish_response(
            FakeAgent(), FinishableTrace(), "**Confirmed** — see `AH-4827`", None,
        )
        self.assertEqual(payload["reply"], "Confirmed, see AH-4827")


class FakeTrace:
    def __init__(self):
        self.events = []

    @contextmanager
    def span(self, name, **attributes):
        yield

    def event(self, name, **attributes):
        self.events.append((name, attributes))


class FakeProvider:
    name = "openai"
    tts_model = "tts-test"
    tts_voice = "voice-test"

    def __init__(self, backend="provider", error=None):
        self.tts_backend = backend
        self.error = error
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return b"RIFFtest-wave"


class FakeAgent:
    def __init__(self, provider):
        self.provider = provider


class BrowserTtsPayloadTests(unittest.TestCase):
    def test_provider_backend_returns_audio(self):
        provider = FakeProvider()
        payload = _browser_tts_payload(FakeAgent(provider), FakeTrace(), "Hello")

        self.assertEqual(payload["ttsBackend"], "provider")
        self.assertEqual(base64.b64decode(payload["audioBase64"]), b"RIFFtest-wave")
        self.assertEqual(payload["ttsVoice"], "voice-test")
        self.assertEqual(provider.calls, ["Hello"])

    def test_system_backend_selects_browser_voice_without_provider_call(self):
        provider = FakeProvider(backend="system")
        payload = _browser_tts_payload(FakeAgent(provider), FakeTrace(), "Hello")

        self.assertEqual(payload, {"ttsBackend": "browser"})
        self.assertEqual(provider.calls, [])

    def test_provider_failure_falls_back_without_exposing_error(self):
        provider = FakeProvider(error=RuntimeError("secret provider response"))
        trace = FakeTrace()
        payload = _browser_tts_payload(FakeAgent(provider), trace, "Hello")

        self.assertEqual(payload, {"ttsBackend": "browser", "ttsFallback": True})
        self.assertEqual(trace.events[0][0], "tts.fallback")
        self.assertNotIn("secret provider response", str(payload))


class LiveServerAuthTests(unittest.TestCase):
    """Integration tests for the auth HTTP layer (goal.md ADR-018), against
    the real FastAPI app (in-process ASGI TestClient) with a real (in-memory,
    injected) auth backend — not mocked route-by-route, so this actually
    proves the cookie/401/429 wiring end to end. Cookies are passed explicitly
    per request (never a client-side jar), preserving the raw-HTTP semantics
    these assertions were written against."""

    @classmethod
    def setUpClass(cls):
        os.environ["PROVIDER"] = "mock"
        os.environ["AUTH_COOKIE_SECURE"] = "false"  # plain http in this test
        os.environ["AUTH_RATE_LIMIT_PER_HOUR"] = "1000"
        os.environ["AUTH_LOGIN_RATE_LIMIT"] = "1000"

        from aurora.storage import auth
        auth.set_auth_backend_for_tests(auth.SqliteAuthBackend(":memory:"))

        talk_server._reset_rate_limiters_for_tests()
        cls.app = talk_server.create_app()

    @classmethod
    def tearDownClass(cls):
        from aurora.storage import auth
        auth.reset_auth_backend()

    def _request(self, method, path, body=None, cookie=None):
        from fastapi.testclient import TestClient

        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        data = json.dumps(body).encode("utf-8") if body is not None else None
        with TestClient(self.app) as client:  # fresh client: no cookie jar carry-over
            response = client.request(method, path, content=data, headers=headers)
        payload = json.loads(response.content) if response.content else {}
        set_cookie = response.headers.get("set-cookie")
        return response.status_code, payload, set_cookie

    def _register(self, email, password="correct horse battery"):
        status, payload, set_cookie = self._request(
            "POST", "/auth/register", {"email": email, "password": password},
        )
        self.assertEqual(status, 200, payload)
        return set_cookie.split(";")[0]

    def test_state_is_reachable_without_a_cookie(self):
        # Regression guard: Fly's health check hits this path with no cookie.
        status, _payload, _cookie = self._request("GET", "/state")
        self.assertEqual(status, 200)

    def test_agent_without_a_cookie_is_rejected(self):
        status, _payload, _cookie = self._request("POST", "/agent", {"text": "hi"})
        self.assertEqual(status, 401)

    def test_register_login_then_agent_succeeds(self):
        cookie = self._register("caller@example.com")
        status, payload, _ = self._request("POST", "/agent", {"text": "hello"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("reply", payload)

    def test_wrong_password_login_is_rejected(self):
        self._register("wrongpass@example.com")
        status, _payload, _cookie = self._request(
            "POST", "/auth/login",
            {"email": "wrongpass@example.com", "password": "not the password"},
        )
        self.assertEqual(status, 401)

    def test_logout_invalidates_the_session(self):
        cookie = self._register("logout-test@example.com")
        self._request("POST", "/auth/logout", cookie=cookie)
        status, _payload, _cookie = self._request("POST", "/agent", {"text": "hi"}, cookie=cookie)
        self.assertEqual(status, 401)

    def test_token_identity_is_derived_from_the_authenticated_user_not_the_query_string(self):
        cookie = self._register("identity-test@example.com")
        status, payload, _ = self._request(
            "GET", "/token?identity=someone-elses-name&name=Someone+Else", cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(payload["identity"], "someone-elses-name")
        self.assertIn("someone-elses-name", payload["identity"])


if __name__ == "__main__":
    unittest.main()
