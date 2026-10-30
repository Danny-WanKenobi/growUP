"""Uriel: opportunity and outreach campaign packages (canonical role).

Uriel prepares genuine campaign packages from dossier lineage, evidence,
Raphael decisions, Onesimus engineering comparisons, Michael economics,
unknowns, and next actions. He does not send, schedule, or otherwise
execute outreach: no real sender is implemented, and preparation-only
scope holds in both DEMO_MODE values.

Packages are append-only: one outcome row per (dossier, version) with
package_state draft/reviewed/prepared decided at insert. The row is
genuine in both modes (simulated=False always); DEMO_MODE is recorded
informationally and never relabels real preparation as synthetic.
Execution state stays not_attempted until a real sender exists; Priscilla
records simulated or actual delivery as separate conversion events.

Commands (operator-invoked, from the repository root)::

    python -m app.uriel list [--state draft|reviewed|prepared]
    python -m app.uriel show <entry-id>
"""

from __future__ import annotations

import json
import sys
from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.modes import demo_mode_enabled
from app.properties_db import get_connection
from app.workflow import is_demo_id

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()

URIEL_PACKAGE_VERSION = "uriel-1"
ROLE_SCHEMA = "canonical1-uriel"
PACKAGE_STATES = ("draft", "reviewed", "prepared")


class UrielError(ValueError):
    """Invalid package request (bad identity, custody, state, or duplicate)."""


def _uuid(value: Any, name: str) -> UUID:
    if is_demo_id(value):
        raise UrielError(f"demo identifiers are rejected by Uriel: {name}")
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise UrielError(f"{name} must be an operational UUID") from exc


def _receipt_in_transaction(conn: Any, receipt_id: UUID) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT receipt_id, dossier_id, version, snapshot_id, kind, "
                "from_agent, to_agent FROM custody_receipts "
                "WHERE receipt_id = %(rid)s",
                {"rid": receipt_id},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    if row is None:
        raise UrielError("unknown custody receipt for package")
    return dict(row)


