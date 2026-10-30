"""Shared dossier custody acceptance (migration 013).

Local tests run the real custody code against an in-memory emulator;
live tests (``GROWUP_PG_INTEGRATION=1``) verify real PostgreSQL behavior
— including the append-only triggers — inside rolled-back transactions.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.custody import (
    CustodyAuthError,
    CustodyError,
    CustodyTamperError,
    StaleHandoffError,
    dossier_id_for_property,
)
from app.custody_db import (
    amend_in_transaction,
    archive_snapshots_in_transaction,
    create_version_in_transaction,
    current_custody_in_transaction,
    get_snapshot_in_transaction,
    get_version_in_transaction,
    issue_receipt_in_transaction,
    latest_receipt_in_transaction,
    latest_version_in_transaction,
    pending_reviews_in_transaction,
    snapshot_in_transaction,
    verify_chain_in_transaction,
    verify_receipt_in_transaction,
)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_TEST_KEY = "test-custody-key-32-chars-minimum"
_TEST_AMENDERS = "op-1,op-2"


@pytest.fixture(autouse=True)
def _custody_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTODY_HMAC_KEY", _TEST_KEY)
    monkeypatch.setenv("CUSTODY_AMENDERS", _TEST_AMENDERS)


def _migration_sql() -> str:
    path = Path(__file__).resolve().parent.parent / "migrations" / "013_dossier_custody.sql"
    return path.read_text(encoding="utf-8")


def test_migration_creates_custody_tables() -> None:
    sql = _migration_sql()
    for table in (
        "dossier_versions",
        "custody_snapshots",
        "custody_snapshots_archive",
        "custody_receipts",
        "michael_intake",
        "pilot_state",
        "pilot_cells",
        "pilot_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "PRIMARY KEY (dossier_id, version)" in sql
    assert "UNIQUE (dossier_id, version)" in sql
    assert "CHECK (kind IN ('forward', 'return', 'review'))" in sql
    assert "CHECK (status = 'queued')" in sql
    assert "UNIQUE (dossier_id, transition)" in sql
    assert "reject_custody_write" in sql
    assert "ALTER TABLE properties" not in sql


class _Emulator:
    """Minimal in-memory stand-in for the custody tables."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, int], dict[str, Any]] = {}
        self.snaps: dict[str, dict[str, Any]] = {}
        self.snaps_by_version: dict[tuple[str, int], str] = {}
        self.archive: dict[str, dict[str, Any]] = {}
        self.archive_by_version: dict[tuple[str, int], str] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self._clock = 0

    def _now(self) -> datetime:
        self._clock += 1
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self._clock)

    def _fake_now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self._clock)


