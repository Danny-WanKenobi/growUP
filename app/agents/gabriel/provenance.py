"""Durable research/dossier verdict events (G6-1).

Builds permanent, retrievable ``agent_events`` for final candidate
dispositions. Verdicts observe finished ranking/dossier outputs; they
never recompute grouping, scoring, or identity. Research mutates no
operational records, so every verdict carries
``mutation_status=NOT_ATTEMPTED`` and a NULL ``property_id``.

A candidate reference (grouping key + basis + run ordinal) is
descriptive research context, never proof of physical identity.
"""

from __future__ import annotations

import time

from app.agents.gabriel.dossier import DossierSet
from app.agents.gabriel.extractor import (
    ResearchIntelligence,
    ResultIntelligence,
    assembled_identity,
    group_by_property,
    identity_disputes,
)
from app.agents.gabriel.ranking import PropertyRanking, RankedCandidate
from app.observability import (
    BatchEmitOutcome,
    Outbox,
    OutboxConfigError,
    OutboxUnavailableError,
    get_observability_config,
    require_durable_batch,
)
from app.observability.config import ObservabilityConfig
from app.observability.events import (
    AgentEvent,
    AgentId,
    EvidenceRef,
    RetentionClass,
    Severity,
)
from app.observability.jsonlog import JsonlSink

RESEARCH_VERDICT_EVENT_TYPE = "gabriel.research.candidate_decision"
DOSSIER_VERDICT_EVENT_TYPE = "gabriel.dossier.verdict"

MUTATION_NOT_ATTEMPTED = "NOT_ATTEMPTED"

_MAX_UNRESOLVED = 20
_MAX_EVIDENCE_REFS = 50
_MAX_REF_CHARS = 2000
_MAX_SUMMARY_KEY_CHARS = 200

_RESEARCH_DECISIONS = {
    "pass": "PASS",
    "research_required": "RESEARCH_REQUIRED",
    "exclude": "EXCLUDED",
    "deferred": "DEFERRED",
}


def _normalized_from_key(group_key: str) -> dict[str, str | None]:
    """Normalized identity components encoded in an address group key."""
    found: dict[str, str | None] = {
        "street_address": None, "city": None, "state": None, "zip_code": None,
    }
    if group_key.startswith("addr:"):
        parts = group_key[len("addr:"):].split("|")
        if len(parts) == 4:
            for field, raw in zip(found, parts):
                found[field] = raw or None
    return found


def _candidate_ref(
    group_key: str, basis: str, ordinal: int,
    members: list[int], items: list[ResultIntelligence],
) -> dict:
    assignments = sorted({items[i].assignment_id for i in members if items[i].assignment_id})
    return {
        "group_key": group_key,  # full key; never truncated here
        "basis": basis,
        "ordinal": ordinal,
        "member_count": len(members),
        "assignment_ids": assignments,
    }


def _evidence_refs(
    items: list[ResultIntelligence], members: list[int]
) -> tuple[list[dict[str, str]], int]:
    urls = [items[i].result_url for i in members if items[i].result_url]
    refs = [{"kind": "source_url", "ref": url[:_MAX_REF_CHARS]} for url in urls[:_MAX_EVIDENCE_REFS]]
    return refs, len(urls)


def _to_evidence_refs(refs: list[dict[str, str]]) -> list[EvidenceRef]:
    return [EvidenceRef(kind=item["kind"], ref=item["ref"]) for item in refs]


def _exclusion_records(exclusions) -> list[dict]:
    """Legacy-compatible exclusion audit records (rule + evidence + date)."""
    records = []
    for exclusion in exclusions:
        records.append({
            "rule": exclusion.rule,
            "evidence": (exclusion.evidence or "")[:300],
            "date": getattr(exclusion, "date", None),
        })
    return records


def _short_key(group_key: str) -> str:
    if len(group_key) <= _MAX_SUMMARY_KEY_CHARS:
        return group_key
    return group_key[:_MAX_SUMMARY_KEY_CHARS] + "…"


def _build_event(
    *,
    event_type: str,
    run_id: str,
    correlation_id: str,
    decision: str,
    reason_codes: list[str],
    message: str,
    qc_status: str,
    insufficient_data: bool,
    candidate_ref: dict,
    observed: dict,
    inferred: dict,
    result: dict,
    unresolved: list[str],
    disputes: dict,
    evidence_refs: list[dict[str, str]],
    ranking_context: str,
    started: float | None,
    exclusions: list | None = None,
) -> AgentEvent:
    kept = unresolved[:_MAX_UNRESOLVED]
    details: dict = {
        "decision": decision,
        "reason_codes": reason_codes,
        "message": message,
        "qc_status": qc_status,
        "insufficient_data": insufficient_data,
        "candidate_ref": candidate_ref,
        "observed": observed,
        "inferred": inferred,
        "result": result,
        "unresolved": kept,
        "unresolved_truncated": max(0, len(unresolved) - len(kept)),
        "exclusions": _exclusion_records(exclusions or []),
        "identity_disputes": disputes,
        "ranking_context": ranking_context,
        "mutation_status": MUTATION_NOT_ATTEMPTED,
    }
    if started is not None:
        details["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return AgentEvent(
        agent=AgentId.GABRIEL,
        run_id=run_id,
        correlation_id=correlation_id,
        event_type=event_type,
        severity=Severity.INFO,
        summary=f"{message} [{_short_key(str(candidate_ref.get('group_key', '')))}]",
        details=details,
        evidence_refs=_to_evidence_refs(evidence_refs),
        retention_class=RetentionClass.PERMANENT,
    )


