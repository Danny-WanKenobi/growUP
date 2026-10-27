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
  enrolled service is calling; a separate scope map
  (``EXECUTOR_SCOPES``, Phase 3C-3B-2B) authorizes specific action
  types by exact match. Executor isolation,
  executor isolation, and execution safety remain prerequisites for
  enabling real external actions -- executor identity is now
  authenticated (who) AND scoped per action type (may claim what).
  Proportionate for service credentials, with the honest
  in-process limit below.
- ORCHESTRATOR services (Phase 3C-3B-2B) use a SEPARATE env-held
  credential namespace (``ORCHESTRATOR_KEYS``) plus their own scope
  map (``ORCHESTRATOR_SCOPES``). Orchestrators schedule -- they move
  AUTHORIZED actions to PENDING -- but never claim or record
  outcomes; executors claim and record but never enqueue. The
  namespaces never mix: an orchestrator credential cannot verify
  as an executor and vice versa, and each role's policy accepts
  only its own principal type. The separation is by secret VALUE,
  not by name: any value enrolled in both maps is rejected
  outright (``ensure_credential_namespaces_disjoint``), so one
  bearer can never authenticate as both roles even under
  different names -- while identical names with distinct
  secrets remain permitted.

SINGLE-PROCESS LIMITATION (read carefully): this module genuinely
stops credential-less forgery -- fabricating a principal object or
string confers nothing, because the ledger verifies proofs itself
and derives stored identities server-side. It does NOT create a
security boundary against arbitrary malicious code running inside
this same process: such code could read ``EXECUTOR_KEYS`` /
``ORCHESTRATOR_KEYS`` from the process environment as easily as
this module does. Human approvals are immune (no private key
material exists in-process); executor and orchestrator credentials
await service-process separation for full isolation.
No production route accepts ledger credentials in this phase, which
keeps the credential exposure surface to operator tooling and tests.

Replacement path: human verification can later move to sessions,
OAuth/OIDC, or Wondrue-mediated approvals, and executors to
service-identity providers, by swapping the ``verify_*`` functions.
Ledger entry points only consume ``HumanPrincipal`` /
``ExecutorPrincipal`` / ``OrchestratorPrincipal`` plus the
``ensure_may_*`` policy checks, so the boundary shape survives
that migration.
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
ORCHESTRATOR_KEYS_ENV_VAR = "ORCHESTRATOR_KEYS"
EXECUTOR_SCOPES_ENV_VAR = "EXECUTOR_SCOPES"
ORCHESTRATOR_SCOPES_ENV_VAR = "ORCHESTRATOR_SCOPES"

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

# Scope maps: bounded grants of exact action-type strings per
# service id (``w-1:outreach_email|contract_sign,...``). Granted
# types match stored action_type values byte-for-byte; a bare "*"
# is rejected at parse (no wildcards, no default-allow).
MAX_SCOPE_TYPES = 64
MAX_SCOPE_TYPE_CHARS = 128

_DUMMY_SERVICE_SECRET = "dummy-secret-for-unknown-service-comparison-only"


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


@dataclass(frozen=True)
class OrchestratorPrincipal:
    """A data representation of an orchestrator service.

    Same trust rule as :class:`HumanPrincipal`: a bare instance is
    an unauthenticated claim. Only :func:`verify_orchestrator`
    outputs count as authenticated, and only those may reach the
    enqueue policy checks.
    """

    orchestrator_id: str
    method: str = "orchestrator-key"


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


def _parse_service_keys(raw: str | None, *, kind: str) -> dict[str, str]:
    """Parse ``id:secret`` service credentials (secrets stay in this map).

    Shared by the executor and orchestrator namespaces; each caller
    passes its own ``kind`` so error text names the right map. The
    namespaces never mix: verification reads exactly one map.
    """
    entries: dict[str, str] = {}
    if raw is None or not raw.strip():
        return entries
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        service_id, sep, secret = item.partition(":")
        if not sep:
            raise AuthenticationFailedError(f"{kind} map entry missing ':'")
        try:
            canonical = canonical_identity_name(service_id)
        except ValueError:
            raise AuthenticationFailedError(f"{kind} map has invalid id entry") from None
        if not (MIN_EXECUTOR_SECRET_CHARS <= len(secret) <= MAX_EXECUTOR_SECRET_CHARS):
            raise AuthenticationFailedError(f"{kind} map has invalid secret entry")
        if canonical in entries or len(entries) >= MAX_IDENTITY_ENTRIES:
            raise AuthenticationFailedError(f"{kind} map has invalid id entry")
        entries[canonical] = secret
    return entries


