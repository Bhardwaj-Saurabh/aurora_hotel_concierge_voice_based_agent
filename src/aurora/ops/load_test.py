"""load_test.py  -  measured concurrency numbers for scale_check.py (goal.md 4.3).

Drives real, authenticated concurrent /agent turns against a deployed
talk-server (default: the live Fly app), through the SAME Agent.respond()
code path the room-native worker uses (agent_worker.py's RoomAgentAdapter) —
so the observed per-turn latency/throughput is a valid proxy for
`sessions_per_worker` even though this hits the HTTP bridge, not a LiveKit
room directly.

Cost-aware by construction: the query is a read-only knowledge-base lookup
(no booking tool calls, so no rows written to the production bookings
table), the account/request counts are explicit CLI flags (never open-ended),
and every test account this script creates is disabled again at the end via
the same CLI-only revocation path as manage_users.py.

Usage:
    python load_test.py --base-url https://aurora-hotel-talk-server.fly.dev \
        --accounts 5 --requests 40 --concurrency 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

QUERY = "What is the pet policy?"  # read-only: search_hotel_knowledge only, no mutation


def _post(url: str, payload: dict, cookie: str | None = None) -> tuple[int, dict, str | None]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
            set_cookie = response.headers.get("Set-Cookie")
            return response.status, body, set_cookie
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read() or b"{}")
        return exc.code, body, None


def _register(base_url: str, email: str, password: str) -> str:
    status, body, set_cookie = _post(f"{base_url}/auth/register", {"email": email, "password": password})
    if status != 200:
        raise RuntimeError(f"registration failed for {email}: {status} {body}")
    return set_cookie.split(";")[0]


def _agent_turn(base_url: str, cookie: str) -> tuple[float, int]:
    start = time.perf_counter()
    status, _body, _cookie = _post(f"{base_url}/agent", {"text": QUERY}, cookie=cookie)
    return time.perf_counter() - start, status


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, int(round(pct * (len(values) - 1))))
    return values[index]


def run(base_url: str, accounts: int, requests: int, concurrency: int, password: str) -> dict:
    run_id = uuid.uuid4().hex[:8]
    emails = [f"load-test-{run_id}-{i}@example.com" for i in range(accounts)]
    print(f"Registering {accounts} disposable test accounts...")
    cookies = [_register(base_url, email, password) for email in emails]

    print(f"Firing {requests} concurrent /agent turns (concurrency={concurrency})...")
    latencies: list[float] = []
    statuses: list[int] = []
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(_agent_turn, base_url, cookies[i % len(cookies)])
            for i in range(requests)
        ]
        for future in as_completed(futures):
            latency, status = future.result()
            latencies.append(latency)
            statuses.append(status)
    wall_elapsed = time.perf_counter() - wall_start

    successes = sum(1 for s in statuses if s == 200)
    result = {
        "requests": requests,
        "successes": successes,
        "failures": requests - successes,
        "statusCounts": {str(s): statuses.count(s) for s in sorted(set(statuses))},
        "wallClockSeconds": round(wall_elapsed, 2),
        "observedThroughputPerSecond": round(requests / wall_elapsed, 2) if wall_elapsed else 0.0,
        "p50Seconds": round(_percentile(latencies, 0.5), 3),
        "p95Seconds": round(_percentile(latencies, 0.95), 3),
        "maxSeconds": round(max(latencies), 3) if latencies else 0.0,
    }

    print("Disabling test accounts...")
    from aurora.storage.auth import get_auth_backend
    backend = get_auth_backend()
    by_email = {u["email"]: u["id"] for u in backend.list_users()}
    for email in emails:
        user_id = by_email.get(email)
        if user_id is not None:
            backend.set_active(user_id, False)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Measured concurrency load test (goal.md 4.3)")
    parser.add_argument("--base-url", default="https://aurora-hotel-talk-server.fly.dev")
    parser.add_argument("--accounts", type=int, default=5)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--password", default="load-test-password-1")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run(args.base_url, args.accounts, args.requests, args.concurrency, args.password)
    if args.json:
        print(json.dumps(result, indent=2))
        return

    print("\nLoad test result")
    print(f"  requests                 {result['requests']:>10}")
    print(f"  successes / failures      {result['successes']:>6} / {result['failures']}")
    print(f"  status counts             {result['statusCounts']}")
    print(f"  wall clock (s)            {result['wallClockSeconds']:>10}")
    print(f"  observed throughput/s     {result['observedThroughputPerSecond']:>10}")
    print(f"  p50 latency (s)           {result['p50Seconds']:>10}")
    print(f"  p95 latency (s)           {result['p95Seconds']:>10}")
    print(f"  max latency (s)           {result['maxSeconds']:>10}")


if __name__ == "__main__":
    main()
