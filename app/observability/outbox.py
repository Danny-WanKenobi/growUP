"""Durable SQLite outbox for permanent agent events (MVP-004C-A Phase 3A).

This module persists permanent events to a local SQLite database
so they survive application restarts when PostgreSQL is unavailable.
The Phase 3B emitter uses this outbox for durable acceptance, and
the recovery worker forwards pending events to PostgreSQL.

Durability contract: :meth:`Outbox.accept` returns only after the
event row is committed (WAL mode, ``synchronous=FULL``, explicit
transaction). A return means durable local acceptance; any failure
raises and the event must be treated as NOT accepted. Commits depend
on the underlying filesystem and storage -- SQLite does not protect
against physical disk failure.

Identity is the existing ``AgentEvent.event_id``: re-accepting an
identical event is a silent idempotent no-op, while the same ID with
a different payload raises :class:`OutboxConflictError`. Only
permanent-retention events are accepted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.observability.events import AgentEvent, RetentionClass
from app.observability.redaction import sanitize_structure, scrub_text, redact

DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
DEFAULT_PENDING_LIMIT = 100
MAX_PENDING_LIMIT = 1000

DIR_MODE = 0o700
DB_MODE = 0o600

# Bounded exponential backoff for delivery retries: 30s, 60s, ... capped
# at one hour. Timer-paced (30s) plus backoff means no retry storms.
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 3600

DEFAULT_DELIVERED_RETENTION_DAYS = 30
DEFAULT_PURGE_LIMIT = 1000

# Release-tree locations that must never hold the production outbox:
# releases are immutable and replaced on deploy; `current` symlinks
# into them. Checked after symlink resolution.
PRODUCTION_FORBIDDEN_PREFIXES = (
    Path("/var/www/growup/releases"),
    Path("/var/www/growup/current"),
)

# Table and index DDL are separate statements on purpose: migration
# order is table -> ADD COLUMN -> indexes, so an existing Phase 3A
# database (no delivered_at column) upgrades instead of failing on
# an index over a missing column.
_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NULL,
    last_error TEXT NULL,
    next_retry_at TEXT NULL,
    delivered_at TEXT NULL,
    CONSTRAINT outbox_status_check CHECK (status IN ('pending', 'delivered'))
);
"""
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS outbox_pending_idx
    ON outbox_events (status, enqueued_at, event_id);
CREATE INDEX IF NOT EXISTS outbox_delivered_idx
    ON outbox_events (status, delivered_at, event_id);
"""


class OutboxError(Exception):
    """Base class for outbox failures."""


class OutboxUnavailableError(OutboxError):
    """Storage is unreachable, locked, corrupt, or a write failed."""


class OutboxConfigError(OutboxError):
    """The outbox path or configuration is invalid."""


class OutboxConflictError(OutboxError):
    """An event ID was resubmitted with a different payload."""


def retry_delay_seconds(attempts: int) -> int:
    """Bounded exponential backoff shared by emitter and worker.

    ``attempts`` is the count BEFORE the upcoming wait (0 for the first
    failure). Returns 30..3600 seconds; never zero, never unbounded.
    """
    attempts = max(0, int(attempts))
    return min(RETRY_BASE_SECONDS * (2 ** min(attempts, 7)), RETRY_MAX_SECONDS)


def validate_production_path(path: str | Path | None) -> Path:
    """Validate an outbox path for production durability.

    Production requires an explicitly configured absolute path outside
    the versioned release tree. The check runs on the symlink-resolved
    path, so aliases into ``releases/`` or ``current/`` are rejected
    even when spelled differently. Raises :class:`OutboxConfigError`.
    """
    if path is None or not str(path).strip():
        raise OutboxConfigError("production outbox path must be configured")
    candidate = Path(str(path).strip())
    if not candidate.is_absolute():
        raise OutboxConfigError(
            f"production outbox path must be absolute: {candidate}"
        )
    resolved = candidate.resolve()
    for prefix in PRODUCTION_FORBIDDEN_PREFIXES:
        if resolved == prefix or resolved.is_relative_to(prefix):
            raise OutboxConfigError(
                f"production outbox path must not live under {prefix}: {candidate}"
            )
    return candidate


@dataclass(frozen=True)
class PendingEvent:
    """A pending outbox row with its delivery metadata."""

    event: AgentEvent
    enqueued_at: datetime
    attempts: int
    last_attempt_at: datetime | None
    last_error: str | None
    next_retry_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """Fixed-shape UTC ISO text (lexicographically sortable)."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _scrub_event(event: AgentEvent) -> AgentEvent:
    """Defensively re-validate and re-scrub before storage.

    Emitter integration (Phase 3B) will pass already-prepared events;
    the outbox scrubs again so a direct caller can never persist
    secrets or control characters.
    """
    cleaned = AgentEvent.model_validate(event.model_dump())
    details = sanitize_structure(redact(cleaned.details))
    refs = [
        {"kind": scrub_text(ref.kind), "ref": scrub_text(ref.ref)}
        for ref in cleaned.evidence_refs
    ]
    return AgentEvent.model_validate(
        {
            **cleaned.model_dump(),
            "summary": scrub_text(cleaned.summary),
            "details": details,
            "evidence_refs": refs,
        }
    )


