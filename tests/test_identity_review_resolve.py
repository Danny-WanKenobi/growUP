"""IR-4A: signed human identity-review resolutions (tests-first).

Covers the migration 010 decision table shape, the purpose-bound
``growup-review-v1:`` approval helpers in ``app/identity.py``, and the
atomic resolution writer ``app/identity_review_resolve.py``.

Local tests run against a thread-safe fake backend mimicking PG row
locks and ``ON CONFLICT`` replay; live-PostgreSQL tests are gated by
``GROWUP_PG_INTEGRATION=1`` with migration 010 applied. Each live
test removes its own rows.
"""

from __future__ import annotations

import copy
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_010 = REPO_ROOT / "migrations" / "010_identity_review_decisions.sql"

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(
    not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _review_keypair() -> tuple[str, str]:
    """One fresh Ed25519 keypair as (private_hex, public_hex). Test-only keys."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private = Ed25519PrivateKey.generate()
    priv_hex = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    pub_hex = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return priv_hex, pub_hex


@pytest.fixture()
def reviewer_keys() -> tuple[str, str]:
    return _review_keypair()


@pytest.fixture()
def reviewer_enrollment(monkeypatch, reviewer_keys) -> str:
    priv_hex, pub_hex = reviewer_keys
    monkeypatch.setenv("REVIEWER_PUBLIC_KEYS", f"operator-alice:{pub_hex}")
    return priv_hex


def _approval_args(review_id, outcome="SAME_PROPERTY", count=1, **overrides):
    from app.identity_review_resolve import canonical_review_snapshot

    issued = _utcnow()
    args = {
        "review_id": review_id,
        "proposed_outcome": outcome,
        "evidence_summary": "reviewed listing and parcel records",
        "expected_occurrence_count": count,
        "resolved_by": "operator-alice",
        "resolution_reference": "ticket-7",
        "approval_issued_at": issued,
        "approval_expires_at": issued + timedelta(hours=1),
    }
    args.update(overrides)
    snapshot = canonical_review_snapshot(
        review_id=args["review_id"],
        outcome=args["proposed_outcome"],
        expected_occurrence_count=args["expected_occurrence_count"],
        summary=args["evidence_summary"],
    )
    return args, snapshot


def _signed_args(review_id, private_hex, **kwargs):
    """Approval args with a valid review-domain signature attached."""
    from app.identity import canonical_review_digest, sign_review_approval

    args, snapshot = _approval_args(review_id, **kwargs)
    digest = canonical_review_digest(snapshot)
    args["approval_signature"] = sign_review_approval(
        private_key_hex=private_hex,
        review_id=args["review_id"],
        proposed_outcome=args["proposed_outcome"],
        evidence_digest=digest,
        expected_occurrence_count=args["expected_occurrence_count"],
        resolved_by=args["resolved_by"],
        resolution_reference=args["resolution_reference"],
        issued_at=args["approval_issued_at"],
        expires_at=args["approval_expires_at"],
    )
    return args


# --- Migration 010 shape (always runs; live PG applies it via migrate) --------


def test_migration_010_exists_and_is_idempotent() -> None:
    assert MIGRATION_010.exists()
    text = MIGRATION_010.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS identity_review_decisions" in text
    assert "CREATE INDEX IF NOT EXISTS" in text


def test_migration_010_enforces_decision_shape() -> None:
    text = MIGRATION_010.read_text(encoding="utf-8")
    assert "UNIQUE (review_id)" in text
    assert "REFERENCES identity_reviews(review_id)" in text
    assert "ON DELETE RESTRICT" in text
    assert "'SAME_PROPERTY'" in text and "'DISTINCT_PROPERTIES'" in text
    assert "prior_status" in text and "'PENDING'" in text
    assert "approval_signature" in text and "^[0-9a-f]{128}$" in text
    assert "signing_public_key" in text and "^[0-9a-f]{64}$" in text
    assert "sha256:[0-9a-f]{64}" in text
    assert "expected_occurrence_count" in text
    assert "decided_at" in text
    # Additive only: no existing table is altered.
    assert "ALTER TABLE" not in text


# --- Review approval cryptography --------------------------------------------


def test_review_approval_roundtrip_both_outcomes(reviewer_enrollment, reviewer_keys) -> None:
    from app.identity import (
        canonical_review_digest,
        sign_review_approval,
        verify_review_approval,
    )

    _, pub_hex = reviewer_keys
    for outcome in ("SAME_PROPERTY", "DISTINCT_PROPERTIES"):
        review_id = uuid4()
        issued = _utcnow()
        snapshot = {
            "review_id": str(review_id),
            "outcome": outcome,
            "expected_occurrence_count": 3,
            "summary": "checked evidence",
        }
        digest = canonical_review_digest(snapshot)
        assert digest.startswith("sha256:")
        signature = sign_review_approval(
            private_key_hex=reviewer_enrollment,
            review_id=review_id,
            proposed_outcome=outcome,
            evidence_digest=digest,
            expected_occurrence_count=3,
            resolved_by="operator-alice",
            resolution_reference="ticket-7",
            issued_at=issued,
            expires_at=issued + timedelta(hours=1),
        )
        approved = verify_review_approval(
            claimed_name="operator-alice",
            review_id=review_id,
            proposed_outcome=outcome,
            evidence_digest=digest,
            expected_occurrence_count=3,
            resolution_reference="ticket-7",
            issued_at=issued,
            expires_at=issued + timedelta(hours=1),
            signature_hex=signature,
        )
        assert approved.principal.name == "operator-alice"
        assert approved.public_key_hex == pub_hex
        assert approved.key_fingerprint.startswith("sha256:")


def test_review_approval_rejects_forged_and_wrong_key(reviewer_keys) -> None:
    import os

    from app.identity import AuthenticationFailedError, verify_review_approval

    _, pub_hex = reviewer_keys
    other_priv, _ = _review_keypair()
    os.environ["REVIEWER_PUBLIC_KEYS"] = f"operator-alice:{pub_hex}"
    try:
        from app.identity import sign_review_approval

        review_id = uuid4()
        issued = _utcnow()
        digest = "sha256:" + "ab" * 32
        forged_by_other = sign_review_approval(
            private_key_hex=other_priv,
            review_id=review_id,
            proposed_outcome="SAME_PROPERTY",
            evidence_digest=digest,
            expected_occurrence_count=1,
            resolved_by="operator-alice",
            resolution_reference="ticket-7",
            issued_at=issued,
            expires_at=issued + timedelta(hours=1),
        )
        base = {
            "claimed_name": "operator-alice",
            "review_id": review_id,
            "proposed_outcome": "SAME_PROPERTY",
            "evidence_digest": digest,
            "expected_occurrence_count": 1,
            "resolution_reference": "ticket-7",
            "issued_at": issued,
            "expires_at": issued + timedelta(hours=1),
        }
        with pytest.raises(AuthenticationFailedError):
            verify_review_approval(**{**base, "signature_hex": forged_by_other})
        with pytest.raises(AuthenticationFailedError):
            verify_review_approval(**{**base, "signature_hex": "00" * 64})
    finally:
        del os.environ["REVIEWER_PUBLIC_KEYS"]


def test_review_approval_rejects_tampered_fields(reviewer_enrollment) -> None:
    from app.identity import AuthenticationFailedError, verify_review_approval

    review_id = uuid4()
    args = _signed_args(review_id, reviewer_enrollment)
    issued, expires = args["approval_issued_at"], args["approval_expires_at"]
    from app.identity import canonical_review_digest
    from app.identity_review_resolve import canonical_review_snapshot

    snapshot = canonical_review_snapshot(
        review_id=review_id,
        outcome="SAME_PROPERTY",
        expected_occurrence_count=1,
        summary=args["evidence_summary"],
    )
    digest = canonical_review_digest(snapshot)
    base = {
        "claimed_name": "operator-alice",
        "review_id": review_id,
        "proposed_outcome": "SAME_PROPERTY",
        "evidence_digest": digest,
        "expected_occurrence_count": 1,
        "resolution_reference": "ticket-7",
        "issued_at": issued,
        "expires_at": expires,
        "signature_hex": args["approval_signature"],
    }
    for field, bad in [
        ("review_id", uuid4()),
        ("proposed_outcome", "DISTINCT_PROPERTIES"),
        ("expected_occurrence_count", 2),
        ("evidence_digest", "sha256:" + "00" * 32),
        ("resolution_reference", "ticket-8"),
        ("claimed_name", "operator-mallory"),
    ]:
        with pytest.raises(AuthenticationFailedError):
            verify_review_approval(**{**base, field: bad})


def test_review_approval_rejects_bad_lifetimes(reviewer_enrollment) -> None:
    from app.identity import AuthenticationFailedError, verify_review_approval

    review_id = uuid4()
    args = _signed_args(review_id, reviewer_enrollment)
    from app.identity import canonical_review_digest
    from app.identity_review_resolve import canonical_review_snapshot

    digest = canonical_review_digest(
        canonical_review_snapshot(
            review_id=review_id,
            outcome="SAME_PROPERTY",
            expected_occurrence_count=1,
            summary=args["evidence_summary"],
        )
    )
    base = {
        "claimed_name": "operator-alice",
        "review_id": review_id,
        "proposed_outcome": "SAME_PROPERTY",
        "evidence_digest": digest,
        "expected_occurrence_count": 1,
        "resolution_reference": "ticket-7",
        "signature_hex": args["approval_signature"],
    }
    now = _utcnow()
    # Expired approval.
    with pytest.raises(AuthenticationFailedError):
        verify_review_approval(
            **base,
            issued_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
    # Issued in the future beyond skew.
    with pytest.raises(AuthenticationFailedError):
        verify_review_approval(
            **base,
            issued_at=now + timedelta(minutes=5),
            expires_at=now + timedelta(hours=1),
        )
    # Lifetime exceeds the 24h cap.
    with pytest.raises(AuthenticationFailedError):
        verify_review_approval(
            **base,
            issued_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=25),
        )


def test_review_approval_rejects_reconciler_domain_and_keys(reviewer_keys) -> None:
    """A 007 reconciliation signature/key must never verify as a review approval."""
    import os

    from app.identity import (
        AuthenticationFailedError,
        sign_reconciliation_approval,
        verify_review_approval,
    )

    _, pub_hex = reviewer_keys
    os.environ["REVIEWER_PUBLIC_KEYS"] = f"operator-alice:{pub_hex}"
    os.environ["RECONCILER_PUBLIC_KEYS"] = f"operator-alice:{pub_hex}"
    try:
        issued = _utcnow()
        reconciler_sig = sign_reconciliation_approval(
            private_key_hex=reviewer_keys[0],
            action_id=uuid4(),
            attempt_id=uuid4(),
            proposed_outcome="SUCCEEDED",
            evidence_digest="sha256:" + "cd" * 32,
            reconciled_by="operator-alice",
            reconciliation_reference="ticket-7",
            issued_at=issued,
            expires_at=issued + timedelta(hours=1),
        )
        with pytest.raises(AuthenticationFailedError):
            verify_review_approval(
                claimed_name="operator-alice",
                review_id=uuid4(),
                proposed_outcome="SAME_PROPERTY",
                evidence_digest="sha256:" + "cd" * 32,
                expected_occurrence_count=1,
                resolution_reference="ticket-7",
                issued_at=issued,
                expires_at=issued + timedelta(hours=1),
                signature_hex=reconciler_sig,
            )
    finally:
        del os.environ["REVIEWER_PUBLIC_KEYS"]
        del os.environ["RECONCILER_PUBLIC_KEYS"]


def test_review_approval_rejects_reconciler_only_enrollment(reviewer_keys) -> None:
    """A key enrolled only as reconciler authorizes no review."""
    import os

    from app.identity import (
        AuthenticationFailedError,
        sign_review_approval,
        verify_review_approval,
    )

    priv_hex, pub_hex = reviewer_keys
    os.environ["RECONCILER_PUBLIC_KEYS"] = f"operator-alice:{pub_hex}"
    os.environ.pop("REVIEWER_PUBLIC_KEYS", None)
    try:
        review_id = uuid4()
        issued = _utcnow()
        digest = "sha256:" + "ef" * 32
        signature = sign_review_approval(
            private_key_hex=priv_hex,
            review_id=review_id,
            proposed_outcome="SAME_PROPERTY",
            evidence_digest=digest,
            expected_occurrence_count=1,
            resolved_by="operator-alice",
            resolution_reference="ticket-7",
            issued_at=issued,
            expires_at=issued + timedelta(hours=1),
        )
        with pytest.raises(AuthenticationFailedError):
            verify_review_approval(
                claimed_name="operator-alice",
                review_id=review_id,
                proposed_outcome="SAME_PROPERTY",
                evidence_digest=digest,
                expected_occurrence_count=1,
                resolution_reference="ticket-7",
                issued_at=issued,
                expires_at=issued + timedelta(hours=1),
                signature_hex=signature,
            )
    finally:
        del os.environ["RECONCILER_PUBLIC_KEYS"]


def test_reconciliation_approval_regression() -> None:
    """Existing 007 behavior is untouched by the review sibling."""
    from app.identity import (
        sign_reconciliation_approval,
        verify_reconciliation_approval,
    )

    priv_hex, pub_hex = _review_keypair()
    issued = _utcnow()
    action_id, attempt_id = uuid4(), uuid4()
    signature = sign_reconciliation_approval(
        private_key_hex=priv_hex,
        action_id=action_id,
        attempt_id=attempt_id,
        proposed_outcome="FAILED",
        evidence_digest="sha256:" + "11" * 32,
        reconciled_by="operator-alice",
        reconciliation_reference="ticket-9",
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
    )
    approved = verify_reconciliation_approval(
        claimed_name="operator-alice",
        action_id=action_id,
        attempt_id=attempt_id,
        proposed_outcome="FAILED",
        evidence_digest="sha256:" + "11" * 32,
        reconciliation_reference="ticket-9",
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
        signature_hex=signature,
        reconcilers={"operator-alice": bytes.fromhex(pub_hex)},
    )
    assert approved.principal.name == "operator-alice"


# --- Fake PostgreSQL backend (row locks + ON CONFLICT replay) -----------------


class _FakeDB:
    """Thread-safe tables with per-review locks mimicking FOR UPDATE."""

    def __init__(self) -> None:
        self.guard = threading.RLock()
        self.reviews: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}  # keyed by review_id
        self.events: dict[str, dict] = {}
        self.row_locks: dict[str, threading.Lock] = {}
        self.opened = 0

    def lock_for(self, review_id: str) -> threading.Lock:
        with self.guard:
            return self.row_locks.setdefault(review_id, threading.Lock())


class _FakeCursor:
    def __init__(self, db: _FakeDB, conn: "_FakeConn") -> None:
        self._db = db
        self._conn = conn
        self._row = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql: str, params=None):
        params = dict(params or {})
        lowered = " ".join(sql.split()).lower()
        if any(
            verb in lowered and table in lowered
            for verb in ("insert into", "update", "delete from")
            for table in ("properties", "listing_evidence", "identity_review_occurrences")
        ):
            raise AssertionError(f"resolution must not write business rows: {sql[:80]}")
        if lowered.startswith(("insert into", "update ", "delete from")):
            self._conn._dirty = True
        with self._db.guard:
            if lowered.startswith("select * from identity_reviews where review_id"):
                rid = str(params["review_id"])
                self._row = copy.deepcopy(self._db.reviews.get(rid))
                if "for update" in lowered:
                    self._conn._acquire(rid)
                return
            if lowered.startswith("select * from identity_review_decisions where review_id"):
                self._row = copy.deepcopy(self._db.decisions.get(str(params["review_id"])))
                return
            if lowered.startswith("insert into identity_review_decisions"):
                rid = str(params["review_id"])
                if rid in self._db.decisions:
                    self._row = None  # ON CONFLICT DO NOTHING
                else:
                    row = {**params, "review_id": rid, "decided_at": _utcnow()}
                    self._db.decisions[rid] = row
                    self._row = copy.deepcopy(row)
                return
            if lowered.startswith("update identity_reviews set status"):
                rid = str(params["review_id"])
                row = self._db.reviews[rid]
                row["status"] = params["status"]
                row["updated_at"] = _utcnow()
                self._row = copy.deepcopy(row)
                return
            if lowered.startswith("insert into agent_events"):
                eid = params["event_id"]
                if eid in self._db.events:
                    raise Exception(f"duplicate event_id: {eid}")
                if self._conn._poison_events:
                    raise Exception("poisoned event insert")
                self._db.events[eid] = dict(params)
                self._row = None
                return
            raise AssertionError(f"unexpected SQL: {sql[:100]}")

    def fetchone(self):
        return copy.deepcopy(self._row)

    def fetchall(self):
        return [] if self._row is None else [copy.deepcopy(self._row)]


class _FakeConn:
    """Connection with creation-time snapshot rollback."""

    def __init__(self, db: _FakeDB) -> None:
        self._db = db
        self._held: list[threading.Lock] = []
        self._poison_events = False
        self._dirty = False
        with db.guard:
            db.opened += 1
            self._snapshot = (
                copy.deepcopy(db.reviews),
                copy.deepcopy(db.decisions),
                copy.deepcopy(db.events),
            )
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def _acquire(self, review_id: str) -> None:
        lock = self._db.lock_for(review_id)
        assert lock.acquire(timeout=10), "row-lock deadlock in fake backend"
        self._held.append(lock)

    def _release(self) -> None:
        for lock in self._held:
            try:
                lock.release()
            except RuntimeError:
                pass
        self._held = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db, self)

    def commit(self) -> None:
        self.committed = True
        self._release()

    def rollback(self) -> None:
        # Real rollback undoes only this connection's writes; a clean
        # connection (e.g. the replay path) must not clobber committed rows.
        if self._dirty:
            with self._db.guard:
                reviews, decisions, events = copy.deepcopy(self._snapshot)
                self._db.reviews = reviews
                self._db.decisions = decisions
                self._db.events = events
        self.rolled_back = True
        self._release()

    def close(self) -> None:
        self.closed = True
        self._release()

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
        return None


@pytest.fixture()
def fake_db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture()
def fake_conn_factory(fake_db, monkeypatch):
    import app.identity_review_resolve as writer

    conns: list[_FakeConn] = []

    def _factory(config=None) -> _FakeConn:
        conn = _FakeConn(fake_db)
        conns.append(conn)
        return conn

    monkeypatch.setattr(writer, "get_connection", _factory)
    return conns


def _seed_review(
    fake_db: _FakeDB,
    review_id,
    *,
    status: str = "PENDING",
    occurrence_count: int = 1,
    run_id: str = "run-seed",
) -> str:
    rid = str(review_id)
    fake_db.reviews[rid] = {
        "review_id": rid,
        "dedup_key": f"test:{rid}",
        "source": "intake",
        "reason_code": "AMBIGUOUS_ADDRESS",
        "status": status,
        "occurrence_count": occurrence_count,
        "run_id": run_id,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    return rid


# --- Writer behavior ----------------------------------------------------------


def test_valid_resolution_same_property(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)
    result = writer.record_review_resolution(
        **_signed_args(review_id, reviewer_enrollment)
    )
    assert result.created is True
    assert result.review["status"] == "RESOLVED"
    assert result.review["occurrence_count"] == 1
    decision = result.decision
    assert decision["outcome"] == "SAME_PROPERTY"
    assert decision["prior_status"] == "PENDING"
    assert decision["resolved_by"] == "operator-alice"
    assert decision["resolution_reference"] == "ticket-7"
    assert decision["evidence_digest"].startswith("sha256:")
    assert decision["expected_occurrence_count"] == 1
    assert len(decision["approval_signature"]) == 128
    assert result.audit_event_id is not None
    event = fake_db.events[result.audit_event_id]
    assert event["event_type"] == "identity.review.resolved"
    assert event["retention_class"] == "permanent"
    assert event["property_id"] is None
    assert fake_conn_factory[0].committed is True


def test_valid_resolution_distinct_properties(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id, occurrence_count=4)
    result = writer.record_review_resolution(
        **_signed_args(review_id, reviewer_enrollment,
                       outcome="DISTINCT_PROPERTIES", count=4)
    )
    assert result.created is True
    assert result.decision["outcome"] == "DISTINCT_PROPERTIES"
    assert result.review["status"] == "RESOLVED"


def test_unknown_review_raises_not_found(reviewer_enrollment, fake_db, fake_conn_factory) -> None:
    import app.identity_review_resolve as writer

    with pytest.raises(writer.ReviewNotFoundError):
        writer.record_review_resolution(
            **_signed_args(uuid4(), reviewer_enrollment)
        )
    assert fake_db.decisions == {}
    assert fake_db.events == {}


def test_stale_occurrence_count_writes_nothing(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id, occurrence_count=3)
    with pytest.raises(writer.StaleReviewError):
        writer.record_review_resolution(
            **_signed_args(review_id, reviewer_enrollment, count=2)
        )
    assert fake_db.decisions == {}
    assert fake_db.events == {}
    assert fake_db.reviews[str(review_id)]["status"] == "PENDING"


def test_forged_signature_never_opens_connection(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    from app.identity import AuthenticationFailedError

    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)
    args = _signed_args(review_id, reviewer_enrollment)
    args["approval_signature"] = "ff" * 64
    with pytest.raises(AuthenticationFailedError):
        writer.record_review_resolution(**args)
    assert fake_db.opened == 0
    assert fake_db.reviews[str(review_id)]["status"] == "PENDING"


def test_identical_replay_writes_nothing(reviewer_enrollment, fake_db, fake_conn_factory) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)
    first = writer.record_review_resolution(
        **_signed_args(review_id, reviewer_enrollment)
    )
    assert first.created is True
    events_before = len(fake_db.events)
    # Renewed approval, fresh timestamps, same decision content.
    replay = writer.record_review_resolution(
        **_signed_args(review_id, reviewer_enrollment)
    )
    assert replay.created is False
    assert replay.decision["decision_id"] == first.decision["decision_id"]
    assert len(fake_db.events) == events_before
    assert fake_db.reviews[str(review_id)]["status"] == "RESOLVED"


def test_conflicting_second_resolution_rejected(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)
    writer.record_review_resolution(**_signed_args(review_id, reviewer_enrollment))
    with pytest.raises(writer.ReviewResolutionConflictError):
        writer.record_review_resolution(
            **_signed_args(review_id, reviewer_enrollment,
                           outcome="DISTINCT_PROPERTIES")
        )
    with pytest.raises(writer.ReviewResolutionConflictError):
        writer.record_review_resolution(
            **_signed_args(review_id, reviewer_enrollment,
                           resolution_reference="ticket-8")
        )
    assert len(fake_db.decisions) == 1


def test_audit_failure_rolls_back_decision_and_status(
    reviewer_enrollment, fake_db, fake_conn_factory, monkeypatch
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)

    real_factory = writer.get_connection

    def _poisoned(config=None):
        conn = real_factory(config)
        conn._poison_events = True
        return conn

    monkeypatch.setattr(writer, "get_connection", _poisoned)
    with pytest.raises(Exception):
        writer.record_review_resolution(
            **_signed_args(review_id, reviewer_enrollment)
        )
    assert fake_db.decisions == {}
    assert fake_db.events == {}
    assert fake_db.reviews[str(review_id)]["status"] == "PENDING"


def test_no_private_key_material_in_stored_rows(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)
    result = writer.record_review_resolution(
        **_signed_args(review_id, reviewer_enrollment)
    )
    blob = repr(result.decision) + repr(fake_db.events[result.audit_event_id])
    assert reviewer_enrollment not in blob


def test_concurrent_resolvers_single_winner(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def _resolve():
        barrier.wait(timeout=10)
        try:
            outcomes.append(
                writer.record_review_resolution(
                    **_signed_args(review_id, reviewer_enrollment)
                )
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            outcomes.append(exc)

    threads = [threading.Thread(target=_resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)
    assert len(fake_db.decisions) == 1
    created = [item for item in outcomes if not isinstance(item, Exception) and item.created]
    assert len(created) == 1


def test_concurrent_rival_outcomes_one_conflict(
    reviewer_enrollment, fake_db, fake_conn_factory
) -> None:
    import app.identity_review_resolve as writer

    review_id = uuid4()
    _seed_review(fake_db, review_id)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def _resolve(outcome):
        barrier.wait(timeout=10)
        try:
            outcomes.append(
                writer.record_review_resolution(
                    **_signed_args(review_id, reviewer_enrollment, outcome=outcome)
                )
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            outcomes.append(exc)

    threads = [
        threading.Thread(target=_resolve, args=(outcome,))
        for outcome in ("SAME_PROPERTY", "DISTINCT_PROPERTIES")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert len(fake_db.decisions) == 1
    assert sum(isinstance(item, writer.ReviewResolutionConflictError) for item in outcomes) == 1
    assert sum(
        not isinstance(item, Exception) and item.created for item in outcomes
    ) == 1


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1, migration 010) ------
#
# Seeds run through real SQL plus the genuine queue writer, so every
# resolution below exercises true row locks, constraints, and
# commit/rollback. Each test removes its own rows in a finally block.


def _live_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _live_seed_review(cfg, tag: str):
    """Insert one decision event, review, occurrence event, and occurrence."""
    from app.properties_db import get_connection

    review_id = uuid4()
    event_id = f"evt-resolve-{tag}-{uuid4().hex[:8]}"
    occ_event_id = f"evt-resolve-{tag}-occ-{uuid4().hex[:8]}"
    pid = uuid4()
    with get_connection(cfg) as conn:
        with conn.cursor() as cur:
            for eid in (event_id, occ_event_id):
                cur.execute(
                    "INSERT INTO agent_events (event_id, occurred_at, agent, run_id,"
                    " correlation_id, event_type, severity, summary, details,"
                    " evidence_refs, retention_class)"
                    " VALUES (%(eid)s, now(), 'gabriel', %(run)s, '',"
                    " 'gabriel.intake.decision', 'INFO', 'seed', '{}', '[]', 'permanent')",
                    {"eid": eid, "run": f"run-resolve-{tag}"},
                )
            cur.execute(
                "INSERT INTO identity_reviews (review_id, dedup_key, source, reason_code,"
                " observed_identity, conflicting_property_ids, decision_event_id,"
                " run_id, status, occurrence_count, last_event_id)"
                " VALUES (%(rid)s, %(key)s, 'intake', 'AMBIGUOUS_ADDRESS', '{}',"
                " %(pids)s, %(eid)s, %(run)s, 'PENDING', 1, %(eid)s)",
                {
                    "rid": str(review_id),
                    "key": f"live-resolve:{tag}:{review_id}",
                    "pids": [str(pid)],
                    "eid": event_id,
                    "run": f"run-resolve-{tag}",
                },
            )
            cur.execute(
                "INSERT INTO identity_review_occurrences (occurrence_id, review_id, event_id)"
                " VALUES (%(oid)s, %(rid)s, %(eid)s)",
                {"oid": str(uuid4()), "rid": str(review_id), "eid": occ_event_id},
            )
        conn.commit()
    return review_id, [event_id, occ_event_id]


def _live_cleanup(cfg, review_ids, event_ids) -> None:
    from app.properties_db import get_connection

    with get_connection(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM identity_review_decisions WHERE review_id = ANY(%(ids)s)",
                {"ids": [str(rid) for rid in review_ids]},
            )
            cur.execute(
                "DELETE FROM identity_review_occurrences WHERE review_id = ANY(%(ids)s)",
                {"ids": [str(rid) for rid in review_ids]},
            )
            cur.execute(
                "DELETE FROM identity_reviews WHERE review_id = ANY(%(ids)s)",
                {"ids": [str(rid) for rid in review_ids]},
            )
            if event_ids:
                cur.execute(
                    "DELETE FROM agent_events WHERE event_id = ANY(%(ids)s)",
                    {"ids": list(event_ids)},
                )
        conn.commit()


def _live_intake_event(event_id: str, run_id: str, pid: UUID):
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity

    return AgentEvent(
        event_id=event_id,
        agent=AgentId.GABRIEL,
        run_id=run_id,
        event_type="gabriel.intake.decision",
        summary="live intake decision (needs_identity_review)",
        severity=Severity.INFO,
        retention_class=RetentionClass.PERMANENT,
        details={
            "outcome": "needs_identity_review",
            "reason_code": "AMBIGUOUS_ADDRESS",
            "message": "live test",
            "mutation_status": "NOT_ATTEMPTED",
            "observed": {"source_name": "live-test", "listing_url": None},
            "inferred": {
                "name": f"Live Resolve Hall {run_id}",
                "street_address": f"700 Live Resolve Way {run_id}",
                "city": "Dallas",
                "state": "TX",
                "zip_code": "75201",
            },
            "considered_property_ids": [str(pid)],
            "conflicting_property_ids": [str(pid)],
        },
    )


@needs_live_pg
def test_live_valid_resolution(monkeypatch) -> None:
    import app.identity_review_resolve as writer

    cfg = _live_config()
    priv_hex, pub_hex = _review_keypair()
    monkeypatch.setenv("REVIEWER_PUBLIC_KEYS", f"operator-alice:{pub_hex}")
    tag = uuid4().hex[:8]
    review_id, event_ids = _live_seed_review(cfg, tag)
    try:
        args = _signed_args(review_id, priv_hex)
        result = writer.record_review_resolution(**args, config=cfg)
        assert result.created is True
        assert result.review["status"] == "RESOLVED"
        assert result.decision["outcome"] == "SAME_PROPERTY"
        event_ids.append(result.audit_event_id)
        from app.properties_db import get_connection

        with get_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT retention_class, event_type FROM agent_events"
                    " WHERE event_id = %(eid)s",
                    {"eid": result.audit_event_id},
                )
                row = cur.fetchone()
        assert row["retention_class"] == "permanent"
        assert row["event_type"] == "identity.review.resolved"
    finally:
        _live_cleanup(cfg, [review_id], event_ids)


@needs_live_pg
def test_live_occurrence_count_freshness(monkeypatch) -> None:
    """Counts change as evidence is added; stale approvals fail, fresh succeed."""
    import app.identity_review_resolve as writer
    from app.identity_reviews_db import record_review_occurrence
    from app.observability.store import insert_event_in_transaction
    from app.properties_db import get_connection

    cfg = _live_config()
    priv_hex, pub_hex = _review_keypair()
    monkeypatch.setenv("REVIEWER_PUBLIC_KEYS", f"operator-alice:{pub_hex}")
    tag = uuid4().hex[:8]
    review_id, event_ids = _live_seed_review(cfg, tag)
    try:
        # Add a genuine second occurrence through the real queue writer.
        pid = uuid4()
        run_id = f"run-resolve-{tag}-q"
        with get_connection(cfg) as conn:
            for suffix in ("a", "b"):
                event = _live_intake_event(f"evt-resolve-{tag}-q{suffix}", run_id, pid)
                insert_event_in_transaction(conn, event)
                record_review_occurrence(conn, event)
                event_ids.append(event.event_id)
            conn.commit()
        with get_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT review_id, occurrence_count FROM identity_reviews"
                    " WHERE run_id = %(run)s",
                    {"run": run_id},
                )
                found = cur.fetchone()
        assert found["occurrence_count"] == 2
        queue_review = found["review_id"]
        with pytest.raises(writer.StaleReviewError):
            writer.record_review_resolution(
                **_signed_args(queue_review, priv_hex, count=1), config=cfg
            )
        result = writer.record_review_resolution(
            **_signed_args(queue_review, priv_hex, count=2), config=cfg
        )
        assert result.created is True
        event_ids.append(result.audit_event_id)
        _live_cleanup(cfg, [review_id, queue_review], event_ids)
        event_ids.clear()
    finally:
        _live_cleanup(cfg, [review_id], event_ids)


@needs_live_pg
def test_live_concurrent_single_winner(monkeypatch) -> None:
    import app.identity_review_resolve as writer

    cfg = _live_config()
    priv_hex, pub_hex = _review_keypair()
    monkeypatch.setenv("REVIEWER_PUBLIC_KEYS", f"operator-alice:{pub_hex}")
    tag = uuid4().hex[:8]
    review_id, event_ids = _live_seed_review(cfg, tag)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def _resolve():
        barrier.wait(timeout=30)
        try:
            outcomes.append(
                writer.record_review_resolution(
                    **_signed_args(review_id, priv_hex), config=cfg
                )
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            outcomes.append(exc)

    try:
        threads = [threading.Thread(target=_resolve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(not thread.is_alive() for thread in threads)
        assert sum(
            not isinstance(item, Exception) and item.created for item in outcomes
        ) == 1
        for item in outcomes:
            if not isinstance(item, Exception) and item.audit_event_id:
                event_ids.append(item.audit_event_id)
        from app.properties_db import get_connection

        with get_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM identity_review_decisions"
                    " WHERE review_id = %(rid)s",
                    {"rid": str(review_id)},
                )
                assert cur.fetchone()["n"] == 1
    finally:
        _live_cleanup(cfg, [review_id], event_ids)


@needs_live_pg
def test_live_no_property_mutation(monkeypatch) -> None:
    import app.identity_review_resolve as writer
    from app.properties_db import get_connection

    cfg = _live_config()
    priv_hex, pub_hex = _review_keypair()
    monkeypatch.setenv("REVIEWER_PUBLIC_KEYS", f"operator-alice:{pub_hex}")
    tag = uuid4().hex[:8]

    def _counts():
        with get_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM properties")
                props = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) AS n FROM listing_evidence")
                evidence = cur.fetchone()["n"]
        return props, evidence

    before = _counts()
    review_id, event_ids = _live_seed_review(cfg, tag)
    try:
        result = writer.record_review_resolution(
            **_signed_args(review_id, priv_hex), config=cfg
        )
        assert result.created is True
        event_ids.append(result.audit_event_id)
        assert _counts() == before
    finally:
        _live_cleanup(cfg, [review_id], event_ids)
