"""
bookings.py  -  durable, idempotent booking storage (goal.md ADR-007/013/014).

BookingBackend semantics (both implementations below satisfy the same
contract, proven by the shared test mixin in test_features.py):
    - A retried create_booking with identical details returns the SAME confirmation
      (created=False) instead of double-booking. Idempotency key = sha256 of the
      session plus normalized booking details.
    - Confirmation IDs are a random, non-guessable code (ADR-014) — never a
      sequential counter. A sequential ID lets anyone enumerate every other
      guest's booking and leaks business volume to a single glance at one
      receipt. The alphabet excludes confusable characters (0/O, 1/I/L) since
      these get read aloud and typed back over the phone.
    - Validation errors raise BookingValidationError with a caller-friendly,
      speakable message.

Two backends:
    - SqliteBookingBackend (default): in-memory for tests, or a file via
      BOOKINGS_DB for single-instance durability.
    - PostgresBookingBackend (ADR-013): used when POSTGRES_HOST is set. A
      single SQLite file breaks idempotency the moment more than one process
      (a pool of workers, ADR-012) writes bookings — each replica would get
      its own file. Postgres's `INSERT ... ON CONFLICT ... DO NOTHING` makes
      the idempotency check atomic *across* processes, not just within one.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

# Room catalog  -  the one source of truth (check_availability reads it too).
ROOMS = {
    "standard": {"name": "Standard Queen", "rate": "$189/night", "capacity": 2},
    "king": {"name": "Deluxe King", "rate": "$229/night", "capacity": 2},
    "suite": {"name": "Harbor Suite", "rate": "$329/night", "capacity": 4},
    "family": {"name": "Family Double Queen", "rate": "$269/night", "capacity": 5},
    "accessible": {"name": "Accessible Queen", "rate": "$199/night", "capacity": 2},
}

# No 0/O, 1/I/L: characters a caller or STT could confuse when a confirmation
# code is read aloud and typed back (goal.md ADR-014).
_CONFIRMATION_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CONFIRMATION_CODE_LENGTH = 6
_MAX_CONFIRMATION_RETRIES = 5  # collision odds are ~1 in 900M; a safety net, not an expectation

_DATE_FORMATS = (
    "%B %d %Y", "%B %d", "%d %B %Y", "%d %B", "%Y-%m-%d", "%m/%d/%Y", "%m/%d",
)


class BookingValidationError(ValueError):
    """Raised for invalid booking details; the message is safe to speak to the caller."""


@dataclass(frozen=True)
class BookingRecord:
    confirmation_id: str
    created: bool          # False = idempotent replay of an existing booking
    check_in: str
    check_out: str
    guests: int
    room_type: str
    guest_name: str
    contact: str


def normalize_room_type(value: str | None) -> str | None:
    room_type = (value or "").strip().lower()
    if not room_type:
        return None
    for key in ROOMS:
        if key in room_type:
            return key
    if "double" in room_type:
        return "family"
    if "queen" in room_type:
        return "standard"
    return None


def _parse_date(text: str) -> datetime | None:
    """Best-effort parse of caller-stated dates; None when the phrasing is too free-form."""
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", (text or "").strip(), flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.replace(",", " ").split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _validate_and_normalize(
    *,
    check_in: str,
    check_out: str,
    guests,
    room_type: str | None,
    guest_name: str,
    contact: str,
) -> tuple[str, dict]:
    """Shared validation for every backend. Returns (room_key, normalized details)."""
    room_key = normalize_room_type(room_type) or "standard"
    try:
        guest_count = int(guests)
    except (TypeError, ValueError):
        raise BookingValidationError("I did not catch a valid number of guests.")
    if guest_count < 1:
        raise BookingValidationError("A booking needs at least one guest.")
    capacity = ROOMS[room_key]["capacity"]
    if guest_count > capacity:
        raise BookingValidationError(
            f"The {ROOMS[room_key]['name']} sleeps up to {capacity} guests. "
            "A larger room or a second room would be needed."
        )
    if not (guest_name or "").strip() or not (contact or "").strip():
        raise BookingValidationError("A guest name and contact are required to book.")

    arrival = _parse_date(check_in)
    departure = _parse_date(check_out)
    if arrival and departure and departure <= arrival:
        raise BookingValidationError(
            "The check-out date must come after the check-in date."
        )

    details = dict(
        check_in=(check_in or "").strip(),
        check_out=(check_out or "").strip(),
        guests=guest_count,
        room_type=room_key,
        guest_name=guest_name.strip(),
        contact=contact.strip(),
    )
    return room_key, details


def _generate_confirmation_id() -> str:
    code = "".join(
        secrets.choice(_CONFIRMATION_ALPHABET) for _ in range(_CONFIRMATION_CODE_LENGTH)
    )
    return f"AH-{code}"


def _idempotency_key(
    session_id: str,
    check_in: str,
    check_out: str,
    guests: int,
    room_key: str,
    guest_name: str,
    contact: str,
) -> str:
    parts = [
        (session_id or "").strip().lower(),
        " ".join((check_in or "").lower().split()),
        " ".join((check_out or "").lower().split()),
        str(guests),
        room_key,
        " ".join((guest_name or "").lower().split()),
        (contact or "").strip().lower(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class SqliteBookingBackend:
    """SQLite implementation of the booking store. Single-instance only — see
    PostgresBookingBackend for a pool of processes/replicas (ADR-013)."""

    def __init__(self, path: str = ":memory:", id_generator=_generate_confirmation_id):
        self._lock = threading.Lock()
        self._id_generator = id_generator
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS bookings ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  confirmation_id TEXT UNIQUE NOT NULL,"
            "  idempotency_key TEXT UNIQUE NOT NULL,"
            "  session_id TEXT NOT NULL,"
            "  check_in TEXT NOT NULL,"
            "  check_out TEXT NOT NULL,"
            "  guests INTEGER NOT NULL,"
            "  room_type TEXT NOT NULL,"
            "  guest_name TEXT NOT NULL,"
            "  contact TEXT NOT NULL,"
            "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        self._conn.commit()

    def create_booking(
        self,
        *,
        session_id: str,
        check_in: str,
        check_out: str,
        guests: int,
        room_type: str | None,
        guest_name: str,
        contact: str,
    ) -> BookingRecord:
        room_key, details = _validate_and_normalize(
            check_in=check_in, check_out=check_out, guests=guests,
            room_type=room_type, guest_name=guest_name, contact=contact,
        )
        key = _idempotency_key(
            session_id, check_in, check_out, details["guests"], room_key,
            guest_name, contact,
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT confirmation_id FROM bookings WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row:
                return BookingRecord(confirmation_id=row[0], created=False, **details)
            for _ in range(_MAX_CONFIRMATION_RETRIES):
                confirmation = self._id_generator()
                try:
                    self._conn.execute(
                        "INSERT INTO bookings (confirmation_id, idempotency_key,"
                        " session_id, check_in, check_out, guests, room_type,"
                        " guest_name, contact) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            confirmation, key, session_id,
                            details["check_in"], details["check_out"], details["guests"],
                            details["room_type"], details["guest_name"], details["contact"],
                        ),
                    )
                    self._conn.commit()
                    break
                except sqlite3.IntegrityError:
                    continue  # confirmation_id collision (idempotency_key already checked above)
            else:
                raise RuntimeError(
                    "Could not generate a unique confirmation ID after "
                    f"{_MAX_CONFIRMATION_RETRIES} attempts."
                )
        return BookingRecord(confirmation_id=confirmation, created=True, **details)

    def find_booking(
        self,
        *,
        confirmation_id: str | None = None,
        guest_name: str | None = None,
        contact: str | None = None,
    ) -> dict | None:
        """Look up an existing reservation (goal.md, lookup_booking tool).

        A confirmation code is sufficient on its own (it is random and
        non-guessable, ADR-014 — the same trust model as an order number).
        Without one, BOTH guest_name and contact must match: a bare name is
        never enough to disclose someone else's booking (goal.md's
        privacy.other_guest red-team case)."""
        columns = "confirmation_id, check_in, check_out, guests, room_type, guest_name, contact"
        if confirmation_id:
            normalized = confirmation_id.strip().upper()
            if not normalized.startswith("AH-"):
                normalized = f"AH-{normalized}"
            row = self._conn.execute(
                f"SELECT {columns} FROM bookings WHERE confirmation_id = ?",
                (normalized,),
            ).fetchone()
        elif (guest_name or "").strip() and (contact or "").strip():
            row = self._conn.execute(
                f"SELECT {columns} FROM bookings"
                " WHERE lower(guest_name) = lower(?) AND lower(contact) = lower(?)"
                " ORDER BY id DESC LIMIT 1",
                (guest_name.strip(), contact.strip()),
            ).fetchone()
        else:
            return None
        if not row:
            return None
        return dict(zip(
            ("confirmation_id", "check_in", "check_out", "guests", "room_type",
             "guest_name", "contact"),
            row,
        ))

    def close(self) -> None:
        self._conn.close()


class PostgresBookingBackend:
    """Postgres implementation (ADR-013): idempotency enforced by the database
    itself via a UNIQUE constraint + INSERT ... ON CONFLICT, so two different
    processes racing on the same booking can never both create a row — unlike
    SqliteBookingBackend's threading.Lock, which only protects one process.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 5432,
        user: str,
        password: str,
        dbname: str,
        table_name: str = "bookings",
        sslmode: str = "prefer",
        id_generator=_generate_confirmation_id,
    ):
        import psycopg  # imported lazily: offline paths never need this dependency
        self._psycopg = psycopg
        self._table = table_name
        self._id_generator = id_generator
        self._lock = threading.Lock()
        self._conn = psycopg.connect(
            host=host, port=port, user=user, password=password, dbname=dbname,
            sslmode=sslmode, autocommit=True,
        )
        # Postgres 15+ revokes CREATE on `public` by default, and many hosted
        # providers restrict tenants to their own schema anyway. Create (or
        # reuse) a schema owned by our own connecting user and put it first
        # on the search path, so every unqualified CREATE/INSERT/SELECT below
        # resolves there without needing every statement schema-qualified.
        from psycopg import sql
        with self._lock:
            self._conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}")
                .format(schema=sql.Identifier(user))
            )
            self._conn.execute(
                sql.SQL("SET search_path TO {schema}, public")
                .format(schema=sql.Identifier(user))
            )
        self._create_table()
        self._heal_missing_columns()

    def _create_table(self) -> None:
        from psycopg import sql
        with self._lock:
            self._conn.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {table} ("
                "  id BIGSERIAL PRIMARY KEY,"
                "  confirmation_id TEXT UNIQUE NOT NULL,"
                "  idempotency_key TEXT UNIQUE NOT NULL,"
                "  session_id TEXT NOT NULL,"
                "  check_in TEXT NOT NULL,"
                "  check_out TEXT NOT NULL,"
                "  guests INTEGER NOT NULL,"
                "  room_type TEXT NOT NULL,"
                "  guest_name TEXT NOT NULL,"
                "  contact TEXT NOT NULL,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            ).format(table=sql.Identifier(self._table)))

    def _heal_missing_columns(self) -> None:
        """Defensive migration (goal.md, found live 2026-07-22): `CREATE TABLE
        IF NOT EXISTS` above never alters an already-existing table, so a
        column added to this schema after a table was first created would
        otherwise silently never reach it — exactly what happened in
        production with `confirmation_id` (invisible until a real booking
        was attempted; every create_booking call failed until fixed).
        Backfills each pre-existing row with its own freshly generated,
        unique confirmation_id — never a shared placeholder, which would
        violate the UNIQUE constraint added below if more than one row
        needed healing."""
        from psycopg import sql

        with self._lock:
            existing = {
                row[0] for row in self._conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = current_schema()",
                    (self._table,),
                ).fetchall()
            }
            if "confirmation_id" in existing:
                return
            table = sql.Identifier(self._table)
            self._conn.execute(
                sql.SQL("ALTER TABLE {table} ADD COLUMN confirmation_id TEXT")
                .format(table=table)
            )
            rows = self._conn.execute(
                sql.SQL("SELECT id FROM {table} WHERE confirmation_id IS NULL")
                .format(table=table)
            ).fetchall()
            for (row_id,) in rows:
                for _ in range(_MAX_CONFIRMATION_RETRIES):
                    candidate = self._id_generator()
                    try:
                        self._conn.execute(
                            sql.SQL("UPDATE {table} SET confirmation_id = %s WHERE id = %s")
                            .format(table=table),
                            (candidate, row_id),
                        )
                        break
                    except self._psycopg.errors.UniqueViolation:
                        continue
            self._conn.execute(
                sql.SQL("ALTER TABLE {table} ALTER COLUMN confirmation_id SET NOT NULL")
                .format(table=table)
            )
            self._conn.execute(
                sql.SQL("ALTER TABLE {table} ADD CONSTRAINT {constraint} UNIQUE (confirmation_id)")
                .format(
                    table=table,
                    constraint=sql.Identifier(f"{self._table}_confirmation_id_key"),
                )
            )

    def create_booking(
        self,
        *,
        session_id: str,
        check_in: str,
        check_out: str,
        guests: int,
        room_type: str | None,
        guest_name: str,
        contact: str,
    ) -> BookingRecord:
        from psycopg import sql

        room_key, details = _validate_and_normalize(
            check_in=check_in, check_out=check_out, guests=guests,
            room_type=room_type, guest_name=guest_name, contact=contact,
        )
        key = _idempotency_key(
            session_id, check_in, check_out, details["guests"], room_key,
            guest_name, contact,
        )
        insert_sql = sql.SQL(
            "INSERT INTO {table} (confirmation_id, idempotency_key, session_id,"
            " check_in, check_out, guests, room_type, guest_name, contact)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (idempotency_key) DO NOTHING"
            " RETURNING confirmation_id"
        ).format(table=sql.Identifier(self._table))
        with self._lock:
            confirmation = None
            for _ in range(_MAX_CONFIRMATION_RETRIES):
                candidate = self._id_generator()
                try:
                    row = self._conn.execute(
                        insert_sql,
                        (
                            candidate, key, session_id, details["check_in"],
                            details["check_out"], details["guests"],
                            details["room_type"], details["guest_name"],
                            details["contact"],
                        ),
                    ).fetchone()
                except self._psycopg.errors.UniqueViolation:
                    continue  # confirmation_id collision; idempotency_key already checked
                created = row is not None
                if row is not None:
                    confirmation = row[0]
                else:
                    # Conflict on idempotency_key: another process already won it.
                    existing = self._conn.execute(
                        sql.SQL("SELECT confirmation_id FROM {table} WHERE idempotency_key = %s")
                        .format(table=sql.Identifier(self._table)),
                        (key,),
                    ).fetchone()
                    confirmation = existing[0]
                break
            else:
                raise RuntimeError(
                    "Could not generate a unique confirmation ID after "
                    f"{_MAX_CONFIRMATION_RETRIES} attempts."
                )
        return BookingRecord(confirmation_id=confirmation, created=created, **details)

    def find_booking(
        self,
        *,
        confirmation_id: str | None = None,
        guest_name: str | None = None,
        contact: str | None = None,
    ) -> dict | None:
        """Same contract as SqliteBookingBackend.find_booking — see there."""
        from psycopg import sql

        columns = sql.SQL(", ").join(
            sql.Identifier(name) for name in
            ("confirmation_id", "check_in", "check_out", "guests", "room_type",
             "guest_name", "contact")
        )
        if confirmation_id:
            normalized = confirmation_id.strip().upper()
            if not normalized.startswith("AH-"):
                normalized = f"AH-{normalized}"
            row = self._conn.execute(
                sql.SQL("SELECT {columns} FROM {table} WHERE confirmation_id = %s")
                .format(columns=columns, table=sql.Identifier(self._table)),
                (normalized,),
            ).fetchone()
        elif (guest_name or "").strip() and (contact or "").strip():
            row = self._conn.execute(
                sql.SQL(
                    "SELECT {columns} FROM {table}"
                    " WHERE lower(guest_name) = lower(%s) AND lower(contact) = lower(%s)"
                    " ORDER BY id DESC LIMIT 1"
                ).format(columns=columns, table=sql.Identifier(self._table)),
                (guest_name.strip(), contact.strip()),
            ).fetchone()
        else:
            return None
        if not row:
            return None
        return dict(zip(
            ("confirmation_id", "check_in", "check_out", "guests", "room_type",
             "guest_name", "contact"),
            row,
        ))

    def reset_for_tests(self) -> None:
        """Test-only: drop and recreate the table for a clean-slate contract
        test run. Never call this against a table holding real bookings."""
        from psycopg import sql
        with self._lock:
            self._conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {table}").format(table=sql.Identifier(self._table))
            )
        self._create_table()

    def close(self) -> None:
        self._conn.close()


# --- module-level backend (one per process; .env picks the backend + storage) ---

_backend = None
_backend_lock = threading.Lock()


def get_booking_backend():
    """Postgres when POSTGRES_HOST is set (ADR-013); otherwise SQLite/in-memory."""
    global _backend
    with _backend_lock:
        if _backend is None:
            postgres_host = os.getenv("POSTGRES_HOST", "").strip()
            if postgres_host:
                _backend = PostgresBookingBackend(
                    host=postgres_host,
                    port=int(os.getenv("POSTGRES_PORT", "5432") or 5432),
                    user=os.getenv("POSTGRES_USER", ""),
                    password=os.getenv("POSTGRES_PASSWORD", ""),
                    dbname=os.getenv("POSTGRES_DB", ""),
                    sslmode=os.getenv("POSTGRES_SSLMODE", "").strip() or "prefer",
                )
            else:
                path = os.getenv("BOOKINGS_DB", "").strip() or ":memory:"
                _backend = SqliteBookingBackend(path)
        return _backend


def reset_booking_backend() -> None:
    """Test hook: drop the shared backend so the next call builds a fresh one."""
    global _backend
    with _backend_lock:
        _backend = None
