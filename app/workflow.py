"""Typed MVP workflow records (Gabriel -> Raphael -> Onesimus).

One evaluation run carries stable identity, stage/status, approved
limits, usage, and errors; each stage persists versioned JSON payloads
with content digests. Unknown facts stay unknown: every payload model
represents missing information explicitly rather than inventing it.

These models are storage contracts only. Stage transitions, policy
decisions, and calculations live in their owning modules.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

WORKFLOW_SCHEMA_VERSION = 1

# Record kinds persisted in workflow_records.
KIND_RESEARCH_ARTIFACT = "research_artifact"
KIND_DOSSIER_VERSION = "dossier_version"
KIND_QUALIFICATION = "qualification"
KIND_RAPHAEL_DECISION = "raphael_decision"
KIND_SCENARIO = "scenario"
KIND_MODEL_EXECUTION = "model_execution"
KIND_CUSTODY_LINK = "custody_link"
KIND_IDENTITY_REVIEW = "identity_review"

RECORD_KINDS = frozenset(
    {
        KIND_RESEARCH_ARTIFACT,
        KIND_DOSSIER_VERSION,
        KIND_QUALIFICATION,
        KIND_RAPHAEL_DECISION,
        KIND_SCENARIO,
        KIND_MODEL_EXECUTION,
        KIND_CUSTODY_LINK,
        KIND_IDENTITY_REVIEW,
    }
)


def digest_payload(payload: dict[str, Any]) -> str:
    """Canonical sha256 digest of a JSON-compatible payload (deterministic)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


DEMO_ID_PREFIX = "demo-"


def is_demo_id(value: Any) -> bool:
    """Whether an identifier belongs to the synthetic demonstration namespace."""
    return isinstance(value, str) and value.startswith(DEMO_ID_PREFIX)


class WorkflowRun(BaseModel):
    """One durable evaluation run."""

    run_id: UUID
    idempotency_key: str
    stage: str = "created"
    status: str = "open"
    correlation_id: str = ""
    limits: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowRecord(BaseModel):
    """One versioned stage artifact belonging to a run."""

    record_id: UUID
    run_id: UUID
    kind: str
    record_key: str
    version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    digest: str = ""


class ResearchArtifact(BaseModel):
    """Acquired evidence for one assignment/query (server-owned)."""

    provider: str
    assignment_id: str = ""
    query: str = ""
    results: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_at: str = ""
    collection_provenance: str = ""  # e.g. adapter_collected, human_reviewed
    attempt: str = "completed"  # completed | failed | ambiguous
    error: str = ""


class DossierVersion(BaseModel):
    """One complete immutable dossier version."""

    version: int = 1
    builder_version: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    dossier: dict[str, Any] = Field(default_factory=dict)
    property_id: str = ""  # resolved operational property UUID, else ""
    digest: str = ""


class Qualification(BaseModel):
    """Deterministic research qualification (separate from QC and Raphael)."""

    outcome: Literal["qualified", "not_qualified", "needs_research"] = "needs_research"
    policy_version: str = ""
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    county_credit: str = ""  # verified county name, else "" (no credit)


class RaphaelDecision(BaseModel):
    """Raphael authorization verdict bound to an exact dossier version."""

    outcome: Literal["authorized", "needs_evidence", "rejected"] = "needs_evidence"
    dossier_digest: str = ""
    dossier_version: int = 0
    basis: list[str] = Field(default_factory=list)
    policy_version: str = ""
    scope: str = ""
    limitations: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    """One Onesimus scenario: classified inputs, assumptions, outputs."""

    name: str = ""
    property_id: str = ""
    dossier_digest: str = ""
    decision_ref: str = ""
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    complete: bool = False
    calculation_version: str = ""


class ModelExecution(BaseModel):
    """One model invocation with live/replay provenance."""

    provider: str = ""
    model: str = ""
    mode: Literal["live", "replay"] = "replay"
    purpose: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    digests: dict[str, str] = Field(default_factory=dict)
    error: str = ""
