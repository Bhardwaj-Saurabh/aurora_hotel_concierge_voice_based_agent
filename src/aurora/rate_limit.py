"""Sliding-window rate limiter (goal.md ADR-018).

Pure and clock-injectable so tests never need time.sleep. Used twice, with
different keys and thresholds, by the talk server's auth layer (aurora.server.deps):
  - post-auth cost limiter, keyed by user_id
  - pre-auth brute-force limiter, keyed by (client_ip, email)

In-memory and single-process by design: fine at today's one-Fly-machine
scale, and explicitly documented (goal.md ADR-018) as needing to move to a
shared store (Postgres/Redis) if this is ever scaled to more than one
replica — resets on every restart/deploy until then.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, *, limit: int, window_seconds: float, clock=time.time):
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict = defaultdict(deque)

    def allow(self, key) -> bool:
        now = self._clock()
        cutoff = now - self._window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True
