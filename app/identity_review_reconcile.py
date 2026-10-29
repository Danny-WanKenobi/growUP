"""Manual PostgreSQL reconciler for research identity reviews (IR-3C C1b).

Sweeps research/dossier verdict events already delivered to
``agent_events`` and converts dispute-claiming verdicts into durable
identity-review occurrences through the existing C1a writer
(:func:`app.identity_reviews_db.record_review_occurrence`). There is
exactly one persistence pathway; this module owns no transactions
beyond opening the short-lived per-verdict connections the writer
requires, and it never modifies the scanned verdict rows.

Sweep mechanics: ascending keyset pagination over
``(occurred_at, event_id)`` in bounded batches, with the in-memory
cursor advancing past every examined row including failures. The
default invocation sweeps the complete currently discoverable
eligible backlog; completed work is excluded by the occurrence table
itself, so reruns are idempotent and failed rows retry on the next
invocation. No persistent cursor, no skip-lists, no scheduler.

Manual entry point: ``python -m app.identity_review_reconcile``.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity_reviews_db import (
    IdentityReviewError,
    record_review_occurrence,
    review_context_from_event,
)
from app.observability.events import AgentEvent
from app.properties_db import get_connection

RESEARCH_EVENT_TYPES = (
    "gabriel.research.candidate_decision",
    "gabriel.dossier.verdict",
)

DEFAULT_BATCH_SIZE = 100
MAX_FAILURE_SAMPLE = 20

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CONFIG = 2
EXIT_OUTAGE = 3

_SCAN_COLUMNS = (
    "event_id, occurred_at, agent, run_id, correlation_id, property_id, "
    "event_type, severity, summary, details, evidence_refs, parent_event_id, "
    "retention_class"
)


@dataclass
class ReconcileFailure:
    """One examined event that produced no review occurrence."""

    event_id: str
    reason: str


@dataclass
class ReconcileReport:
    """Counts and bounded failure detail for one sweep."""

    examined: int = 0
    reconciled: int = 0
    already_reconciled: int = 0
    skipped_ineligible: int = 0
    failed: list[ReconcileFailure] = field(default_factory=list)
    omitted_failures: int = 0
    more_may_remain: bool = False
    status: str = "ok"

    @property
    def total_failed(self) -> int:
        return len(self.failed) + self.omitted_failures

    def summary_line(self) -> str:
        return (
            f"reconcile examined={self.examined} reconciled={self.reconciled} "
            f"already={self.already_reconciled} skipped={self.skipped_ineligible} "
            f"failed={self.total_failed} omitted_failures={self.omitted_failures} "
            f"more_may_remain={str(self.more_may_remain).lower()} status={self.status}"
        )


def _scan_sql(*, only_ids: bool) -> str:
    """SELECT eligible verdict rows after a keyset cursor (pure, testable)."""
    scope = "AND e.event_id = ANY(%(only_ids)s) " if only_ids else ""
    types = ", ".join(f"'{name}'" for name in RESEARCH_EVENT_TYPES)
    return (
        f"SELECT {_SCAN_COLUMNS} FROM agent_events e "
        f"WHERE e.event_type IN ({types}) "
        "AND e.retention_class = 'permanent' "
        "AND e.details->'identity_disputes' <> '{}'::jsonb "
        "AND NOT EXISTS (SELECT 1 FROM identity_review_occurrences o "
        "WHERE o.event_id = e.event_id) "
        f"{scope}"
        "AND (e.occurred_at, e.event_id) > (%(cursor_ts)s, %(cursor_eid)s) "
        "ORDER BY e.occurred_at ASC, e.event_id ASC "
        "LIMIT %(batch)s"
    )


def _more_sql(*, only_ids: bool) -> str:
    scope = "AND e.event_id = ANY(%(only_ids)s) " if only_ids else ""
    types = ", ".join(f"'{name}'" for name in RESEARCH_EVENT_TYPES)
    return (
        "SELECT EXISTS (SELECT 1 FROM agent_events e "
        f"WHERE e.event_type IN ({types}) "
        "AND e.retention_class = 'permanent' "
        "AND e.details->'identity_disputes' <> '{}'::jsonb "
        "AND NOT EXISTS (SELECT 1 FROM identity_review_occurrences o "
        "WHERE o.event_id = e.event_id) "
        f"{scope}"
        "AND (e.occurred_at, e.event_id) > (%(cursor_ts)s, %(cursor_eid)s)) AS more"
    )


def _row_to_event(row: dict[str, Any]) -> AgentEvent:
    """Rebuild the stored event model verbatim (never a new identity)."""
    return AgentEvent.model_validate(
        {
            "event_id": row["event_id"],
            "timestamp": row["occurred_at"],
            "agent": row["agent"],
            "run_id": row["run_id"],
            "correlation_id": row.get("correlation_id") or "",
            "property_id": row.get("property_id"),
            "event_type": row["event_type"],
            "severity": row["severity"],
            "summary": row["summary"],
            "details": row.get("details") or {},
            "evidence_refs": row.get("evidence_refs") or [],
            "parent_event_id": row.get("parent_event_id"),
            "retention_class": row["retention_class"],
        }
    )


def _fetch_batch(cursor_ts, cursor_eid, batch_size, only_ids, config=None):  # type: ignore[no-untyped-def]
    """Fetch one bounded keyset page on a short-lived connection."""
    params = {
        "cursor_ts": cursor_ts,
        "cursor_eid": cursor_eid,
        "batch": batch_size,
        "only_ids": list(only_ids) if only_ids is not None else None,
    }
    with get_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(_scan_sql(only_ids=only_ids is not None), params)
            return list(cur.fetchall() or [])


def _more_remain(cursor_ts, cursor_eid, only_ids, config=None) -> bool:  # type: ignore[no-untyped-def]
    params = {
        "cursor_ts": cursor_ts,
        "cursor_eid": cursor_eid,
        "only_ids": list(only_ids) if only_ids is not None else None,
    }
    with get_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(_more_sql(only_ids=only_ids is not None), params)
            row = cur.fetchone()
    return bool(row and row.get("more"))


def _occurrence_exists(event_id: str, config=None) -> bool:  # type: ignore[no-untyped-def]
    """Independent existence probe (disambiguates lost commits from failures)."""
    with get_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 AS one FROM identity_review_occurrences "
                "WHERE event_id = %(event_id)s",
                {"event_id": event_id},
            )
            return cur.fetchone() is not None


def _process_row(row: dict[str, Any], config=None) -> tuple[str, str | None]:  # type: ignore[no-untyped-def]
    """Reconcile one scanned row; returns (outcome, reason)."""
    event_id = str(row.get("event_id") or "")
    try:
        event = _row_to_event(row)
    except Exception as exc:
        return "failed", f"reconstruction_failed: {type(exc).__name__}"
    try:
        ctx = review_context_from_event(event)
    except IdentityReviewError as exc:
        return "failed", f"review_context_failed: {exc}"
    if ctx is None:
        # The scan pre-filters dispute-claiming rows, so a declined
        # context means a malformed disputes shape: report it, never
        # silently skip it.
        return "failed", "writer declined dispute-claiming event"
    try:
        with get_connection(config) as conn:
            try:
                review_id = record_review_occurrence(conn, event)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
    except (DatabaseUnavailableError, DatabaseNotConfiguredError) as exc:
        try:
            if _occurrence_exists(event.event_id, config):
                return "already", None
        except (DatabaseUnavailableError, DatabaseNotConfiguredError):
            pass
        return "failed", f"database_error: {type(exc).__name__}"
    if review_id is None:  # pragma: no cover - defensive; ctx was accepted
        return "failed", "writer declined dispute-claiming event"
    return "reconciled", None


def reconcile_once(  # type: ignore[no-untyped-def]
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    only_event_ids: list[str] | None = None,
    config=None,
) -> ReconcileReport:
    """Sweep the eligible backlog once; full sweep unless ``limit`` is set.

    Scan-level database errors propagate for the CLI to map to exit
    codes; every row-level outcome is captured in the report.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    report = ReconcileReport()
    cursor_ts = datetime.min.replace(tzinfo=timezone.utc)
    cursor_eid = ""
    truncated = False
    while True:
        rows = _fetch_batch(cursor_ts, cursor_eid, batch_size, only_event_ids, config)
        if not rows:
            break
        for row in rows:
            if limit is not None and report.examined >= limit:
                truncated = True
                break
            cursor_ts = row["occurred_at"]
            cursor_eid = row["event_id"]
            report.examined += 1
            outcome, reason = _process_row(row, config)
            if outcome == "reconciled":
                report.reconciled += 1
            elif outcome == "already":
                report.already_reconciled += 1
            elif outcome == "skipped":
                report.skipped_ineligible += 1
            else:
                if len(report.failed) < MAX_FAILURE_SAMPLE:
                    report.failed.append(
                        ReconcileFailure(str(row.get("event_id") or ""), reason or "")
                    )
                else:
                    report.omitted_failures += 1
        if truncated:
            break
    if truncated:
        report.more_may_remain = _more_remain(cursor_ts, cursor_eid, only_event_ids, config)
    report.status = "partial" if report.total_failed else "ok"
    return report


