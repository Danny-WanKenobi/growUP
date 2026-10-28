"""Reconciliation tests: identity, evidence, and atomic record (3C-3B-3B-2).

Pure validation (canonical digests, purpose-bound Ed25519
reconciliation approvals, immutable evidence contract), then
record-path tests against a fake PostgreSQL backend that mimics
row locks, ON CONFLICT replay, and commit/rollback counters, and
finally gated live-PostgreSQL tests (GROWUP_PG_INTEGRATION=1,
migrations 005-007 applied) seeding through the real ledger.
No execution, retry, requeue, routes, or workers anywhere here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

import json
import os
import threading

from app.db import DatabaseUnavailableError
from app.external_actions_db import (
    ActionNotFoundError,
    StaleAttemptError,
)
from app.external_action_reconcile import (
    InsufficientEvidenceError,
    ReconciliationConflictError,
    ReconciliationEvidence,
    ensure_observation_not_future,
    ensure_observation_predates_approval,
    ensure_outcome_consistent,
    record_reconciliation,
)
from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    ExecutorPrincipal,
    HumanPrincipal,
    OrchestratorPrincipal,
    canonical_evidence_digest,
    canonical_identity_name,
    ensure_may_reconcile,
    key_fingerprint,
    load_reconciler_keys,
    may_reconcile,
    parse_reconciler_keys,
    sign_approval,
    sign_reconciliation_approval,
    validate_key_fingerprint,
    verify_reconciliation_approval,
)

_T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw().hex(), private.public_key().public_bytes_raw().hex()


def _evidence_dict(**overrides):
    base = {
        "kind": "provider_receipt",
        "source": "provider-support-desk",
        "observed_at": "2026-10-28T11:30:00+00:00",
        "asserts_outcome": "SUCCEEDED",
        "artifact_ref": "receipt-8842",
        "provider_reference": "op-71c0",
        "storage_ref": None,
        "summary": "Provider confirmed completion by ticket.",
    }
    base.update(overrides)
    return base


def _approval_fields(private_hex, name="operator-alice", **overrides):
    issued = overrides.pop("issued_at", _T0)
    expires = overrides.pop("expires_at", _T0 + timedelta(hours=1))
    fields = {
        "action_id": uuid4(),
        "attempt_id": uuid4(),
        "proposed_outcome": "SUCCEEDED",
        "evidence_digest": canonical_evidence_digest(_evidence_dict()),
        "reconciliation_reference": "recon-1",
        "issued_at": issued,
        "expires_at": expires,
    }
    fields.update(overrides)
    signature = sign_reconciliation_approval(
        private_key_hex=private_hex, reconciled_by=name, **fields
    )
    return fields, signature


def test_evidence_digest_stable() -> None:
    first = canonical_evidence_digest(_evidence_dict())
    assert first.startswith("sha256:")
    assert canonical_evidence_digest(_evidence_dict()) == first
    reordered = dict(reversed(list(_evidence_dict().items())))
    assert canonical_evidence_digest(reordered) == first  # key order irrelevant
    for spelling in (
        "2026-10-28T11:30:00Z",
        "2026-10-28 11:30:00+00:00",
        "2026-10-28T12:30:00+01:00",
    ):
        assert canonical_evidence_digest(_evidence_dict(observed_at=spelling)) == first
    assert canonical_evidence_digest(_evidence_dict(summary="other")) != first
    assert canonical_evidence_digest(_evidence_dict(kind="provider_status_reference")) != first
    model = ReconciliationEvidence(**_evidence_dict())
    assert model.digest() == first  # model and raw dict agree
    assert set(model.as_row()) == set(_evidence_dict())
    with pytest.raises(TypeError):
        canonical_evidence_digest("not-a-dict")
    with pytest.raises(ValueError, match="ISO-8601"):
        canonical_evidence_digest(_evidence_dict(observed_at="yesterday"))
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_evidence_digest(_evidence_dict(observed_at="2026-10-28T11:30:00"))


def test_valid_reconciliation_signature_verifies(monkeypatch) -> None:
    priv, pub = _keypair()
    fields, signature = _approval_fields(priv)
    enrolled = {"operator-alice": bytes.fromhex(pub)}
    approved = verify_reconciliation_approval(
        claimed_name="Operator-Alice", signature_hex=signature,
        reconcilers=enrolled, now=_T0 + timedelta(minutes=1), **fields,
    )
    assert approved.principal == HumanPrincipal(name="operator-alice")
    assert approved.key_fingerprint == key_fingerprint(bytes.fromhex(pub))
    assert approved.public_key_hex == pub
    assert validate_key_fingerprint(approved.key_fingerprint) == approved.key_fingerprint
    # Enrollment also loads from the environment.
    monkeypatch.setenv("RECONCILER_PUBLIC_KEYS", f"operator-alice:{pub}")
    again = verify_reconciliation_approval(
        claimed_name="operator-alice", signature_hex=signature,
        now=_T0 + timedelta(minutes=1), **fields,
    )
    assert again.principal.name == "operator-alice"


def test_authorization_signature_rejected_for_reconciliation() -> None:
    priv, pub = _keypair()
    other_signature = sign_approval(
        private_key_hex=priv,
        idempotency_key="key-1",
        request_fingerprint="sha256:abc",
        authorized_by="operator-alice",
        authorization_reference="ticket-1",
        issued_at=_T0,
        expires_at=_T0 + timedelta(hours=1),
    )
    fields, _ = _approval_fields(priv)
    enrolled = {"operator-alice": bytes.fromhex(pub)}
    with pytest.raises(AuthenticationFailedError, match="signature mismatch"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=other_signature,
            reconcilers=enrolled, now=_T0 + timedelta(minutes=1), **fields,
        )


def test_tampered_evidence_rejected() -> None:
    priv, pub = _keypair()
    fields, signature = _approval_fields(priv)
    enrolled = {"operator-alice": bytes.fromhex(pub)}
    tampered = canonical_evidence_digest(_evidence_dict(artifact_ref="receipt-9999"))
    assert tampered != fields["evidence_digest"]
    with pytest.raises(AuthenticationFailedError, match="signature mismatch"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=signature,
            reconcilers=enrolled, now=_T0 + timedelta(minutes=1),
            **{**fields, "evidence_digest": tampered},
        )


def test_wrong_principal_reference_and_key_rejected() -> None:
    priv_alice, pub_alice = _keypair()
    priv_bob, pub_bob = _keypair()
    fields, signature = _approval_fields(priv_alice)
    enrolled = {
        "operator-alice": bytes.fromhex(pub_alice),
        "operator-bob": bytes.fromhex(pub_bob),
    }
    moment = _T0 + timedelta(minutes=1)
    with pytest.raises(AuthenticationFailedError, match="signature mismatch"):
        verify_reconciliation_approval(
            claimed_name="operator-bob", signature_hex=signature,
            reconcilers=enrolled, now=moment, **fields,
        )
    with pytest.raises(AuthenticationFailedError, match="signature mismatch"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=signature,
            reconcilers=enrolled, now=moment,
            **{**fields, "reconciliation_reference": "recon-2"},
        )
    with pytest.raises(AuthenticationFailedError, match="unknown reconciler"):
        verify_reconciliation_approval(
            claimed_name="operator-carol", signature_hex=signature,
            reconcilers=enrolled, now=moment, **fields,
        )
    # Mallory signs AS alice with Mallory's key: name matches, key does not.
    priv_mallory, _ = _keypair()
    forged, forged_sig = _approval_fields(priv_mallory)
    with pytest.raises(AuthenticationFailedError, match="signature mismatch"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=forged_sig,
            reconcilers=enrolled, now=moment, **forged,
        )


def test_expired_and_future_issued_approvals() -> None:
    priv, pub = _keypair()
    enrolled = {"operator-alice": bytes.fromhex(pub)}
    fields, signature = _approval_fields(priv)
    with pytest.raises(AuthenticationFailedError, match="approval expired"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=signature,
            reconcilers=enrolled, now=_T0 + timedelta(hours=1, minutes=2), **fields,
        )
    future_fields, future_sig = _approval_fields(
        priv, issued_at=_T0 + timedelta(seconds=61),
        expires_at=_T0 + timedelta(hours=1, seconds=61),
    )
    with pytest.raises(AuthenticationFailedError, match="in the future"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=future_sig,
            reconcilers=enrolled, now=_T0, **future_fields,
        )
    within_fields, within_sig = _approval_fields(
        priv, issued_at=_T0 + timedelta(seconds=30),
        expires_at=_T0 + timedelta(hours=1, seconds=30),
    )
    approved = verify_reconciliation_approval(
        claimed_name="operator-alice", signature_hex=within_sig,
        reconcilers=enrolled, now=_T0, **within_fields,
    )
    assert approved.principal.name == "operator-alice"  # skew allowance honored
    bad_fields, bad_sig = _approval_fields(priv, issued_at=_T0, expires_at=_T0)
    with pytest.raises(AuthenticationFailedError, match="later than issued"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=bad_sig,
            reconcilers=enrolled, now=_T0, **bad_fields,
        )


def test_approval_lifetime_limits() -> None:
    priv, pub = _keypair()
    enrolled = {"operator-alice": bytes.fromhex(pub)}
    exact_fields, exact_sig = _approval_fields(
        priv, issued_at=_T0, expires_at=_T0 + timedelta(hours=24)
    )
    approved = verify_reconciliation_approval(
        claimed_name="operator-alice", signature_hex=exact_sig,
        reconcilers=enrolled, now=_T0 + timedelta(hours=1), **exact_fields,
    )
    assert approved.principal.name == "operator-alice"  # exactly 24h is allowed
    long_fields, long_sig = _approval_fields(
        priv, issued_at=_T0, expires_at=_T0 + timedelta(hours=24, seconds=1)
    )
    with pytest.raises(AuthenticationFailedError, match="exceeds maximum TTL"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=long_sig,
            reconcilers=enrolled, now=_T0 + timedelta(hours=1), **long_fields,
        )


def test_missing_reconciler_enrollment(monkeypatch) -> None:
    priv, _ = _keypair()
    fields, signature = _approval_fields(priv)
    with pytest.raises(AuthenticationFailedError, match="unknown reconciler"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=signature,
            reconcilers={}, now=_T0 + timedelta(minutes=1), **fields,
        )
    monkeypatch.delenv("RECONCILER_PUBLIC_KEYS", raising=False)
    with pytest.raises(AuthenticationFailedError, match="unknown reconciler"):
        verify_reconciliation_approval(
            claimed_name="operator-alice", signature_hex=signature,
            now=_T0 + timedelta(minutes=1), **fields,
        )


def test_reconciler_enrollment_parsing(monkeypatch) -> None:
    _, pub = _keypair()
    parsed = parse_reconciler_keys(f"Operator-Alice:{pub}")
    assert set(parsed) == {"operator-alice"}
    assert parse_reconciler_keys(None) == {}
    with pytest.raises(AuthenticationFailedError, match="reconciler map"):
        parse_reconciler_keys("operator-alice")
    monkeypatch.setenv("RECONCILER_PUBLIC_KEYS", f"operator-alice:{pub}")
    assert set(load_reconciler_keys()) == {"operator-alice"}
    monkeypatch.delenv("RECONCILER_PUBLIC_KEYS")
    assert load_reconciler_keys() == {}


def test_key_fingerprint_validation() -> None:
    _, pub = _keypair()
    fingerprint = key_fingerprint(bytes.fromhex(pub))
    assert fingerprint.startswith("sha256:") and len(fingerprint) == 71
    assert validate_key_fingerprint(fingerprint) == fingerprint
    for bad in ("sha256:xyz", "sha256:" + "0" * 63, "0" * 64, "", None, 42):
        with pytest.raises(ValueError, match="[Ff]ingerprint|public key"):
            validate_key_fingerprint(bad)
    with pytest.raises(ValueError, match="32 raw bytes"):
        key_fingerprint(b"too-short")


def test_may_reconcile_policy() -> None:
    assert may_reconcile(HumanPrincipal(name="operator-alice")) is True
    for other in (
        ExecutorPrincipal(executor_id="w-1"),
        OrchestratorPrincipal(orchestrator_id="sched-1"),
        "operator-alice", None, 42, object(),
    ):
        assert may_reconcile(other) is False
    ensure_may_reconcile(HumanPrincipal(name="operator-alice"))
    with pytest.raises(AuthorizationDeniedError, match="verified human reconciler"):
        ensure_may_reconcile(ExecutorPrincipal(executor_id="w-1"))


def test_unsupported_evidence_kinds_rejected() -> None:
    for bad_kind in ("human_assertion", "system_proof", "", "PROVIDER_RECEIPT"):
        with pytest.raises(ValidationError):
            ReconciliationEvidence(**_evidence_dict(kind=bad_kind))
    assert ReconciliationEvidence(**_evidence_dict(kind="provider_status_reference")).kind == (
        "provider_status_reference"
    )


def test_missing_artifact_reference_rejected() -> None:
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            ReconciliationEvidence(**_evidence_dict(artifact_ref=bad))
    incomplete = _evidence_dict()
    del incomplete["artifact_ref"]
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**incomplete)


def test_contradictory_asserted_outcome_rejected() -> None:
    assert issubclass(InsufficientEvidenceError, ValueError)
    evidence = ReconciliationEvidence(**_evidence_dict(asserts_outcome="SUCCEEDED"))
    ensure_outcome_consistent(evidence, "SUCCEEDED")
    with pytest.raises(InsufficientEvidenceError, match="does not support"):
        ensure_outcome_consistent(evidence, "FAILED")
    with pytest.raises(ValueError, match="proposed_outcome"):
        ensure_outcome_consistent(evidence, "UNKNOWN")
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(asserts_outcome="UNKNOWN"))


def test_observation_timestamps_validated() -> None:
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(observed_at="2026-10-28T11:30:00"))
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(observed_at="last tuesday"))
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(observed_at=None))
    model = ReconciliationEvidence(
        **_evidence_dict(observed_at="2026-10-28T11:30:00Z")
    )
    assert model.observed_at == "2026-10-28T11:30:00+00:00"  # normalized
    ensure_observation_not_future(model.observed_at, now=_T0)
    with pytest.raises(InsufficientEvidenceError, match="in the future"):
        ensure_observation_not_future("2026-10-28T12:01:01+00:00", now=_T0)
    ensure_observation_not_future("2026-10-28T12:00:30+00:00", now=_T0)  # skew ok
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_observation_not_future(model.observed_at, now=_T0.replace(tzinfo=None))


def test_evidence_observed_after_approval_rejected() -> None:
    ensure_observation_predates_approval("2026-10-28T11:30:00+00:00", _T0)
    ensure_observation_predates_approval("2026-10-28T12:00:00+00:00", _T0)
    ensure_observation_predates_approval("2026-10-28T12:00:30+00:00", _T0)  # skew ok
    with pytest.raises(InsufficientEvidenceError, match="after approval"):
        ensure_observation_predates_approval("2026-10-28T12:01:01+00:00", _T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_observation_predates_approval(
            "2026-10-28T11:30:00+00:00", _T0.replace(tzinfo=None)
        )


def test_system_verified_claims_rejected() -> None:
    with pytest.raises(ValidationError):
        ReconciliationEvidence(
            **_evidence_dict(verification_tier="system_verified")
        )
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(unexpected_field="x"))


def test_field_lengths_and_whitespace() -> None:
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(source="s" * 201))
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(artifact_ref="a" * 501))
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(summary="s" * 501))
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(provider_reference="p" * 501))
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(source="   "))
    with pytest.raises(ValidationError):
        ReconciliationEvidence(**_evidence_dict(provider_reference="  "))
    cleaned = ReconciliationEvidence(
        **_evidence_dict(source="  desk-1  ", summary="  noted  ")
    )
    assert (cleaned.source, cleaned.summary) == ("desk-1", "noted")
    assert ReconciliationEvidence(**_evidence_dict(provider_reference=None)).provider_reference is None
    assert ReconciliationEvidence(**_evidence_dict()).summary.startswith("Provider")


def test_evidence_model_immutable() -> None:
    model = ReconciliationEvidence(**_evidence_dict())
    with pytest.raises(ValidationError):
        model.kind = "provider_status_reference"
    assert isinstance(ReconciliationConflictError(), RuntimeError)


def test_reconcile_module_boundaries() -> None:
    import ast
    from pathlib import Path

    source = Path("app/external_action_reconcile.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "record_reconciliation" in defined
    for name in defined:
        lowered = name.lower()
        assert not any(
            stem in lowered
            for stem in (
                "execute", "retry", "send", "perform", "enqueue", "claim",
                "transition", "worker", "schedul", "publish", "dispatch",
                "requeue", "invoke", "trigger",
            )
        ), name
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in {
                "urllib", "requests", "httpx", "socket", "http",
                "smtplib", "subprocess", "threading", "asyncio",
            }, node.module
            if node.module == "app.external_actions_db":
                assert {
                    alias.name for alias in node.names
                } <= {"ActionNotFoundError", "StaleAttemptError"}, node.names
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {
                    "threading", "subprocess", "socket",
                }, alias.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            first = node.value.lstrip().upper().split(None, 1)
            assert not first or first[0].rstrip(";") not in {
                "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE",
            }, first


def test_record_signature_accepts_only_reconciliation_inputs() -> None:
    import inspect
    from pathlib import Path

    params = set(inspect.signature(record_reconciliation).parameters)
    assert params == {
        "action_id", "attempt_id", "proposed_outcome", "evidence",
        "reconciled_by", "reconciliation_reference",
        "approval_signature", "approval_issued_at", "approval_expires_at",
        "config",
    }
    # No executor/orchestrator credential can substitute for human approval:
    # there is nowhere to put one.
    assert not (params & {
        "executor_id", "executor_proof", "orchestrator_id",
        "orchestrator_proof", "authorized_by", "authorization_signature",
    })


def test_record_has_no_public_route() -> None:
    from pathlib import Path

    for route_file in Path("app/routes").glob("*.py"):
        text = route_file.read_text(encoding="utf-8")
        assert "record_reconciliation" not in text, route_file
        assert "external_action_reconcile" not in text, route_file


# --- record_reconciliation: fake PostgreSQL backend ---


def _utcnow():
    return datetime.now(timezone.utc)


class _ReconTables:
    def __init__(self):
        self.by_id: dict = {}
        self.attempts: dict = {}
        self.decisions: dict = {}
        self.decisions_by_attempt: dict = {}
        self.lock = threading.Lock()
        self.row_locks: dict = {}
        self.row_locks_guard = threading.Lock()

    def row_lock(self, kind, key):
        with self.row_locks_guard:
            return self.row_locks.setdefault((kind, key), threading.Lock())


class _ReconConn:
    def __init__(self, tables):
        self.tables = tables
        self.statements: list = []
        self.audits: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.decision_fails = None
        self.audit_fails = None
        self.update_fails = None
        self.commit_fails = False
        self._held: list = []

    def cursor(self):
        return _ReconCursor(self)

    def _release_rows(self):
        held, self._held = self._held, []
        for lock in held:
            try:
                lock.release()
            except RuntimeError:
                pass

    def commit(self):
        if self.commit_fails:
            raise DatabaseUnavailableError("commit_failed")
        self.commits += 1
        self._release_rows()

    def rollback(self):
        self.rollbacks += 1
        self._release_rows()

    def close(self):
        self.closes += 1
        self._release_rows()


class _ReconCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _take_row_lock(self, kind, key):
        # Mimic SELECT ... FOR UPDATE: block until the holder commits.
        lock = self._conn.tables.row_lock(kind, key)
        if not lock.acquire(timeout=15):
            raise AssertionError(f"fake row lock stuck: {kind} {key}")
        self._conn._held.append(lock)

    def execute(self, sql, params=None):
        params = dict(params or {})
        self._conn.statements.append((sql, params))
        tables = self._conn.tables
        if sql.startswith("SELECT * FROM external_actions"):
            self._take_row_lock("action", params["action_id"])
            row = tables.by_id.get(params["action_id"])
            self._rows = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM external_action_attempts"):
            self._take_row_lock("attempt", params["attempt_id"])
            row = tables.attempts.get(params["attempt_id"])
            self._rows = [dict(row)] if row else []
        elif sql.startswith("INSERT INTO reconciliation_decisions"):
            if self._conn.decision_fails is not None:
                raise self._conn.decision_fails
            with tables.lock:
                if params["attempt_id"] in tables.decisions_by_attempt:
                    self._rows = []  # ON CONFLICT DO NOTHING
                else:
                    row = dict(params)
                    row["decided_at"] = _utcnow()
                    tables.decisions[params["decision_id"]] = row
                    tables.decisions_by_attempt[params["attempt_id"]] = row
                    self._rows = [dict(row)]
        elif sql.startswith("SELECT * FROM reconciliation_decisions"):
            row = tables.decisions_by_attempt.get(params["attempt_id"])
            self._rows = [dict(row)] if row else []
        elif sql.startswith("UPDATE external_actions"):
            if self._conn.update_fails is not None:
                raise self._conn.update_fails
            row = tables.by_id[params["action_id"]]
            row["state"] = params["state"]
            row["updated_at"] = _utcnow()
            self._rows = [dict(row)]
        elif sql.startswith("INSERT INTO agent_events"):
            if self._conn.audit_fails is not None:
                raise self._conn.audit_fails
            self._conn.audits.append(dict(params))
            self._rows = []
        else:  # pragma: no cover - record issues only these statements
            raise AssertionError(f"unexpected SQL: {sql[:70]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _action_row(aid, state="UNKNOWN", count=1, action_type="outreach_email"):
    return {
        "action_id": aid,
        "idempotency_key": f"key-{aid.hex[:12]}",
        "action_type": action_type,
        "agent": "michael",
        "property_id": None,
        "request_fingerprint": "sha256:test-fixture",
        "authorized_by": "operator-alice",
        "authorization_reference": "ticket-1",
        "authorized_at": _T0,
        "state": state,
        "attempt_count": count,
        "created_at": _T0,
        "updated_at": _T0,
    }


def _attempt_row(tid, aid, number=1, outcome="UNKNOWN", executor="w-1"):
    return {
        "attempt_id": tid,
        "action_id": aid,
        "attempt_number": number,
        "executor_id": executor,
        "claimed_at": _T0 - timedelta(hours=1),
        "outcome": outcome,
        "outcome_recorded_at": _T0,
        "created_at": _T0 - timedelta(hours=1),
        "updated_at": _T0,
    }


def _seed_unknown():
    tables = _ReconTables()
    aid, tid = uuid4(), uuid4()
    tables.by_id[aid] = _action_row(aid)
    tables.attempts[tid] = _attempt_row(tid, aid)
    return tables, aid, tid


def _factory(monkeypatch, tables):
    made: list = []
    conns: list = []

    def _make(config=None):
        made.append(config)
        conn = _ReconConn(tables)
        conns.append(conn)
        return conn

    import app.external_action_reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "get_connection", _make)
    return made, conns


_RECON_CREDS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _enrolled_reconciler(monkeypatch):
    """Enroll one test reconciler for record-path tests (pure tests unaffected)."""
    _RECON_CREDS.clear()
    priv, pub = _keypair()
    _RECON_CREDS.update(name="operator-alice", priv=priv, pub=pub)
    monkeypatch.setenv("RECONCILER_PUBLIC_KEYS", f"operator-alice:{pub}")
    yield
    _RECON_CREDS.clear()


def _reconcile_args(aid, tid, **overrides):
    """Fresh valid record submission, newly signed; model evidence accepted."""
    moment = _utcnow()
    issued = overrides.pop("issued_at", moment - timedelta(minutes=5))
    expires = overrides.pop("expires_at", moment + timedelta(minutes=55))
    raw_evidence = overrides.pop("evidence", None)
    if isinstance(raw_evidence, ReconciliationEvidence):
        evidence = raw_evidence.model_dump(mode="json")
    elif isinstance(raw_evidence, dict):
        evidence = dict(raw_evidence)
    elif raw_evidence is None:
        evidence = _evidence_dict(
            observed_at=(moment - timedelta(hours=1)).isoformat()
        )
    else:
        evidence = raw_evidence  # invalid shape passes through to record
    signable = (
        evidence
        if isinstance(evidence, dict)
        else _evidence_dict(
            observed_at=(moment - timedelta(hours=1)).isoformat()
        )
    )
    digest = canonical_evidence_digest(signable)
    outcome = overrides.pop("proposed_outcome", "SUCCEEDED")
    name = overrides.pop("reconciled_by", "operator-alice")
    reference = overrides.pop("reconciliation_reference", "recon-1")
    signature = overrides.pop(
        "approval_signature",
        sign_reconciliation_approval(
            private_key_hex=overrides.pop("private_key_hex", _RECON_CREDS["priv"]),
            action_id=aid,
            attempt_id=tid,
            proposed_outcome=outcome,
            evidence_digest=digest,
            reconciled_by=name,
            reconciliation_reference=reference,
            issued_at=issued,
            expires_at=expires,
        ),
    )
    args = {
        "action_id": aid,
        "attempt_id": tid,
        "proposed_outcome": outcome,
        "evidence": evidence,
        "reconciled_by": name,
        "reconciliation_reference": reference,
        "approval_signature": signature,
        "approval_issued_at": issued,
        "approval_expires_at": expires,
    }
    args.update(overrides)
    return args


def _audit_blob(conn):
    assert len(conn.audits) == 1
    return json.dumps(conn.audits[0], default=str)


# --- A. success paths: one connection, one commit, atomic triple ---


def test_record_success_unknown_to_succeeded(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    result = record_reconciliation(**_reconcile_args(aid, tid))
    assert result.created is True
    assert result.decision["action_id"] == aid
    assert result.decision["attempt_id"] == tid
    assert result.decision["outcome"] == "SUCCEEDED"
    assert result.decision["prior_outcome"] == "UNKNOWN"
    assert result.decision["reconciled_by"] == "operator-alice"
    assert result.decision["reconciliation_reference"] == "recon-1"
    assert result.decision["verification_tier"] == "human_reviewed"
    assert result.decision["evidence_digest"].startswith("sha256:")
    assert result.decision["decision_id"] != tid  # stable but distinct id
    assert result.action["state"] == "SUCCEEDED"
    assert result.attempt["outcome"] == "UNKNOWN"  # history preserved
    assert tables.by_id[aid]["state"] == "SUCCEEDED"
    assert tables.attempts[tid]["outcome"] == "UNKNOWN"
    assert made == [None]  # one connection, default config
    assert len(conns) == 1
    conn = conns[0]
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.closes == 1
    kinds = [sql.split("(")[0] for sql, _ in conn.statements]
    assert any("FOR UPDATE" in sql for sql, _ in conn.statements)
    assert sum(k.startswith("INSERT INTO reconciliation_decisions") for k in kinds) == 1
    assert sum(k.startswith("UPDATE external_actions") for k in kinds) == 1
    for sql, _ in conn.statements:  # parameterized: values never in SQL text
        assert str(aid) not in sql
        assert "operator-alice" not in sql
    blob = _audit_blob(conn)
    assert "external_action.reconciled" in blob
    assert "human_reviewed" in blob
    assert "UNKNOWN -> SUCCEEDED" in blob
    assert _RECON_CREDS["priv"] not in blob
    assert "approval_signature" not in blob
    assert "Provider confirmed completion" not in blob  # no evidence bodies


def test_record_success_unknown_to_failed(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    evidence = _evidence_dict(
        observed_at=(_utcnow() - timedelta(hours=1)).isoformat(),
        asserts_outcome="FAILED",
        summary="Provider rejected the request; see ticket.",
    )
    result = record_reconciliation(
        **_reconcile_args(aid, tid, proposed_outcome="FAILED", evidence=evidence)
    )
    assert result.created is True
    assert result.decision["outcome"] == "FAILED"
    assert tables.by_id[aid]["state"] == "FAILED"
    assert tables.attempts[tid]["outcome"] == "UNKNOWN"  # history preserved
    assert conns[0].commits == 1


def test_record_preserves_attempt_row_byte_for_byte(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    before = dict(tables.attempts[tid])
    result = record_reconciliation(**_reconcile_args(aid, tid))
    assert result.created is True
    assert tables.attempts[tid] == before  # no column touched
    assert result.attempt == before
    writes = [
        sql for sql, _ in conns[0].statements
        if sql.startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert not any("external_action_attempts" in sql for sql in writes)


def test_record_accepts_model_evidence_and_string_uuids(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid)
    args["evidence"] = ReconciliationEvidence(**args["evidence"])
    args["action_id"] = str(aid)
    args["attempt_id"] = str(tid)
    result = record_reconciliation(**args)
    assert result.created is True
    assert tables.by_id[aid]["state"] == "SUCCEEDED"


# --- B. pre-connection gates: auth and evidence before any connection ---


def _assert_no_connection(made, conns, tables):
    assert made == []
    assert conns == []
    assert tables.decisions == {}
    assert tables.decisions_by_attempt == {}


def test_record_rejects_bad_signature_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid)
    flipped = "0" if args["approval_signature"][-1] != "0" else "1"
    args["approval_signature"] = args["approval_signature"][:-1] + flipped
    with pytest.raises(AuthenticationFailedError):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_unenrolled_reconciler_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid)
    _, other_pub = _keypair()
    monkeypatch.setenv("RECONCILER_PUBLIC_KEYS", f"operator-bob:{other_pub}")
    with pytest.raises(AuthenticationFailedError):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_expired_approval_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    moment = _utcnow()
    args = _reconcile_args(
        aid, tid,
        issued_at=moment - timedelta(hours=2),
        expires_at=moment - timedelta(hours=1),
        evidence=_evidence_dict(
            observed_at=(moment - timedelta(hours=3)).isoformat()
        ),
    )
    with pytest.raises(AuthenticationFailedError):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_future_issued_approval_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    moment = _utcnow()
    args = _reconcile_args(
        aid, tid,
        issued_at=moment + timedelta(minutes=10),
        expires_at=moment + timedelta(minutes=70),
    )
    with pytest.raises(AuthenticationFailedError):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_wrong_domain_signature_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid)
    args["approval_signature"] = sign_approval(
        private_key_hex=_RECON_CREDS["priv"],
        idempotency_key="key-1",
        request_fingerprint="sha256:abc",
        authorized_by="operator-alice",
        authorization_reference="ticket-1",
        issued_at=_T0,
        expires_at=_T0 + timedelta(hours=1),
    )
    with pytest.raises(AuthenticationFailedError):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_tampered_evidence_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid)
    args["evidence"]["artifact_ref"] = "receipt-forged"
    with pytest.raises(AuthenticationFailedError, match="signature mismatch"):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_outcome_mismatch_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid, proposed_outcome="FAILED")  # asserts SUCCEEDED
    with pytest.raises(InsufficientEvidenceError, match="does not support"):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_future_observation_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(
        aid, tid,
        evidence=_evidence_dict(
            observed_at=(_utcnow() + timedelta(hours=1)).isoformat()
        ),
    )
    with pytest.raises(InsufficientEvidenceError, match="future"):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_observation_after_approval_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    moment = _utcnow()
    args = _reconcile_args(
        aid, tid,
        issued_at=moment - timedelta(minutes=30),
        evidence=_evidence_dict(
            observed_at=(moment - timedelta(minutes=5)).isoformat()
        ),
    )
    with pytest.raises(InsufficientEvidenceError, match="after approval"):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_bad_evidence_shape_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    evidence = _evidence_dict(
        observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
    )
    del evidence["artifact_ref"]
    args = _reconcile_args(aid, tid, evidence=evidence)
    with pytest.raises(ValidationError):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_system_verified_claim_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(
        aid, tid,
        evidence=_evidence_dict(
            observed_at=(_utcnow() - timedelta(hours=1)).isoformat(),
            verification_tier="system_verified",
        ),
    )
    with pytest.raises(ValidationError):
        record_reconciliation(**args)
    _assert_no_connection(made, conns, tables)


def test_record_rejects_bad_identifiers_before_connect(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(ValueError, match="invalid action_id"):
        record_reconciliation(**_reconcile_args("not-a-uuid", tid))
    with pytest.raises(TypeError, match="evidence must be"):
        record_reconciliation(**_reconcile_args(aid, tid, evidence=42))
    _assert_no_connection(made, conns, tables)


# --- C. in-transaction guards: lock, validate, roll back clean ---


def _assert_no_decision_write(conns, tables):
    """Validation precedes insertion: no decision INSERT was even issued."""
    assert tables.decisions == {}
    assert tables.decisions_by_attempt == {}
    for conn in conns:
        assert not any(
            sql.startswith("INSERT INTO reconciliation_decisions")
            for sql, _ in conn.statements
        ), [sql for sql, _ in conn.statements]


def test_record_unknown_action_rolls_back(monkeypatch):
    tables = _ReconTables()
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(ActionNotFoundError):
        record_reconciliation(**_reconcile_args(uuid4(), uuid4()))
    assert made == [None]
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].closes == 1
    assert conns[0].audits == []
    _assert_no_decision_write(conns, tables)


def test_record_unknown_attempt_rolls_back(monkeypatch):
    tables, aid, _ = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(StaleAttemptError, match="unknown attempt_id"):
        record_reconciliation(**_reconcile_args(aid, uuid4()))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].audits == []
    _assert_no_decision_write(conns, tables)


def test_record_foreign_attempt_rolls_back(monkeypatch):
    tables, aid, tid = _seed_unknown()
    other = uuid4()
    tables.by_id[other] = _action_row(other)
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(StaleAttemptError, match="different action"):
        record_reconciliation(**_reconcile_args(other, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert tables.by_id[other]["state"] == "UNKNOWN"
    _assert_no_decision_write(conns, tables)


def test_record_superseded_attempt_rolls_back(monkeypatch):
    tables, aid, tid = _seed_unknown()
    tables.by_id[aid]["attempt_count"] = 2  # attempt 1 is no longer latest
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(StaleAttemptError, match="superseded"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].audits == []
    assert tables.by_id[aid]["state"] == "UNKNOWN"
    _assert_no_decision_write(conns, tables)


def test_record_non_unknown_attempt_outcome_rolls_back(monkeypatch):
    tables, aid, tid = _seed_unknown()
    tables.attempts[tid]["outcome"] = "SUCCEEDED"
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(StaleAttemptError, match="not UNKNOWN"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].audits == []
    assert tables.by_id[aid]["state"] == "UNKNOWN"
    _assert_no_decision_write(conns, tables)


@pytest.mark.parametrize("state", ["ATTEMPTING", "PENDING", "SUCCEEDED", "FAILED"])
def test_record_non_unknown_action_state_rolls_back(monkeypatch, state):
    tables, aid, tid = _seed_unknown()
    tables.by_id[aid]["state"] = state
    if state in ("SUCCEEDED", "FAILED"):
        tables.attempts[tid]["outcome"] = state
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(ValueError, match="not reconcilable"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].audits == []
    assert tables.by_id[aid]["state"] == state  # untouched
    _assert_no_decision_write(conns, tables)


# --- D. replay and conflicts ---


def test_record_identical_replay_returns_winner_writes_nothing(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    evidence = _evidence_dict(
        observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
    )
    first = record_reconciliation(**_reconcile_args(aid, tid, evidence=evidence))
    renewed = _reconcile_args(aid, tid, evidence=evidence)  # renewed signature
    assert renewed["approval_issued_at"] >= first.decision["approval_issued_at"]
    replay = record_reconciliation(**renewed)
    assert replay.created is False
    assert replay.decision == first.decision  # original signature kept
    assert replay.action["state"] == "SUCCEEDED"
    assert replay.attempt["outcome"] == "UNKNOWN"  # history intact
    assert len(conns) == 2
    assert conns[1].commits == 0
    assert conns[1].rollbacks == 1  # read-only transaction released
    assert conns[1].closes == 1
    assert conns[1].audits == []
    assert len(tables.decisions) == 1
    assert sum(len(c.audits) for c in conns) == 1  # no duplicate audit


def test_record_replay_still_requires_valid_approval(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    record_reconciliation(**_reconcile_args(aid, tid))
    replay = _reconcile_args(aid, tid)
    flipped = "0" if replay["approval_signature"][-1] != "0" else "1"
    replay["approval_signature"] = replay["approval_signature"][:-1] + flipped
    with pytest.raises(AuthenticationFailedError):
        record_reconciliation(**replay)
    assert len(conns) == 1  # rejected before any new connection


def test_record_conflicting_principal_fails_closed(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    evidence = _evidence_dict(
        observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
    )
    record_reconciliation(**_reconcile_args(aid, tid, evidence=evidence))
    bob_priv, bob_pub = _keypair()
    monkeypatch.setenv(
        "RECONCILER_PUBLIC_KEYS",
        f"operator-alice:{_RECON_CREDS['pub']},operator-bob:{bob_pub}",
    )
    rival = _reconcile_args(
        aid, tid, evidence=evidence,
        reconciled_by="operator-bob", private_key_hex=bob_priv,
    )
    with pytest.raises(ReconciliationConflictError):
        record_reconciliation(**rival)
    assert len(tables.decisions) == 1
    assert sum(len(c.audits) for c in conns) == 1
    assert tables.by_id[aid]["state"] == "SUCCEEDED"


def test_record_conflicting_reference_fails_closed(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    evidence = _evidence_dict(
        observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
    )
    record_reconciliation(**_reconcile_args(aid, tid, evidence=evidence))
    rival = _reconcile_args(
        aid, tid, evidence=evidence, reconciliation_reference="recon-2"
    )
    with pytest.raises(ReconciliationConflictError):
        record_reconciliation(**rival)
    assert len(tables.decisions) == 1
    assert sum(len(c.audits) for c in conns) == 1


def test_record_conflicting_evidence_fails_closed(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    record_reconciliation(**_reconcile_args(aid, tid))
    rival = _reconcile_args(
        aid, tid,
        evidence=_evidence_dict(
            observed_at=(_utcnow() - timedelta(hours=2)).isoformat(),
            artifact_ref="receipt-0088",
        ),
    )
    with pytest.raises(ReconciliationConflictError):
        record_reconciliation(**rival)
    assert len(tables.decisions) == 1
    assert sum(len(c.audits) for c in conns) == 1


def test_record_conflicting_outcome_fails_closed(monkeypatch):
    # Outcome is digest-bound, so a rival outcome necessarily carries
    # different evidence too; either mismatch must fail closed.
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    record_reconciliation(**_reconcile_args(aid, tid))
    rival = _reconcile_args(
        aid, tid,
        proposed_outcome="FAILED",
        evidence=_evidence_dict(
            observed_at=(_utcnow() - timedelta(hours=1)).isoformat(),
            asserts_outcome="FAILED",
        ),
    )
    with pytest.raises(ReconciliationConflictError):
        record_reconciliation(**rival)
    assert len(tables.decisions) == 1
    assert tables.by_id[aid]["state"] == "SUCCEEDED"


def test_record_decision_without_matching_state_fails_closed(monkeypatch):
    tables, aid, tid = _seed_unknown()
    args = _reconcile_args(aid, tid)
    digest = canonical_evidence_digest(args["evidence"])
    tables.decisions_by_attempt[tid] = {
        "decision_id": uuid4(),
        "action_id": aid,
        "attempt_id": tid,
        "outcome": "SUCCEEDED",
        "evidence_digest": digest,
        "reconciled_by": "operator-alice",
        "reconciliation_reference": "recon-1",
    }
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(DatabaseUnavailableError, match="state_mismatch"):
        record_reconciliation(**args)
    assert conns[0].commits == 0
    assert conns[0].audits == []


def test_record_missing_winner_after_conflict_fails_closed(monkeypatch):
    tables, aid, tid = _seed_unknown()
    tables.decisions_by_attempt[tid] = {"outcome": "SUCCEEDED"}  # trips DO NOTHING
    made, conns = _factory(monkeypatch, tables)
    real_select = _ReconCursor.execute

    def _hide_winner(self, sql, params=None):
        real_select(self, sql, params)
        if sql.startswith("SELECT * FROM reconciliation_decisions"):
            self._rows = []

    monkeypatch.setattr(_ReconCursor, "execute", _hide_winner)
    with pytest.raises(DatabaseUnavailableError, match="reconciliation_race"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1


def test_record_replay_with_drifted_attempt_outcome_fails_closed(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    evidence = _evidence_dict(
        observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
    )
    record_reconciliation(**_reconcile_args(aid, tid, evidence=evidence))
    tables.attempts[tid]["outcome"] = "SUCCEEDED"  # history rewrite, not ours
    with pytest.raises(DatabaseUnavailableError, match="state_mismatch"):
        record_reconciliation(**_reconcile_args(aid, tid, evidence=evidence))
    assert len(tables.decisions) == 1
    assert sum(len(c.audits) for c in conns) == 1


# --- E. failure atomicity: any break rolls decision + action + audit back ---
#
# The fake applies row writes immediately, so rollback here is
# asserted by counters (no commit, one rollback, connection
# closed, zero audits). True transactional undo is proven by the
# live PostgreSQL tests below. (The attempt row is never written,
# so it needs no rollback.)


def test_record_decision_insert_failure_rolls_back(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid)

    import app.external_action_reconcile as reconcile_module

    def _failing_factory(config=None):
        made.append(config)
        conn = _ReconConn(tables)
        conn.decision_fails = DatabaseUnavailableError("pg_down")
        conns.append(conn)
        return conn

    monkeypatch.setattr(reconcile_module, "get_connection", _failing_factory)
    with pytest.raises(DatabaseUnavailableError, match="pg_down"):
        record_reconciliation(**args)
    assert len(tables.decisions) == 0
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].closes == 1
    assert conns[0].audits == []
    assert tables.by_id[aid]["state"] == "UNKNOWN"


def test_record_audit_insert_failure_rolls_back(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)

    import app.external_action_reconcile as reconcile_module

    def _failing_factory(config=None):
        made.append(config)
        conn = _ReconConn(tables)
        conn.audit_fails = DatabaseUnavailableError("audit_down")
        conns.append(conn)
        return conn

    monkeypatch.setattr(reconcile_module, "get_connection", _failing_factory)
    with pytest.raises(DatabaseUnavailableError, match="audit_down"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].closes == 1
    assert conns[0].audits == []  # no partial audit survives


def test_record_action_update_failure_rolls_back(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)

    import app.external_action_reconcile as reconcile_module

    def _failing_factory(config=None):
        made.append(config)
        conn = _ReconConn(tables)
        conn.update_fails = DatabaseUnavailableError("update_down")
        conns.append(conn)
        return conn

    monkeypatch.setattr(reconcile_module, "get_connection", _failing_factory)
    with pytest.raises(DatabaseUnavailableError, match="update_down"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].audits == []


def test_record_commit_failure_does_not_report_success(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)

    import app.external_action_reconcile as reconcile_module

    def _failing_factory(config=None):
        made.append(config)
        conn = _ReconConn(tables)
        conn.commit_fails = True
        conns.append(conn)
        return conn

    monkeypatch.setattr(reconcile_module, "get_connection", _failing_factory)
    with pytest.raises(DatabaseUnavailableError, match="commit_failed"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].closes == 1


def test_record_unexpected_failure_wrapped_without_leak(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)

    import app.external_action_reconcile as reconcile_module

    def _failing_factory(config=None):
        made.append(config)
        conn = _ReconConn(tables)
        conn.decision_fails = RuntimeError("boom-internal-detail")
        conns.append(conn)
        return conn

    monkeypatch.setattr(reconcile_module, "get_connection", _failing_factory)
    with pytest.raises(DatabaseUnavailableError, match="reconciliation_failed") as exc:
        record_reconciliation(**_reconcile_args(aid, tid))
    assert "boom-internal-detail" not in str(exc.value)
    assert isinstance(exc.value.__cause__, RuntimeError)  # context preserved
    assert conns[0].rollbacks == 1
    assert conns[0].closes == 1


def test_record_connection_failure_runs_nothing(monkeypatch):
    tables, aid, tid = _seed_unknown()

    import app.external_action_reconcile as reconcile_module

    def _down(config=None):
        raise DatabaseUnavailableError("pg_unreachable")

    monkeypatch.setattr(reconcile_module, "get_connection", _down)
    with pytest.raises(DatabaseUnavailableError, match="pg_unreachable"):
        record_reconciliation(**_reconcile_args(aid, tid))
    assert tables.by_id[aid]["state"] == "UNKNOWN"
    assert tables.decisions == {}


# --- F. concurrency: one winner, total quiescence ---


def _run_threads(count, func):
    barrier = threading.Barrier(count)
    outcomes: list = [None] * count

    def _entry(index):
        barrier.wait(timeout=10)
        try:
            outcomes[index] = ("ok", func(index))
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            outcomes[index] = ("error", exc)

    workers = [
        threading.Thread(target=_entry, args=(index,)) for index in range(count)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert not worker.is_alive()
    return outcomes


def test_record_concurrent_identical_single_winner(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    args = _reconcile_args(aid, tid)  # one approval shared by both racers
    outcomes = _run_threads(
        2, lambda _index: record_reconciliation(**dict(args))
    )
    assert [kind for kind, _ in outcomes] == ["ok", "ok"]
    created = sorted(result.created for _, result in outcomes)
    assert created == [False, True]  # exactly one winner
    first, second = (result for _, result in outcomes)
    assert first.decision["decision_id"] == second.decision["decision_id"]
    assert len(tables.decisions) == 1
    assert tables.by_id[aid]["state"] == "SUCCEEDED"
    assert tables.attempts[tid]["outcome"] == "UNKNOWN"  # history preserved
    assert sum(c.commits for c in conns) == 1
    assert sum(len(c.audits) for c in conns) == 1
    assert all(c.closes == 1 for c in conns)


def test_record_concurrent_conflicting_one_decision(monkeypatch):
    tables, aid, tid = _seed_unknown()
    made, conns = _factory(monkeypatch, tables)
    bob_priv, bob_pub = _keypair()
    monkeypatch.setenv(
        "RECONCILER_PUBLIC_KEYS",
        f"operator-alice:{_RECON_CREDS['pub']},operator-bob:{bob_pub}",
    )
    alice_args = _reconcile_args(aid, tid)
    bob_args = _reconcile_args(
        aid, tid, reconciled_by="operator-bob", private_key_hex=bob_priv
    )
    submissions = (alice_args, bob_args)
    first, second = _run_threads(
        2, lambda index: record_reconciliation(**dict(submissions[index]))
    )
    kinds = sorted(kind for kind, _ in (first, second))
    assert kinds == ["error", "ok"]
    error = first[1] if first[0] == "error" else second[1]
    assert isinstance(error, ReconciliationConflictError)
    assert len(tables.decisions) == 1
    assert sum(len(c.audits) for c in conns) == 1


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1, migrations 005-007) ---
#
# Seeds run through the real ledger (authorize -> enqueue -> claim ->
# UNKNOWN), so every record call below exercises genuine row locks,
# constraints, and commit/rollback. Each test removes its own rows.


_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(
    not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set"
)

_LIVE_EXECUTORS = {"w-1": "test-only-executor-secret-rc-w-1-aaaa"}
_LIVE_ORCHESTRATORS = {"sched-1": "test-only-orchestrator-secret-rc-s1"}


def _live_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _live_identities(monkeypatch, authorizer_pub):
    """Enroll the ledger authorizer plus test-only service credentials."""
    monkeypatch.setenv(
        "AUTHORIZER_PUBLIC_KEYS", f"operator-alice:{authorizer_pub}"
    )
    monkeypatch.setenv(
        "EXECUTOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _LIVE_EXECUTORS.items()),
    )
    monkeypatch.setenv(
        "ORCHESTRATOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _LIVE_ORCHESTRATORS.items()),
    )
    monkeypatch.setenv("EXECUTOR_SCOPES", "w-1:outreach_email")
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "sched-1:outreach_email")


def _live_auth(key, authorizer_priv):
    import app.external_actions_db as ledger
    from app.observability import AgentId

    issued = _utcnow()
    expires = issued + timedelta(hours=1)
    canonical = canonical_identity_name("operator-alice")
    fingerprint = ledger.canonical_request_fingerprint(
        "outreach_email",
        AgentId.MICHAEL,
        None,
        {"template": "owner_intro"},
        canonical,
        "ticket-123",
    )
    return {
        "idempotency_key": key,
        "action_type": "outreach_email",
        "agent": AgentId.MICHAEL,
        "request": {"template": "owner_intro"},
        "authorized_by": "operator-alice",
        "authorization_reference": "ticket-123",
        "authorization_signature": sign_approval(
            private_key_hex=authorizer_priv,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            authorized_by=canonical,
            authorization_reference="ticket-123",
            issued_at=issued,
            expires_at=expires,
        ),
        "authorization_issued_at": issued,
        "authorization_expires_at": expires,
    }


def _live_seed_unknown(key, authorizer_priv):
    """Authorize, enqueue, claim, and mark UNKNOWN on live PG."""
    import app.external_actions_db as ledger

    created = ledger.record_authorization(**_live_auth(key, authorizer_priv))
    aid = created.action["action_id"]
    ledger.transition_action(
        action_id=aid,
        to_state="PENDING",
        orchestrator_id="sched-1",
        orchestrator_proof=_LIVE_ORCHESTRATORS["sched-1"],
    )
    claim = ledger.claim_action(
        action_id=aid,
        executor_id="w-1",
        executor_proof=_LIVE_EXECUTORS["w-1"],
    )
    assert claim.claimed is True
    tid = claim.attempt["attempt_id"]
    ledger.transition_action(
        action_id=aid,
        to_state="UNKNOWN",
        attempt_id=tid,
        executor_id="w-1",
        executor_proof=_LIVE_EXECUTORS["w-1"],
    )
    return aid, tid


def _live_cleanup(action_ids, keys):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reconciliation_decisions"
                " WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
            cur.execute(
                "DELETE FROM external_action_attempts"
                " WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
            cur.execute(
                "DELETE FROM external_actions WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
            cur.execute(
                "DELETE FROM agent_events WHERE correlation_id = ANY(%(ids)s)",
                {"ids": list(keys)},
            )
        conn.commit()


def _live_state(aid, tid, key):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM external_actions WHERE action_id = %(aid)s",
                {"aid": aid},
            )
            state = cur.fetchone()
            cur.execute(
                "SELECT outcome FROM external_action_attempts"
                " WHERE attempt_id = %(tid)s",
                {"tid": tid},
            )
            outcome = cur.fetchone()
            cur.execute(
                "SELECT decision_id, outcome, reconciled_by,"
                " reconciliation_reference, verification_tier, evidence_digest"
                " FROM reconciliation_decisions WHERE attempt_id = %(tid)s",
                {"tid": tid},
            )
            decisions = cur.fetchall()
            cur.execute(
                "SELECT count(*) AS n FROM agent_events"
                " WHERE correlation_id = %(key)s"
                " AND event_type = 'external_action.reconciled'",
                {"key": key},
            )
            audits = cur.fetchone()["n"]
    return state, outcome, decisions, audits


@needs_live_pg
def test_live_record_success_replay_and_row_shapes(monkeypatch):
    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        aid, tid = _live_seed_unknown(key, authorizer_priv)
        evidence = _evidence_dict(
            observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
        )
        result = record_reconciliation(
            **_reconcile_args(aid, tid, evidence=evidence)
        )
        assert result.created is True
        assert result.attempt["outcome"] == "UNKNOWN"  # history preserved
        state, outcome, decisions, audits = _live_state(aid, tid, key)
        assert state["state"] == "SUCCEEDED"
        assert outcome["outcome"] == "UNKNOWN"  # attempt row untouched
        assert len(decisions) == 1
        assert decisions[0]["outcome"] == "SUCCEEDED"
        assert decisions[0]["reconciled_by"] == "operator-alice"
        assert decisions[0]["reconciliation_reference"] == "recon-1"
        assert decisions[0]["verification_tier"] == "human_reviewed"
        assert audits == 1
        replay = record_reconciliation(
            **_reconcile_args(aid, tid, evidence=evidence)  # renewed approval
        )
        assert replay.created is False
        assert replay.decision["decision_id"] == result.decision["decision_id"]
        _, _, decisions_after, audits_after = _live_state(aid, tid, key)
        assert len(decisions_after) == 1
        assert audits_after == 1  # replay writes nothing
    finally:
        _live_cleanup([aid] if aid else [], [key])


@needs_live_pg
def test_live_record_failed(monkeypatch):
    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        aid, tid = _live_seed_unknown(key, authorizer_priv)
        evidence = _evidence_dict(
            observed_at=(_utcnow() - timedelta(hours=1)).isoformat(),
            asserts_outcome="FAILED",
        )
        result = record_reconciliation(
            **_reconcile_args(
                aid, tid, proposed_outcome="FAILED", evidence=evidence
            )
        )
        assert result.created is True
        state, outcome, decisions, audits = _live_state(aid, tid, key)
        assert state["state"] == "FAILED"
        assert outcome["outcome"] == "UNKNOWN"  # attempt row untouched
        assert len(decisions) == 1
        assert audits == 1
    finally:
        _live_cleanup([aid] if aid else [], [key])


@needs_live_pg
def test_live_record_wrong_state_rejected_without_rows(monkeypatch):
    import app.external_actions_db as ledger

    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        created = ledger.record_authorization(
            **_live_auth(key, authorizer_priv)
        )
        aid = created.action["action_id"]
        ledger.transition_action(
            action_id=aid,
            to_state="PENDING",
            orchestrator_id="sched-1",
            orchestrator_proof=_LIVE_ORCHESTRATORS["sched-1"],
        )
        claim = ledger.claim_action(
            action_id=aid,
            executor_id="w-1",
            executor_proof=_LIVE_EXECUTORS["w-1"],
        )
        tid = claim.attempt["attempt_id"]
        with pytest.raises(ValueError, match="not reconcilable"):
            record_reconciliation(**_reconcile_args(aid, tid))
        state, outcome, decisions, audits = _live_state(aid, tid, key)
        assert state["state"] == "ATTEMPTING"  # untouched
        assert outcome["outcome"] is None  # still open, never marked UNKNOWN
        assert decisions == []
        assert audits == 0
    finally:
        _live_cleanup([aid] if aid else [], [key])


@needs_live_pg
def test_live_record_conflict_keeps_single_decision(monkeypatch):
    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    bob_priv, bob_pub = _keypair()
    monkeypatch.setenv(
        "RECONCILER_PUBLIC_KEYS",
        f"operator-alice:{_RECON_CREDS['pub']},operator-bob:{bob_pub}",
    )
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        aid, tid = _live_seed_unknown(key, authorizer_priv)
        evidence = _evidence_dict(
            observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
        )
        record_reconciliation(**_reconcile_args(aid, tid, evidence=evidence))
        rival = _reconcile_args(
            aid, tid, evidence=evidence,
            reconciled_by="operator-bob", private_key_hex=bob_priv,
        )
        with pytest.raises(ReconciliationConflictError):
            record_reconciliation(**rival)
        _, _, decisions, audits = _live_state(aid, tid, key)
        assert len(decisions) == 1
        assert decisions[0]["reconciled_by"] == "operator-alice"
        assert audits == 1
    finally:
        _live_cleanup([aid] if aid else [], [key])


@needs_live_pg
def test_live_record_audit_failure_rolls_back_triple(monkeypatch):
    import app.external_action_reconcile as reconcile_module

    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        aid, tid = _live_seed_unknown(key, authorizer_priv)

        def _audit_down(conn, event):
            raise DatabaseUnavailableError("audit_down")

        monkeypatch.setattr(
            reconcile_module, "insert_event_in_transaction", _audit_down
        )
        with pytest.raises(DatabaseUnavailableError, match="audit_down"):
            record_reconciliation(**_reconcile_args(aid, tid))
        state, outcome, decisions, audits = _live_state(aid, tid, key)
        assert state["state"] == "UNKNOWN"  # transition rolled back
        assert outcome["outcome"] == "UNKNOWN"
        assert decisions == []  # decision rolled back
        assert audits == 0
    finally:
        _live_cleanup([aid] if aid else [], [key])


@needs_live_pg
def test_live_record_concurrent_identical_single_winner(monkeypatch):
    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        aid, tid = _live_seed_unknown(key, authorizer_priv)
        args = _reconcile_args(aid, tid)
        outcomes = _run_threads(
            2, lambda _index: record_reconciliation(**dict(args))
        )
        assert [kind for kind, _ in outcomes] == ["ok", "ok"]
        assert sorted(r.created for _, r in outcomes) == [False, True]
        _, _, decisions, audits = _live_state(aid, tid, key)
        assert len(decisions) == 1
        assert audits == 1
    finally:
        _live_cleanup([aid] if aid else [], [key])


@needs_live_pg
def test_live_record_concurrent_conflicting_one_decision(monkeypatch):
    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    bob_priv, bob_pub = _keypair()
    monkeypatch.setenv(
        "RECONCILER_PUBLIC_KEYS",
        f"operator-alice:{_RECON_CREDS['pub']},operator-bob:{bob_pub}",
    )
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        aid, tid = _live_seed_unknown(key, authorizer_priv)
        evidence = _evidence_dict(
            observed_at=(_utcnow() - timedelta(hours=1)).isoformat()
        )
        submissions = (
            _reconcile_args(aid, tid, evidence=evidence),
            _reconcile_args(
                aid, tid, evidence=evidence,
                reconciled_by="operator-bob", private_key_hex=bob_priv,
            ),
        )
        first, second = _run_threads(
            2, lambda index: record_reconciliation(**dict(submissions[index]))
        )
        assert sorted(kind for kind, _ in (first, second)) == ["error", "ok"]
        error = first[1] if first[0] == "error" else second[1]
        assert isinstance(error, ReconciliationConflictError)
        _, _, decisions, audits = _live_state(aid, tid, key)
        assert len(decisions) == 1
        assert audits == 1
    finally:
        _live_cleanup([aid] if aid else [], [key])


@needs_live_pg
def test_live_record_bad_approval_writes_nothing(monkeypatch):
    _live_config()
    authorizer_priv, authorizer_pub = _keypair()
    _live_identities(monkeypatch, authorizer_pub)
    key = f"live-3b3b2-{uuid4().hex}"
    aid = tid = None
    try:
        aid, tid = _live_seed_unknown(key, authorizer_priv)
        args = _reconcile_args(aid, tid)
        flipped = "0" if args["approval_signature"][-1] != "0" else "1"
        args["approval_signature"] = args["approval_signature"][:-1] + flipped
        with pytest.raises(AuthenticationFailedError):
            record_reconciliation(**args)
        state, outcome, decisions, audits = _live_state(aid, tid, key)
        assert state["state"] == "UNKNOWN"
        assert outcome["outcome"] == "UNKNOWN"
        assert decisions == []
        assert audits == 0
    finally:
        _live_cleanup([aid] if aid else [], [key])