class _FakeCursor:
    def __init__(self, db: _Emulator) -> None:
        self._db = db
        self._result: list[Any] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    @staticmethod
    def _key(value: Any) -> str:
        return str(value)

    def execute(self, sql: str, params: Any = None) -> None:
        p = dict(params or {})
        db = self._db
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            self._result = []
        elif sql.startswith("SELECT COALESCE(MAX(version)"):
            dossier = self._key(p["dossier"])
            versions = [v for (d, v) in db.versions if d == dossier]
            self._result = [{"max_version": max(versions) if versions else 0}]
        elif sql.startswith("INSERT INTO dossier_versions"):
            row = {
                "dossier_id": p["dossier"],
                "version": p["version"],
                "property_id": p["property"],
                "payload": p["payload"],
                "digest": p["digest"],
                "supersedes": p["supersedes"],
                "amendment": p["amendment"],
                "created_by": p["created_by"],
                "created_at": db._now(),
            }
            db.versions[(self._key(p["dossier"]), p["version"])] = row
            self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM dossier_versions WHERE dossier_id = %(dossier)s AND version"):
            row = db.versions.get((self._key(p["dossier"]), p["version"]))
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM dossier_versions WHERE dossier_id = %(dossier)s ORDER BY version DESC"):
            dossier = self._key(p["dossier"])
            versions = sorted((v for (d, v) in db.versions if d == dossier), reverse=True)
            row = db.versions.get((dossier, versions[0])) if versions else None
            self._result = [dict(row)] if row else []
        elif sql.startswith("INSERT INTO custody_snapshots "):
            key = (self._key(p["dossier"]), p["version"])
            if key in db.snaps_by_version:
                self._result = []
            else:
                row = {
                    "snapshot_id": p["sid"],
                    "dossier_id": p["dossier"],
                    "version": p["version"],
                    "payload": p["payload"],
                    "digest": p["digest"],
                    "created_at": db._now(),
                }
                db.snaps[self._key(p["sid"])] = row
                db.snaps_by_version[key] = self._key(p["sid"])
                self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM custody_snapshots WHERE dossier_id"):
            sid = db.snaps_by_version.get((self._key(p["dossier"]), p["version"]))
            row = db.snaps.get(sid or "")
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM custody_snapshots WHERE snapshot_id"):
            row = db.snaps.get(self._key(p["sid"]))
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM custody_snapshots_archive WHERE snapshot_id"):
            row = db.archive.get(self._key(p["sid"]))
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM custody_snapshots_archive WHERE dossier_id"):
            sid = db.archive_by_version.get((self._key(p["dossier"]), p["version"]))
            row = db.archive.get(sid or "")
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM custody_receipts WHERE dossier_id"):
            dossier = self._key(p["dossier"])
            rows = [r for r in db.receipts.values() if self._key(r["dossier_id"]) == dossier]
            rows.sort(key=lambda r: (r["created_at"], self._key(r["receipt_id"])))
            self._result = [dict(r) for r in rows]
        elif sql.startswith("INSERT INTO custody_receipts"):
            row = {
                "receipt_id": p["rid"],
                "dossier_id": p["dossier"],
                "version": p["version"],
                "snapshot_id": p["snapshot"],
                "kind": p["kind"],
                "from_agent": p["from"],
                "to_agent": p["to"],
                "payload": p["payload"],
                "mac": p["mac"],
                "prev_receipt": p["prev"],
                "pipeline_version": p.get("pipeline", "canonical-1"),
                "created_at": db._now(),
            }
            db.receipts[self._key(p["rid"])] = row
            self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM custody_receipts WHERE receipt_id"):
            row = db.receipts.get(self._key(p["rid"]))
            self._result = [dict(row)] if row else []
        elif sql == "SELECT * FROM custody_receipts":
            self._result = [dict(row) for row in db.receipts.values()]
        elif sql.startswith("INSERT INTO custody_snapshots_archive"):
            cutoff = db._fake_now() - timedelta(days=p["days"])
            moved = [sid for sid, row in db.snaps.items() if row["created_at"] < cutoff]
            for sid in moved:
                row = dict(db.snaps[sid])
                row["archived_at"] = db._now()
                db.archive[sid] = row
                db.archive_by_version[
                    (self._key(row["dossier_id"]), row["version"])
                ] = sid
            self._result = [{"snapshot_id": sid} for sid in moved]
        elif sql.startswith("DELETE FROM custody_snapshots WHERE created_at <"):
            cutoff = db._fake_now() - timedelta(days=p["days"])
            doomed = [sid for sid, row in db.snaps.items() if row["created_at"] < cutoff]
            for sid in doomed:
                row = db.snaps.pop(sid)
                db.snaps_by_version.pop((self._key(row["dossier_id"]), row["version"]), None)
            self._result = []
        else:  # pragma: no cover - emulator must cover every custody statement
            raise AssertionError(f"unhandled SQL: {sql[:80]}")


class _FakeConn:
    def __init__(self, db: _Emulator | None = None) -> None:
        self.db = db or _Emulator()

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.db)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _conn() -> _FakeConn:
    return _FakeConn()


# --- identity ---


def test_dossier_identity_is_shared_and_deterministic() -> None:
    prop = uuid4()
    assert dossier_id_for_property(prop) == dossier_id_for_property(str(prop))
    assert dossier_id_for_property(prop) == dossier_id_for_property(prop)
    assert dossier_id_for_property(uuid4()) != dossier_id_for_property(prop)


def test_dossier_identity_rejects_demo_and_garbage() -> None:
    with pytest.raises(CustodyError):
        dossier_id_for_property("demo-synthetic-1")
    with pytest.raises(CustodyError):
        dossier_id_for_property("not-a-uuid")
    with pytest.raises(CustodyError):
        create_version_in_transaction(_conn(), property_id="demo-x", payload={}, created_by="gabriel")


# --- versions and amendments ---


def test_versions_append_monotonically() -> None:
    conn = _conn()
    prop = uuid4()
    v1 = create_version_in_transaction(conn, property_id=prop, payload={"a": 1}, created_by="gabriel")
    assert v1.version == 1 and v1.supersedes is None and v1.digest
    v2 = create_version_in_transaction(conn, property_id=prop, payload={"a": 2}, created_by="gabriel")
    assert v2.version == 2 and v2.dossier_id == v1.dossier_id
    assert get_version_in_transaction(conn, v1.dossier_id, 1) is not None
    latest = latest_version_in_transaction(conn, v1.dossier_id)
    assert latest is not None and latest.version == 2
    assert get_version_in_transaction(conn, v1.dossier_id, 9) is None
    assert latest_version_in_transaction(conn, uuid4()) is None


def test_create_version_validates_inputs() -> None:
    conn = _conn()
    with pytest.raises(CustodyError):
        create_version_in_transaction(conn, property_id=uuid4(), payload=["nope"], created_by="gabriel")  # type: ignore[arg-type]
    with pytest.raises(CustodyError):
        create_version_in_transaction(conn, property_id=uuid4(), payload={}, created_by="")


