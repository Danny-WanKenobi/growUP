"""Operational dossier custody persistence (migration 013).

Cursor-level helpers compose into caller-owned transactions; public
wrappers own a short connection for single-operation calls.

Rules enforced here (and partly by database triggers):

- Versions append monotonically per dossier; nothing updates them.
- A receipt requires an existing pre-handoff snapshot of its version.
- Forward receipts require the latest version (stale = refused).
  Return/review receipts may name an older version but record whether
  it was latest, so consumers never mistake them for current.
- Every receipt carries an HMAC over its core fields plus the previous
  receipt link, forming a per-dossier tamper-evident chain.
- Amendments require an enrolled authorizer and a reason, and create a
  new version; they never rewrite history.
- Archival moves old snapshots to the archive table; versions,
  receipts, and intake entries are permanent.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig
from app.custody import (
    MAX_NOTE_CHARS,
    PIPELINE_VERSION,
    SNAPSHOT_RETENTION_DAYS,
    CustodyAuthError,
    CustodyError,
    CustodyReceipt,
    CustodySnapshot,
    CustodyTamperError,
    DossierVersion,
    StaleHandoffError,
    check_amendment_permission,
    check_handoff_routing,
    check_jsonable,
    dossier_id_for_property,
    hmac_key,
    interpret_custody_agent,
    receipt_core,
    receipt_mac,
    snapshot_digest,
    verify_mac,
    version_digest,
)
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()


def _wrap_driver(exc: Exception) -> DatabaseUnavailableError:
    return DatabaseUnavailableError("custody_unavailable")


def _version_from_row(row: dict[str, Any]) -> DossierVersion:
    data = dict(row)
    for key in ("payload", "amendment"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return DossierVersion.model_validate(data)


def _snapshot_from_row(row: dict[str, Any], *, archived: bool) -> CustodySnapshot:
    data = dict(row)
    if isinstance(data.get("payload"), str):
        data["payload"] = json.loads(data["payload"])
    data["archived"] = archived
    return CustodySnapshot.model_validate(data)


def _receipt_from_row(row: dict[str, Any]) -> CustodyReceipt:
    data = dict(row)
    if isinstance(data.get("payload"), str):
        data["payload"] = json.loads(data["payload"])
    return CustodyReceipt.model_validate(data)


def _append_version_in_transaction(
    conn: Any,
    *,
    property_id: UUID | str,
    payload: dict[str, Any],
    created_by: str,
    supersedes: int | None = None,
    amendment: dict[str, Any] | None = None,
) -> DossierVersion:
    """Append the next version for a dossier (no commit)."""
    if not isinstance(payload, dict):
        raise CustodyError("version payload must be an object")
    check_jsonable(payload)
    if not (created_by or "").strip():
        raise CustodyError("created_by is required")
    dossier_id = dossier_id_for_property(property_id)
    # Validated above; normalize to UUID for storage.
    prop = property_id if isinstance(property_id, UUID) else UUID(str(property_id))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%(key)s))",
                {"key": f"custody:{dossier_id}"},
            )
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version "
                "FROM dossier_versions WHERE dossier_id = %(dossier)s",
                {"dossier": dossier_id},
            )
            row = cur.fetchone()
            version = int((row or {}).get("max_version", 0) or 0) + 1
            if supersedes is not None and supersedes != version - 1:
                raise CustodyError("amendment must supersede the latest version")
            digest = version_digest(
                property_id=prop,
                dossier_id=dossier_id,
                version=version,
                payload=payload,
                supersedes=supersedes,
                amendment=amendment,
                created_by=created_by,
            )
            cur.execute(
                "INSERT INTO dossier_versions (dossier_id, version, property_id, "
                "payload, digest, supersedes, amendment, created_by) "
                "VALUES (%(dossier)s, %(version)s, %(property)s, %(payload)s, "
                "%(digest)s, %(supersedes)s, %(amendment)s, %(created_by)s) "
                "RETURNING *",
                {
                    "dossier": dossier_id,
                    "version": version,
                    "property": prop,
                    "payload": json.dumps(payload),
                    "digest": digest,
                    "supersedes": supersedes,
                    "amendment": json.dumps(amendment) if amendment is not None else None,
                    "created_by": created_by,
                },
            )
            inserted = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if inserted is None:  # pragma: no cover - defensive; row was just inserted
        raise DatabaseUnavailableError("custody_unavailable")
    return _version_from_row(dict(inserted))


def create_version_in_transaction(
    conn: Any, *, property_id: UUID | str, payload: dict[str, Any], created_by: str
) -> DossierVersion:
    """Append a routine new version (v1 has no superseded version)."""
    return _append_version_in_transaction(
        conn, property_id=property_id, payload=payload, created_by=created_by
    )


def amend_in_transaction(
    conn: Any,
    *,
    property_id: UUID | str,
    payload: dict[str, Any],
    authorized_by: str,
    reason: str,
) -> DossierVersion:
    """Permission-checked amendment: a new version superseding the latest."""
    check_amendment_permission(authorized_by, reason)
    dossier_id = dossier_id_for_property(property_id)
    latest = latest_version_in_transaction(conn, dossier_id)
    if latest is None:
        raise CustodyError("cannot amend a dossier with no versions")
    # Amendments are permitted only over verified custody: integrity of
    # the existing chain is proven before any new version is appended.
    verify_chain_in_transaction(conn, dossier_id)
    return _append_version_in_transaction(
        conn,
        property_id=property_id,
        payload=payload,
        created_by=authorized_by.strip(),
        supersedes=latest.version,
        amendment={"authorized_by": authorized_by.strip(), "reason": reason.strip()},
    )


def get_version_in_transaction(
    conn: Any, dossier_id: UUID, version: int
) -> DossierVersion | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dossier_versions "
                "WHERE dossier_id = %(dossier)s AND version = %(version)s",
                {"dossier": dossier_id, "version": version},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return _version_from_row(dict(row)) if row is not None else None


def latest_version_in_transaction(conn: Any, dossier_id: UUID) -> DossierVersion | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dossier_versions WHERE dossier_id = %(dossier)s "
                "ORDER BY version DESC LIMIT 1",
                {"dossier": dossier_id},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return _version_from_row(dict(row)) if row is not None else None


def snapshot_in_transaction(conn: Any, *, dossier_id: UUID, version: int) -> CustodySnapshot:
    """Freeze an immutable pre-handoff snapshot (idempotent per version)."""
    stored = get_version_in_transaction(conn, dossier_id, version)
    if stored is None:
        raise CustodyError("cannot snapshot a missing dossier version")
    digest = snapshot_digest(stored.payload)
    snapshot_id = uuid4()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO custody_snapshots (snapshot_id, dossier_id, version, "
                "payload, digest) VALUES (%(sid)s, %(dossier)s, %(version)s, "
                "%(payload)s, %(digest)s) "
                "ON CONFLICT (dossier_id, version) DO NOTHING RETURNING *",
                {
                    "sid": snapshot_id,
                    "dossier": dossier_id,
                    "version": version,
                    "payload": json.dumps(stored.payload),
                    "digest": digest,
                },
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT * FROM custody_snapshots "
                    "WHERE dossier_id = %(dossier)s AND version = %(version)s",
                    {"dossier": dossier_id, "version": version},
                )
                row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if row is None:  # pragma: no cover - defensive; row was just inserted
        raise DatabaseUnavailableError("custody_unavailable")
    return _snapshot_from_row(dict(row), archived=False)


def _snapshot_row_for_version(
    conn: Any, dossier_id: UUID, version: int
) -> tuple[dict[str, Any], bool] | None:
    """Hot snapshot row for a version, else the archived row, else None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM custody_snapshots "
            "WHERE dossier_id = %(dossier)s AND version = %(version)s",
            {"dossier": dossier_id, "version": version},
        )
        row = cur.fetchone()
        if row is not None:
            return dict(row), False
        cur.execute(
            "SELECT * FROM custody_snapshots_archive "
            "WHERE dossier_id = %(dossier)s AND version = %(version)s",
            {"dossier": dossier_id, "version": version},
        )
        row = cur.fetchone()
        if row is not None:
            return dict(row), True
    return None


