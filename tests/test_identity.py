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
    canonical_identity_name,
    ensure_may_authorize,
    ensure_may_claim,
    load_authorizer_keys,
    load_executor_keys,
    may_authorize,
    may_claim,
    parse_authorizer_keys,
    parse_executor_keys,
    sign_approval,
    verify_executor,
    verify_human_approval,
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
    assert set(load_authorizer_keys()) == {"alice"}
    assert load_executor_keys() == {"w-1": _EXEC_SECRET}
    monkeypatch.delenv("AUTHORIZER_PUBLIC_KEYS")
    monkeypatch.delenv("EXECUTOR_KEYS")
    assert load_authorizer_keys() == {} and load_executor_keys() == {}


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


# --- Policy separation ---


def test_authorization_policy_matrix() -> None:
    human = HumanPrincipal(name="alice")
    executor = ExecutorPrincipal(executor_id="w-1")
    assert (may_authorize(human), may_claim(human)) == (True, False)
    assert (may_authorize(executor), may_claim(executor)) == (False, True)
    for other in ("alice", None, 42, {"name": "alice"}, object()):
        assert may_authorize(other) is False
        assert may_claim(other) is False
    ensure_may_authorize(human)
    ensure_may_claim(executor)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_authorize(executor)
    with pytest.raises(AuthorizationDeniedError):
        ensure_may_claim(human)


def test_constructed_principals_confer_no_ledger_path() -> None:
    import inspect

    import app.external_actions_db as ledger

    # The ledger accepts claims plus proofs, never principal objects:
    # constructing one cannot reach any authorization path.
    for fn in (ledger.record_authorization, ledger.claim_action):
        params = inspect.signature(fn).parameters
        assert "principal" not in params
        assert "HumanPrincipal" not in str(params.values())
        assert "ExecutorPrincipal" not in str(params.values())


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


def test_credential_env_access_confined_to_identity() -> None:
    import ast
    from pathlib import Path

    offenders = []
    for source in sorted(Path("app").rglob("*.py")):
        if source.name == "identity.py":
            continue
        text = source.read_text(encoding="utf-8")
        if "AUTHORIZER_PUBLIC_KEYS" in text or "EXECUTOR_KEYS" in text:
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
                "verify_executor", "AUTHORIZER_PUBLIC_KEYS", "EXECUTOR_KEYS",
                "external_actions_db", "record_authorization", "claim_action",
            )
            if token in text
        ]
        if hits:
            offenders.append((source.name, hits))
    assert offenders == [], offenders
