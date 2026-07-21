"""manage_users.py  -  the only account-revocation mechanism (goal.md ADR-018).

There is no admin web UI. Disabling a user immediately invalidates every
session they currently hold (auth.py's resolve_session checks is_active).

Usage (from pipeline/, with POSTGRES_* set in the environment or .env):
    python manage_users.py list
    python manage_users.py disable someone@example.com
    python manage_users.py enable someone@example.com
"""

from __future__ import annotations

import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

from auth import get_auth_backend


def _find_user_id(backend, email: str) -> int | None:
    email = email.strip().lower()
    for user in backend.list_users():
        if user["email"] == email:
            return user["id"]
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    command = argv[0]
    backend = get_auth_backend()

    if command == "list":
        users = backend.list_users()
        if not users:
            print("No users.")
            return 0
        for user in users:
            status = "active" if user["is_active"] else "disabled"
            print(f"{user['id']:>6}  {user['email']:<40}  {status:<8}  {user['created_at']}")
        return 0

    if command in ("disable", "enable"):
        if len(argv) != 2:
            print(f"Usage: python manage_users.py {command} <email>")
            return 2
        email = argv[1]
        user_id = _find_user_id(backend, email)
        if user_id is None:
            print(f"No user with email {email!r}.")
            return 1
        backend.set_active(user_id, command == "enable")
        print(f"{email}: {'enabled' if command == 'enable' else 'disabled'}.")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
