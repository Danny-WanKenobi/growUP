"""Shared operational dossier custody: models and pure policy.

One dossier identity per operational property, append-only versions,
immutable pre-handoff snapshots, and authenticated custody receipts.
The same receipt contract covers forward handoffs, returns, and human
review; only the routing rules differ.

Identity: ``dossier_id`` is a deterministic UUID derived from the
operational property UUID, so every agent computes the same identity
with no registry table and no merge step. Demo-namespace identifiers
are rejected: simulated outcomes can never enter operational custody.

Persistence lives in ``app/custody_db.py`` (migration 013). This module
holds the models, digests, receipt authentication, routing validation,
and credential readers shared by every caller.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any
from uuid import UUID, uuid5, NAMESPACE_URL

from pydantic import BaseModel, Field

from app.workflow import is_demo_id

HMAC_KEY_ENV = "CUSTODY_HMAC_KEY"
AMENDERS_ENV = "CUSTODY_AMENDERS"
MIN_HMAC_KEY_CHARS = 16
MAX_NOTE_CHARS = 500
MAX_REASON_CHARS = 500

DOSSIER_NAMESPACE_SEED = "growup:operational-dossier:v1:"

AGENTS = ("gabriel", "raphael", "onesimus", "michael", "uriel", "priscilla", "human_review")
RECEIPT_KINDS = ("forward", "return", "review")

# Canonical forward pipeline order. Returns move against it; human
# review stands outside it (escalate with kind="review", route back
# with kind="return").
_PIPELINE_ORDER = {
    "gabriel": 0,
    "raphael": 1,
    "onesimus": 2,
    "michael": 3,
    "uriel": 4,
    "priscilla": 5,
}

# Receipt pipeline versions. Stored rows keep the version they were
# written under so historical agent names retain their original
# meaning; new receipts always use PIPELINE_VERSION.
PIPELINE_VERSION_MVP1 = "mvp-1"
PIPELINE_VERSION = "canonical-1"

_MVP1_MEANINGS = {
    "gabriel": "Gabriel discovery and dossiers",
    "raphael": "Raphael inspection and authorization",
    "onesimus": "Onesimus engineering (pre-canonical: also computed economics, now Michael)",
    "michael": "Outreach intake (pre-canonical: function now owned by Uriel)",
    "human_review": "Human review",
}

_CANONICAL_MEANINGS = {
    "gabriel": "Gabriel discovery and dossiers",
    "raphael": "Raphael inspection and authorization",
    "onesimus": "Onesimus engineering scenarios",
    "michael": "Michael financial feasibility",
    "uriel": "Uriel opportunity and outreach packages",
    "priscilla": "Priscilla delivery and conversion tracking",
    "human_review": "Human review",
}


def interpret_custody_agent(pipeline_version: str, agent: str) -> dict[str, Any]:
    """Explain a stored agent name under its own pipeline version.

    Never rewrites history: an 'mvp-1' receipt naming Michael keeps its
    outreach-intake meaning instead of silently becoming financial.
    """
    if pipeline_version == PIPELINE_VERSION_MVP1:
        meanings = _MVP1_MEANINGS
    elif pipeline_version == PIPELINE_VERSION:
        meanings = _CANONICAL_MEANINGS
    else:
        raise CustodyError(f"unknown receipt pipeline version: {pipeline_version!r}")
    if agent not in meanings:
        raise CustodyError(f"unknown {pipeline_version} agent: {agent!r}")
    return {
        "agent": agent,
        "pipeline_version": pipeline_version,
        "meaning": meanings[agent],
        "historic": pipeline_version != PIPELINE_VERSION,
    }

SNAPSHOT_RETENTION_DAYS = 90


class CustodyError(ValueError):
    """Invalid custody request or unreadable custody state."""


class CustodyAuthError(CustodyError):
    """Amendment or receipt refused: missing/unknown authorizer or key."""


class StaleHandoffError(CustodyError):
    """A forward handoff named a superseded dossier version."""


class CustodyTamperError(CustodyError):
    """Stored custody bytes do not match their digest or MAC."""


class DossierVersion(BaseModel):
    """One immutable version of a shared operational dossier."""

    dossier_id: UUID
    version: int = 1
    property_id: UUID
    payload: dict[str, Any] = Field(default_factory=dict)
    digest: str = ""
    supersedes: int | None = None
    amendment: dict[str, Any] | None = None
    created_by: str = ""


class CustodySnapshot(BaseModel):
    """Frozen pre-handoff copy of one dossier version."""

    snapshot_id: UUID
    dossier_id: UUID
    version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    digest: str = ""
    archived: bool = False


class CustodyReceipt(BaseModel):
    """Authenticated handoff record: forward, return, or review."""

    receipt_id: UUID
    dossier_id: UUID
    version: int = 1
    snapshot_id: UUID
    kind: str = "forward"
    from_agent: str = ""
    to_agent: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    mac: str = ""
    prev_receipt: UUID | None = None
    pipeline_version: str = PIPELINE_VERSION_MVP1


def canonical_json(value: Any) -> str:
    """Deterministic JSON for digests and MACs."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def dossier_id_for_property(property_id: UUID | str) -> UUID:
    """Derive the one shared dossier identity for an operational property.

    Rejects demo-namespace and non-UUID identifiers: simulated upstream
    outcomes have no operational dossier.
    """
    if is_demo_id(property_id):
        raise CustodyError("demo identifiers have no operational dossier")
    try:
        prop = property_id if isinstance(property_id, UUID) else UUID(str(property_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CustodyError("property_id must be an operational UUID") from exc
    return uuid5(NAMESPACE_URL, f"{DOSSIER_NAMESPACE_SEED}{prop}")


def version_digest(
    *,
    property_id: UUID,
    dossier_id: UUID,
    version: int,
    payload: dict[str, Any],
    supersedes: int | None,
    amendment: dict[str, Any] | None,
    created_by: str,
) -> str:
    """Content digest for a dossier version (timestamps excluded)."""
    body = {
        "property_id": str(property_id),
        "dossier_id": str(dossier_id),
        "version": version,
        "payload": payload,
        "supersedes": supersedes,
        "amendment": amendment,
        "created_by": created_by,
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def snapshot_digest(payload: dict[str, Any]) -> str:
    """Digest over a frozen snapshot payload."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def receipt_core(
    *,
    dossier_id: UUID,
    version: int,
    snapshot_id: UUID,
    snapshot_digest: str,
    kind: str,
    from_agent: str,
    to_agent: str,
    note: str,
    prev_receipt: UUID | None,
    pipeline_version: str = PIPELINE_VERSION_MVP1,
    issued_by: str = "",
) -> dict[str, Any]:
    """The authenticated fields of a custody receipt (timestamps excluded).

    The 'mvp-1' core is frozen so historical MACs keep verifying;
    'canonical-1' additionally binds the pipeline version and the
    issuing principal.
    """
    if pipeline_version not in (PIPELINE_VERSION_MVP1, PIPELINE_VERSION):
        raise CustodyError(f"unknown receipt pipeline version: {pipeline_version!r}")
    core: dict[str, Any] = {
        "dossier_id": str(dossier_id),
        "version": version,
        "snapshot_id": str(snapshot_id),
        "snapshot_digest": snapshot_digest,
        "kind": kind,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "note": note,
        "prev_receipt": str(prev_receipt) if prev_receipt is not None else None,
    }
    if pipeline_version == PIPELINE_VERSION:
        core["pipeline_version"] = pipeline_version
        core["issued_by"] = issued_by
    return core


def hmac_key() -> bytes:
    """Server-side receipt-authentication key; fails closed when unusable."""
    raw = os.getenv(HMAC_KEY_ENV, "")
    if len(raw) < MIN_HMAC_KEY_CHARS:
        raise CustodyAuthError(
            f"{HMAC_KEY_ENV} is missing or too short; custody refusing to sign"
        )
    return raw.encode("utf-8")


def receipt_mac(core: dict[str, Any], key: bytes | None = None) -> str:
    """HMAC-SHA256 over the canonical receipt core."""
    return hmac.new(
        key if key is not None else hmac_key(),
        canonical_json(core).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_mac(core: dict[str, Any], mac: str, key: bytes | None = None) -> bool:
    """Constant-time MAC comparison."""
    expected = receipt_mac(core, key)
    return hmac.compare_digest(expected, mac)


def enrolled_amenders() -> frozenset[str]:
    """Handles permitted to author dossier amendments (empty = none)."""
    raw = os.getenv(AMENDERS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def check_amendment_permission(authorized_by: str, reason: str) -> None:
    """Refuse amendments without an enrolled authorizer and a reason."""
    handle = (authorized_by or "").strip()
    if not handle or handle not in enrolled_amenders():
        raise CustodyAuthError("amendment requires an enrolled authorizer")
    if not (reason or "").strip():
        raise CustodyError("amendment requires a reason")
    if len(reason) > MAX_REASON_CHARS:
        raise CustodyError("amendment reason too long")


def check_handoff_routing(kind: str, from_agent: str, to_agent: str) -> None:
    """Validate one receipt's routing under the shared contract.

    - forward: downstream along gabriel -> raphael -> onesimus ->
      michael -> uriel -> priscilla.
    - return: upstream against that order (rework, more evidence), or a
      human-review disposition routing back to a pipeline agent.
    - review: any pipeline agent escalates to human_review.
    """
    if kind not in RECEIPT_KINDS:
        raise CustodyError(f"unknown receipt kind: {kind!r}")
    if from_agent not in AGENTS or to_agent not in AGENTS:
        raise CustodyError("unknown handoff agent")
    if from_agent == to_agent:
        raise CustodyError("handoff requires two distinct agents")
    if kind == "review":
        if to_agent != "human_review" or from_agent == "human_review":
            raise CustodyError("review receipts escalate to human_review")
        return
    if kind == "return" and from_agent == "human_review":
        if to_agent not in _PIPELINE_ORDER:
            raise CustodyError("human_review dispositions return to a pipeline agent")
        return
    if "human_review" in (from_agent, to_agent):
        raise CustodyError("human_review moves only under kind='review' or return")
    before = _PIPELINE_ORDER[from_agent]
    after = _PIPELINE_ORDER[to_agent]
    if kind == "forward" and not after > before:
        raise CustodyError("forward handoffs move downstream only")
    if kind == "return" and not after < before:
        raise CustodyError("return handoffs move upstream only")


def check_jsonable(payload: Any) -> None:
    """Refuse payloads that cannot be stored deterministically."""
    try:
        canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise CustodyError("payload is not JSON-serializable") from exc
