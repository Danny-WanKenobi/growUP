"""Verified principal identities (Phase 3C-3B-2A).

Authentication and policy boundary for the external-action ledger.
Answers "who is making this request" (authentication) and "is this
identity permitted" (authorization) as two explicit steps. Depends on
the standard library plus ``cryptography`` (Ed25519 only); imports
nothing from ``app`` so no ledger caller can create an import cycle
and no principal type can smuggle business logic.

Two identity kinds, two mechanisms -- the asymmetry is deliberate:

- HUMAN authorizers use offline Ed25519 approval signatures. Private
  keys live with the human operator OUTSIDE this application (never
  in its environment, files, or database); the app holds only public
  keys, which are not secrets. Each approval signature binds one
  idempotency key, request fingerprint, principal, reference, and
  expiry, so a captured approval cannot authorize anything else.
  Because minting requires a private key the process never holds,
  in-process code cannot forge human approvals.
- EXECUTOR services use env-held shared secrets (``EXECUTOR_KEYS``),
  following the existing ``auth.py`` shared-secret pattern. These are
  reusable service BEARER secrets: presenting one proves which
  enrolled service is calling, but it does NOT independently
  authorize any specific action type. Action-specific authorization,
  executor isolation, and execution safety remain prerequisites for
  enabling real external actions -- this phase authenticates the
  claimant only. Proportionate for service credentials, with the
  honest in-process limit below.

SINGLE-PROCESS LIMITATION (read carefully): this module genuinely
stops credential-less forgery -- fabricating a principal object or
string confers nothing, because the ledger verifies proofs itself
and derives stored identities server-side. It does NOT create a
security boundary against arbitrary malicious code running inside
this same process: such code could read ``EXECUTOR_KEYS`` from the
process environment as easily as this module does. Human approvals
are immune (no private key material exists in-process); executor
credentials await service-process separation for full isolation.
No production route accepts ledger credentials in this phase, which
keeps the credential exposure surface to operator tooling and tests.

Replacement path: human verification can later move to sessions,
OAuth/OIDC, or Wondrue-mediated approvals, and executors to
service-identity providers, by swapping the ``verify_*`` functions.
Ledger entry points only consume ``HumanPrincipal`` /
``ExecutorPrincipal`` plus the ``ensure_may_*`` policy checks, so the
boundary shape survives that migration.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

AUTHORIZER_PUBLIC_KEYS_ENV_VAR = "AUTHORIZER_PUBLIC_KEYS"
EXECUTOR_KEYS_ENV_VAR = "EXECUTOR_KEYS"

# Approval signature domain separator: binds signatures to this exact
# protocol so they cannot be transplanted from any other signed flow.
_APPROVAL_DOMAIN = b"growup-approval-v1:"

# Canonical identity names: stable lowercase handles (usernames,
# service ids). Canonicalization happens at the verification boundary
# so fingerprint replays stay stable across spelling variants.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MAX_IDENTITY_ENTRIES = 64

# Human approvals are short-lived capabilities, not sessions.
MAX_APPROVAL_TTL_SECONDS = 24 * 60 * 60
CLOCK_SKEW_SECONDS = 60

# Executor service secrets: long enough to resist guessing, bounded
# to keep config parsing safe.
MIN_EXECUTOR_SECRET_CHARS = 16
MAX_EXECUTOR_SECRET_CHARS = 1024

_DUMMY_EXECUTOR_SECRET = "dummy-secret-for-unknown-executor-comparison-only"


class AuthenticationFailedError(RuntimeError):
    """A credential, signature, or grant did not verify (who failed)."""


class AuthorizationDeniedError(RuntimeError):
    """A verified identity is not permitted for the operation (may failed)."""


@dataclass(frozen=True)
class HumanPrincipal:
    """A data representation of a human authorizer.

    Carries no proof by itself: constructing one (or any object of
    this type outside :func:`verify_human_approval`) establishes
    nothing. Only instances returned by the trusted verifier below
    represent authenticated humans, and policy functions must receive
    verifier-produced instances -- never caller-supplied ones.
    """

    name: str
    method: str = "ed25519-approval"


@dataclass(frozen=True)
class ExecutorPrincipal:
    """A data representation of an executor service.

    Same trust rule as :class:`HumanPrincipal`: a bare instance is an
    unauthenticated claim. Only :func:`verify_executor` outputs count
    as authenticated, and only those may reach policy checks.
    """

    executor_id: str
    method: str = "executor-key"


def canonical_identity_name(value: object) -> str:
    """Canonicalize a claimed identity to its stable lowercase handle.

    Malformed input is a caller shape error (ValueError), distinct
    from a well-formed-but-unverifiable claim, which fails later with
    :class:`AuthenticationFailedError`. Canonicalization is idempotent
    and runs before fingerprinting so replays stay stable.
    """
    if not isinstance(value, str):
        raise ValueError("identity claim must be a string")
    name = value.strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"malformed identity name: {value!r}")
    return name


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(moment: object, name: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return moment.astimezone(timezone.utc)


def parse_authorizer_keys(raw: str | None) -> dict[str, bytes]:
    """Parse ``name:hex-pubkey`` entries into canonical name -> raw key bytes.

    Public keys are verification material, not secrets. Malformed maps
    fail closed (empty result denies everyone); callers never see keys
    in error text.
    """
    entries: dict[str, bytes] = {}
    if raw is None or not raw.strip():
        return entries
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, hexkey = item.partition(":")
        if not sep:
            raise AuthenticationFailedError("authorizer map entry missing ':'")
        try:
            canonical = canonical_identity_name(name)
        except ValueError:
            raise AuthenticationFailedError("authorizer map has invalid name entry") from None
        try:
            rawkey = bytes.fromhex(hexkey.strip())
        except ValueError:
            raise AuthenticationFailedError("authorizer map has non-hex key") from None
        if len(rawkey) != 32 or canonical in entries or len(entries) >= MAX_IDENTITY_ENTRIES:
            raise AuthenticationFailedError("authorizer map has invalid key entry")
        entries[canonical] = rawkey
    return entries


def parse_executor_keys(raw: str | None) -> dict[str, str]:
    """Parse ``id:secret`` service credentials (secrets stay in this map)."""
    entries: dict[str, str] = {}
    if raw is None or not raw.strip():
        return entries
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        executor_id, sep, secret = item.partition(":")
        if not sep:
            raise AuthenticationFailedError("executor map entry missing ':'")
        try:
            canonical = canonical_identity_name(executor_id)
        except ValueError:
            raise AuthenticationFailedError("executor map has invalid id entry") from None
        if not (MIN_EXECUTOR_SECRET_CHARS <= len(secret) <= MAX_EXECUTOR_SECRET_CHARS):
            raise AuthenticationFailedError("executor map has invalid secret entry")
        if canonical in entries or len(entries) >= MAX_IDENTITY_ENTRIES:
            raise AuthenticationFailedError("executor map has invalid id entry")
        entries[canonical] = secret
    return entries


def load_authorizer_keys() -> dict[str, bytes]:
    """Load human-verification public keys from the environment."""
    return parse_authorizer_keys(os.getenv(AUTHORIZER_PUBLIC_KEYS_ENV_VAR))


def load_executor_keys() -> dict[str, str]:
    """Load executor service credentials from the environment."""
    return parse_executor_keys(os.getenv(EXECUTOR_KEYS_ENV_VAR))


def _approval_payload(
    idempotency_key: str,
    request_fingerprint: str,
    authorized_by: str,
    authorization_reference: str,
    issued_at: datetime,
    expires_at: datetime,
) -> bytes:
    canonical = {
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "authorized_by": authorized_by,
        "authorization_reference": authorization_reference,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return _APPROVAL_DOMAIN + blob.encode("utf-8")


def sign_approval(
    *,
    private_key_hex: str,
    idempotency_key: str,
    request_fingerprint: str,
    authorized_by: str,
    authorization_reference: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    """Sign one human approval (operator-side helper; pure function).

    The private key arrives as an argument from operator-held storage
    and is NEVER read from the application environment here -- its
    absence from this process is what makes approvals unforgeable
    in-process. Both timestamps are bound into the signature, so
    backdating, postdating, or extending a captured approval
    invalidates it. Returns the hex signature to submit with the
    request. Timestamps must be timezone-aware; lifetime rules are
    enforced at verification.
    """
    from cryptography.exceptions import InvalidKey
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    except (ValueError, TypeError, InvalidKey):
        raise ValueError("unusable approval signing key") from None
    issued = _require_aware(issued_at, "issued_at")
    expiry = _require_aware(expires_at, "expires_at")
    payload = _approval_payload(
        idempotency_key, request_fingerprint, authorized_by,
        authorization_reference, issued, expiry,
    )
    return private.sign(payload).hex()


def verify_human_approval(
    *,
    claimed_name: str,
    idempotency_key: str,
    request_fingerprint: str,
    authorization_reference: str,
    issued_at: datetime,
    expires_at: datetime,
    signature_hex: str,
    authorizers: dict[str, bytes] | None = None,
    now: datetime | None = None,
) -> HumanPrincipal:
    """Verify one human approval signature against enrolled public keys.

    Succeeds only for a known principal, a coherent signed lifetime,
    and a signature over the exact supplied fields. Lifetime rules:

    - Both timestamps must be timezone-aware (naive input is a
      caller shape error, never silently assumed UTC).
    - ``expires_at`` must be strictly later than ``issued_at``.
    - The signed lifetime (expiry minus issuance) may not exceed
      24 hours: approvals are short-lived capabilities.
    - ``issued_at`` may not lie more than ``CLOCK_SKEW_SECONDS``
      (60s) in the future: backdated-or-postdated grants are
      rejected, with a small allowance for signer/verifier clock
      difference -- not for human scheduling.
    - Expired approvals are rejected once ``now`` passes
      ``expires_at`` plus the same 60s skew allowance.

    The returned principal name is the enrolled canonical name --
    never an echo of the claim. Anything else raises
    :class:`AuthenticationFailedError` with no key material in text.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
    name = canonical_identity_name(claimed_name)
    moment = _utcnow() if now is None else _require_aware(now, "now")
    issued = _require_aware(issued_at, "issued_at")
    expiry = _require_aware(expires_at, "expires_at")
    if expiry <= issued:
        raise AuthenticationFailedError("approval expires_at must be later than issued_at")
    if (expiry - issued).total_seconds() > MAX_APPROVAL_TTL_SECONDS:
        raise AuthenticationFailedError("approval lifetime exceeds maximum TTL")
    if issued > moment + skew:
        raise AuthenticationFailedError("approval issued in the future")
    if moment > expiry + skew:
        raise AuthenticationFailedError("approval expired")
    known = load_authorizer_keys() if authorizers is None else authorizers
    rawkey = known.get(name)
    if rawkey is None:
        raise AuthenticationFailedError("unknown authorizer")
    try:
        signature = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        raise AuthenticationFailedError("malformed approval signature") from None
    payload = _approval_payload(
        idempotency_key, request_fingerprint, name,
        authorization_reference, issued, expiry,
    )
    try:
        Ed25519PublicKey.from_public_bytes(rawkey).verify(signature, payload)
    except (InvalidSignature, ValueError):
        raise AuthenticationFailedError("approval signature mismatch") from None
    return HumanPrincipal(name=name)


