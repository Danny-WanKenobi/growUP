"""Narrowly scoped evidence acquisition and replay for Raphael.

Acquisition runs one bounded Brave query per call and persists the
collected results as a server-owned research artifact with actual
collection provenance and a content digest. Replay loads a persisted
artifact by key without any network call.

Safety rules:
- Acquisition requires explicit approval (``approved=True``); the
  Wondrue layer grants it only in an authorized mode with a spending
  approval. There is no ambient authority.
- A per-run attempt bound (default 9, hard ceiling 18) is enforced
  against persisted usage, so the bound survives month changes and
  restarts. Monthly Brave accounting is preserved by the caller.
- Ambiguous outcomes (timeouts, transport failures) are recorded as
  ``attempt="ambiguous"`` and are never silently retried.
- Human-supplied or reviewed evidence keeps its own provenance and is
  never relabeled ``adapter_collected``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents.gabriel.sources.brave import (
    BraveConfigError,
    BraveError,
    BraveTimeout,
    execute_query,
)
from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload, is_demo_id
from app.workflow_db import (
    WorkflowError,
    add_usage_in_transaction,
    append_error_in_transaction,
    get_record_in_transaction,
    get_run_in_transaction,
    put_record_in_transaction,
)

USAGE_KEY = "brave_attempts"
DEFAULT_ATTEMPT_LIMIT = 9
MAX_ATTEMPT_LIMIT = 18


def _artifact_payload(
    assignment_id: str,
    query: str,
    results: list[dict[str, Any]],
    attempt: str,
    error: str,
    provenance: str = "adapter_collected",
) -> dict[str, Any]:
    return {
        "provider": "brave",
        "assignment_id": assignment_id,
        "query": query,
        "results": results,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "collection_provenance": provenance,
        "attempt": attempt,
        "error": error,
    }


def _result_payload(item: Any) -> dict[str, Any]:
    dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
    return {
        "url": dumped.get("url", ""),
        "title": dumped.get("title", ""),
        "snippet": dumped.get("snippet", ""),
        "domain": dumped.get("domain", ""),
        "published_at": dumped.get("published_at"),
        "retrieved_at": dumped.get("retrieved_at", ""),
        "rank": dumped.get("rank"),
    }


def _attempt_bound(run: Any) -> tuple[int, int]:
    """Return (used, limit) for Brave attempts on a run."""
    limit = run.limits.get("brave_attempts", DEFAULT_ATTEMPT_LIMIT)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise WorkflowError("invalid brave_attempts limit on run")
    limit = min(max(limit, 0), MAX_ATTEMPT_LIMIT)
    used = run.usage.get(USAGE_KEY, 0)
    try:
        used = int(used)
    except (TypeError, ValueError):
        used = 0
    return used, limit


def begin_attempt_in_transaction(
    conn: Any, run_id: UUID, assignment_id: str, *, approved: bool = False
) -> None:
    """Commit the usage charge and started-marker BEFORE the provider call.

    The caller must commit immediately: if the process crashes after
    the provider call but before the artifact persists, resume sees a
    started-marker without an artifact and refuses to silently retry.
    """
    if not approved:
        raise WorkflowError("evidence acquisition requires explicit approval")
    run = get_run_in_transaction(conn, run_id)
    if run is None:
        raise WorkflowError(f"unknown workflow run {run_id}")
    used, limit = _attempt_bound(run)
    if used >= limit:
        raise WorkflowError(f"run Brave attempt bound reached ({used}/{limit})")
    add_usage_in_transaction(conn, run_id, USAGE_KEY, 1)
    append_error_in_transaction(
        conn,
        run_id,
        {"assignment_id": assignment_id, "attempt": "started"},
    )


def acquire_evidence_in_transaction(
    conn: Any,
    run_id: UUID,
    assignment_id: str,
    query: str,
    *,
    approved: bool = False,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """Run one approved acquisition and persist its artifact (no commit).

    Returns the persisted artifact payload. Raises WorkflowError when
    unapproved or over the run's attempt bound; records failed and
    ambiguous attempts instead of raising for provider failures. The
    usage charge is recorded by begin_attempt_in_transaction, which the
    caller commits before this runs.
    """
    if not approved:
        raise WorkflowError("evidence acquisition requires explicit approval")
    if not assignment_id or not query:
        raise WorkflowError("assignment_id and query are required")
    if is_demo_id(assignment_id):
        raise WorkflowError("demo identifiers are rejected by production acquisition")
    run = get_run_in_transaction(conn, run_id)
    if run is None:
        raise WorkflowError(f"unknown workflow run {run_id}")
    used, limit = _attempt_bound(run)
    if used > limit:
        raise WorkflowError(f"run Brave attempt bound reached ({used}/{limit})")
    try:
        found = execute_query(query, assignment_id, timeout_seconds)
    except BraveConfigError:
        raise
    except BraveTimeout as exc:
        payload = _artifact_payload(assignment_id, query, [], "ambiguous", f"timeout: {exc}")
    except BraveError as exc:
        payload = _artifact_payload(assignment_id, query, [], "ambiguous", f"provider: {exc}")
    except Exception as exc:  # transport-level surprise; record, do not retry
        payload = _artifact_payload(assignment_id, query, [], "ambiguous", f"transport: {type(exc).__name__}")
    else:
        payload = _artifact_payload(
            assignment_id, query, [_result_payload(item) for item in found], "completed", ""
        )
    stored = put_record_in_transaction(
        conn, run_id, kind=KIND_RESEARCH_ARTIFACT, record_key=assignment_id,
        version=1, payload=payload, digest=digest_payload(payload),
    )
    return stored.payload


def begin_attempt(
    run_id: UUID, assignment_id: str, *, approved: bool = False, config: Any = None
) -> None:
    """Record the usage charge and started-marker on a committed connection."""
    from app.properties_db import get_connection

    conn = get_connection(config)
    try:
        begin_attempt_in_transaction(conn, run_id, assignment_id, approved=approved)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def replay_artifact(conn: Any, run_id: UUID, assignment_id: str) -> dict[str, Any]:
    """Load a persisted artifact without any network call."""
    record = get_record_in_transaction(conn, run_id, KIND_RESEARCH_ARTIFACT, assignment_id)
    if record is None:
        raise WorkflowError(f"no persisted artifact for {assignment_id}")
    return record.payload