def test_amend_requires_enrolled_authorizer_and_reason() -> None:
    conn = _conn()
    prop = uuid4()
    create_version_in_transaction(conn, property_id=prop, payload={}, created_by="gabriel")
    with pytest.raises(CustodyAuthError):
        amend_in_transaction(conn, property_id=prop, payload={}, authorized_by="", reason="fix")
    with pytest.raises(CustodyAuthError):
        amend_in_transaction(conn, property_id=prop, payload={}, authorized_by="mallory", reason="fix")
    with pytest.raises(CustodyError):
        amend_in_transaction(conn, property_id=prop, payload={}, authorized_by="op-1", reason="")
    with pytest.raises(CustodyError):
        amend_in_transaction(conn, property_id=prop, payload={}, authorized_by="op-1", reason="x" * 501)


def test_amend_refused_when_nobody_enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTODY_AMENDERS", "")
    conn = _conn()
    prop = uuid4()
    create_version_in_transaction(conn, property_id=prop, payload={}, created_by="gabriel")
    with pytest.raises(CustodyAuthError):
        amend_in_transaction(conn, property_id=prop, payload={}, authorized_by="op-1", reason="fix")


def test_amend_appends_superseding_version() -> None:
    conn = _conn()
    prop = uuid4()
    create_version_in_transaction(conn, property_id=prop, payload={"a": 1}, created_by="gabriel")
    v2 = amend_in_transaction(
        conn, property_id=prop, payload={"a": 2}, authorized_by="op-1", reason="corrected area"
    )
    assert v2.version == 2 and v2.supersedes == 1
    assert v2.amendment == {"authorized_by": "op-1", "reason": "corrected area"}
    assert v2.created_by == "op-1"
    original = get_version_in_transaction(conn, v2.dossier_id, 1)
    assert original is not None and original.payload == {"a": 1}


def test_amend_missing_dossier_refused() -> None:
    with pytest.raises(CustodyError, match="no versions"):
        amend_in_transaction(
            _conn(), property_id=uuid4(), payload={}, authorized_by="op-1", reason="fix"
        )


# --- snapshots ---


def test_snapshot_requires_an_existing_version() -> None:
    with pytest.raises(CustodyError, match="missing dossier version"):
        snapshot_in_transaction(_conn(), dossier_id=uuid4(), version=1)


def test_snapshot_freezes_version_payload_idempotently() -> None:
    conn = _conn()
    prop = uuid4()
    v1 = create_version_in_transaction(conn, property_id=prop, payload={"a": 1}, created_by="gabriel")
    first = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
    assert first.digest and first.archived is False
    assert first.payload == {"a": 1}
    again = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
    assert again.snapshot_id == first.snapshot_id
    assert get_snapshot_in_transaction(conn, uuid4()) is None


# --- receipts: shared forward/return/review contract ---


def _handed_off(conn: _FakeConn) -> tuple[Any, Any]:
    prop = uuid4()
    v1 = create_version_in_transaction(conn, property_id=prop, payload={"a": 1}, created_by="gabriel")
    snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
    return prop, v1


def test_receipt_requires_pre_handoff_snapshot() -> None:
    conn = _conn()
    prop = uuid4()
    v1 = create_version_in_transaction(conn, property_id=prop, payload={}, created_by="gabriel")
    with pytest.raises(CustodyError, match="snapshot first"):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )


def test_forward_chain_verifies_end_to_end() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    first = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1", note="initial",
    )
    assert first.prev_receipt is None
    second = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="raphael", to_agent="onesimus", issued_by="op-1",
    )
    assert second.prev_receipt == first.receipt_id
    assert verify_receipt_in_transaction(conn, second.receipt_id).receipt_id == second.receipt_id
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 2
    custody = current_custody_in_transaction(conn, v1.dossier_id)
    assert custody is not None and custody["holder"] == "onesimus"


def test_empty_chain_verifies_as_zero() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 0
    assert current_custody_in_transaction(conn, v1.dossier_id) is None


def _chain3(conn: _FakeConn) -> tuple[Any, tuple[Any, Any, Any]]:
    _prop, v1 = _handed_off(conn)
    first = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    second = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="raphael", to_agent="onesimus", issued_by="op-1",
    )
    third = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="onesimus", to_agent="michael", issued_by="op-1",
    )
    return v1, (first, second, third)


