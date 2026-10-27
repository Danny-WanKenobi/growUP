"""Centralized operation classification and durability enforcement (Phase 3C-1).

This module is the enforcement contract for Grow Up operations. It
classifies an operation and routes its audit event through the existing
observability path with class-appropriate failure handling. It performs
no business logic, executes no business operations, and mutates no
business state.

Classifications (explicit enum members -- never substring matching):

- PRELIMINARY: discovery, intake, enrichment, research, planning,
  dossiers. Validation and provenance stay mandatory, but audit
  failures are nonfatal: the operation reports its own outcome and
  observability degrades to best-effort.
- CONSEQUENTIAL_INTERNAL: investment approvals, financial commitments,
  deal-state changes. Durable auditing is mandatory: the operation
  must not report success unless its audit event reached PostgreSQL
  or the SQLite outbox.
- CONSEQUENTIAL_EXTERNAL: outreach, contractual actions, external
  financial instructions. Durable auditing is mandatory, and durable
  authorization must precede external execution.

Enforcement boundary:

- Preliminary operations use :func:`emit_nonfatal` (never raises on
  durability errors).
- Consequential operations use :func:`require_durable`, which raises
  :class:`DurabilityError` when neither durable store accepts the
  event. JSONL-only persistence NEVER satisfies a consequential
  audit, and consequential audits are never silently downgraded to
  nonfatal.
- Either confirmed PostgreSQL persistence or confirmed SQLite outbox
  acceptance counts as durable event acceptance; the recovery timer
  forwards outbox-accepted events to PostgreSQL.

CRITICAL LIMITATION -- durable acceptance is NOT atomicity. A
durably recorded audit event says nothing, by itself, about whether
a separate business database transaction committed. Do not claim
otherwise.

Explicitly deferred work (later phases, not this module):

- Phase 3C-2: consequential PostgreSQL mutations must commit the
  business mutation and its permanent audit event together in ONE
  PostgreSQL transaction. No generic wrapper here can provide that;
  each workflow must own its transaction.
- Phase 3C-3: consequential external actions must distinguish durable
  authorization, attempted execution, and confirmed outcome, with
  idempotency keys and safe recovery after ambiguous failures.

No existing workflow calls this module yet: Gabriel's research,
intake, and persistence paths are unchanged and stay on the legacy
nonfatal path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.observability.config import ObservabilityConfig
from app.observability.emitter import EmitOutcome, emit_nonfatal, require_durable
from app.observability.events import AgentEvent
from app.observability.jsonlog import JsonlSink
from app.observability.outbox import Outbox


class OperationClass(str, Enum):
    """Explicit operation classification for durability enforcement."""

    PRELIMINARY = "preliminary"
    CONSEQUENTIAL_INTERNAL = "consequential_internal"
    CONSEQUENTIAL_EXTERNAL = "consequential_external"


@dataclass(frozen=True)
class OperationPolicy:
    """Durability policy for one operation classification."""

    operation_class: OperationClass
    requires_durable_audit: bool
    summary: str


_POLICIES: dict[OperationClass, OperationPolicy] = {
    OperationClass.PRELIMINARY: OperationPolicy(
        operation_class=OperationClass.PRELIMINARY,
        requires_durable_audit=False,
        summary=(
            "Preliminary research and intake: observability is nonfatal. "
            "Validation, provenance, and existing persistence failures "
            "keep their current fatal behavior; only audit durability "
            "degrades to best-effort."
        ),
    ),
    OperationClass.CONSEQUENTIAL_INTERNAL: OperationPolicy(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        requires_durable_audit=True,
        summary=(
            "Consequential internal decisions (approvals, commitments, "
            "deal-state changes): the operation must fail closed unless "
            "its audit event is durably accepted. Same-transaction "
            "business+audit commits are deferred to Phase 3C-2."
        ),
    ),
    OperationClass.CONSEQUENTIAL_EXTERNAL: OperationPolicy(
        operation_class=OperationClass.CONSEQUENTIAL_EXTERNAL,
        requires_durable_audit=True,
        summary=(
            "Consequential external actions (outreach, contracts, "
            "external instructions): durable authorization must precede "
            "execution. Authorization/attempt/outcome lifecycle with "
            "idempotency is deferred to Phase 3C-3."
        ),
    ),
}


def policy_for(operation_class: OperationClass) -> OperationPolicy:
    """Return the durability policy for an explicit classification.

    The argument must be an :class:`OperationClass` member; strings,
    names, and substrings are rejected so callers cannot smuggle an
    operation into a weaker policy by misspelling it.
    """
    if not isinstance(operation_class, OperationClass):
        raise TypeError(
            "operation_class must be an OperationClass member, "
            f"got {type(operation_class).__name__}"
        )
    return _POLICIES[operation_class]


def audit_event(
    event: AgentEvent,
    operation_class: OperationClass,
    outbox: Outbox | None = None,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> EmitOutcome:
    """Request audit acceptance with class-appropriate failure handling.

    Preliminary operations route through :func:`emit_nonfatal` and
    never raise on durability errors. Consequential operations route
    through :func:`require_durable` and raise :class:`DurabilityError`
    when neither PostgreSQL nor the supplied outbox accepts the event.
    The classification controls ONLY whether durability failure
    raises; both paths use the same sinks, and the outbox (when
    supplied) is used on both paths.

    This function requests audit acceptance and nothing else: it does
    not execute, validate, or mutate any business operation, and a
    returned outcome is not proof that a business transaction
    committed. Future consequential workflows must check the outcome,
    handle :class:`DurabilityError` by aborting, and (from Phase 3C-2
    on) commit business and audit writes in one transaction.
    """
    policy = policy_for(operation_class)  # validates the classification
    if policy.requires_durable_audit:
        return require_durable(event, outbox=outbox, config=config, sink=sink)
    return emit_nonfatal(event, config=config, sink=sink, outbox=outbox)