def get_snapshot_in_transaction(conn: Any, snapshot_id: UUID) -> CustodySnapshot | None:
    """Load a snapshot from the hot table or the archive; verify its digest."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM custody_snapshots WHERE snapshot_id = %(sid)s",
                {"sid": snapshot_id},
            )
            row = cur.fetchone()
            archived = False
            if row is None:
                cur.execute(
                    "SELECT * FROM custody_snapshots_archive WHERE snapshot_id = %(sid)s",
                    {"sid": snapshot_id},
                )
                row = cur.fetchone()
                archived = True
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if row is None:
        return None
    found = _snapshot_from_row(dict(row), archived=archived)
    if snapshot_digest(found.payload) != found.digest:
        raise CustodyTamperError("snapshot bytes do not match their digest")
    return found


def _order_chain(rows: list[dict[str, Any]]) -> list[CustodyReceipt]:
    """Order one dossier's receipts root-first via predecessor links.

    The ``prev_receipt`` chain is the sole authority for chain order:
    timestamps (equal within a transaction) and random UUIDs never
    decide it. Rejects forks, cycles, missing predecessors, and
    cross-dossier links as tamper. Historical rows are never rewritten.
    """
    receipts = [_receipt_from_row(dict(raw)) for raw in rows or []]
    if not receipts:
        return []
    if len({row.dossier_id for row in receipts}) != 1:
        raise CustodyTamperError("receipt chain mixes dossiers")
    roots = [row for row in receipts if row.prev_receipt is None]
    if len(roots) != 1:
        raise CustodyTamperError("receipt chain has no unique root")
    by_prev: dict[str, list[CustodyReceipt]] = {}
    for row in receipts:
        if row.prev_receipt is not None:
            by_prev.setdefault(str(row.prev_receipt), []).append(row)
    ordered = [roots[0]]
    seen = {str(roots[0].receipt_id)}
    while True:
        children = by_prev.get(str(ordered[-1].receipt_id), [])
        if not children:
            break
        if len(children) > 1:
            raise CustodyTamperError("receipt chain has a fork")
        child = children[0]
        if str(child.receipt_id) in seen:
            raise CustodyTamperError("receipt chain has a cycle")
        seen.add(str(child.receipt_id))
        ordered.append(child)
    if len(ordered) != len(receipts):
        raise CustodyTamperError("receipt chain link is broken")
    return ordered


def latest_receipt_in_transaction(conn: Any, dossier_id: UUID) -> CustodyReceipt | None:
    """Newest receipt by chain position (tip), or None pre-handoff."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM custody_receipts WHERE dossier_id = %(dossier)s",
                {"dossier": dossier_id},
            )
            rows = cur.fetchall()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    ordered = _order_chain([dict(row) for row in rows or []])
    return ordered[-1] if ordered else None