class Outbox:
    """Transactional SQLite store for permanent events (storage only)."""

    def __init__(
        self,
        path: str | Path | None,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
        production: bool = False,
    ) -> None:
        if production:
            # Explicit opt-in to production durability semantics; the
            # Phase 3B worker must construct its outbox this way.
            self._path = validate_production_path(path)
        elif path is None or not str(path).strip():
            raise OutboxConfigError("outbox path must be a non-empty string")
        else:
            self._path = Path(str(path).strip())
        if busy_timeout_seconds <= 0:
            raise OutboxConfigError("busy timeout must be positive")
        self._timeout = float(busy_timeout_seconds)

    @property
    def path(self) -> Path:
        """Configured database path (no I/O performed by reading it)."""
        return self._path

    # -- lifecycle ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._timeout)
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox unreachable: {exc}") from exc
        try:
            conn.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as exc:
            conn.close()
            raise OutboxUnavailableError(f"outbox unreachable: {exc}") from exc
        return conn

    def _secure_files(self) -> None:
        """Best-effort 0600 on the database and WAL sidecar files."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(self._path) + suffix)
            if candidate.exists():
                try:
                    os.chmod(candidate, DB_MODE)
                except OSError as exc:
                    print(
                        f"observability outbox chmod failed: {candidate}: {exc}",
                        file=sys.stderr,
                    )

    def initialize(self) -> Path:
        """Create parent dirs, schema, and restrictive permissions.

        Idempotent: safe to call on every startup. Returns the path.
        Upgrades existing Phase 3A databases by adding new columns
        BEFORE creating indexes over them. Migration steps only add
        schema (table, columns, indexes) -- they never delete or
        rewrite event rows, so a failed migration leaves stored
        events untouched (and raises instead of reporting success).
        """
        if self._path.exists() and not self._path.is_file():
            raise OutboxConfigError(f"outbox path is not a file: {self._path}")
        parent = self._path.parent
        parent_existed = parent.exists()
        try:
            parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        except OSError as exc:
            raise OutboxUnavailableError(f"outbox directory unavailable: {exc}") from exc
        if not parent_existed:
            # Only directories we created; never alter admin-owned parents.
            try:
                os.chmod(parent, DIR_MODE)
            except OSError as exc:
                print(f"observability outbox chmod failed: {parent}: {exc}", file=sys.stderr)
        existed = self._path.exists()
        try:
            conn = self._connect()
        except OutboxUnavailableError:
            raise
        try:
            conn.executescript(_SCHEMA_TABLE)  # self-committing
            with conn:
                self._ensure_column(conn, "delivered_at", "TEXT NULL")
            conn.executescript(_SCHEMA_INDEXES)  # self-committing
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox schema failed: {exc}") from exc
        finally:
            conn.close()
        if not existed:
            self._secure_files()
        return self._path

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, name: str, ddl: str) -> None:
        """ADD COLUMN for databases created before the column existed."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox_events)")}
        if name not in columns:
            conn.execute(f"ALTER TABLE outbox_events ADD COLUMN {name} {ddl}")

    # -- writes (explicit transactions; failure means NOT accepted) ---

    def accept(self, event: AgentEvent, now: datetime | None = None) -> str:
        """Durably accept one permanent event. Returns its event ID.

        Idempotent for identical resubmission; conflicting payloads for
        the same ID raise :class:`OutboxConflictError`. Nonpermanent
        events are rejected with :class:`ValueError`. Returns only
        after commit -- any exception means NOT accepted.
        """
        if not isinstance(event, AgentEvent):
            raise ValueError("outbox accepts AgentEvent instances only")
        if event.retention_class is not RetentionClass.PERMANENT:
            raise ValueError("outbox accepts permanent events only")
        scrubbed = _scrub_event(event)
        payload = json.dumps(scrubbed.to_record(), sort_keys=True)
        moment = _iso(now or _utcnow())
        try:
            conn = self._connect()
        except OutboxUnavailableError:
            raise
        try:
            with conn:
                # IMMEDIATE reserves the write lock up front so the
                # read-then-insert stays atomic under concurrency.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT payload FROM outbox_events WHERE event_id = ?",
                    (scrubbed.event_id,),
                ).fetchone()
                if row is not None:
                    if row[0] == payload:
                        return scrubbed.event_id
                    raise OutboxConflictError(
                        f"event {scrubbed.event_id} already stored with a different payload"
                    )
                conn.execute(
                    "INSERT INTO outbox_events (event_id, payload, event_timestamp, enqueued_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        scrubbed.event_id,
                        payload,
                        _iso(scrubbed.timestamp),
                        moment,
                    ),
                )
        except OutboxConflictError:
            raise
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox accept failed: {exc}") from exc
        finally:
            conn.close()
        self._secure_files()
        return scrubbed.event_id

    def mark_delivered(self, event_id: str, now: datetime | None = None) -> bool:
        """Mark a pending event delivered (stamps confirmation time)."""
        moment = _iso(now or _utcnow())
        try:
            conn = self._connect()
        except OutboxUnavailableError:
            raise
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE outbox_events SET status = 'delivered', delivered_at = ? "
                    "WHERE event_id = ? AND status = 'pending'",
                    (moment, event_id),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox mark_delivered failed: {exc}") from exc
        finally:
            conn.close()

    def record_attempt(
        self,
        event_id: str,
        error_category: str,
        next_retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record a failed delivery attempt with its next retry time.

        True if a pending row was updated. When ``next_retry_at`` is
        omitted, it is computed from the row's current attempt count
        with bounded exponential backoff. The error category is a
        short code (bounded, single-line) -- never a traceback.
        """
        category = " ".join(str(error_category).split())[:64] or "unknown"
        moment = now or _utcnow()
        try:
            conn = self._connect()
        except OutboxUnavailableError:
            raise
        try:
            with conn:
                row = conn.execute(
                    "SELECT attempts FROM outbox_events WHERE event_id = ? AND status = 'pending'",
                    (event_id,),
                ).fetchone()
                if row is None:
                    return False
                retry = next_retry_at or (
                    moment + timedelta(seconds=retry_delay_seconds(row[0]))
                )
                conn.execute(
                    "UPDATE outbox_events SET attempts = attempts + 1, "
                    "last_attempt_at = ?, last_error = ?, next_retry_at = ? "
                    "WHERE event_id = ? AND status = 'pending'",
                    (_iso(moment), category, _iso(retry), event_id),
                )
                return True
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox record_attempt failed: {exc}") from exc
        finally:
            conn.close()

    def purge_delivered(
        self,
        older_than_days: int = DEFAULT_DELIVERED_RETENTION_DAYS,
        limit: int = DEFAULT_PURGE_LIMIT,
        now: datetime | None = None,
    ) -> int:
        """Delete delivered rows confirmed longer ago than the window.

        Only rows explicitly marked delivered (with a confirmation
        timestamp) can match: pending rows, attempted-but-pending rows,
        and recent deliveries always survive. Bounded by ``limit``.
        """
        if older_than_days < 1:
            raise ValueError("retention window must be positive")
        if limit < 1:
            raise ValueError("limit must be positive")
        cutoff = _iso((now or _utcnow()) - timedelta(days=older_than_days))
        try:
            conn = self._connect()
        except OutboxUnavailableError:
            raise
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM outbox_events WHERE event_id IN ("
                    "SELECT event_id FROM outbox_events "
                    "WHERE status = 'delivered' AND delivered_at IS NOT NULL "
                    "AND delivered_at < ? ORDER BY delivered_at ASC, event_id ASC LIMIT ?)",
                    (cutoff, limit),
                )
                return cursor.rowcount if cursor.rowcount is not None else 0
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox purge failed: {exc}") from exc
        finally:
            conn.close()

    # -- reads ----------------------------------------------------------

    def _read_conn(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, event_id: str) -> AgentEvent | None:
        """Retrieve one event by ID, or None when absent."""
        try:
            conn = self._read_conn()
        except OutboxUnavailableError:
            raise
        try:
            row = conn.execute(
                "SELECT payload FROM outbox_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox read failed: {exc}") from exc
        finally:
            conn.close()
        if row is None:
            return None
        try:
            return AgentEvent.model_validate(json.loads(row["payload"]))
        except (ValueError, TypeError) as exc:
            raise OutboxUnavailableError(f"outbox payload corrupt: {event_id}") from exc

    def pending(self, limit: int = DEFAULT_PENDING_LIMIT, now: datetime | None = None) -> list[PendingEvent]:
        """Eligible pending events: retry-due, oldest first, id tiebreak.

        Only rows whose retry time has arrived (or was never scheduled)
        are returned, in deterministic ``(enqueued_at, event_id)`` order.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        limit = min(limit, MAX_PENDING_LIMIT)
        moment = _iso(now or _utcnow())
        try:
            conn = self._read_conn()
        except OutboxUnavailableError:
            raise
        try:
            rows = conn.execute(
                "SELECT payload, enqueued_at, attempts, last_attempt_at, last_error, next_retry_at"
                " FROM outbox_events WHERE status = 'pending'"
                " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
                " ORDER BY enqueued_at ASC, event_id ASC LIMIT ?",
                (moment, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox read failed: {exc}") from exc
        finally:
            conn.close()
        pending: list[PendingEvent] = []
        for row in rows:
            try:
                event = AgentEvent.model_validate(json.loads(row["payload"]))
            except (ValueError, TypeError) as exc:
                raise OutboxUnavailableError("outbox payload corrupt") from exc
            enqueued = _parse_iso(row["enqueued_at"])
            assert enqueued is not None  # schema guarantees NOT NULL
            pending.append(
                PendingEvent(
                    event=event,
                    enqueued_at=enqueued,
                    attempts=row["attempts"],
                    last_attempt_at=_parse_iso(row["last_attempt_at"]),
                    last_error=row["last_error"],
                    next_retry_at=_parse_iso(row["next_retry_at"]),
                )
            )
        return pending

    def count_pending(self) -> int:
        """Number of rows awaiting delivery (regardless of retry time)."""
        try:
            conn = self._read_conn()
        except OutboxUnavailableError:
            raise
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox_events WHERE status = 'pending'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox read failed: {exc}") from exc
        finally:
            conn.close()
        return int(row["n"])

    def oldest_pending_at(self) -> datetime | None:
        """Enqueue time of the oldest pending row, or None when empty."""
        try:
            conn = self._read_conn()
        except OutboxUnavailableError:
            raise
        try:
            row = conn.execute(
                "SELECT MIN(enqueued_at) AS oldest FROM outbox_events WHERE status = 'pending'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise OutboxUnavailableError(f"outbox read failed: {exc}") from exc
        finally:
            conn.close()
        return _parse_iso(row["oldest"]) if row else None