def verify_executor(
    *,
    executor_id: str,
    proof: str,
    executors: dict[str, str] | None = None,
) -> ExecutorPrincipal:
    """Verify one executor service credential (constant-time compare).

    Unknown ids compare against a dummy secret first so rejection
    timing does not trivially enumerate enrolled executors. Returns
    the enrolled canonical id; anything else raises
    :class:`AuthenticationFailedError` without secret material.
    """
    claimed = canonical_identity_name(executor_id)
    known = load_executor_keys() if executors is None else executors
    expected = known.get(claimed)
    if expected is None:
        hmac.compare_digest(str(proof), _DUMMY_EXECUTOR_SECRET)
        raise AuthenticationFailedError("unknown executor")
    if not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
        raise AuthenticationFailedError("executor proof mismatch")
    return ExecutorPrincipal(executor_id=claimed)


def may_authorize(principal: object) -> bool:
    """Only a verified human authorizer may record authorization.

    The ``isinstance`` check establishes principal TYPE, not proof of
    authentication: callers must pass principals produced by
    :func:`verify_human_approval`. Ledger entry points verify proofs
    themselves and never accept caller-supplied principal objects.
    """
    return isinstance(principal, HumanPrincipal)


def may_claim(principal: object) -> bool:
    """Only a verified executor service may claim actions.

    Same trust rule as :func:`may_authorize`: type check only, valid
    solely for :func:`verify_executor` outputs. Ledger entry points
    verify proofs themselves.
    """
    return isinstance(principal, ExecutorPrincipal)


def ensure_may_authorize(principal: object) -> None:
    """Enforce the authorization policy (fail closed, explicit denial)."""
    if not may_authorize(principal):
        raise AuthorizationDeniedError(
            "recording authorization requires a verified human authorizer"
        )


def ensure_may_claim(principal: object) -> None:
    """Enforce the claim policy (fail closed, explicit denial)."""
    if not may_claim(principal):
        raise AuthorizationDeniedError(
            "claiming an action requires a verified executor service"
        )
