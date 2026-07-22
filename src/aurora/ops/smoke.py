"""
smoke_test.py  -  full end-to-end check. No LLM/STT/TTS key, no mic.

Forces PROVIDER=mock and drives scripted turns through the REAL Agent + adaptor,
asserting that tools fire and control actions (transfer/hangup) surface. Run
this in CI or before a workshop to confirm the loop is wired correctly.

Needs a real Postgres connection (goal.md ADR-021 — bookings has no local
fallback): the booking turn persists to a disposable table, never the real
`bookings` table. Set POSTGRES_* in .env, or CI's `gates` job provides one.

    python -m aurora.ops.smoke
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

os.environ["PROVIDER"] = "mock"          # offline backend
os.environ.setdefault("TTS_BACKEND", "print")

from aurora.core.agent import Agent                   # noqa: E402
from aurora.core.providers import make_provider        # noqa: E402
from aurora.storage.bookings import (                 # noqa: E402
    new_disposable_backend_for_offline_gates,
    set_booking_backend_for_tests,
)

# Postgres-only (goal.md ADR-021) — a disposable table, never the real
# 'bookings' table, so this gate never touches production data.
set_booking_backend_for_tests(new_disposable_backend_for_offline_gates())


def main() -> None:
    agent = Agent(make_provider())
    ok = True

    def turn(user: str, expect_action=None, expect_in=None):
        nonlocal ok
        reply, action = agent.respond(user)
        print(f"you>   {user}")
        print(f"agent> {reply}")
        if action:
            print(f"[action: {action}]")
        if expect_action and action != expect_action:
            print(f"   expected action {expect_action!r}, got {action!r}"); ok = False
        if expect_in and expect_in.lower() not in reply.lower():
            print(f"   expected {expect_in!r} in reply"); ok = False
        print()

    # Guardrail path -> off-topic redirect
    turn("Can you tell me the weather?", expect_in="hotel reservations")

    # Tool call -> availability result -> spoken reply
    turn(
        "I need a room from August 12 to August 14 for two guests.",
        expect_in="Standard Queen",
    )
    # Booking path -> confirmation (random non-guessable code, goal.md ADR-014)
    turn(
        "Yes, book it for Priya Shah at priya@example.com.",
        expect_in="AH-",
    )
    # Transfer path -> SIP REFER
    turn("Actually, connect me to a person", expect_action="transfer")

    # Fresh call for the hangup path (transfer ended the first one)
    agent2 = Agent(make_provider())
    reply, action = agent2.respond("Goodbye")
    print(f"you>   Goodbye\nagent> {reply}\n[action: {action}]\n")
    if action != "hangup":
        print(f"   expected hangup, got {action!r}"); ok = False

    print("RESULT:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