def _positive_int(raw: str | None, name: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m app.identity_review_reconcile``."""
    parser = argparse.ArgumentParser(
        description="Reconcile delivered research verdicts into identity reviews."
    )
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="optional operator valve; full sweep is the default")
    parser.add_argument("--event-id", action="append", default=None,
                        help="restrict the sweep (repeatable)")
    args = parser.parse_args(argv)
    try:
        batch_size = args.batch_size
        if batch_size is None:
            batch_size = _positive_int(os.getenv("GROWUP_RECONCILE_BATCH"), "batch size")
        if batch_size is None:
            batch_size = DEFAULT_BATCH_SIZE
        if batch_size < 1:
            raise ValueError("batch size must be positive")
        limit = args.limit
        if limit is None:
            limit = _positive_int(os.getenv("GROWUP_RECONCILE_LIMIT"), "limit")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
    except ValueError as exc:
        print(f"reconcile config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    only_ids = list(args.event_id) if args.event_id else None
    try:
        report = reconcile_once(batch_size=batch_size, limit=limit, only_event_ids=only_ids)
    except DatabaseNotConfiguredError:
        print("reconcile error: database_not_configured", file=sys.stderr)
        return EXIT_CONFIG
    except DatabaseUnavailableError:
        print("reconcile error: database_unavailable", file=sys.stderr)
        return EXIT_OUTAGE
    print(report.summary_line())
    for failure in report.failed:
        print(f"failed {failure.event_id}: {failure.reason}")
    if report.omitted_failures:
        print(f"omitted {report.omitted_failures} further failures from this listing")
    return EXIT_OK if report.status == "ok" else EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
