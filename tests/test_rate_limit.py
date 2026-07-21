"""Tests for the sliding-window rate limiter (goal.md ADR-018).

Two independent uses share this one pure, clock-injectable class: a post-auth
per-user cost limiter and a pre-auth per-(ip, email) brute-force limiter on
/auth/login and /auth/register. No time.sleep — a fake clock makes the window
roll forward deterministically.
"""

from __future__ import annotations

import unittest

from aurora.rate_limit import SlidingWindowRateLimiter


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlidingWindowRateLimiterTests(unittest.TestCase):
    def test_allows_requests_under_the_limit(self):
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60, clock=clock)
        self.assertTrue(limiter.allow("user-1"))
        self.assertTrue(limiter.allow("user-1"))
        self.assertTrue(limiter.allow("user-1"))

    def test_blocks_once_the_limit_is_exceeded(self):
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60, clock=clock)
        self.assertTrue(limiter.allow("user-1"))
        self.assertTrue(limiter.allow("user-1"))
        self.assertFalse(limiter.allow("user-1"))

    def test_window_rolls_forward_and_admits_new_requests(self):
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60, clock=clock)
        self.assertTrue(limiter.allow("user-1"))
        self.assertFalse(limiter.allow("user-1"))
        clock.advance(61)
        self.assertTrue(limiter.allow("user-1"))

    def test_partial_window_expiry_only_frees_the_expired_slots(self):
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60, clock=clock)
        self.assertTrue(limiter.allow("user-1"))  # t=0
        clock.advance(50)
        self.assertTrue(limiter.allow("user-1"))  # t=50
        clock.advance(11)  # t=61: the t=0 request has expired, t=50 has not
        self.assertTrue(limiter.allow("user-1"))
        self.assertFalse(limiter.allow("user-1"))

    def test_different_keys_do_not_interfere(self):
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60, clock=clock)
        self.assertTrue(limiter.allow("user-1"))
        self.assertTrue(limiter.allow("user-2"))
        self.assertFalse(limiter.allow("user-1"))
        self.assertFalse(limiter.allow("user-2"))

    def test_tuple_keys_work_for_ip_email_pairs(self):
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60, clock=clock)
        self.assertTrue(limiter.allow(("1.2.3.4", "a@example.com")))
        self.assertFalse(limiter.allow(("1.2.3.4", "a@example.com")))
        self.assertTrue(limiter.allow(("1.2.3.4", "b@example.com")))


if __name__ == "__main__":
    unittest.main()