def build_research_verdicts(
    *,
    run_id: str,
    correlation_id: str,
    ranking: PropertyRanking,
    intel: ResearchIntelligence,
    started: float | None = None,
) -> list[AgentEvent]:
    """One permanent verdict per final ranked candidate, in rank order."""
    items = intel.results
    groups = {group.key: group for group in group_by_property(items)}
    verdicts: list[AgentEvent] = []
    for ordinal, candidate in enumerate(ranking.candidates):
        group = groups.get(candidate.key)
        members = list(group.members) if group is not None else []
        basis = group.basis if group is not None else "unknown"
        decision = _RESEARCH_DECISIONS.get(candidate.qc_status.value, "RESEARCH_REQUIRED")
        reason_codes = [e.rule for e in candidate.exclusions]
        reason_codes += [d.rule for d in candidate.deferrals]
        insufficient_data = not candidate.opportunity.sufficient
        if insufficient_data:
            reason_codes.append("INSUFFICIENT_DATA")
        disputes = dict(candidate.identity_disputes)
        if disputes:
            reason_codes.append("IDENTITY_DISPUTE")
        if not reason_codes:
            reason_codes.append(f"QC_{candidate.qc_status.value.upper()}")
        observed = assembled_identity(items, members, candidate.key)
        refs, total_refs = _evidence_refs(items, members)
        detail = candidate.value_detail
        try:
            opportunity_rank: int | None = ranking.opportunity_order.index(candidate.key) + 1
        except ValueError:
            opportunity_rank = None
        verdicts.append(_build_event(
            event_type=RESEARCH_VERDICT_EVENT_TYPE,
            run_id=run_id,
            correlation_id=correlation_id,
            decision=decision,
            reason_codes=reason_codes,
            message=f"research decision: {decision} ({reason_codes[0]})",
            qc_status=candidate.qc_status.value,
            insufficient_data=insufficient_data,
            candidate_ref=_candidate_ref(candidate.key, basis, ordinal, members, items),
            observed=dict(observed),
            inferred=_normalized_from_key(candidate.key),
            result={
                "opportunity_score": candidate.opportunity.score,
                "value_rank": candidate.value_rank,
                "opportunity_rank": opportunity_rank,
                "value_amount": detail.amount if detail else None,
                "source_ref_count": total_refs,
            },
            unresolved=list(candidate.unresolved),
            disputes=disputes,
            evidence_refs=refs,
            ranking_context="research_run",
            started=started,
            exclusions=list(candidate.exclusions),
        ))
    return verdicts


def build_dossier_verdicts(
    *,
    run_id: str,
    correlation_id: str,
    dossier_set: DossierSet,
    items: list[ResultIntelligence],
    started: float | None = None,
) -> list[AgentEvent]:
    """One permanent verdict per final dossier, in dossier order."""
    groups = {group.key: group for group in group_by_property(items)}
    verdicts: list[AgentEvent] = []
    for ordinal, dossier in enumerate(dossier_set.dossiers):
        group = groups.get(dossier.key)
        members = list(group.members) if group is not None else []
        basis = group.basis if group is not None else "unknown"
        excluded = dossier.qc_status.value == "exclude"
        decision = "EXCLUDED" if excluded else "READY"
        reason_codes = [e.rule for e in dossier.exclusions]
        if not excluded:
            reason_codes.append(f"QC_{dossier.qc_status.value.upper()}")
        elif not reason_codes:
            reason_codes.append("QC_EXCLUDE")
        if dossier.conflicts:
            reason_codes.append("HAS_CONFLICTS")
        disputes = identity_disputes(items, members, dossier.key)
        observed = assembled_identity(items, members, dossier.key)
        refs, total_refs = _evidence_refs(items, members)
        verdicts.append(_build_event(
            event_type=DOSSIER_VERDICT_EVENT_TYPE,
            run_id=run_id,
            correlation_id=correlation_id,
            decision=decision,
            reason_codes=reason_codes,
            message=f"dossier verdict: {decision} ({reason_codes[0]})",
            qc_status=dossier.qc_status.value,
            insufficient_data=False,
            candidate_ref=_candidate_ref(dossier.key, basis, ordinal, members, items),
            observed=dict(observed),
            inferred=_normalized_from_key(dossier.key),
            result={
                "value_amount": dossier.value_amount,
                "conflicts_count": len(dossier.conflicts),
                "handoff_agents": [h.agent for h in dossier.handoffs],
                "remaining_questions": len(dossier.remaining_questions),
                "source_ref_count": total_refs,
            },
            unresolved=list(dossier.missing),
            disputes=dict(disputes),
            evidence_refs=refs,
            ranking_context="dossier_construction",
            started=started,
            exclusions=list(dossier.exclusions),
        ))
    return verdicts


def persist_verdicts(
    events: list[AgentEvent],
    *,
    outbox: Outbox | None = None,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> BatchEmitOutcome:
    """Durably persist one execution's verdict batch (fail closed).

    An empty batch is an explicit no-op: no I/O, ``pending=False``.
    Otherwise the batch goes through atomic outbox acceptance plus
    idempotent PostgreSQL delivery; raises :class:`DurabilityError`
    when the verdicts reached no durable store.
    """
    if not events:
        return BatchEmitOutcome()
    cfg = config or get_observability_config()
    box = outbox
    if box is None:
        candidate = Outbox(cfg.outbox_path)
        try:
            candidate.initialize()
        except OutboxConfigError:
            raise  # programmer error: fail loud, never silently reroute
        except OutboxUnavailableError:
            box = None  # fall through to atomic PostgreSQL fallback
        else:
            box = candidate
    return require_durable_batch(events, outbox=box, config=cfg, sink=sink)
