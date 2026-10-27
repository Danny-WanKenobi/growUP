"""PostgreSQL recovery worker for the SQLite outbox (MVP-004C-A Phase 3B).

Reads pending outbox events, delivers them idempotently to PostgreSQL,
and marks them delivered only after PostgreSQL confirms. One bounded
cycle per invocation; systemd runs it every 30 seconds.

Crash/restart safety: delivery is idempotent on ``event_id``, and the
outbox row is marked only after confirmation, so an interrupted cycle
simply redelivers and confirms on the next run. Overlapping executions
are prevented by systemd (flock); if they ever overlap anyway, delivery
stays correct (at most duplicated effort, never duplicated rows).

This module never calls Brave Search, never reruns business operations,
and never imports the FastAPI server. Monitoring output carries counts
only -- no payloads, no secrets.

Exit statuses (``python -m app.observability.recovery``):

- 0: cycle ok -- every attempted event was confirmed, or nothing was
  pending. A quiet success; the timer simply runs the next cycle.
- 1: partial recovery -- some events delivered, others failed or
  conflicted. Failed events stay pending with backoff; conflicts need
  human comparison of the two payloads.
- 2: configuration error (missing/unsafe outbox path).
- 3: recovery failure -- the cycle ran but nothing was delivered
  (e.g. a PostgreSQL outage). Events stay pending and retryable;
  the next timer tick retries them.
- 4: outbox infrastructure failure -- SQLite was unreachable before
  or during the run, so no delivery was attempted.

Only exit 2 means "do not retry without operator action on the
config"; 1 and 3 keep the queue intact for the next cycle, and 4
leaves the outbox untouched for diagnosis.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability.config import DEFAULT_DELIVERED_RETENTION_DAYS, get_observability_config
from app.observability.outbox import Outbox, OutboxConfigError, OutboxError
from app.observability.store import deliver_event

DEFAULT_BATCH_LIMIT = 100
DEFAULT_PURGE_LIMIT = 1000

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CONFIG = 2
EXIT_FAILED = 3
EXIT_OUTBOX = 4

_STATUS_EXITS = {"ok": EXIT_OK, "partial": EXIT_PARTIAL, "failed": EXIT_FAILED}


@dataclass(frozen=True)
class RecoveryReport:
    """Structured metrics for one bounded recovery cycle."""

    pending_before: int
    oldest_pending_age_seconds: int | None
    attempted: int = 0
    delivered: int = 0
    already_delivered: int = 0
    failed: int = 0
    conflicts: int = 0
    retries_scheduled: int = 0
    purged: int = 0
    status: str = "ok"  # ok | partial | failed

    def summary_line(self) -> str:
        """Single-line operational metrics (counts only, no payloads)."""
        oldest = self.oldest_pending_age_seconds if self.oldest_pending_age_seconds is not None else -1
        return (
            "recovery"
            f" status={self.status}"
            f" pending={self.pending_before}"
            f" attempted={self.attempted}"
            f" delivered={self.delivered}"
            f" already={self.already_delivered}"
            f" failed={self.failed}"
            f" conflicts={self.conflicts}"
            f" retries={self.retries_scheduled}"
            f" purged={self.purged}"
            f" oldest_age_s={oldest}"
        )


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, DatabaseNotConfiguredError):
        return "pg_not_configured"
    return "pg_unreachable"


def recover_once(
    outbox: Outbox,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
    now: datetime | None = None,
    purge_delivered_days: int | None = DEFAULT_DELIVERED_RETENTION_DAYS,
    purge_limit: int = DEFAULT_PURGE_LIMIT,
) -> RecoveryReport:
    """Run one bounded recovery cycle against an initialized outbox."""
    if batch_limit < 1:
        raise ValueError("batch_limit must be positive")
    moment = now or datetime.now(timezone.utc)
    pending_before = outbox.count_pending()
    oldest = outbox.oldest_pending_at()
    oldest_age = int((moment - oldest).total_seconds()) if oldest else None
    attempted = delivered = already = failed = conflicts = retries = 0
    for pending in outbox.pending(limit=batch_limit, now=moment):
        attempted += 1
        try:
            result = deliver_event(pending.event)
        except (DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
            outbox.record_attempt(pending.event.event_id, _error_category(exc), now=moment)
            failed += 1
            retries += 1
            continue
        if result.status == "conflict":
            outbox.record_attempt(pending.event.event_id, "pg_content_conflict", now=moment)
            conflicts += 1
            retries += 1
            continue
        outbox.mark_delivered(pending.event.event_id, now=moment)
        if result.status == "already_present":
            already += 1
        else:
            delivered += 1
    purged = 0
    if purge_delivered_days is not None:
        purged = outbox.purge_delivered(
            older_than_days=purge_delivered_days, limit=purge_limit, now=moment
        )
    if failed == 0 and conflicts == 0:
        status = "ok"
    elif delivered + already > 0:
        status = "partial"
    else:
        status = "failed"
    return RecoveryReport(
        pending_before=pending_before,
        oldest_pending_age_seconds=oldest_age,
        attempted=attempted,
        delivered=delivered,
        already_delivered=already,
        failed=failed,
        conflicts=conflicts,
        retries_scheduled=retries,
        purged=purged,
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m app.observability.recovery``."""
    del argv  # no flags in Phase 3B; configuration comes from the environment
    raw_path = (os.getenv("OBS_OUTBOX_PATH", "") or "").strip()
    if not raw_path:
        print("recovery error: OBS_OUTBOX_PATH is not configured", file=sys.stderr)
        return EXIT_CONFIG
    try:
        outbox = Outbox(raw_path, production=True)
        outbox.initialize()
    except OutboxConfigError as exc:
        print(f"recovery config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except OutboxError as exc:
        print(f"recovery error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_OUTBOX
    started = time.perf_counter()
    try:
        report = recover_once(
            outbox,
            purge_delivered_days=get_observability_config().delivered_retention_days,
        )
    except OutboxError as exc:
        print(f"recovery error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_OUTBOX
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    print(f"{report.summary_line()} elapsed_ms={elapsed_ms}")
    return _STATUS_EXITS[report.status]


if __name__ == "__main__":
    sys.exit(main())