def parse_executor_keys(raw: str | None) -> dict[str, str]:
    """Parse ``id:secret`` executor service credentials (secrets stay in this map)."""
    return _parse_service_keys(raw, kind="executor")


def parse_orchestrator_keys(raw: str | None) -> dict[str, str]:
    """Parse ``id:secret`` orchestrator credentials (separate namespace).

    Same rules as executors; enrolled ids here verify ONLY via
    :func:`verify_orchestrator` and can never authenticate as an
    executor, even if the same id string appears in both maps.
    """
    return _parse_service_keys(raw, kind="orchestrator")


def _parse_scope_map(raw: str | None, *, kind: str) -> dict[str, frozenset[str]]:
    """Parse ``id:type|type,...`` action-type grants (fail closed).

    Grants are EXACT action-type strings: a service may act only on
    rows whose stored ``action_type`` equals a granted string
    byte-for-byte (case-sensitive, no wildcards -- a bare "*" entry
    is rejected at parse). Missing or empty maps deny everyone;
    malformed entries raise :class:`AuthorizationDeniedError`
    (policy, not identity, failed) with no map content echoed.
    """
    entries: dict[str, frozenset[str]] = {}
    if raw is None or not raw.strip():
        return entries
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        service_id, sep, types_blob = item.partition(":")
        if not sep:
            raise AuthorizationDeniedError(f"{kind} scope map entry missing ':'")
        try:
            canonical = canonical_identity_name(service_id)
        except ValueError:
            raise AuthorizationDeniedError(f"{kind} scope map has invalid id entry") from None
        granted = frozenset(t.strip() for t in types_blob.split("|"))
        if (
            not types_blob.strip()
            or "" in granted
            or "*" in granted
            or any(len(t) > MAX_SCOPE_TYPE_CHARS for t in granted)
            or len(granted) > MAX_SCOPE_TYPES
        ):
            raise AuthorizationDeniedError(f"{kind} scope map has invalid type entry")
        if canonical in entries or len(entries) >= MAX_IDENTITY_ENTRIES:
            raise AuthorizationDeniedError(f"{kind} scope map has invalid id entry")
        entries[canonical] = granted
    return entries


def parse_executor_scopes(raw: str | None) -> dict[str, frozenset[str]]:
    """Parse per-executor claim grants (exact action-type match)."""
    return _parse_scope_map(raw, kind="executor")


def parse_orchestrator_scopes(raw: str | None) -> dict[str, frozenset[str]]:
    """Parse per-orchestrator enqueue grants (exact action-type match)."""
    return _parse_scope_map(raw, kind="orchestrator")


def load_authorizer_keys() -> dict[str, bytes]:
    """Load human-verification public keys from the environment."""
    return parse_authorizer_keys(os.getenv(AUTHORIZER_PUBLIC_KEYS_ENV_VAR))


def load_executor_keys() -> dict[str, str]:
    """Load executor service credentials from the environment."""
    return parse_executor_keys(os.getenv(EXECUTOR_KEYS_ENV_VAR))


def load_orchestrator_keys() -> dict[str, str]:
    """Load orchestrator service credentials from the environment."""
    return parse_orchestrator_keys(os.getenv(ORCHESTRATOR_KEYS_ENV_VAR))


def load_executor_scopes() -> dict[str, frozenset[str]]:
    """Load per-executor claim grants from the environment."""
    return parse_executor_scopes(os.getenv(EXECUTOR_SCOPES_ENV_VAR))


def load_orchestrator_scopes() -> dict[str, frozenset[str]]:
    """Load per-orchestrator enqueue grants from the environment."""
    return parse_orchestrator_scopes(os.getenv(ORCHESTRATOR_SCOPES_ENV_VAR))


def ensure_credential_namespaces_disjoint(
    executors: dict[str, str] | None = None,
    orchestrators: dict[str, str] | None = None,
) -> None:
    """Reject any secret value enrolled in both service namespaces.

    Orchestrator and executor credentials must never be reusable
    across roles: if any secret VALUE appears in both maps --
    regardless of identity name -- the configuration is rejected
    and NEITHER role can authenticate. A side left as None loads
    from its environment variable. Missing maps are empty (nothing
    to overlap); malformed maps fail closed via their own parsers.
    The error is uniform and carries no secret material. Called by
    both ``verify_*`` functions so the invariant holds for every
    present and future caller.
    """
    exec_map = load_executor_keys() if executors is None else executors
    orch_map = load_orchestrator_keys() if orchestrators is None else orchestrators
    if set(exec_map.values()) & set(orch_map.values()):
        raise AuthenticationFailedError("service credential namespaces overlap")


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
    ensure_credential_namespaces_disjoint(executors=known, orchestrators=None)
    expected = known.get(claimed)
    if expected is None:
        hmac.compare_digest(str(proof), _DUMMY_SERVICE_SECRET)
        raise AuthenticationFailedError("unknown executor")
    if not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
        raise AuthenticationFailedError("executor proof mismatch")
    return ExecutorPrincipal(executor_id=claimed)