def _tie_timestamps_reverse_uuids(conn: _FakeConn, ordered: tuple[Any, ...]) -> None:
    """Force the PostgreSQL failure mode: tied timestamps, reversed UUIDs.

    Within one transaction every receipt shares one timestamp, so any
    timestamp/UUID ordering is adversarial by luck. Rewriting ids is
    MAC-safe: a receipt's own id is not MAC-bound, only its prev link.
    """
    from app.custody import receipt_core, receipt_mac

    tied = datetime(2026, 1, 1, tzinfo=timezone.utc)
    remapped: dict[str, str] = {}
    for position, receipt in enumerate(ordered):
        # Root keeps the smallest UUID so a (created_at, receipt_id)
        # ascending pick of rows[0] would crown the chain root.
        remapped[str(receipt.receipt_id)] = str(UUID(int=position + 1))
    snap = snapshot_in_transaction(
        conn, dossier_id=ordered[0].dossier_id, version=ordered[0].version,
    )
    rebuilt: dict[str, dict[str, Any]] = {}
    for key, row in conn.db.receipts.items():
        row = dict(row)
        if str(row["receipt_id"]) in remapped:
            new_id = UUID(remapped[str(row["receipt_id"])])
            new_prev = (
                UUID(remapped[str(row["prev_receipt"])])
                if row["prev_receipt"] is not None
                else None
            )
            row["receipt_id"] = new_id
            row["prev_receipt"] = new_prev
            row["created_at"] = tied
            body = json.loads(row["payload"])
            core = receipt_core(
                dossier_id=row["dossier_id"], version=row["version"],
                snapshot_id=row["snapshot_id"], snapshot_digest=snap.digest,
                kind=row["kind"], from_agent=row["from_agent"],
                to_agent=row["to_agent"], note=body.get("note", ""),
                prev_receipt=new_prev,
                pipeline_version=row["pipeline_version"],
                issued_by=body.get("issued_by"),
            )
            row["mac"] = receipt_mac(core, _TEST_KEY.encode())
        rebuilt[str(row["receipt_id"]) if str(key) in remapped else key] = row
    conn.db.receipts = rebuilt


def test_chain_order_ignores_timestamps_and_uuids() -> None:
    conn = _conn()
    v1, ordered = _chain3(conn)
    _tie_timestamps_reverse_uuids(conn, ordered)
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 3
    assert latest_receipt_in_transaction(conn, v1.dossier_id) is not None
    tip = latest_receipt_in_transaction(conn, v1.dossier_id)
    assert tip is not None and tip.to_agent == "michael"
    custody = current_custody_in_transaction(conn, v1.dossier_id)
    assert custody is not None and custody["holder"] == "michael"


def test_chain_fork_rejected() -> None:
    conn = _conn()
    v1, (first, _second, third) = _chain3(conn)
    sibling = dict(conn.db.receipts[str(third.receipt_id)])
    sibling["receipt_id"] = uuid4()
    sibling["prev_receipt"] = first.receipt_id
    conn.db.receipts[str(sibling["receipt_id"])] = sibling
    with pytest.raises(CustodyTamperError, match="fork"):
        verify_chain_in_transaction(conn, v1.dossier_id)
    with pytest.raises(CustodyTamperError, match="fork"):
        latest_receipt_in_transaction(conn, v1.dossier_id)


def test_chain_missing_predecessor_rejected() -> None:
    conn = _conn()
    v1, (_first, second, _third) = _chain3(conn)
    conn.db.receipts[str(second.receipt_id)]["prev_receipt"] = uuid4()
    with pytest.raises(CustodyTamperError, match="broken"):
        verify_chain_in_transaction(conn, v1.dossier_id)


