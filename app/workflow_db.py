"""Typed workflow-run persistence (migration 012).

Cursor-level helpers compose into caller-owned transactions (e.g. the
consequential coordinator in app/observability/transactions.py); public
wrappers own a short connection for single-operation calls.

Idempotency rules:
- create_run with a repeated idempotency key returns the existing run.
- put_record with a repeated (kind, key, version) and identical digest
  returns the existing row; a different digest raises WorkflowConflictError.
- advance_stage is compare-and-set on the expected stage for safe resume.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection
from app.workflow import RECORD_KINDS, WorkflowRecord, WorkflowRun, is_demo_id

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()


class WorkflowError(ValueError):
    """Invalid workflow request (bad kind, bad stage, conflicting replay)."""


class WorkflowConflictError(WorkflowError):
    """A versioned record was replayed with a different digest."""


def _wrap_driver(exc: Exception) -> DatabaseUnavailableError:
    return DatabaseUnavailableError("workflow_unavailable")


def _run_from_row(row: dict[str, Any]) -> WorkflowRun:
    data = dict(row)
    for key in ("limits", "usage"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    if isinstance(data.get("errors"), str):
        data["errors"] = json.loads(data["errors"])
    return WorkflowRun.model_validate(data)


def _record_from_row(row: dict[str, Any]) -> WorkflowRecord:
    data = dict(row)
    if isinstance(data.get("payload"), str):
        data["payload"] = json.loads(data["payload"])
    return WorkflowRecord.model_validate(data)


def create_run_in_transaction(
    conn: Any,
    *,
    idempotency_key: str,
    stage: str = "created",
    correlation_id: str = "",
    limits: dict[str, Any] | None = None,
) -> WorkflowRun:
    """Insert a run, or return the existing one for a repeated key (no commit)."""
    if not idempotency_key:
        raise WorkflowError("idempotency_key is required")
    run_id = uuid4()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workflow_runs (run_id, idempotency_key, stage, "
                "correlation_id, limits) VALUES (%(run_id)s, %(key)s, "
                "%(stage)s, %(correlation)s, %(limits)s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                {
                    "run_id": run_id,
                    "key": idempotency_key,
                    "stage": stage,
                    "correlation": correlation_id,
                    "limits": json.dumps(limits or {}),
                },
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT * FROM workflow_runs WHERE idempotency_key = %(key)s",
                    {"key": idempotency_key},
                )
                row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if row is None:  # pragma: no cover - defensive; key was just inserted
        raise DatabaseUnavailableError("workflow_unavailable")
    return _run_from_row(dict(row))


def get_run_in_transaction(conn: Any, run_id: UUID) -> WorkflowRun | None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM workflow_runs WHERE run_id = %(rid)s", {"rid": run_id})
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return _run_from_row(dict(row)) if row is not None else None


def set_status_in_transaction(conn: Any, run_id: UUID, status: str) -> None:
    """Set run status without moving the stage (e.g. stopped)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET status = %(status)s, updated_at = now() "
                "WHERE run_id = %(rid)s",
                {"rid": run_id, "status": status},
            )
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc


def pilot_progress_in_transaction(conn: Any) -> dict[str, Any]:
    """Honest pilot counts from persisted qualification records only."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload ->> 'outcome' AS outcome, COUNT(*) AS n "
                "FROM workflow_records WHERE kind = 'qualification' "
                "GROUP BY payload ->> 'outcome'"
            )
            by_outcome = {row["outcome"]: row["n"] for row in cur.fetchall()}
            cur.execute(
                "SELECT payload ->> 'county_credit' AS county, COUNT(*) AS n "
                "FROM workflow_records WHERE kind = 'qualification' "
                "AND payload ->> 'outcome' = 'qualified' "
                "AND COALESCE(payload ->> 'county_credit', '') != '' "
                "GROUP BY payload ->> 'county_credit'"
            )
            by_county = {row["county"]: row["n"] for row in cur.fetchall()}
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return {"by_outcome": by_outcome, "county_credits": by_county}


def advance_stage_in_transaction(
    conn: Any, run_id: UUID, expected_stage: str, next_stage: str, status: str = "open"
) -> bool:
    """Move a run from expected_stage to next_stage (compare-and-set)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET stage = %(next)s, status = %(status)s, "
                "updated_at = now() WHERE run_id = %(rid)s AND stage = %(expected)s",
                {"rid": run_id, "expected": expected_stage, "next": next_stage, "status": status},
            )
            return cur.rowcount == 1
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc


