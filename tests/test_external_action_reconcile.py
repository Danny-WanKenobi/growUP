"""Reconciliation schema, identity, and evidence tests (Phase 3C-3B-3B-1).

Pure validation only: canonical digests, purpose-bound Ed25519
approvals under a separate domain and enrollment, and the
immutable evidence contract. No database access, no state
transitions, no record function exists yet in this stage (pinned
by a structural test). Migration 007 ships as a file here; its
live application is proven by the stage-2 suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.external_action_reconcile import (
    InsufficientEvidenceError,
    ReconciliationConflictError,
    ReconciliationEvidence,
    ensure_observation_not_future,
    ensure_observation_predates_approval,
    ensure_outcome_consistent,
)
from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    ExecutorPrincipal,
    HumanPrincipal,
    OrchestratorPrincipal,
    canonical_evidence_digest,
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


def test_stage1_has_no_database_writes() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_action_reconcile.py").read_text(encoding="utf-8")
    )
    defined = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "record_reconciliation" not in defined  # arrives in 3B-3B-2
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not (modules & {"app.properties_db", "app.observability.store"}), modules
    text = Path("app/external_action_reconcile.py").read_text(encoding="utf-8")
    assert "get_connection" not in text
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            first = node.value.lstrip().upper().split(None, 1)
            assert not first or first[0].rstrip(";") not in {
                "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
                "DROP", "TRUNCATE", "GRANT", "REVOKE",
            }, first
