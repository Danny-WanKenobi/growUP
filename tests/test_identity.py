"""Verified identity tests (Phase 3C-3B-2A).

Unit coverage for app/identity.py parsing, proofs, and policy: no
database, no network. Operator keypairs are runtime-generated (never
committed); executor secrets are fixed test-only strings. Ledger-level
boundary tests live with the ledger suites, which sign through these
same functions.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    ExecutorPrincipal,
    HumanPrincipal,
    OrchestratorPrincipal,
    canonical_identity_name,
    ensure_credential_namespaces_disjoint,
    ensure_may_authorize,
    ensure_may_claim,
    ensure_may_claim_type,
    ensure_may_enqueue,
    ensure_may_enqueue_type,
    ensure_may_inspect_type,
    load_authorizer_keys,
    load_executor_keys,
    load_executor_scopes,
    load_orchestrator_keys,
    load_orchestrator_scopes,
    load_wondrue_reader_id,
    may_authorize,
    may_claim,
    may_claim_type,
    may_enqueue,
    may_enqueue_type,
    may_inspect_systemwide,
    may_inspect_type,
    parse_authorizer_keys,
    parse_executor_keys,
    parse_executor_scopes,
    parse_orchestrator_keys,
    parse_orchestrator_scopes,
    sign_approval,
    verify_executor,
    verify_human_approval,
    verify_orchestrator,
)

_NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
_EXEC_SECRET = "test-only-executor-secret-xyz-0123456789ab"


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw().hex(), private.public_key().public_bytes_raw().hex()


def _approval(priv, name="alice", **overrides):
    """Sign with `name`; returns verify-kwargs with claimed_name."""
    claim = {
        "idempotency_key": "key-1",
        "request_fingerprint": "sha256:abc",
        "authorized_by": name,
        "authorization_reference": "ticket-1",
        "issued_at": _NOW,
        "expires_at": _NOW + timedelta(hours=1),
    }
    claim.update(overrides)
    signature = sign_approval(private_key_hex=priv, **claim)
    return {
        "claimed_name": name,
        "idempotency_key": claim["idempotency_key"],
        "request_fingerprint": claim["request_fingerprint"],
        "authorization_reference": claim["authorization_reference"],
        "issued_at": claim["issued_at"],
        "expires_at": claim["expires_at"],
        "signature_hex": signature,
    }


# --- Canonicalization ---


def test_canonical_identity_name() -> None:
    assert canonical_identity_name("Alice") == "alice"
    assert canonical_identity_name("  W-1  ") == "w-1"
    assert canonical_identity_name("alice") == "alice"  # idempotent
    for bad in (None, 42, "", "   ", "has space", "UPPER" * 20, "x" * 65,
                "-lead", ".lead", "semi;colon", "uni\u00e9"):
        with pytest.raises(ValueError):
            canonical_identity_name(bad)


# --- Key map parsing ---


def test_parse_authorizer_keys() -> None:
    _, pub_a = _keypair()
    _, pub_b = _keypair()
    parsed = parse_authorizer_keys(f"Alice:{pub_a},bob:{pub_b}")
    assert set(parsed) == {"alice", "bob"}
    assert all(len(v) == 32 for v in parsed.values())
    assert parse_authorizer_keys(None) == {}
    assert parse_authorizer_keys("  ") == {}
    for bad in (
        "alice",  # missing colon
        "alice:zz",  # non-hex
        f"alice:{'ab' * 31}",  # short key
        f"alice:{'ab' * 33}",  # long key
        "bad name:x" * 32,  # malformed name with separator
        f"alice:{pub_a},ALICE:{pub_b}",  # canonical duplicate
    ):
        with pytest.raises(AuthenticationFailedError):
            parse_authorizer_keys(bad)
    many = ",".join(f"op-{n}:{'ab' * 32}" for n in range(65))
    with pytest.raises(AuthenticationFailedError):
        parse_authorizer_keys(many)


def test_parse_executor_keys() -> None:
    parsed = parse_executor_keys(f"w-1:{_EXEC_SECRET},W-2:{_EXEC_SECRET}x")
    assert set(parsed) == {"w-1", "w-2"}
    assert parse_executor_keys(None) == {}
    for bad in (
        "w-1",  # missing colon
        "w-1:short",  # weak secret
        f"w-1:{'s' * 1025}",  # oversized secret
        f"w-1:{_EXEC_SECRET},W-1:{_EXEC_SECRET}",  # canonical duplicate
    ):
        with pytest.raises(AuthenticationFailedError):
            parse_executor_keys(bad)


def test_parse_orchestrator_keys() -> None:
    parsed = parse_orchestrator_keys(f"sched-1:{_EXEC_SECRET},Sched-2:{_EXEC_SECRET}x")
    assert set(parsed) == {"sched-1", "sched-2"}
    assert parse_orchestrator_keys(None) == {}
    for bad in (
        "sched-1",  # missing colon
        "sched-1:short",  # weak secret
        f"sched-1:{'s' * 1025}",  # oversized secret
        f"sched-1:{_EXEC_SECRET},SCHED-1:{_EXEC_SECRET}",  # canonical duplicate
    ):
        with pytest.raises(AuthenticationFailedError, match="orchestrator map"):
            parse_orchestrator_keys(bad)


def test_parse_executor_scopes() -> None:
    parsed = parse_executor_scopes(
        "w-1:outreach_email|contract_sign, W-2 : outreach_email ,"
    )
    assert parsed == {
        "w-1": frozenset({"outreach_email", "contract_sign"}),
        "w-2": frozenset({"outreach_email"}),
    }
    assert parse_executor_scopes(None) == {}
    assert parse_executor_scopes("  ") == {}
    for bad in (
        "w-1",  # missing colon
        "bad name:x",  # malformed id
        "w-1:",  # no grants is a config error, not silent deny
        "w-1:  ",  # whitespace-only grants
        "w-1:outreach_email||contract_sign",  # empty element
        "w-1:outreach_email| ",  # trailing empty element
        "w-1:*",  # wildcards rejected
        f"w-1:{'t' * 129}",  # overlong type
        "w-1:a,W-1:b",  # canonical duplicate
    ):
        with pytest.raises(AuthorizationDeniedError, match="scope map"):
            parse_executor_scopes(bad)
    many_ids = ",".join(f"w-{n}:t" for n in range(65))
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        parse_executor_scopes(many_ids)
    many_types = "w-1:" + "|".join(f"t{n}" for n in range(65))
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        parse_executor_scopes(many_types)


def test_parse_orchestrator_scopes() -> None:
    parsed = parse_orchestrator_scopes("sched-1:outreach_email")
    assert parsed == {"sched-1": frozenset({"outreach_email"})}
    assert parse_orchestrator_scopes(None) == {}
    with pytest.raises(AuthorizationDeniedError, match="orchestrator scope map"):
        parse_orchestrator_scopes("sched-1")
    with pytest.raises(AuthorizationDeniedError, match="orchestrator scope map"):
        parse_orchestrator_scopes("sched-1:*")


def test_scope_errors_carry_no_map_content() -> None:
    with pytest.raises(AuthorizationDeniedError) as exc:
        parse_executor_scopes("w-1:*,w-2:secret-type-name")
    text = str(exc.value)
    assert "secret-type-name" not in text and "w-2" not in text


def test_no_key_material_in_parse_errors() -> None:
    _, pub = _keypair()
    try:
        parse_authorizer_keys(f"alice:{pub},alice:{pub}")
    except AuthenticationFailedError as exc:
        assert pub not in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("duplicate must fail")
    with pytest.raises(AuthenticationFailedError) as exc:
        parse_executor_keys("w-1:short")
    assert "short" not in str(exc.value)


def test_loaders_read_environment(monkeypatch) -> None:
    _, pub = _keypair()
    monkeypatch.setenv("AUTHORIZER_PUBLIC_KEYS", f"alice:{pub}")
    monkeypatch.setenv("EXECUTOR_KEYS", f"w-1:{_EXEC_SECRET}")
    monkeypatch.setenv("ORCHESTRATOR_KEYS", f"sched-1:{_EXEC_SECRET}")
    monkeypatch.setenv("EXECUTOR_SCOPES", "w-1:outreach_email")
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "sched-1:outreach_email")
    assert set(load_authorizer_keys()) == {"alice"}
    assert load_executor_keys() == {"w-1": _EXEC_SECRET}
    assert load_orchestrator_keys() == {"sched-1": _EXEC_SECRET}
    assert load_executor_scopes() == {"w-1": frozenset({"outreach_email"})}
    assert load_orchestrator_scopes() == {"sched-1": frozenset({"outreach_email"})}
    monkeypatch.delenv("AUTHORIZER_PUBLIC_KEYS")
    monkeypatch.delenv("EXECUTOR_KEYS")
    monkeypatch.delenv("ORCHESTRATOR_KEYS")
    monkeypatch.delenv("EXECUTOR_SCOPES")
    monkeypatch.delenv("ORCHESTRATOR_SCOPES")
    assert load_authorizer_keys() == {} and load_executor_keys() == {}
    assert load_orchestrator_keys() == {} and load_executor_scopes() == {}
    assert load_orchestrator_scopes() == {}


# --- Human approval signatures ---


def test_sign_verify_roundtrip() -> None:
    priv, pub = _keypair()
    kwargs = _approval(priv)
    kwargs["claimed_name"] = "Alice"  # spelling variants canonicalize
    principal = verify_human_approval(
        authorizers={"alice": bytes.fromhex(pub)}, now=_NOW, **kwargs,
    )
    assert principal == HumanPrincipal(name="alice", method="ed25519-approval")
    with pytest.raises(FrozenInstanceError):
        principal.name = "mallory"  # type: ignore[misc]


def test_verify_rejects_tampered_fields() -> None:
    priv, pub = _keypair()
    kwargs = _approval(priv)
    enrolled = {"alice": bytes.fromhex(pub), "bob": bytes.fromhex(_keypair()[1])}
    for field, tampered in (
        ("idempotency_key", "key-2"),
        ("request_fingerprint", "sha256:other"),
        ("claimed_name", "bob"),
        ("authorization_reference", "ticket-2"),
        ("issued_at", _NOW - timedelta(hours=1)),
        ("expires_at", _NOW + timedelta(hours=2)),
    ):
        with pytest.raises(AuthenticationFailedError):
            verify_human_approval(
                authorizers=enrolled, now=_NOW,
                **{**kwargs, field: tampered},
            )


def test_verify_rejects_wrong_key_unknown_and_malformed() -> None:
    priv_a, pub_a = _keypair()
    priv_b, _ = _keypair()
    forged = _approval(priv_b)  # signed by an unenrolled key
    enrolled = {"alice": bytes.fromhex(pub_a)}
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        verify_human_approval(authorizers=enrolled, now=_NOW, **forged)
    kwargs = _approval(priv_a)
    with pytest.raises(AuthenticationFailedError, match="unknown authorizer"):
        verify_human_approval(
            authorizers=enrolled, now=_NOW,
            **{**kwargs, "claimed_name": "mallory"},
        )
    with pytest.raises(AuthenticationFailedError, match="unknown authorizer"):
        verify_human_approval(authorizers={}, now=_NOW, **kwargs)
    for bad_sig in ("zz", "00" * 63, "00" * 65, "", None, 42):
        with pytest.raises(AuthenticationFailedError):
            verify_human_approval(
                authorizers=enrolled, now=_NOW,
                **{**kwargs, "signature_hex": bad_sig},
            )


def test_verify_issuance_ordering_and_lifetime() -> None:
    priv, pub = _keypair()
    enrolled = {"alice": bytes.fromhex(pub)}
    for issued, expires in (
        (_NOW, _NOW),  # equal is not strictly later
        (_NOW, _NOW - timedelta(seconds=1)),
    ):
        bad = _approval(priv, issued_at=issued, expires_at=expires)
        with pytest.raises(AuthenticationFailedError, match="later than issued"):
            verify_human_approval(authorizers=enrolled, now=_NOW, **bad)
    overlong = _approval(priv, issued_at=_NOW, expires_at=_NOW + timedelta(hours=25))
    with pytest.raises(AuthenticationFailedError, match="maximum TTL"):
        verify_human_approval(authorizers=enrolled, now=_NOW, **overlong)
    boundary = _approval(
        priv, issued_at=_NOW, expires_at=_NOW + timedelta(hours=24)
    )
    verify_human_approval(  # exactly 24h is within the cap
        authorizers=enrolled, now=_NOW, **boundary
    )


def test_verify_rejects_future_issuance() -> None:
    priv, pub = _keypair()
    enrolled = {"alice": bytes.fromhex(pub)}
    ahead = _approval(
        priv,
        issued_at=_NOW + timedelta(hours=1),
        expires_at=_NOW + timedelta(hours=2),
    )
    with pytest.raises(AuthenticationFailedError, match="issued in the future"):
        verify_human_approval(authorizers=enrolled, now=_NOW, **ahead)
    drifted = _approval(
        priv,
        issued_at=_NOW + timedelta(seconds=30),
        expires_at=_NOW + timedelta(hours=1),
    )
    verify_human_approval(  # inside skew leeway: accepted
        authorizers=enrolled, now=_NOW, **drifted
    )
    beyond = _approval(
        priv,
        issued_at=_NOW + timedelta(seconds=90),
        expires_at=_NOW + timedelta(hours=1),
    )
    with pytest.raises(AuthenticationFailedError, match="issued in the future"):
        verify_human_approval(authorizers=enrolled, now=_NOW, **beyond)


def test_verify_expiry_ttl_and_clock_rules() -> None:
    priv, pub = _keypair()
    enrolled = {"alice": bytes.fromhex(pub)}
    stale = _approval(
        priv,
        issued_at=_NOW - timedelta(hours=2),
        expires_at=_NOW - timedelta(hours=1),
    )
    with pytest.raises(AuthenticationFailedError, match="expired"):
        verify_human_approval(authorizers=enrolled, now=_NOW, **stale)
    skewed = _approval(
        priv,
        issued_at=_NOW - timedelta(hours=1),
        expires_at=_NOW - timedelta(seconds=30),
    )
    verify_human_approval(  # inside skew leeway: accepted
        authorizers=enrolled, now=_NOW, **skewed,
    )
    far = _approval(priv, expires_at=_NOW + timedelta(hours=25))
    with pytest.raises(AuthenticationFailedError, match="maximum TTL"):
        verify_human_approval(authorizers=enrolled, now=_NOW, **far)
    with pytest.raises(ValueError):  # naive datetimes rejected, not coerced
        verify_human_approval(
            authorizers=enrolled, now=_NOW.replace(tzinfo=None), **far,
        )
    naive_issued = dict(far, issued_at=_NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        verify_human_approval(authorizers=enrolled, now=_NOW, **naive_issued)
    naive_expiry = dict(
        far, expires_at=(_NOW + timedelta(hours=1)).replace(tzinfo=None)
    )
    with pytest.raises(ValueError):
        verify_human_approval(authorizers=enrolled, now=_NOW, **naive_expiry)


def test_sign_rejects_unusable_keys() -> None:
    for bad in ("zz", "00" * 31, "", None, 42):
        with pytest.raises(ValueError):
            sign_approval(
                private_key_hex=bad, idempotency_key="k",
                request_fingerprint="f", authorized_by="alice",
                authorization_reference="r", issued_at=_NOW,
                expires_at=_NOW + timedelta(hours=1),
            )


# --- Executor credentials ---


def test_verify_executor() -> None:
    enrolled = {"w-1": _EXEC_SECRET}
    principal = verify_executor(executor_id="W-1", proof=_EXEC_SECRET, executors=enrolled)
    assert principal == ExecutorPrincipal(executor_id="w-1", method="executor-key")
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        verify_executor(executor_id="w-1", proof="wrong-proof-00000000", executors=enrolled)
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        verify_executor(executor_id="ghost-9", proof=_EXEC_SECRET, executors=enrolled)
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        verify_executor(executor_id="w-1", proof=_EXEC_SECRET, executors={})
    with pytest.raises(AuthenticationFailedError):
        verify_executor(executor_id="w-1", proof=None, executors=enrolled)
    with pytest.raises(ValueError):
        verify_executor(executor_id="not an id!", proof=_EXEC_SECRET, executors=enrolled)


# --- Orchestrator credentials and namespace separation ---


def test_verify_orchestrator() -> None:
    enrolled = {"sched-1": _EXEC_SECRET}
    principal = verify_orchestrator(
        orchestrator_id="Sched-1", proof=_EXEC_SECRET, orchestrators=enrolled
    )
    assert principal == OrchestratorPrincipal(
        orchestrator_id="sched-1", method="orchestrator-key"
    )
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        verify_orchestrator(
            orchestrator_id="sched-1", proof="wrong-proof-00000000",
            orchestrators=enrolled,
        )
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        verify_orchestrator(
            orchestrator_id="ghost-9", proof=_EXEC_SECRET, orchestrators=enrolled
        )
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        verify_orchestrator(
            orchestrator_id="sched-1", proof=_EXEC_SECRET, orchestrators={}
        )
    with pytest.raises(AuthenticationFailedError):
        verify_orchestrator(
            orchestrator_id="sched-1", proof=None, orchestrators=enrolled
        )
    with pytest.raises(ValueError):
        verify_orchestrator(
            orchestrator_id="not an id!", proof=_EXEC_SECRET, orchestrators=enrolled
        )


def test_credential_namespaces_never_mix() -> None:
    # A bearer enrolled in both maps authenticates as NEITHER role.
    with pytest.raises(AuthenticationFailedError, match="namespaces overlap"):
        ensure_credential_namespaces_disjoint(
            executors={"dual-1": _EXEC_SECRET},
            orchestrators={"dual-1": _EXEC_SECRET},
        )
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        verify_orchestrator(
            orchestrator_id="w-1", proof=_EXEC_SECRET,
            orchestrators={"sched-1": _EXEC_SECRET},
        )
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        verify_executor(
            executor_id="sched-1", proof=_EXEC_SECRET,
            executors={"w-1": _EXEC_SECRET},
        )


def test_ensure_credential_namespaces_disjoint() -> None:
    other = "test-only-second-secret-0123456789abcdef"
    ensure_credential_namespaces_disjoint(executors={}, orchestrators={})
    ensure_credential_namespaces_disjoint(  # same id, distinct secrets: permitted
        executors={"dual-1": _EXEC_SECRET}, orchestrators={"dual-1": other}
    )
    ensure_credential_namespaces_disjoint(  # distinct ids and secrets: permitted
        executors={"w-1": _EXEC_SECRET}, orchestrators={"sched-1": other}
    )
    with pytest.raises(AuthenticationFailedError, match="namespaces overlap"):
        ensure_credential_namespaces_disjoint(  # same id + same secret: rejected
            executors={"dual-1": _EXEC_SECRET},
            orchestrators={"dual-1": _EXEC_SECRET},
        )
    with pytest.raises(AuthenticationFailedError, match="namespaces overlap"):
        ensure_credential_namespaces_disjoint(  # different ids, same secret: rejected
            executors={"w-1": _EXEC_SECRET},
            orchestrators={"sched-9": _EXEC_SECRET},
        )


def test_verify_rejects_overlapping_namespaces(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTOR_KEYS", f"w-1:{_EXEC_SECRET}")
    monkeypatch.setenv("ORCHESTRATOR_KEYS", f"sched-9:{_EXEC_SECRET}")
    with pytest.raises(AuthenticationFailedError, match="namespaces overlap"):
        verify_executor(executor_id="w-1", proof=_EXEC_SECRET)
    with pytest.raises(AuthenticationFailedError, match="namespaces overlap"):
        verify_orchestrator(orchestrator_id="sched-9", proof=_EXEC_SECRET)


def test_shared_names_with_distinct_secrets_permitted(monkeypatch) -> None:
    other = _EXEC_SECRET + "-second"
    monkeypatch.setenv("EXECUTOR_KEYS", f"dual-1:{_EXEC_SECRET}")
    monkeypatch.setenv("ORCHESTRATOR_KEYS", f"dual-1:{other}")
    assert verify_executor(executor_id="dual-1", proof=_EXEC_SECRET).executor_id == "dual-1"
    assert verify_orchestrator(
        orchestrator_id="dual-1", proof=other
    ).orchestrator_id == "dual-1"


def test_verify_fails_closed_on_malformed_counterpart(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTOR_KEYS", f"w-1:{_EXEC_SECRET}")
    monkeypatch.setenv("ORCHESTRATOR_KEYS", "bogus-entry")
    with pytest.raises(AuthenticationFailedError, match="orchestrator map"):
        verify_executor(executor_id="w-1", proof=_EXEC_SECRET)
    monkeypatch.setenv("ORCHESTRATOR_KEYS", f"sched-1:{_EXEC_SECRET}")
    monkeypatch.setenv("EXECUTOR_KEYS", "bogus-entry")
    with pytest.raises(AuthenticationFailedError, match="executor map"):
        verify_orchestrator(orchestrator_id="sched-1", proof=_EXEC_SECRET)


def test_disjointness_error_reveals_nothing() -> None:
    with pytest.raises(AuthenticationFailedError) as exc:
        ensure_credential_namespaces_disjoint(
            executors={"w-1": _EXEC_SECRET},
            orchestrators={"sched-1": _EXEC_SECRET},
        )
    assert _EXEC_SECRET not in str(exc.value)


# --- Policy separation ---


def test_authorization_policy_matrix() -> None:
    human = HumanPrincipal(name="alice")
    executor = ExecutorPrincipal(executor_id="w-1")
    orchestrator = OrchestratorPrincipal(orchestrator_id="sched-1")
    assert (may_authorize(human), may_claim(human), may_enqueue(human)) == (True, False, False)
    assert (may_authorize(executor), may_claim(executor), may_enqueue(executor)) == (False, True, False)
    assert (
        may_authorize(orchestrator), may_claim(orchestrator), may_enqueue(orchestrator)
    ) == (False, False, True)
    for other in ("alice", None, 42, {"name": "alice"}, object()):
        assert may_authorize(other) is False
        assert may_claim(other) is False
        assert may_enqueue(other) is False
    ensure_may_authorize(human)
    ensure_may_claim(executor)
    ensure_may_enqueue(orchestrator)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_authorize(executor)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_authorize(orchestrator)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_claim(human)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_claim(orchestrator)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_enqueue(human)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_enqueue(executor)


def test_scoped_policy_exact_match() -> None:
    executor = ExecutorPrincipal(executor_id="w-1")
    orchestrator = OrchestratorPrincipal(orchestrator_id="sched-1")
    exec_scopes = parse_executor_scopes("w-1:outreach_email|contract_sign")
    orch_scopes = parse_orchestrator_scopes("sched-1:outreach_email")
    assert may_claim_type(executor, "outreach_email", exec_scopes) is True
    assert may_claim_type(executor, "contract_sign", exec_scopes) is True
    assert may_enqueue_type(orchestrator, "outreach_email", orch_scopes) is True
    # Exact match only: case, substrings, and unknown types deny.
    for denied in ("Outreach_Email", "outreach", "outreach_email_v2", "wire_transfer", ""):
        assert may_claim_type(executor, denied, exec_scopes) is False
        assert may_enqueue_type(orchestrator, denied, orch_scopes) is False
    # Unknown ids and missing/foreign maps deny.
    assert may_claim_type(executor, "outreach_email", {}) is False
    assert may_claim_type(executor, "outreach_email", None) is False
    assert may_enqueue_type(orchestrator, "outreach_email", {}) is False
    assert may_claim_type(
        ExecutorPrincipal(executor_id="w-9"), "outreach_email", exec_scopes
    ) is False
    assert may_claim_type(executor, "outreach_email", orch_scopes) is False
    assert may_enqueue_type(orchestrator, "outreach_email", exec_scopes) is False
    # Wrong principal types deny even with a granting map.
    human = HumanPrincipal(name="alice")
    assert may_claim_type(human, "outreach_email", exec_scopes) is False
    assert may_claim_type(orchestrator, "outreach_email", exec_scopes) is False
    assert may_enqueue_type(human, "outreach_email", orch_scopes) is False
    assert may_enqueue_type(executor, "outreach_email", orch_scopes) is False
    for other in ("w-1", None, 42, object()):
        assert may_claim_type(other, "outreach_email", exec_scopes) is False
        assert may_enqueue_type(other, "outreach_email", orch_scopes) is False
    # Non-dict maps and non-string types deny (never raise, never grant).
    assert may_claim_type(executor, "outreach_email", "w-1:outreach_email") is False
    assert may_claim_type(executor, None, exec_scopes) is False
    ensure_may_claim_type(executor, "outreach_email", exec_scopes)
    ensure_may_enqueue_type(orchestrator, "outreach_email", orch_scopes)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to claim"):
        ensure_may_claim_type(executor, "wire_transfer", exec_scopes)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to enqueue"):
        ensure_may_enqueue_type(orchestrator, "wire_transfer", orch_scopes)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_claim_type(orchestrator, "outreach_email", exec_scopes)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_enqueue_type(executor, "outreach_email", orch_scopes)


def test_scoped_policy_loads_environment(monkeypatch) -> None:
    executor = ExecutorPrincipal(executor_id="w-1")
    monkeypatch.setenv("EXECUTOR_SCOPES", "w-1:outreach_email")
    ensure_may_claim_type(executor, "outreach_email")  # env map loaded
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_claim_type(executor, "wire_transfer")
    monkeypatch.delenv("EXECUTOR_SCOPES")
    with pytest.raises(AuthorizationDeniedError, match="not permitted to claim"):
        ensure_may_claim_type(executor, "outreach_email")  # missing map denies
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "bogus-entry")
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        ensure_may_enqueue_type(
            OrchestratorPrincipal(orchestrator_id="sched-1"), "outreach_email"
        )  # malformed map fails closed


def test_wondrue_reader_designation(monkeypatch) -> None:
    orch = OrchestratorPrincipal(orchestrator_id="wondrue-1")
    assert may_inspect_systemwide(orch, "wondrue-1") is True
    assert may_inspect_systemwide(orch, "sched-1") is False
    assert may_inspect_systemwide(orch, None) is False  # missing matches nobody
    for other in (
        ExecutorPrincipal(executor_id="wondrue-1"),  # same name, wrong role
        HumanPrincipal(name="wondrue-1"),
        "wondrue-1", None, 42, object(),
    ):
        assert may_inspect_systemwide(other, "wondrue-1") is False
    monkeypatch.setenv("WONDRUE_READER_ID", "Wondrue-1")
    assert load_wondrue_reader_id() == "wondrue-1"  # canonicalized
    monkeypatch.delenv("WONDRUE_READER_ID")
    assert load_wondrue_reader_id() is None
    monkeypatch.setenv("WONDRUE_READER_ID", "   ")
    assert load_wondrue_reader_id() is None
    monkeypatch.setenv("WONDRUE_READER_ID", "not an id!")
    with pytest.raises(AuthenticationFailedError, match="designation invalid"):
        load_wondrue_reader_id()


def test_scoped_inspection_policy() -> None:
    orch = OrchestratorPrincipal(orchestrator_id="sched-1")
    scopes = parse_orchestrator_scopes("sched-1:outreach_email")
    assert may_inspect_type(orch, "outreach_email", scopes) is True
    assert may_inspect_type(orch, "Outreach_Email", scopes) is False
    assert may_inspect_type(orch, "wire_transfer", scopes) is False
    assert may_inspect_type(orch, "outreach_email", {}) is False
    assert may_inspect_type(orch, "outreach_email", None) is False
    for other in (
        ExecutorPrincipal(executor_id="sched-1"),
        HumanPrincipal(name="sched-1"),
        "sched-1", None, 42, object(),
    ):
        assert may_inspect_type(other, "outreach_email", scopes) is False
    ensure_may_inspect_type(orch, "outreach_email", scopes)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to inspect"):
        ensure_may_inspect_type(orch, "wire_transfer", scopes)


def test_scoped_inspection_loads_environment(monkeypatch) -> None:
    orch = OrchestratorPrincipal(orchestrator_id="sched-1")
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "sched-1:outreach_email")
    ensure_may_inspect_type(orch, "outreach_email")
    monkeypatch.delenv("ORCHESTRATOR_SCOPES")
    with pytest.raises(AuthorizationDeniedError, match="not permitted to inspect"):
        ensure_may_inspect_type(orch, "outreach_email")


def test_constructed_principals_confer_no_ledger_path() -> None:
    import inspect

    import app.external_actions_db as ledger
    import app.external_action_reads as reads

    # The ledger accepts claims plus proofs, never principal objects:
    # constructing one cannot reach any authorization path.
    for fn in (
        ledger.record_authorization, ledger.claim_action, ledger.transition_action,
        reads.get_action, reads.list_attempts, reads.list_own_open_attempts,
        reads.find_stale_attempting, reads.stale_summary,
    ):
        params = inspect.signature(fn).parameters
        assert "principal" not in params
        assert "HumanPrincipal" not in str(params.values())
        assert "ExecutorPrincipal" not in str(params.values())
        assert "OrchestratorPrincipal" not in str(params.values())


# --- Secret hygiene and boundary confinement ---


def test_no_secrets_in_verification_errors() -> None:
    priv, pub = _keypair()
    kwargs = _approval(priv)
    with pytest.raises(AuthenticationFailedError) as human_exc:
        verify_human_approval(
            authorizers={"alice": bytes.fromhex(pub)}, now=_NOW,
            **{**kwargs, "signature_hex": "ff" * 64},
        )
    assert "ff" * 64 not in str(human_exc.value) and pub not in str(human_exc.value)
    with pytest.raises(AuthenticationFailedError) as exec_exc:
        verify_executor(executor_id="w-1", proof="wrong-proof-00000000",
                        executors={"w-1": _EXEC_SECRET})
    assert _EXEC_SECRET not in str(exec_exc.value)
    assert "wrong-proof" not in str(exec_exc.value)
    with pytest.raises(AuthenticationFailedError) as orch_exc:
        verify_orchestrator(orchestrator_id="sched-1", proof="wrong-proof-00000000",
                            orchestrators={"sched-1": _EXEC_SECRET})
    assert _EXEC_SECRET not in str(orch_exc.value)
    assert "wrong-proof" not in str(orch_exc.value)


def test_credential_env_access_confined_to_identity() -> None:
    import ast
    from pathlib import Path

    offenders = []
    for source in sorted(Path("app").rglob("*.py")):
        if source.name == "identity.py":
            continue
        text = source.read_text(encoding="utf-8")
        if any(
            token in text
            for token in (
                "AUTHORIZER_PUBLIC_KEYS",
                "EXECUTOR_KEYS",
                "ORCHESTRATOR_KEYS",
                "EXECUTOR_SCOPES",
                "ORCHESTRATOR_SCOPES",
                "WONDRUE_READER_ID",
                "RECONCILER_PUBLIC_KEYS",
            )
        ):
            offenders.append(str(source))
    assert offenders == [], offenders


def test_routes_reference_no_identity_or_ledger_surface() -> None:
    from pathlib import Path

    offenders = []
    for source in sorted(Path("app/routes").glob("*.py")):
        text = source.read_text(encoding="utf-8")
        hits = [
            token for token in (
                "app.identity", "sign_approval", "verify_human_approval",
                "verify_executor", "verify_orchestrator", "may_enqueue",
                "may_inspect", "load_wondrue_reader_id",
                "verify_reconciliation_approval", "sign_reconciliation_approval",
                "may_reconcile", "ReconciliationEvidence",
                "AUTHORIZER_PUBLIC_KEYS", "EXECUTOR_KEYS", "ORCHESTRATOR_KEYS",
                "EXECUTOR_SCOPES", "ORCHESTRATOR_SCOPES", "WONDRUE_READER_ID",
                "RECONCILER_PUBLIC_KEYS",
                "external_actions_db", "external_action_reads",
                "external_action_reconcile",
                "record_authorization", "claim_action",
                "transition_action", "record_reconciliation",
                "get_action", "list_attempts",
                "list_own_open_attempts", "find_stale_attempting",
                "stale_summary",
            )
            if token in text
        ]
        if hits:
            offenders.append((source.name, hits))
    assert offenders == [], offenders