def issue_receipt_in_transaction(
    conn: Any,
    *,
    dossier_id: UUID,
    version: int,
    kind: str,
    from_agent: str,
    to_agent: str,
    issued_by: str,
    note: str = "",
) -> CustodyReceipt:
    """Authenticate one handoff under the shared forward/return/review contract.

    Sender identity is enforced server-side: the claimed sender must be
    the chain-derived current holder (or Gabriel for the first receipt),
    and the issuing principal is recorded and MAC-bound. The HMAC alone
    does not prove origin; the holder match does.

    Issuance serializes per dossier on the same advisory key the
    version append uses, so concurrent issuers cannot read the same
    holder/predecessor and fork the chain. The lock is
    transaction-scoped: held until the caller's commit or rollback.
    """
    check_handoff_routing(kind, from_agent, to_agent)
    principal = (issued_by or "").strip()
    if not principal:
        raise CustodyAuthError("receipt issuance requires an issuing principal")
    if len(principal) > 64:
        raise CustodyError("issuing principal too long")
    if len(note or "") > MAX_NOTE_CHARS:
        raise CustodyError("receipt note too long")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%(key)s))",
                {"key": f"custody:{dossier_id}"},
            )
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    stored = get_version_in_transaction(conn, dossier_id, version)
    if stored is None:
        raise CustodyError("cannot hand off a missing dossier version")
    latest = latest_version_in_transaction(conn, dossier_id)
    assert latest is not None
    is_latest = version == latest.version
    if kind == "forward" and not is_latest:
        raise StaleHandoffError(
            f"version {version} is superseded by version {latest.version}"
        )
    prev = latest_receipt_in_transaction(conn, dossier_id)
    prev_id = prev.receipt_id if prev is not None else None
    holder = prev.to_agent if prev is not None else "gabriel"
    if from_agent != holder:
        raise CustodyAuthError(
            f"sender {from_agent} does not hold custody (held by {holder})"
        )
    try:
        found = _snapshot_row_for_version(conn, dossier_id, version)
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if found is None:
        raise CustodyError("handoff requires a pre-handoff snapshot first")
    snap_row, _archived = found
    snap = _snapshot_from_row(snap_row, archived=False)
    if snapshot_digest(snap.payload) != snap.digest:
        raise CustodyTamperError("snapshot bytes do not match their digest")
    core = receipt_core(
        dossier_id=dossier_id,
        version=version,
        snapshot_id=snap.snapshot_id,
        snapshot_digest=snap.digest,
        kind=kind,
        from_agent=from_agent,
        to_agent=to_agent,
        note=note or "",
        prev_receipt=prev_id,
        pipeline_version=PIPELINE_VERSION,
        issued_by=principal,
    )
    try:
        mac = receipt_mac(core, hmac_key())
    except CustodyAuthError:
        raise
    receipt_id = uuid4()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO custody_receipts (receipt_id, dossier_id, version, "
                "snapshot_id, kind, from_agent, to_agent, payload, mac, "
                "prev_receipt, pipeline_version) VALUES (%(rid)s, %(dossier)s, "
                "%(version)s, %(snapshot)s, %(kind)s, %(from)s, %(to)s, "
                "%(payload)s, %(mac)s, %(prev)s, %(pipeline)s) RETURNING *",
                {
                    "rid": receipt_id,
                    "dossier": dossier_id,
                    "version": version,
                    "snapshot": snap.snapshot_id,
                    "kind": kind,
                    "from": from_agent,
                    "to": to_agent,
                    "payload": json.dumps(
                        {"note": note or "", "is_latest": is_latest, "issued_by": principal}
                    ),
                    "mac": mac,
                    "prev": prev_id,
                    "pipeline": PIPELINE_VERSION,
                },
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if row is None:  # pragma: no cover - defensive; row was just inserted
        raise DatabaseUnavailableError("custody_unavailable")
    return _receipt_from_row(dict(row))


def get_receipt_in_transaction(conn: Any, receipt_id: UUID) -> CustodyReceipt | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM custody_receipts WHERE receipt_id = %(rid)s",
                {"rid": receipt_id},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return _receipt_from_row(dict(row)) if row is not None else None


def _verify_receipt_row(conn: Any, row: CustodyReceipt) -> CustodyReceipt:
    """Recompute a receipt's snapshot digest and MAC; raise on mismatch."""
    snap = get_snapshot_in_transaction(conn, row.snapshot_id)
    if snap is None:
        raise CustodyTamperError("receipt references a missing snapshot")
    core = receipt_core(
        dossier_id=row.dossier_id,
        version=row.version,
        snapshot_id=row.snapshot_id,
        snapshot_digest=snap.digest,
        kind=row.kind,
        from_agent=row.from_agent,
        to_agent=row.to_agent,
        note=str(row.payload.get("note", "")),
        prev_receipt=row.prev_receipt,
        pipeline_version=row.pipeline_version,
        issued_by=str(row.payload.get("issued_by", "")),
    )
    if not verify_mac(core, row.mac, hmac_key()):
        raise CustodyTamperError("receipt MAC does not match its contents")
    return row


def verify_receipt_in_transaction(conn: Any, receipt_id: UUID) -> CustodyReceipt:
    """Load one receipt and prove it is untampered."""
    row = get_receipt_in_transaction(conn, receipt_id)
    if row is None:
        raise CustodyError("unknown receipt")
    return _verify_receipt_row(conn, row)


def verify_chain_in_transaction(conn: Any, dossier_id: UUID) -> int:
    """Walk a dossier's receipt chain in order; return receipts verified.

    Order comes from predecessor links, never timestamps or UUIDs.
    Checks every MAC, every snapshot digest, and chain connectivity
    (unique root and tip; no forks, cycles, or missing predecessors).
    Zero receipts is valid (not yet handed off) and verifies as 0.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM custody_receipts WHERE dossier_id = %(dossier)s",
                {"dossier": dossier_id},
            )
            rows = cur.fetchall()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    for row in _order_chain([dict(raw) for raw in rows or []]):
        _verify_receipt_row(conn, row)
    return len(rows or [])


def current_custody_in_transaction(conn: Any, dossier_id: UUID) -> dict[str, Any] | None:
    """Current holder of a dossier: the latest receipt, or None pre-handoff."""
    latest = latest_receipt_in_transaction(conn, dossier_id)
    if latest is None:
        return None
    return {
        "holder": latest.to_agent,
        "holder_meaning": interpret_custody_agent(
            latest.pipeline_version, latest.to_agent
        )["meaning"],
        "kind": latest.kind,
        "version": latest.version,
        "receipt_id": str(latest.receipt_id),
        "snapshot_id": str(latest.snapshot_id),
        "pipeline_version": latest.pipeline_version,
    }


def pending_reviews_in_transaction(conn: Any) -> list[dict[str, Any]]:
    """Dossiers whose chain tip escalates them to human review.

    The tip of each dossier's predecessor chain decides; timestamp or
    UUID ordering never does. A tampered chain fails closed instead of
    silently dropping (or inventing) a review.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM custody_receipts")
            rows = cur.fetchall()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    by_dossier: dict[str, list[dict[str, Any]]] = {}
    for raw in rows or []:
        row = dict(raw)
        by_dossier.setdefault(str(row["dossier_id"]), []).append(row)
    items: list[dict[str, Any]] = []
    for dossier_rows in by_dossier.values():
        tip = _order_chain(dossier_rows)[-1]
        if tip.kind != "review":
            continue
        items.append(
            {
                "dossier_id": str(tip.dossier_id),
                "version": tip.version,
                "from_agent": tip.from_agent,
                "from_meaning": interpret_custody_agent(
                    tip.pipeline_version, tip.from_agent
                )["meaning"],
                "receipt_id": str(tip.receipt_id),
            }
        )
    return items


def archive_snapshots_in_transaction(conn: Any, *, older_than_days: int = 90) -> int:
    """Move old snapshots to the archive table; permanent records untouched.

    The archival copy and the hot-table delete commit atomically in the
    caller's transaction. Versions, receipts, and intake entries are
    never moved or deleted.
    """
    if older_than_days < 1:
        raise CustodyError("older_than_days must be positive")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO custody_snapshots_archive (snapshot_id, dossier_id, "
                "version, payload, digest, created_at) SELECT snapshot_id, "
                "dossier_id, version, payload, digest, created_at "
                "FROM custody_snapshots WHERE created_at < "
                "now() - make_interval(days => %(days)s) "
                "ON CONFLICT (snapshot_id) DO NOTHING RETURNING snapshot_id",
                {"days": older_than_days},
            )
            moved = cur.fetchall() or []
            cur.execute(
                "DELETE FROM custody_snapshots WHERE created_at < "
                "now() - make_interval(days => %(days)s)",
                {"days": older_than_days},
            )
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return len(moved)


def _owned(conn_fn: Any, *args: Any, config: DatabaseConfig | None = None, **kwargs: Any) -> Any:
    """Run one cursor-level helper on a short owned connection (commit once)."""
    conn = get_connection(config)
    try:
        result = conn_fn(conn, *args, **kwargs)
        conn.commit()
        return result
    except (CustodyError, DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def create_version(
    *,
    property_id: UUID | str,
    payload: dict[str, Any],
    created_by: str,
    config: DatabaseConfig | None = None,
) -> DossierVersion:
    return _owned(
        create_version_in_transaction,
        property_id=property_id,
        payload=payload,
        created_by=created_by,
        config=config,
    )


def amend(
    *,
    property_id: UUID | str,
    payload: dict[str, Any],
    authorized_by: str,
    reason: str,
    config: DatabaseConfig | None = None,
) -> DossierVersion:
    return _owned(
        amend_in_transaction,
        property_id=property_id,
        payload=payload,
        authorized_by=authorized_by,
        reason=reason,
        config=config,
    )


def get_version(
    dossier_id: UUID, version: int, config: DatabaseConfig | None = None
) -> DossierVersion | None:
    return _owned(get_version_in_transaction, dossier_id, version, config=config)


def latest_version(
    dossier_id: UUID, config: DatabaseConfig | None = None
) -> DossierVersion | None:
    return _owned(latest_version_in_transaction, dossier_id, config=config)


def snapshot(
    *, dossier_id: UUID, version: int, config: DatabaseConfig | None = None
) -> CustodySnapshot:
    return _owned(snapshot_in_transaction, dossier_id=dossier_id, version=version, config=config)


def get_snapshot(
    snapshot_id: UUID, config: DatabaseConfig | None = None
) -> CustodySnapshot | None:
    return _owned(get_snapshot_in_transaction, snapshot_id, config=config)


def issue_receipt(
    *,
    dossier_id: UUID,
    version: int,
    kind: str,
    from_agent: str,
    to_agent: str,
    issued_by: str,
    note: str = "",
    config: DatabaseConfig | None = None,
) -> CustodyReceipt:
    return _owned(
        issue_receipt_in_transaction,
        dossier_id=dossier_id,
        version=version,
        kind=kind,
        from_agent=from_agent,
        to_agent=to_agent,
        issued_by=issued_by,
        note=note,
        config=config,
    )


def verify_receipt(
    receipt_id: UUID, config: DatabaseConfig | None = None
) -> CustodyReceipt:
    return _owned(verify_receipt_in_transaction, receipt_id, config=config)


def verify_chain(dossier_id: UUID, config: DatabaseConfig | None = None) -> int:
    return _owned(verify_chain_in_transaction, dossier_id, config=config)


def current_custody(
    dossier_id: UUID, config: DatabaseConfig | None = None
) -> dict[str, Any] | None:
    return _owned(current_custody_in_transaction, dossier_id, config=config)


def pending_reviews(config: DatabaseConfig | None = None) -> list[dict[str, Any]]:
    return _owned(pending_reviews_in_transaction, config=config)


def archive_snapshots(
    *, older_than_days: int = SNAPSHOT_RETENTION_DAYS, config: DatabaseConfig | None = None
) -> int:
    return _owned(archive_snapshots_in_transaction, older_than_days=older_than_days, config=config)