def add_usage_in_transaction(conn: Any, run_id: UUID, key: str, amount: int = 1) -> None:
    """Increment a usage counter (task/run bound accounting)."""
    if not key:
        raise WorkflowError("usage key is required")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET usage = jsonb_set(usage, %(path)s, "
                "to_jsonb(COALESCE((usage ->> %(key)s)::int, 0) + %(amount)s), "
                "true), updated_at = now() WHERE run_id = %(rid)s",
                {"path": [key], "key": key, "amount": amount, "rid": run_id},
            )
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc


def append_error_in_transaction(conn: Any, run_id: UUID, error: dict[str, Any]) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_runs SET errors = errors || %(error)s::jsonb, "
                "updated_at = now() WHERE run_id = %(rid)s",
                {"rid": run_id, "error": json.dumps(error)},
            )
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc


def put_record_in_transaction(
    conn: Any,
    run_id: UUID,
    *,
    kind: str,
    record_key: str,
    version: int,
    payload: dict[str, Any],
    digest: str,
) -> WorkflowRecord:
    """Insert one versioned record; identical replays return the existing row."""
    if kind not in RECORD_KINDS:
        raise WorkflowError(f"unknown record kind: {kind}")
    if not record_key:
        raise WorkflowError("record_key is required")
    if is_demo_id(record_key):
        raise WorkflowError("demo identifiers are rejected by production storage")
    if version < 1:
        raise WorkflowError("version must be >= 1")
    record_id = uuid4()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workflow_records (record_id, run_id, kind, record_key, "
                "version, payload, digest) VALUES (%(rid)s, %(run)s, %(kind)s, "
                "%(key)s, %(version)s, %(payload)s, %(digest)s) "
                "ON CONFLICT (run_id, kind, record_key, version) DO NOTHING RETURNING *",
                {
                    "rid": record_id,
                    "run": run_id,
                    "kind": kind,
                    "key": record_key,
                    "version": version,
                    "payload": json.dumps(payload),
                    "digest": digest,
                },
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT * FROM workflow_records WHERE run_id = %(run)s "
                    "AND kind = %(kind)s AND record_key = %(key)s AND version = %(version)s",
                    {"run": run_id, "kind": kind, "key": record_key, "version": version},
                )
                row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if row is None:  # pragma: no cover - defensive; row was just inserted
        raise DatabaseUnavailableError("workflow_unavailable")
    existing = _record_from_row(dict(row))
    if existing.digest != digest:
        raise WorkflowConflictError(
            f"conflicting replay of {kind}/{record_key} v{version}"
        )
    return existing


def get_record_in_transaction(
    conn: Any, run_id: UUID, kind: str, record_key: str, version: int | None = None
) -> WorkflowRecord | None:
    try:
        with conn.cursor() as cur:
            if version is None:
                cur.execute(
                    "SELECT * FROM workflow_records WHERE run_id = %(run)s "
                    "AND kind = %(kind)s AND record_key = %(key)s "
                    "ORDER BY version DESC LIMIT 1",
                    {"run": run_id, "kind": kind, "key": record_key},
                )
            else:
                cur.execute(
                    "SELECT * FROM workflow_records WHERE run_id = %(run)s "
                    "AND kind = %(kind)s AND record_key = %(key)s AND version = %(version)s",
                    {"run": run_id, "kind": kind, "key": record_key, "version": version},
                )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return _record_from_row(dict(row)) if row is not None else None


def list_records_in_transaction(
    conn: Any, run_id: UUID, kind: str | None = None
) -> list[WorkflowRecord]:
    try:
        with conn.cursor() as cur:
            if kind is None:
                cur.execute(
                    "SELECT * FROM workflow_records WHERE run_id = %(run)s "
                    "ORDER BY kind, record_key, version",
                    {"run": run_id},
                )
            else:
                cur.execute(
                    "SELECT * FROM workflow_records WHERE run_id = %(run)s "
                    "AND kind = %(kind)s ORDER BY record_key, version",
                    {"run": run_id, "kind": kind},
                )
            rows = cur.fetchall()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return [_record_from_row(dict(row)) for row in rows]


def _owned(conn_fn: Any, *args: Any, config: DatabaseConfig | None = None, **kwargs: Any) -> Any:
    """Run one cursor-level helper on a short owned connection (commit once)."""
    conn = get_connection(config)
    try:
        result = conn_fn(conn, *args, **kwargs)
        conn.commit()
        return result
    except (WorkflowError, DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def create_run(
    idempotency_key: str,
    *,
    stage: str = "created",
    correlation_id: str = "",
    limits: dict[str, Any] | None = None,
    config: DatabaseConfig | None = None,
) -> WorkflowRun:
    return _owned(
        create_run_in_transaction,
        idempotency_key=idempotency_key,
        stage=stage,
        correlation_id=correlation_id,
        limits=limits,
        config=config,
    )


def get_run(run_id: UUID, config: DatabaseConfig | None = None) -> WorkflowRun | None:
    return _owned(get_run_in_transaction, run_id, config=config)