def verify_orchestrator(
    *,
    orchestrator_id: str,
    proof: str,
    orchestrators: dict[str, str] | None = None,
) -> OrchestratorPrincipal:
    """Verify one orchestrator service credential (constant-time compare).

    Reads ONLY the orchestrator namespace: an executor credential --
    even for an identically spelled id -- never verifies here.
    Returns the enrolled canonical id; anything else raises
    :class:`AuthenticationFailedError` without secret material.
    """
    claimed = canonical_identity_name(orchestrator_id)
    known = load_orchestrator_keys() if orchestrators is None else orchestrators
    ensure_credential_namespaces_disjoint(executors=None, orchestrators=known)
    expected = known.get(claimed)
    if expected is None:
        hmac.compare_digest(str(proof), _DUMMY_SERVICE_SECRET)
        raise AuthenticationFailedError("unknown orchestrator")
    if not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
        raise AuthenticationFailedError("orchestrator proof mismatch")
    return OrchestratorPrincipal(orchestrator_id=claimed)


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


def may_enqueue(principal: object) -> bool:
    """Only a verified orchestrator service may enqueue actions.

    Same trust rule as :func:`may_authorize`: type check only, valid
    solely for :func:`verify_orchestrator` outputs. Ledger entry
    points verify proofs themselves.
    """
    return isinstance(principal, OrchestratorPrincipal)


def _scope_grants(
    scopes: dict[str, frozenset[str]] | None,
    service_id: str,
    action_type: object,
) -> bool:
    """Exact-match grant check over one parser-produced scope map."""
    if not isinstance(scopes, dict) or not isinstance(action_type, str):
        return False
    granted = scopes.get(service_id)
    if not isinstance(granted, (set, frozenset)):
        return False
    return action_type in granted


def may_claim_type(
    principal: object,
    action_type: str,
    scopes: dict[str, frozenset[str]] | None,
) -> bool:
    """May this verified executor claim this exact action type?

    Pure predicate over a caller-supplied map (normally
    :func:`parse_executor_scopes` output): role type AND exact
    grant membership. Unknown ids, unknown types, missing maps,
    and non-dict maps all deny.
    """
    if not isinstance(principal, ExecutorPrincipal):
        return False
    return _scope_grants(scopes, principal.executor_id, action_type)


def may_enqueue_type(
    principal: object,
    action_type: str,
    scopes: dict[str, frozenset[str]] | None,
) -> bool:
    """May this verified orchestrator enqueue this exact action type?

    Same shape as :func:`may_claim_type`, over the orchestrator
    namespace and :func:`parse_orchestrator_scopes` output.
    """
    if not isinstance(principal, OrchestratorPrincipal):
        return False
    return _scope_grants(scopes, principal.orchestrator_id, action_type)


def ensure_may_enqueue(principal: object) -> None:
    """Enforce the enqueue role policy (fail closed, explicit denial)."""
    if not may_enqueue(principal):
        raise AuthorizationDeniedError(
            "enqueueing an action requires a verified orchestrator service"
        )


def ensure_may_claim_type(
    principal: object,
    action_type: str,
    scopes: dict[str, frozenset[str]] | None = None,
) -> None:
    """Enforce scoped claim permission (loads env map when omitted)."""
    granted = load_executor_scopes() if scopes is None else scopes
    if not may_claim_type(principal, action_type, granted):
        raise AuthorizationDeniedError(
            "executor is not permitted to claim this action type"
        )


def ensure_may_enqueue_type(
    principal: object,
    action_type: str,
    scopes: dict[str, frozenset[str]] | None = None,
) -> None:
    """Enforce scoped enqueue permission (loads env map when omitted)."""
    granted = load_orchestrator_scopes() if scopes is None else scopes
    if not may_enqueue_type(principal, action_type, granted):
        raise AuthorizationDeniedError(
            "orchestrator is not permitted to enqueue this action type"
        )