def test_chain_cross_dossier_prev_rejected() -> None:
    conn = _conn()
    v1, (_a1, a2, _a3) = _chain3(conn)
    _prop_b, vb = _handed_off(conn)
    b1 = issue_receipt_in_transaction(
        conn, dossier_id=vb.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    conn.db.receipts[str(b1.receipt_id)]["prev_receipt"] = a2.receipt_id
    with pytest.raises(CustodyTamperError, match="unique root"):
        verify_chain_in_transaction(conn, vb.dossier_id)
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 3


class _RecordingConn(_FakeConn):
    """Fake connection logging every statement in execution order."""

    def __init__(self, db: _Emulator | None = None) -> None:
        super().__init__(db)
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def cursor(self) -> _FakeCursor:
        cursor = super().cursor()
        real_execute = cursor.execute
        log = self.statements

        def _execute(sql: str, params: Any = None) -> None:
            log.append((sql, dict(params or {})))
            real_execute(sql, params)

        cursor.execute = _execute  # type: ignore[method-assign]
        return cursor


def test_issue_locks_dossier_before_holder_read() -> None:
    setup = _conn()
    _prop, v1 = _handed_off(setup)
    conn = _RecordingConn(setup.db)
    issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    locks = [
        index
        for index, (sql, params) in enumerate(conn.statements)
        if sql.startswith("SELECT pg_advisory_xact_lock")
        and params.get("key") == f"custody:{v1.dossier_id}"
    ]
    reads = [
        index
        for index, (sql, _params) in enumerate(conn.statements)
        if "FROM custody_receipts" in sql
    ]
    assert locks, "issuance must take the per-dossier advisory lock"
    assert reads, "issuance must read the predecessor chain"
    assert min(locks) < min(reads)


def test_pending_reviews_follow_tips_not_timestamps() -> None:
    conn = _conn()
    _prop_a, va = _handed_off(conn)
    issue_receipt_in_transaction(
        conn, dossier_id=va.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    issue_receipt_in_transaction(
        conn, dossier_id=va.dossier_id, version=1,
        kind="review", from_agent="raphael", to_agent="human_review", issued_by="op-1",
    )
    _prop_b, vb = _handed_off(conn)
    stale_review = issue_receipt_in_transaction(
        conn, dossier_id=vb.dossier_id, version=1,
        kind="review", from_agent="gabriel", to_agent="human_review", issued_by="op-1",
    )
    issue_receipt_in_transaction(
        conn, dossier_id=vb.dossier_id, version=1,
        kind="return", from_agent="human_review", to_agent="raphael", issued_by="op-1",
    )
    # Adversarial clock: every receipt shares one timestamp, and the
    # superseded review carries the greatest UUID a timestamp-order
    # query would crown.
    tied = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for row in conn.db.receipts.values():
        row["created_at"] = tied
    stale_key = str(stale_review.receipt_id)
    giant = UUID(int=2**122)
    conn.db.receipts[stale_key]["receipt_id"] = giant
    for row in conn.db.receipts.values():
        if str(row.get("prev_receipt")) == stale_key:
            row["prev_receipt"] = giant
    conn.db.receipts[str(giant)] = conn.db.receipts.pop(stale_key)
    pending = pending_reviews_in_transaction(conn)
    assert [item["dossier_id"] for item in pending] == [str(va.dossier_id)]


def test_routing_rules_shared_across_kinds() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    base = {"dossier_id": v1.dossier_id, "version": 1}
    with pytest.raises(CustodyError, match="unknown receipt kind"):
        issue_receipt_in_transaction(conn, kind="teleport", from_agent="gabriel", to_agent="raphael", issued_by="op-1", **base)
    with pytest.raises(CustodyError, match="unknown handoff agent"):
        issue_receipt_in_transaction(conn, kind="forward", from_agent="gabriel", to_agent="wondrue", issued_by="op-1", **base)
    with pytest.raises(CustodyError, match="distinct agents"):
        issue_receipt_in_transaction(conn, kind="forward", from_agent="gabriel", to_agent="gabriel", issued_by="op-1", **base)
    with pytest.raises(CustodyError, match="downstream only"):
        issue_receipt_in_transaction(conn, kind="forward", from_agent="raphael", to_agent="gabriel", issued_by="op-1", **base)
    with pytest.raises(CustodyError, match="upstream only"):
        issue_receipt_in_transaction(conn, kind="return", from_agent="gabriel", to_agent="raphael", issued_by="op-1", **base)
    with pytest.raises(CustodyError, match="human_review"):
        issue_receipt_in_transaction(conn, kind="forward", from_agent="raphael", to_agent="human_review", issued_by="op-1", **base)
    with pytest.raises(CustodyError, match="human_review"):
        issue_receipt_in_transaction(conn, kind="review", from_agent="gabriel", to_agent="raphael", issued_by="op-1", **base)
    with pytest.raises(CustodyError, match="note too long"):
        issue_receipt_in_transaction(
            conn, kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1", note="x" * 501, **base
        )


def test_return_moves_upstream_and_review_escalates() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    back = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="return", from_agent="raphael", to_agent="gabriel", issued_by="op-1", note="need county evidence",
    )
    assert back.payload["is_latest"] is True
    assert current_custody_in_transaction(conn, v1.dossier_id)["holder"] == "gabriel"
    review = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="review", from_agent="gabriel", to_agent="human_review", issued_by="op-1",
    )
    assert review.payload["is_latest"] is True
    pending = pending_reviews_in_transaction(conn)
    assert [item["receipt_id"] for item in pending] == [str(review.receipt_id)]
    # Custody sits with human_review: only a disposition return routes back.
    with pytest.raises(CustodyAuthError, match="does not hold custody"):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )
    issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="return", from_agent="human_review", to_agent="gabriel", issued_by="op-1",
    )
    issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    assert pending_reviews_in_transaction(conn) == []
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 5


def test_forward_on_superseded_version_is_stale() -> None:
    conn = _conn()
    prop, v1 = _handed_off(conn)
    issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    amend_in_transaction(conn, property_id=prop, payload={"a": 2}, authorized_by="op-1", reason="fix")
    snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=2)
    with pytest.raises(StaleHandoffError):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )
    ret = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="return", from_agent="raphael", to_agent="gabriel", issued_by="op-1",
    )
    assert ret.payload["is_latest"] is False
    fresh = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=2,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    assert fresh.payload["is_latest"] is True


# --- tampering ---


def test_receipt_note_tampering_detected() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1", note="ok",
    )
    stored = conn.db.receipts[str(receipt.receipt_id)]
    payload = json.loads(stored["payload"])
    payload["note"] = "forged"
    stored["payload"] = json.dumps(payload)
    with pytest.raises(CustodyTamperError):
        verify_receipt_in_transaction(conn, receipt.receipt_id)
    with pytest.raises(CustodyTamperError):
        verify_chain_in_transaction(conn, v1.dossier_id)