def prepare_package_in_transaction(
    conn: Any,
    *,
    dossier_id: UUID | str,
    version: int,
    receipt_id: UUID | str,
    prepared_by: str,
    dossier: dict[str, Any] | None = None,
    engineering: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    unknowns: list[str] | None = None,
    blockers: list[str] | None = None,
    next_actions: list[str] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare one genuine package; state decided at insert (append-only).

    Custody: the referenced receipt must exist, name this dossier
    version, and hand custody to Uriel. State is prepared when a
    review is supplied and no blockers remain, reviewed when a review
    is supplied with blockers, else draft. Re-preparing the same
    dossier version is rejected (one outcome row per version).
    """
    dossier_uuid = _uuid(dossier_id, "dossier_id")
    receipt_uuid = _uuid(receipt_id, "receipt_id")
    if not isinstance(version, int) or version < 1:
        raise UrielError("version must be a positive integer")
    if not prepared_by or not str(prepared_by).strip():
        raise UrielError("prepared_by principal is required")
    receipt = _receipt_in_transaction(conn, receipt_uuid)
    if str(receipt.get("dossier_id")) != str(dossier_uuid):
        raise UrielError("receipt names a different dossier")
    if int(receipt.get("version", -1)) != version:
        raise UrielError("receipt names a different dossier version")
    if str(receipt.get("to_agent", "")) != "uriel":
        raise UrielError("receipt does not hand custody to Uriel")
    snapshot_uuid = _uuid(receipt.get("snapshot_id"), "snapshot_id")
    from app.pilot import (
        cell_from_dossier,
        record_transition_in_transaction,
    )

    cell = cell_from_dossier(dossier or {})
    state = "draft"
    if review is not None:
        if not str(review.get("reviewed_by", "") or "").strip():
            raise UrielError("review requires a reviewed_by principal")
        state = "prepared" if not (blockers or []) else "reviewed"
    payload = {
        "package_version": URIEL_PACKAGE_VERSION,
        "prepared_by": str(prepared_by),
        "demo_mode": demo_mode_enabled(),
        "lineage": {
            "dossier_id": str(dossier_uuid),
            "version": version,
            "snapshot_id": str(snapshot_uuid),
            "receipt_id": str(receipt_uuid),
            "receipt_kind": str(receipt.get("kind", "")),
            "receipt_from": str(receipt.get("from_agent", "")),
        },
        "cell": {"county": cell[0], "category": cell[1]} if cell else None,
        "engineering": engineering or {},
        "economics": economics or {},
        "unknowns": list(unknowns or []),
        "blockers": list(blockers or []),
        "next_actions": list(next_actions or []),
        "review": review or {},
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_id FROM uriel_packages "
                "WHERE dossier_id = %(dossier)s AND version = %(version)s",
                {"dossier": dossier_uuid, "version": version},
            )
            if cur.fetchone() is not None:
                raise UrielError("package already prepared for this dossier version")
            entry_id = uuid4()
            cur.execute(
                "INSERT INTO uriel_packages (entry_id, dossier_id, version, "
                "snapshot_id, receipt_id, demo_mode, simulated, payload, "
                "role_schema, package_state, execution_state) "
                "VALUES (%(eid)s, %(dossier)s, %(version)s, %(snapshot)s, "
                "%(receipt)s, %(demo)s, FALSE, %(payload)s, %(schema)s, "
                "%(state)s, 'not_attempted') "
                "RETURNING entry_id, created_at",
                {
                    "eid": entry_id,
                    "dossier": dossier_uuid,
                    "version": version,
                    "snapshot": snapshot_uuid,
                    "receipt": receipt_uuid,
                    "demo": payload["demo_mode"],
                    "payload": json.dumps(payload),
                    "schema": ROLE_SCHEMA,
                    "state": state,
                },
            )
            inserted = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        if getattr(exc, "sqlstate", "") == "23505":
            raise UrielError("package already prepared for this dossier version") from exc
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    if cell is not None:
        record_transition_in_transaction(
            conn, county=cell[0], category=cell[1], dossier_id=dossier_uuid,
            transition="package_prepared", record_key=f"package-v{version}",
        )
    created = dict(inserted) if inserted is not None else {}
    return {
        "entry_id": str(entry_id),
        "dossier_id": str(dossier_uuid),
        "version": version,
        "package_state": state,
        "simulated": False,
        "payload": payload,
        "created_at": str(created.get("created_at", "")),
    }


def get_package_in_transaction(conn: Any, entry_id: UUID | str) -> dict[str, Any] | None:
    """Load one package row with its JSON payload decoded."""
    entry_uuid = _uuid(entry_id, "entry_id")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_id, dossier_id, version, snapshot_id, receipt_id, "
                "demo_mode, simulated, payload, role_schema, package_state, "
                "execution_state, created_at FROM uriel_packages "
                "WHERE entry_id = %(eid)s",
                {"eid": entry_uuid},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    if row is None:
        return None
    record = dict(row)
    payload = record.get("payload")
    if isinstance(payload, str):
        record["payload"] = json.loads(payload)
    for key in ("entry_id", "dossier_id", "snapshot_id", "receipt_id"):
        record[key] = str(record.get(key))
    record["created_at"] = str(record.get("created_at", ""))
    return record


def list_packages_in_transaction(
    conn: Any, *, package_state: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List packages newest-first, optionally filtered by state."""
    if package_state is not None and package_state not in PACKAGE_STATES:
        raise UrielError(f"unknown package state: {package_state!r}")
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise UrielError("limit must be 1..200")
    try:
        with conn.cursor() as cur:
            if package_state is None:
                cur.execute(
                    "SELECT entry_id, dossier_id, version, package_state, "
                    "created_at FROM uriel_packages "
                    "ORDER BY created_at DESC, entry_id DESC LIMIT %(limit)s",
                    {"limit": limit},
                )
            else:
                cur.execute(
                    "SELECT entry_id, dossier_id, version, package_state, "
                    "created_at FROM uriel_packages "
                    "WHERE package_state = %(state)s "
                    "ORDER BY created_at DESC, entry_id DESC LIMIT %(limit)s",
                    {"state": package_state, "limit": limit},
                )
            rows = cur.fetchall() or []
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    return [
        {
            "entry_id": str(row.get("entry_id")),
            "dossier_id": str(row.get("dossier_id")),
            "version": row.get("version"),
            "package_state": row.get("package_state"),
            "created_at": str(row.get("created_at", "")),
        }
        for row in (dict(item) for item in rows)
    ]


def _owned(fn: Any, *args: Any, config: DatabaseConfig | None = None, **kwargs: Any) -> Any:
    conn = get_connection(config)
    try:
        result = fn(conn, *args, **kwargs)
        conn.commit()
        return result
    except (UrielError, DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _print_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("no packages")
        return
    for row in rows:
        print(
            f"{row['entry_id']} dossier={row['dossier_id']} "
            f"v{row['version']} state={row['package_state']} "
            f"created={row['created_at']}"
        )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("list", "show"):
        print("usage: python -m app.uriel list [--state S] | show <entry-id>", file=sys.stderr)
        return 2
    try:
        if args[0] == "list":
            state = None
            if args[1:3] == ["--state"] or (len(args) == 3 and args[1] == "--state"):
                state = args[2]
            elif len(args) > 1:
                print("usage: python -m app.uriel list [--state S]", file=sys.stderr)
                return 2
            _print_list(_owned(list_packages_in_transaction, package_state=state))
            return 0
        if len(args) != 2:
            print("usage: python -m app.uriel show <entry-id>", file=sys.stderr)
            return 2
        record = _owned(get_package_in_transaction, args[1])
        if record is None:
            print("package not found", file=sys.stderr)
            return 1
        print(json.dumps(record, indent=2, sort_keys=True, default=str))
        return 0
    except (UrielError, DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
        print(f"uriel: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
