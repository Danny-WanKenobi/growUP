"""Narrowly scoped evidence acquisition and replay for Raphael.

:func:`ensure_artifact_owned` is the single supported acquisition
entry point: it claims one assignment under the run lock (shared
monthly reservation plus the durable per-run charge), transmits
exactly one bounded Brave query with no business transaction open,
and persists the collected results as a server-owned research
artifact with actual collection provenance and a content digest.
Replay loads a persisted artifact by key without any network call.
No other helper in this module transmits.

Safety rules:
- Acquisition requires explicit approval (``approved=True``); the
  Wondrue layer grants it only in an authorized mode with a spending
  approval. There is no ambient authority.
- Every transmitted coordinator request reserves one unit of the
  existing shared monthly Brave budget (``app/research_budget_db.py``)
  before transmission, and also respects the durable per-run attempt
  bound (default 9, hard ceiling 18). Either exhaustion refuses the
  provider call. Reservations are never refunded, so ambiguous and
  failed transmitted attempts count conservatively.
- Exactly one monthly reservation covers one transmitted request:
  reservation happens in ``begin_attempt`` only. Replay paths and
  the research-run path (which reserves for its own transmissions)
  never double-charge the same request.
- One assignment is claimed atomically: under the locked run row,
  ``begin_attempt`` rechecks the stored artifact and started-marker
  before charging either budget, returning ``claimed``, ``replayed``,
  or raising a refusal. Only the caller that successfully claims may
  transmit, and the lock is never held during HTTP.
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
from app.research_budget_db import (
    BudgetConfigError,
    BudgetExhausted,
    reserve_requests,
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
        "image_url": dumped.get("image_url"),
        "images": dumped.get("images", []),
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


def _transmit(
    query: str, assignment_id: str, timeout_seconds: float
) -> tuple[list[Any], str, str]:
    """Run one provider request outside any business transaction.

    Returns ``(results, attempt, error)``: ``attempt`` is
    ``"completed"`` with normalized results on success, else
    ``"ambiguous"`` with a sanitized error (timeouts, transport and
    provider failures never raise here, so the caller always persists
    an attributable artifact). Only missing credentials raise
    (``BraveConfigError``): nothing was transmitted, so nothing is
    recorded. This is the single shared transmit seam, used only by
    :func:`ensure_artifact_owned` after a successful claim; no other
    path duplicates provider logic.
    """
    try:
        found = execute_query(query, assignment_id, timeout_seconds)
    except BraveConfigError:
        raise
    except BraveTimeout as exc:
        return [], "ambiguous", f"timeout: {exc}"
    except BraveError as exc:
        return [], "ambiguous", f"provider: {exc}"
    except Exception as exc:  # transport-level surprise; record, do not retry
        return [], "ambiguous", f"transport: {type(exc).__name__}"
    return list(found), "completed", ""


def _started_markers(run: Any, assignment_id: str) -> list[dict[str, Any]]:
    """Started-markers for one assignment on a run (never silently retried)."""
    return [
        e for e in (run.errors if run else [])
        if e.get("assignment_id") == assignment_id and e.get("attempt") == "started"
    ]


def begin_attempt(
    run_id: UUID, assignment_id: str, *, approved: bool = False, config: Any = None
) -> dict[str, Any]:
    """Claim one assignment atomically (committed) for a single transmit.

    Under the locked run row, and before charging either budget: the
    stored artifact and started-marker are rechecked (a pre-lock
    observation may be stale when two callers race), stopped runs
    are refused, and the persisted attempt bound is enforced. Only
    the caller that successfully claims may transmit. Returns
    ``{"outcome": "claimed"}`` after both budgets and the
    started-marker commit, or ``{"outcome": "replayed", "payload"}``
    when completed evidence already exists (no new charge); every
    other state raises WorkflowError without charging. The lock is
    never held during HTTP: transmission happens after this returns.
    A crash between the monthly reservation and the per-run commit
    over-counts the month by one (conservative, fail-closed);
    monthly exhaustion itself charges nothing.
    """
    from app.properties_db import get_connection

    if not approved:
        raise WorkflowError("evidence acquisition requires explicit approval")
    conn = get_connection(config)
    try:
        run = get_run_in_transaction(conn, run_id, for_update=True)
        if run is None:
            raise WorkflowError(f"unknown workflow run {run_id}")
        if run.status == "stopped":
            raise WorkflowError("run is stopped")
        existing = get_record_in_transaction(
            conn, run_id, KIND_RESEARCH_ARTIFACT, assignment_id
        )
        if existing is not None:
            if existing.payload.get("attempt") != "completed":
                raise WorkflowError(
                    f"prior acquisition recorded as {existing.payload.get('attempt')}; "
                    "retry requires a new assignment id"
                )
            return {"outcome": "replayed", "payload": existing.payload}
        if _started_markers(run, assignment_id):
            raise WorkflowError(
                "ambiguous prior attempt crashed before completion; "
                "retry requires a new assignment id"
            )
        used, limit = _attempt_bound(run)
        if used >= limit:
            raise WorkflowError(f"run Brave attempt bound reached ({used}/{limit})")
        try:
            reserve_requests(1, config=config)
        except BudgetExhausted as exc:
            raise WorkflowError(
                "monthly Brave budget exhausted; refusing provider call"
            ) from exc
        except BudgetConfigError as exc:
            raise WorkflowError(
                "Brave monthly budget misconfigured; refusing provider call"
            ) from exc
        add_usage_in_transaction(conn, run_id, USAGE_KEY, 1)
        append_error_in_transaction(
            conn,
            run_id,
            {"assignment_id": assignment_id, "attempt": "started"},
        )
        conn.commit()
        return {"outcome": "claimed"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ensure_artifact_owned(
    run_id: UUID,
    assignment_id: str,
    query: str,
    *,
    approved: bool = False,
    timeout_seconds: float = 12.0,
    config: Any = None,
) -> dict[str, Any]:
    """Acquire (or replay) one artifact with the provider outside any txn.

    Owned-connection flow with committed phases: the run must already
    be durably committed (a separate connection reads it, so an
    uncommitted run is correctly reported unknown). A fast unlocked
    precheck replays completed evidence or fails fast on terminal
    states, but the authoritative decision is the atomic claim in
    :func:`begin_attempt`, which rechecks artifact and marker under
    the run lock: only the ``claimed`` caller transmits (with no
    business transaction open), a ``replayed`` caller returns the
    stored payload uncharged, and refusals raise before anything is
    transmitted. Raises WorkflowError on any refusal or
    non-completed attempt; a transmitted attempt (completed or
    ambiguous) always leaves an attributable persisted artifact.
    """
    from app.properties_db import get_connection

    if not approved:
        raise WorkflowError("evidence acquisition requires explicit approval")
    if not assignment_id or not query:
        raise WorkflowError("assignment_id and query are required")
    if is_demo_id(assignment_id):
        raise WorkflowError("demo identifiers are rejected by production acquisition")
    conn = get_connection(config)
    try:
        existing = get_record_in_transaction(
            conn, run_id, KIND_RESEARCH_ARTIFACT, assignment_id
        )
        if existing is not None:
            if existing.payload.get("attempt") != "completed":
                raise WorkflowError(
                    f"prior acquisition recorded as {existing.payload.get('attempt')}; "
                    "retry requires a new assignment id"
                )
            return existing.payload
        run = get_run_in_transaction(conn, run_id)
        if run is None:
            raise WorkflowError(f"unknown workflow run {run_id}")
        if run.status == "stopped":
            raise WorkflowError("run is stopped")
        if _started_markers(run, assignment_id):
            raise WorkflowError(
                "ambiguous prior attempt crashed before completion; "
                "retry requires a new assignment id"
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    claim = begin_attempt(run_id, assignment_id, approved=approved, config=config)
    if claim.get("outcome") == "replayed":
        return claim["payload"]
    found, attempt, error = _transmit(query, assignment_id, timeout_seconds)
    if attempt == "completed":
        payload = _artifact_payload(
            assignment_id, query, [_result_payload(item) for item in found], attempt, error
        )
    else:
        payload = _artifact_payload(assignment_id, query, [], attempt, error)
    if attempt == 'completed':
        from app.dossier_media import collect_search_images
        try:
            collect_search_images(run_id, payload['results'], config=config)
        except (WorkflowError, OSError):
            # An optional-media problem must not discard already collected facts.
            payload['image_cache_status'] = 'unavailable; retained textual evidence preserved'
    conn = get_connection(config)
    try:
        stored = put_record_in_transaction(
            conn, run_id, kind=KIND_RESEARCH_ARTIFACT, record_key=assignment_id,
            version=1, payload=payload, digest=digest_payload(payload),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if stored.payload.get("attempt") != "completed":
        raise WorkflowError(f"acquisition {attempt}: {error}")
    return stored.payload


def replay_artifact(conn: Any, run_id: UUID, assignment_id: str) -> dict[str, Any]:
    """Load a persisted artifact without any network call."""
    record = get_record_in_transaction(conn, run_id, KIND_RESEARCH_ARTIFACT, assignment_id)
    if record is None:
        raise WorkflowError(f"no persisted artifact for {assignment_id}")
    return record.payload