def test_receipt_mac_tampering_detected() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    conn.db.receipts[str(receipt.receipt_id)]["mac"] = "0" * 64
    with pytest.raises(CustodyTamperError):
        verify_receipt_in_transaction(conn, receipt.receipt_id)


def test_receipt_routing_tampering_detected() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    conn.db.receipts[str(receipt.receipt_id)]["to_agent"] = "michael"
    with pytest.raises(CustodyTamperError):
        verify_receipt_in_transaction(conn, receipt.receipt_id)


def test_snapshot_payload_tampering_detected() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    snap = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
    stored = conn.db.snaps[str(snap.snapshot_id)]
    stored["payload"] = json.dumps({"a": "forged"})
    with pytest.raises(CustodyTamperError):
        get_snapshot_in_transaction(conn, snap.snapshot_id)


def test_broken_chain_link_detected() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    first = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    second = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="raphael", to_agent="onesimus", issued_by="op-1",
    )
    conn.db.receipts[str(second.receipt_id)]["prev_receipt"] = None
    with pytest.raises(CustodyTamperError):
        verify_chain_in_transaction(conn, v1.dossier_id)
    assert first.receipt_id != second.receipt_id


def test_receipt_with_missing_snapshot_detected() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    conn.db.snaps.clear()
    conn.db.snaps_by_version.clear()
    with pytest.raises(CustodyTamperError, match="missing snapshot"):
        verify_receipt_in_transaction(conn, receipt.receipt_id)


def test_unknown_receipt_is_an_error_not_tampering() -> None:
    with pytest.raises(CustodyError, match="unknown receipt"):
        verify_receipt_in_transaction(_conn(), uuid4())


def test_custody_fails_closed_without_hmac_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "")
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    with pytest.raises(CustodyAuthError):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "short")
    with pytest.raises(CustodyAuthError):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )


# --- archival ---


def test_archival_moves_old_snapshots_and_keeps_receipts() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    snap = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    stored = conn.db.snaps[str(snap.snapshot_id)]
    stored["created_at"] = stored["created_at"] - timedelta(days=100)
    assert archive_snapshots_in_transaction(conn, older_than_days=90) == 1
    found = get_snapshot_in_transaction(conn, snap.snapshot_id)
    assert found is not None and found.archived is True
    assert found.payload == {"a": 1}
    assert verify_receipt_in_transaction(conn, receipt.receipt_id) is not None
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 1
    assert archive_snapshots_in_transaction(conn, older_than_days=90) == 0


def test_archival_keeps_young_snapshots_hot() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    snap = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
    assert archive_snapshots_in_transaction(conn, older_than_days=90) == 0
    found = get_snapshot_in_transaction(conn, snap.snapshot_id)
    assert found is not None and found.archived is False


def test_archival_rejects_nonpositive_window() -> None:
    with pytest.raises(CustodyError):
        archive_snapshots_in_transaction(_conn(), older_than_days=0)


# --- canonical pipeline: order, principals, history ---


def test_first_receipt_must_come_from_gabriel() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    with pytest.raises(CustodyAuthError, match="does not hold custody"):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="raphael", to_agent="onesimus", issued_by="op-1",
        )


def test_issuance_requires_a_principal() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    with pytest.raises(CustodyAuthError, match="issuing principal"):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="",
        )
    with pytest.raises(CustodyError, match="principal too long"):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="x" * 65,
        )


def test_canonical_downstream_order_to_priscilla() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    path = ["gabriel", "raphael", "onesimus", "michael", "uriel", "priscilla"]
    for giver, taker in zip(path, path[1:]):
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent=giver, to_agent=taker, issued_by="op-1",
        )
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 5
    custody = current_custody_in_transaction(conn, v1.dossier_id)
    assert custody is not None and custody["holder"] == "priscilla"
    assert custody["pipeline_version"] == "canonical-1"
    assert "conversion tracking" in custody["holder_meaning"]
    back = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="return", from_agent="priscilla", to_agent="uriel", issued_by="op-1",
    )
    assert back.payload["is_latest"] is True


def test_historical_michael_keeps_outreach_meaning() -> None:
    from app.custody import interpret_custody_agent

    old = interpret_custody_agent("mvp-1", "michael")
    assert old["historic"] is True
    assert "Uriel" in old["meaning"]
    new = interpret_custody_agent("canonical-1", "michael")
    assert new["historic"] is False
    assert "financial" in new["meaning"].lower()
    with pytest.raises(CustodyError, match="pipeline version"):
        interpret_custody_agent("v99", "michael")


