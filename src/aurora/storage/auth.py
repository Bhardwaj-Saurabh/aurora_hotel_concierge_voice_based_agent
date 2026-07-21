"""auth.py  -  user accounts + sessions for talk-server (goal.md ADR-018).

Closes ADR-015's documented, previously-accepted gap: talk-server's /token
and /agent endpoints had no caller authentication, so anyone reaching the
public URL got a valid LiveKit token and could trigger real, OpenAI-billed
calls.

Unlike bookings.py's dual SQLite/Postgres backend (which has a genuine
single-instance file-durability use case), credentials/sessions should never
live in a file-backed SQLite in production — SqliteAuthBackend below exists
for unit tests only. config_check.py hard-requires POSTGRES_HOST whenever
this module's real backend is selected outside a test run.

Session tokens are hashed with SHA-256, not argon2: argon2 is deliberately
slow, which is correct for a password (checked once, at login) but wrong for
a session-cookie check that runs on every /agent and /voice-agent turn during
a live voice call.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHasher()
# A fixed, valid hash of a password nobody will ever type. Verifying against
# this on an unknown-email login keeps failure timing indistinguishable from
# a real wrong-password failure — no early return before a hash comparison.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


class AuthValidationError(ValueError):
    """Raised for invalid registration input; message is safe to show the caller."""


def _validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise AuthValidationError("That does not look like a valid email address.")
    return email


def _validate_password(password: str) -> str:
    if not password or len(password) < _MIN_PASSWORD_LENGTH:
        raise AuthValidationError(
            f"Password must be at least {_MIN_PASSWORD_LENGTH} characters."
        )
    return password


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SqliteAuthBackend:
    """In-memory SQLite implementation — tests only, never production."""

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_users ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  email TEXT UNIQUE NOT NULL,"
            "  password_hash TEXT NOT NULL,"
            "  is_active INTEGER NOT NULL DEFAULT 1,"
            "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_sessions ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  user_id INTEGER NOT NULL,"
            "  token_hash TEXT UNIQUE NOT NULL,"
            "  expires_at REAL NOT NULL,"
            "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        self._conn.commit()

    def register_user(self, email: str, password: str) -> int:
        email = _validate_email(email)
        password = _validate_password(password)
        password_hash = _hasher.hash(password)
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM auth_users WHERE email = ?", (email,)
            ).fetchone()
            if existing:
                raise AuthValidationError("An account with that email already exists.")
            cursor = self._conn.execute(
                "INSERT INTO auth_users (email, password_hash) VALUES (?, ?)",
                (email, password_hash),
            )
            self._conn.commit()
            return cursor.lastrowid

    def verify_credentials(self, email: str, password: str) -> int | None:
        email = (email or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, password_hash, is_active FROM auth_users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None:
            try:
                _hasher.verify(_DUMMY_HASH, password or "")
            except VerifyMismatchError:
                pass
            return None
        user_id, password_hash, is_active = row
        try:
            _hasher.verify(password_hash, password or "")
        except VerifyMismatchError:
            return None
        if not is_active:
            return None
        return user_id

    def create_session(self, user_id: int, ttl_seconds: float) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + ttl_seconds
        with self._lock:
            self._conn.execute(
                "INSERT INTO auth_sessions (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, _hash_session_token(token), expires_at),
            )
            self._conn.commit()
        return token

    def resolve_session(self, token: str) -> int | None:
        token_hash = _hash_session_token(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT auth_sessions.user_id, auth_sessions.expires_at, auth_users.is_active"
                " FROM auth_sessions JOIN auth_users ON auth_users.id = auth_sessions.user_id"
                " WHERE auth_sessions.token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        user_id, expires_at, is_active = row
        if not is_active or expires_at <= time.time():
            return None
        return user_id

    def revoke_session(self, token: str) -> None:
        token_hash = _hash_session_token(token)
        with self._lock:
            self._conn.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            )
            self._conn.commit()

    def set_active(self, user_id: int, active: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_users SET is_active = ? WHERE id = ?", (int(active), user_id)
            )
            self._conn.commit()

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT password_hash FROM auth_users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return False
        try:
            _hasher.verify(row[0], current_password or "")
        except VerifyMismatchError:
            return False
        new_password = _validate_password(new_password)
        new_hash = _hasher.hash(new_password)
        with self._lock:
            self._conn.execute(
                "UPDATE auth_users SET password_hash = ? WHERE id = ?", (new_hash, user_id)
            )
            self._conn.commit()
        return True

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, email, is_active, created_at FROM auth_users ORDER BY id"
            ).fetchall()
        return [
            {"id": r[0], "email": r[1], "is_active": bool(r[2]), "created_at": r[3]}
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


class PostgresAuthBackend:
    """Postgres implementation (goal.md ADR-018) — the only backend used
    outside a test run. Shares the same instance/schema as bookings.py's
    PostgresBookingBackend (same POSTGRES_HOST, new tables)."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 5432,
        user: str,
        password: str,
        dbname: str,
        users_table: str = "auth_users",
        sessions_table: str = "auth_sessions",
        sslmode: str = "prefer",
    ):
        import psycopg  # imported lazily: offline paths never need this dependency

        self._psycopg = psycopg
        self._users_table = users_table
        self._sessions_table = sessions_table
        self._lock = threading.Lock()
        self._conn = psycopg.connect(
            host=host, port=port, user=user, password=password, dbname=dbname,
            sslmode=sslmode, autocommit=True,
        )
        from psycopg import sql
        with self._lock:
            self._conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=sql.Identifier(user))
            )
            self._conn.execute(
                sql.SQL("SET search_path TO {schema}, public").format(schema=sql.Identifier(user))
            )
        self._create_tables()

    def _create_tables(self) -> None:
        from psycopg import sql
        with self._lock:
            self._conn.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {table} ("
                "  id BIGSERIAL PRIMARY KEY,"
                "  email TEXT UNIQUE NOT NULL,"
                "  password_hash TEXT NOT NULL,"
                "  is_active BOOLEAN NOT NULL DEFAULT true,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            ).format(table=sql.Identifier(self._users_table)))
            self._conn.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {table} ("
                "  id BIGSERIAL PRIMARY KEY,"
                "  user_id BIGINT NOT NULL,"
                "  token_hash TEXT UNIQUE NOT NULL,"
                "  expires_at DOUBLE PRECISION NOT NULL,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            ).format(table=sql.Identifier(self._sessions_table)))

    def register_user(self, email: str, password: str) -> int:
        from psycopg import sql
        email = _validate_email(email)
        password = _validate_password(password)
        password_hash = _hasher.hash(password)
        try:
            with self._lock:
                row = self._conn.execute(
                    sql.SQL(
                        "INSERT INTO {table} (email, password_hash) VALUES (%s, %s) RETURNING id"
                    ).format(table=sql.Identifier(self._users_table)),
                    (email, password_hash),
                ).fetchone()
        except self._psycopg.errors.UniqueViolation:
            raise AuthValidationError("An account with that email already exists.")
        return row[0]

    def verify_credentials(self, email: str, password: str) -> int | None:
        from psycopg import sql
        email = (email or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                sql.SQL(
                    "SELECT id, password_hash, is_active FROM {table} WHERE email = %s"
                ).format(table=sql.Identifier(self._users_table)),
                (email,),
            ).fetchone()
        if row is None:
            try:
                _hasher.verify(_DUMMY_HASH, password or "")
            except VerifyMismatchError:
                pass
            return None
        user_id, password_hash, is_active = row
        try:
            _hasher.verify(password_hash, password or "")
        except VerifyMismatchError:
            return None
        if not is_active:
            return None
        return user_id

    def create_session(self, user_id: int, ttl_seconds: float) -> str:
        from psycopg import sql
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + ttl_seconds
        with self._lock:
            self._conn.execute(
                sql.SQL(
                    "INSERT INTO {table} (user_id, token_hash, expires_at) VALUES (%s, %s, %s)"
                ).format(table=sql.Identifier(self._sessions_table)),
                (user_id, _hash_session_token(token), expires_at),
            )
        return token

    def resolve_session(self, token: str) -> int | None:
        from psycopg import sql
        token_hash = _hash_session_token(token)
        with self._lock:
            row = self._conn.execute(
                sql.SQL(
                    "SELECT s.user_id, s.expires_at, u.is_active FROM {sessions} s"
                    " JOIN {users} u ON u.id = s.user_id"
                    " WHERE s.token_hash = %s"
                ).format(
                    sessions=sql.Identifier(self._sessions_table),
                    users=sql.Identifier(self._users_table),
                ),
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        user_id, expires_at, is_active = row
        if not is_active or expires_at <= time.time():
            return None
        return user_id

    def revoke_session(self, token: str) -> None:
        from psycopg import sql
        token_hash = _hash_session_token(token)
        with self._lock:
            self._conn.execute(
                sql.SQL("DELETE FROM {table} WHERE token_hash = %s")
                .format(table=sql.Identifier(self._sessions_table)),
                (token_hash,),
            )

    def set_active(self, user_id: int, active: bool) -> None:
        from psycopg import sql
        with self._lock:
            self._conn.execute(
                sql.SQL("UPDATE {table} SET is_active = %s WHERE id = %s")
                .format(table=sql.Identifier(self._users_table)),
                (active, user_id),
            )

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        from psycopg import sql
        with self._lock:
            row = self._conn.execute(
                sql.SQL("SELECT password_hash FROM {table} WHERE id = %s")
                .format(table=sql.Identifier(self._users_table)),
                (user_id,),
            ).fetchone()
        if row is None:
            return False
        try:
            _hasher.verify(row[0], current_password or "")
        except VerifyMismatchError:
            return False
        new_password = _validate_password(new_password)
        new_hash = _hasher.hash(new_password)
        with self._lock:
            self._conn.execute(
                sql.SQL("UPDATE {table} SET password_hash = %s WHERE id = %s")
                .format(table=sql.Identifier(self._users_table)),
                (new_hash, user_id),
            )
        return True

    def list_users(self) -> list[dict]:
        from psycopg import sql
        with self._lock:
            rows = self._conn.execute(
                sql.SQL("SELECT id, email, is_active, created_at FROM {table} ORDER BY id")
                .format(table=sql.Identifier(self._users_table))
            ).fetchall()
        return [
            {"id": r[0], "email": r[1], "is_active": r[2], "created_at": r[3]}
            for r in rows
        ]

    def reset_for_tests(self) -> None:
        """Test-only: drop and recreate the tables for a clean-slate contract
        test run. Never call this against tables holding real accounts."""
        from psycopg import sql
        with self._lock:
            self._conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {table}").format(table=sql.Identifier(self._sessions_table))
            )
            self._conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {table}").format(table=sql.Identifier(self._users_table))
            )
        self._create_tables()

    def close(self) -> None:
        self._conn.close()


# --- module-level backend (one per process; .env picks the backend + storage) ---

_backend = None
_backend_lock = threading.Lock()


def get_auth_backend():
    """Postgres in every real run; SQLite only when a test has injected one
    via reset_auth_backend (goal.md ADR-018 — no file-backed SQLite auth in
    production, unlike bookings.py's dual-durability design)."""
    global _backend
    with _backend_lock:
        if _backend is None:
            postgres_host = os.getenv("POSTGRES_HOST", "").strip()
            if not postgres_host:
                raise RuntimeError(
                    "POSTGRES_HOST is not set. The user-auth system requires Postgres "
                    "(goal.md ADR-018) — SQLite is test-only for credentials/sessions."
                )
            _backend = PostgresAuthBackend(
                host=postgres_host,
                port=int(os.getenv("POSTGRES_PORT", "5432") or 5432),
                user=os.getenv("POSTGRES_USER", ""),
                password=os.getenv("POSTGRES_PASSWORD", ""),
                dbname=os.getenv("POSTGRES_DB", ""),
                sslmode=os.getenv("POSTGRES_SSLMODE", "").strip() or "prefer",
            )
        return _backend


def reset_auth_backend() -> None:
    """Test hook: drop the shared backend so the next call builds a fresh one."""
    global _backend
    with _backend_lock:
        _backend = None


def set_auth_backend_for_tests(backend) -> None:
    """Test-only: inject a backend directly (e.g. SqliteAuthBackend), bypassing
    get_auth_backend()'s POSTGRES_HOST requirement so HTTP-layer integration
    tests (tests/test_talk_server.py) don't need a live Postgres instance."""
    global _backend
    with _backend_lock:
        _backend = backend