def test_legacy_receipt_still_verifies() -> None:
    """An mvp-1 row (frozen core, no principal) verifies under its version."""
    import json as _json

    from app.custody import receipt_core, receipt_mac

    conn = _conn()
    _prop, v1 = _handed_off(conn)
    snap = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
    core = receipt_core(
        dossier_id=v1.dossier_id, version=1, snapshot_id=snap.snapshot_id,
        snapshot_digest=snap.digest, kind="forward",
        from_agent="gabriel", to_agent="raphael", note="", prev_receipt=None,
        pipeline_version="mvp-1",
    )
    rid = uuid4()
    conn.db.receipts[str(rid)] = {
        "receipt_id": rid,
        "dossier_id": v1.dossier_id,
        "version": 1,
        "snapshot_id": snap.snapshot_id,
        "kind": "forward",
        "from_agent": "gabriel",
        "to_agent": "raphael",
        "payload": _json.dumps({"note": "", "is_latest": True}),
        "mac": receipt_mac(core, _TEST_KEY.encode()),
        "prev_receipt": None,
        "pipeline_version": "mvp-1",
        "created_at": conn.db._now(),
    }
    assert verify_chain_in_transaction(conn, v1.dossier_id) == 1
    custody = current_custody_in_transaction(conn, v1.dossier_id)
    assert custody is not None and custody["pipeline_version"] == "mvp-1"


def test_amendment_refused_over_broken_custody() -> None:
    conn = _conn()
    prop, v1 = _handed_off(conn)
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    conn.db.receipts[str(receipt.receipt_id)]["mac"] = "0" * 64
    with pytest.raises(CustodyTamperError):
        amend_in_transaction(
            conn, property_id=prop, payload={"a": 2},
            authorized_by="op-1", reason="fix",
        )


def test_issuing_principal_tampering_detected() -> None:
    conn = _conn()
    _prop, v1 = _handed_off(conn)
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=v1.dossier_id, version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
    )
    stored = conn.db.receipts[str(receipt.receipt_id)]
    payload = json.loads(stored["payload"])
    payload["issued_by"] = "mallory"
    stored["payload"] = json.dumps(payload)
    with pytest.raises(CustodyTamperError):
        verify_receipt_in_transaction(conn, receipt.receipt_id)


# --- live PostgreSQL ---


def _require_custody_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.dossier_versions') AS v, "
                "to_regclass('public.custody_snapshots') AS s, "
                "to_regclass('public.custody_receipts') AS r, "
                "to_regclass('public.uriel_packages') AS u"
            )
            row = cur.fetchone()
    if (
        row is None
        or row["v"] is None
        or row["s"] is None
        or row["r"] is None
        or row["u"] is None
    ):
        pytest.skip("custody tables not present (migrations 013/014 not applied)")


@needs_live_pg
def test_live_custody_roundtrip_rolls_back() -> None:
    from app.properties_db import get_connection

    _require_custody_tables()
    conn = get_connection()
    try:
        prop = uuid4()
        v1 = create_version_in_transaction(
            conn, property_id=prop, payload={"a": 1}, created_by="gabriel"
        )
        assert v1.version == 1
        snap = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
        again = snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
        assert again.snapshot_id == snap.snapshot_id
        first = issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )
        second = issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="raphael", to_agent="onesimus", issued_by="op-1",
        )
        assert second.prev_receipt == first.receipt_id
        assert verify_chain_in_transaction(conn, v1.dossier_id) == 2
        custody = current_custody_in_transaction(conn, v1.dossier_id)
        assert custody is not None and custody["holder"] == "onesimus"
    finally:
        conn.rollback()
        conn.close()


@needs_live_pg
def test_live_concurrent_issuance_serializes_no_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG: two racing issuers on one dossier leave one receipt, no fork.

    Both workers issue the same gabriel->raphael handoff. The per-dossier
    advisory lock serializes them: the loser blocks until the winner
    commits, then fails the holder check instead of appending a second
    root child. The winner commits (append-only rows cannot be cleaned
    up); the uuid property identifies the residue for review.
    """
    import threading

    from app.properties_db import get_connection

    monkeypatch.setenv("CUSTODY_HMAC_KEY", "live-custody-key-0123456789abcdef")
    seed_conn = get_connection()
    try:
        prop = uuid4()
        v1 = create_version_in_transaction(
            seed_conn, property_id=prop, payload={"a": 1}, created_by="gabriel"
        )
        snapshot_in_transaction(
            seed_conn, dossier_id=v1.dossier_id, version=1
        )
        dossier = v1.dossier_id
        seed_conn.commit()
    finally:
        seed_conn.close()
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}
    errors: list[str] = []

    def _worker(slot: str) -> None:
        worker_conn = get_connection()
        try:
            barrier.wait(timeout=30)
            receipt = issue_receipt_in_transaction(
                worker_conn, dossier_id=dossier, version=1,
                kind="forward", from_agent="gabriel", to_agent="raphael",
                issued_by=f"op-{slot}",
            )
            results[slot] = str(receipt.receipt_id)
            worker_conn.commit()
        except CustodyAuthError as exc:
            errors.append(f"{slot}: CustodyAuthError: {exc}")
            worker_conn.rollback()
        except Exception as exc:  # surfaced below, never swallowed
            errors.append(f"{slot}: {type(exc).__name__}: {exc}")
            worker_conn.rollback()
        finally:
            worker_conn.close()

    threads = [
        threading.Thread(target=_worker, args=("a",)),
        threading.Thread(target=_worker, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not [thread for thread in threads if thread.is_alive()]
    assert len(results) == 1, f"expected one winner, got {results} / {errors}"
    assert len(errors) == 1 and "CustodyAuthError" in errors[0]
    assert "does not hold custody" in errors[0]
    check_conn = get_connection()
    try:
        assert verify_chain_in_transaction(check_conn, dossier) == 1
        custody = current_custody_in_transaction(check_conn, dossier)
        assert custody is not None and custody["holder"] == "raphael"
        with check_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total FROM custody_receipts "
                "WHERE dossier_id = %(d)s AND prev_receipt IS NULL",
                {"d": dossier},
            )
            assert (cur.fetchone() or {})["total"] == 1
    finally:
        check_conn.rollback()
        check_conn.close()


@needs_live_pg
def test_live_append_only_triggers_reject_mutation() -> None:
    from app.properties_db import get_connection

    _require_custody_tables()
    conn = get_connection()
    try:
        prop = uuid4()
        v1 = create_version_in_transaction(
            conn, property_id=prop, payload={"a": 1}, created_by="gabriel"
        )
        snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )
        with conn.cursor() as cur:
            with pytest.raises(Exception, match="append-only"):
                cur.execute(
                    "UPDATE dossier_versions SET payload = '{}' "
                    "WHERE dossier_id = %(d)s",
                    {"d": v1.dossier_id},
                )
        conn.rollback()
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    conn = get_connection()
    try:
        prop = uuid4()
        v1 = create_version_in_transaction(
            conn, property_id=prop, payload={"a": 1}, created_by="gabriel"
        )
        snapshot_in_transaction(conn, dossier_id=v1.dossier_id, version=1)
        issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael", issued_by="op-1",
        )
        with conn.cursor() as cur:
            with pytest.raises(Exception, match="append-only"):
                cur.execute(
                    "DELETE FROM custody_receipts WHERE dossier_id = %(d)s",
                    {"d": v1.dossier_id},
                )
        conn.rollback()
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


@needs_live_pg
def test_live_archival_moves_old_snapshots() -> None:
    """Real PG: 90-day archival moves snapshots, receipts stay verifiable."""
    import json

    from app.custody import snapshot_digest
    from app.properties_db import get_connection

    _require_custody_tables()
    conn = get_connection()
    try:
        prop = uuid4()
        v1 = create_version_in_transaction(
            conn, property_id=prop, payload={"a": 1}, created_by="gabriel"
        )
        # Immutable rows are never updated: the aged fixture is born old.
        old_id = uuid4()
        old_payload = {"a": 1}
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO custody_snapshots (snapshot_id, dossier_id, "
                "version, payload, digest, created_at) VALUES (%(sid)s, "
                "%(dossier)s, %(version)s, %(payload)s, %(digest)s, "
                "now() - make_interval(days := 91))",
                {
                    "sid": old_id,
                    "dossier": v1.dossier_id,
                    "version": 1,
                    "payload": json.dumps(old_payload),
                    "digest": snapshot_digest(old_payload),
                },
            )
        old = get_snapshot_in_transaction(conn, old_id)
        assert old is not None
        receipt = issue_receipt_in_transaction(
            conn, dossier_id=v1.dossier_id, version=1,
            kind="forward", from_agent="gabriel", to_agent="raphael",
            issued_by="op-1",
        )
        prop2 = uuid4()
        v2 = create_version_in_transaction(
            conn, property_id=prop2, payload={"b": 2}, created_by="gabriel"
        )
        young = snapshot_in_transaction(
            conn, dossier_id=v2.dossier_id, version=1
        )
        assert archive_snapshots_in_transaction(conn) == 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total FROM custody_snapshots "
                "WHERE snapshot_id = %(sid)s",
                {"sid": old.snapshot_id},
            )
            assert (cur.fetchone() or {})["total"] == 0
            cur.execute(
                "SELECT COUNT(*) AS total FROM custody_snapshots_archive "
                "WHERE snapshot_id = %(sid)s",
                {"sid": old.snapshot_id},
            )
            assert (cur.fetchone() or {})["total"] == 1
        retrieved = get_snapshot_in_transaction(conn, old.snapshot_id)
        assert retrieved is not None and retrieved.archived is True
        assert retrieved.digest == old.digest
        fresh = get_snapshot_in_transaction(conn, young.snapshot_id)
        assert fresh is not None and fresh.archived is False
        assert verify_chain_in_transaction(conn, v1.dossier_id) == 1
        assert verify_receipt_in_transaction(conn, receipt.receipt_id).receipt_id == (
            receipt.receipt_id
        )
    finally:
        conn.rollback()
        conn.close()
