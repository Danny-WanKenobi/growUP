"""Thin MVP orchestration for the real-property evaluation path.

Chains the owned stage modules (Gabriel finalize, custody handoffs,
Raphael authorize, Onesimus scenarios, Michael finance, Uriel
packages, optional Priscilla simulation) with resume-by-stage and
idempotent records. Disabled by default: ``MVP_WORKFLOW_ENABLED=1``
is required.

Stage machine per run: created -> gabriel -> raphael -> onesimus ->
uriel -> done. A re-invocation resumes from the persisted stage;
completed stages are never re-executed (no repeated provider calls,
no duplicate records). A withheld, incomplete, or unfavorable
assessment ends the run at `done` with status `complete_stopped`
and a visible reason: fail-closed success. Custody failures fail
the run loudly: an evaluation that cannot prove its handoff chain
cannot claim the connected pipeline.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from app.agents.gabriel.dossier import DossierRequest, build_dossiers
from app.agents.gabriel.extractor import extract_research_intelligence
from app.agents.gabriel.persist import (
    persist_dossier_versions,
    persist_qualifications,
    persist_research_artifacts,
)
from app.agents.gabriel.ranking import rank_intelligence
from app.agents.gabriel.sources.brave import BraveSearchResult
from app.agents.onesimus.crops import crop_profile
from app.agents.onesimus.service import ClassifiedInput, run_scenario_in_transaction
from app.agents.raphael.acquire import (
    acquire_evidence_in_transaction,
    begin_attempt,
    replay_artifact,
)
from app.agents.raphael.service import authorize_in_transaction
from app.custody_db import (
    create_version_in_transaction,
    issue_receipt_in_transaction,
    snapshot_in_transaction,
)
from app.priscilla import run_simulation_in_transaction
from app.uriel import prepare_package_in_transaction
from app.workflow import (
    DEMO_ID_PREFIX,
    KIND_CUSTODY_LINK,
    KIND_IDENTITY_REVIEW,
    KIND_MODEL_EXECUTION,
    digest_payload,
)
from app.workflow_db import (
    WorkflowError,
    advance_stage_in_transaction,
    get_record_in_transaction,
    get_run_in_transaction,
    list_records_in_transaction,
    put_record_in_transaction,
    set_status_in_transaction,
)

MVP_WORKFLOW_ENABLED_ENV = "MVP_WORKFLOW_ENABLED"


def workflow_enabled() -> bool:
    return os.getenv(MVP_WORKFLOW_ENABLED_ENV, "") == "1"


def _require_enabled() -> None:
    if not workflow_enabled():
        raise WorkflowError("MVP workflow entry points are disabled")


def _reconstruct_results(artifact: dict[str, Any]) -> list[BraveSearchResult]:
    items = []
    for stored in artifact.get("results", []) or []:
        items.append(
            BraveSearchResult.model_validate(
                {
                    "assignment_id": artifact.get("assignment_id", ""),
                    "query": artifact.get("query", ""),
                    "title": stored.get("title", ""),
                    "url": stored.get("url", ""),
                    "snippet": stored.get("snippet", ""),
                    "domain": stored.get("domain", ""),
                    "retrieved_at": stored.get("retrieved_at", "") or "1970-01-01T00:00:00+00:00",
                    "published_at": stored.get("published_at"),
                    "rank": stored.get("rank") or 1,
                }
            )
        )
    return items


def _dossier_address_signature(dossier: Any) -> tuple[str, str, str]:
    """Normalized (street, city, state) for one dossier ("" when missing)."""
    from app.agents.gabriel.dedup import normalize_text

    return (
        normalize_text(getattr(dossier, "street_address", None)),
        normalize_text(getattr(dossier, "city", None)),
        normalize_text(getattr(dossier, "state", None)),
    )


def associate_dossiers_with_property(
    dossiers: list[Any], target_id: str
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Associate at most one dossier with an operational property.

    The search target is context, not identity proof: a dossier is
    associated only when its own normalized address uniquely supports
    the target — exactly one dossier in the set matches, and the
    target address is unambiguous in storage (checked with the
    existing ``find_by_address``, which never selects among multiple
    matches). Anything else leaves every dossier unassociated;
    genuine ambiguity additionally returns a review request so an
    operator can resolve it instead of the pipeline guessing.
    """
    from app.agents.gabriel.dedup import normalize_text
    from app.properties_db import find_by_address, get_property

    try:
        target_row = get_property(UUID(target_id))
    except (ValueError, AttributeError, TypeError):
        return {}, None
    if target_row is None:
        return {}, None
    target_sig = (
        normalize_text(target_row.get("street_address")),
        normalize_text(target_row.get("city")),
        normalize_text(target_row.get("state")),
    )
    if not all(target_sig):
        return {}, None
    supporters = [
        str(getattr(dossier, "key", ""))
        for dossier in dossiers or []
        if _dossier_address_signature(dossier) == target_sig
    ]
    supporters = [key for key in supporters if key]
    if not supporters:
        return {}, None
    if len(supporters) > 1:
        return {}, {
            "reason": "multiple_dossiers_match_target",
            "property_id": target_id,
            "dossier_keys": sorted(supporters),
        }
    rows = find_by_address(
        street=target_sig[0], city=target_sig[1], state=target_sig[2],
        zip_code=normalize_text(target_row.get("zip_code")),
    )
    stored = [str(row.get("id", "")) for row in rows]
    if target_id not in stored:
        return {}, None
    if len(stored) > 1:
        return {}, {
            "reason": "target_address_shared_by_multiple_properties",
            "property_id": target_id,
            "candidate_ids": sorted(stored),
        }
    return {supporters[0]: target_id}, None


def _gabriel_stage(
    conn: Any,
    run_id: UUID,
    assignment_id: str,
    query: str,
    approved: bool,
    timeout_seconds: float,
    property_id: str | None = None,
) -> tuple[list[str], list[str], dict[str, Any] | None]:
    """Run acquisition/dossier/qualification; return keys, outcomes, review.

    ``property_id`` is explicit targeting context from the caller
    (e.g. the discovery runner evaluating one operational property).
    It is not identity proof: at most one dossier whose own address
    uniquely supports the target is associated with it (see
    :func:`associate_dossiers_with_property`); unrelated dossiers are
    preserved without that association, and ambiguity is recorded as
    an identity-review request for an operator instead of guessed.
    Raphael's verification policy still decides authorization from
    evidence alone.
    """
    target: str | None = None
    if property_id is not None:
        target = str(property_id).strip()
        if target.startswith(DEMO_ID_PREFIX):
            raise WorkflowError("demo identifiers cannot target real evaluation")
        try:
            UUID(target)
        except (ValueError, AttributeError, TypeError) as exc:
            raise WorkflowError("property_id must be an operational UUID") from exc
    existing = get_record_in_transaction(conn, run_id, "research_artifact", assignment_id)
    if existing is not None:
        if existing.payload.get("attempt") != "completed":
            raise WorkflowError(
                f"prior acquisition recorded as {existing.payload.get('attempt')}; "
                "retry requires a new assignment id"
            )
        replayed = existing.payload
    else:
        run = get_run_in_transaction(conn, run_id)
        started = [
            e for e in (run.errors if run else [])
            if e.get("assignment_id") == assignment_id and e.get("attempt") == "started"
        ]
        if started:
            raise WorkflowError(
                "ambiguous prior attempt crashed before completion; "
                "retry requires a new assignment id"
            )
        begin_attempt(run_id, assignment_id, approved=approved)
        artifact = acquire_evidence_in_transaction(
            conn, run_id, assignment_id, query,
            approved=approved, timeout_seconds=timeout_seconds,
        )
        if artifact.get("attempt") != "completed":
            raise WorkflowError(
                f"acquisition {artifact.get('attempt')}: {artifact.get('error')}"
            )
        replayed = replay_artifact(conn, run_id, assignment_id)
    intelligence = extract_research_intelligence(_reconstruct_results(replayed))
    ranking = rank_intelligence(intelligence)
    dossier_set = build_dossiers(DossierRequest(rounds=[intelligence]))

    class _Result:
        executed = [
            type(
                "Exec", (),
                {"assignment_id": assignment_id, "query": query},
            )()
        ]
        results = _reconstruct_results(replayed)
        errors: list[Any] = []
        retrieved_at = str(replayed.get("retrieved_at", ""))

    keys = persist_research_artifacts(conn, run_id, _Result())
    dossier_keys = [d.key for d in dossier_set.dossiers]
    association: dict[str, str] | None = None
    review: dict[str, Any] | None = None
    if target is not None:
        association, review = associate_dossiers_with_property(
            list(dossier_set.dossiers or []), target
        )
    persist_dossier_versions(
        conn, run_id, dossier_set, ranking, keys, property_ids=association
    )
    if review is not None:
        payload = {
            "reason": review["reason"],
            "property_id": review["property_id"],
            "dossier_keys": list(review.get("dossier_keys", [])),
            "candidate_ids": list(review.get("candidate_ids", [])),
            "assignment_id": assignment_id,
            "status": "pending",
        }
        put_record_in_transaction(
            conn, run_id, kind=KIND_IDENTITY_REVIEW,
            record_key="dossier-association", version=1,
            payload=payload, digest=digest_payload(payload),
        )
    outcomes = persist_qualifications(conn, run_id, dossier_keys)
    return dossier_keys, outcomes, review


def _site_inputs(raw: dict[str, Any]) -> dict[str, ClassifiedInput]:
    """Physical site inputs for Onesimus (financial figures go to Michael)."""
    site: dict[str, ClassifiedInput] = {}
    entry = (raw or {}).get("usable_area_m2")
    if isinstance(entry, dict):
        site["usable_area_m2"] = ClassifiedInput.model_validate(
            {"name": "usable_area_m2", **entry}
        )
    else:
        site["usable_area_m2"] = ClassifiedInput(
            name="usable_area_m2", value=None, classification="unknown"
        )
    return site


def _financial_bundle(raw: dict[str, Any], scenario: str) -> dict[str, Any]:
    """Financial bundle for Michael from caller-supplied raw entries."""
    from app.michael import financial_inputs_from_raw

    return financial_inputs_from_raw(raw or {}, scenario=scenario)


def _select_custody_target(
    conn: Any, run_id: UUID, dossier_key: str | None
) -> str | None:
    """Pick the dossier version custody should authorize.

    An explicit key wins. Otherwise prefer the dossier associated
    with an operational property (at most one exists); fall back to
    ``main``, then the first persisted key. Unassociated dossiers
    stay persisted and inspectable but cannot enter custody.
    """
    if dossier_key is not None:
        return dossier_key
    versions = list_records_in_transaction(conn, run_id, "dossier_version")
    for record in versions:
        payload = getattr(record, "payload", None) or {}
        if str(payload.get("property_id", "") or ""):
            return str(record.record_key)
    dossier = get_record_in_transaction(conn, run_id, "dossier_version", "main")
    if dossier is not None:
        return "main"
    return str(versions[0].record_key) if versions else None


def _link_in_transaction(
    conn: Any, run_id: UUID, dossier_key: str
) -> tuple[dict[str, Any], int]:
    """Latest custody-link payload and version for one dossier (resume-safe)."""
    record = get_record_in_transaction(conn, run_id, KIND_CUSTODY_LINK, dossier_key)
    if record is None:
        return {}, 0
    return dict(record.payload or {}), int(record.version or 0)


def _save_link_in_transaction(
    conn: Any, run_id: UUID, dossier_key: str, link: dict[str, Any], version: int
) -> dict[str, Any]:
    stored = put_record_in_transaction(
        conn, run_id, kind=KIND_CUSTODY_LINK, record_key=dossier_key,
        version=version + 1, payload=link, digest=digest_payload(link),
    )
    return dict(stored.payload or {})


def _ensure_receipt_in_transaction(
    conn: Any,
    run_id: UUID,
    dossier_key: str,
    *,
    dossier_id: Any,
    version: int,
    kind: str,
    from_agent: str,
    to_agent: str,
    principal: str,
    note: str,
) -> str:
    """Issue one custody receipt unless the link already records it."""
    link, link_version = _link_in_transaction(conn, run_id, dossier_key)
    receipts = dict(link.get("receipts") or {})
    slot = f"{kind}:{from_agent}->{to_agent}"
    if receipts.get(slot):
        return str(receipts[slot])
    receipt = issue_receipt_in_transaction(
        conn, dossier_id=dossier_id, version=version, kind=kind,
        from_agent=from_agent, to_agent=to_agent, issued_by=principal, note=note,
    )
    receipts[slot] = str(receipt.receipt_id)
    link["receipts"] = receipts
    _save_link_in_transaction(conn, run_id, dossier_key, link, link_version)
    return str(receipt.receipt_id)


def evaluate_real(
    conn: Any,
    run_id: UUID,
    assignment_id: str,
    query: str,
    site_inputs: dict[str, Any] | None = None,
    *,
    approved: bool = False,
    timeout_seconds: float = 12.0,
    dossier_key: str | None = None,
    principal: str = "mvp-coordinator",
    reviewed_by: str | None = None,
    run_simulation: bool = False,
    simulation_key: str | None = None,
    property_id: str | None = None,
) -> dict[str, Any]:
    """Run or resume the real-property evaluation (caller owns commit).

    Chain: Gabriel finalize -> custody version/snapshot -> Raphael
    authorize -> Onesimus scenarios (+Michael finance) -> Uriel
    package -> optional approved Priscilla simulation. ``reviewed_by``
    is an operator-supplied review attestation (never invented);
    without it packages stay draft and simulation is refused.
    ``property_id`` binds built dossiers to the operational property
    under evaluation (explicit caller targeting context).
    """
    _require_enabled()
    run = get_run_in_transaction(conn, run_id)
    if run is None:
        raise WorkflowError(f"unknown workflow run {run_id}")
    if run.status == "stopped":
        raise WorkflowError("run is stopped")
    summary: dict[str, Any] = {"run_id": str(run_id), "synthetic": False}
    if run.stage == "created":
        keys, outcomes, review = _gabriel_stage(
            conn, run_id, assignment_id, query, approved, timeout_seconds,
            property_id=property_id,
        )
        advance_stage_in_transaction(conn, run_id, "created", "gabriel")
        summary["dossier_keys"] = keys
        summary["qualification"] = dict(zip(keys, outcomes))
        summary["property_id"] = property_id or ""
        summary["identity_review"] = review or {}
        run = get_run_in_transaction(conn, run_id)
        assert run is not None
    if run.stage == "gabriel":
        target = _select_custody_target(conn, run_id, dossier_key)
        if target is None:
            raise WorkflowError("no persisted dossier to authorize")
        summary["dossier_key"] = target
        link, link_version = _link_in_transaction(conn, run_id, target)
        if not link:
            dossier_record = get_record_in_transaction(conn, run_id, "dossier_version", target)
            property_id = (
                (dossier_record.payload or {}).get("property_id", "")
                if dossier_record is not None else ""
            )
            if not property_id:
                advance_stage_in_transaction(conn, run_id, "gabriel", "done", status="complete_stopped")
                summary["stage"] = "done"
                custody = "blocked: dossier has no resolved property identity"
                review_record = get_record_in_transaction(
                    conn, run_id, KIND_IDENTITY_REVIEW, "dossier-association"
                )
                if review_record is not None:
                    reason = (review_record.payload or {}).get("reason", "association ambiguous")
                    custody += f"; identity review pending ({reason})"
                summary["custody"] = custody
                return summary
            stored_version = create_version_in_transaction(
                conn, property_id=str(property_id),
                payload={
                    "dossier": (dossier_record.payload or {}).get("dossier", {}),
                    "dossier_digest": dossier_record.digest if dossier_record else "",
                    "run_id": str(run_id),
                    "dossier_key": target,
                },
                created_by="gabriel",
            )
            snapshot_in_transaction(
                conn, dossier_id=stored_version.dossier_id, version=stored_version.version
            )
            link = _save_link_in_transaction(
                conn, run_id, target,
                {
                    "dossier_id": str(stored_version.dossier_id),
                    "custody_version": stored_version.version,
                    "property_id": str(property_id),
                    "dossier_key": target,
                    "receipts": {},
                },
                link_version,
            )
        dossier_uuid = UUID(str(link["dossier_id"]))
        custody_version = int(link.get("custody_version", 1))
        _ensure_receipt_in_transaction(
            conn, run_id, target, dossier_id=dossier_uuid,
            version=custody_version,
            kind="forward", from_agent="gabriel", to_agent="raphael",
            principal=principal, note="handoff to Raphael for assessment",
        )
        decision = authorize_in_transaction(conn, run_id, target)
        summary["decision"] = decision["outcome"]
        summary["decision_basis"] = decision["basis"]
        summary["dossier_id"] = str(dossier_uuid)
        if decision["outcome"] != "authorized":
            _ensure_receipt_in_transaction(
                conn, run_id, target, dossier_id=dossier_uuid,
                version=custody_version,
                kind="return", from_agent="raphael", to_agent="gabriel",
                principal=principal,
                note=f"Raphael {decision['outcome']}: {str(decision['basis'][0])[:160] if decision['basis'] else 'see basis'}",
            )
            advance_stage_in_transaction(conn, run_id, "gabriel", "done", status="complete_stopped")
            summary["stage"] = "done"
            return summary
        _ensure_receipt_in_transaction(
            conn, run_id, target, dossier_id=dossier_uuid,
            version=custody_version,
            kind="forward", from_agent="raphael", to_agent="onesimus",
            principal=principal, note="authorized for engineering scenarios",
        )
        advance_stage_in_transaction(conn, run_id, "gabriel", "raphael")
        summary["dossier_digest"] = decision["dossier_digest"]
        run = get_run_in_transaction(conn, run_id)
        assert run is not None
    if run.stage == "raphael":
        site = _site_inputs(site_inputs or {})
        crop = crop_profile()
        results = {}
        for config in ("bench", "rack3"):
            stored = run_scenario_in_transaction(
                conn, run_id, scenario_key=f"scenario-{config}",
                config_name=config, crop=crop, site=site,
                decision_digest=str(summary.get("dossier_digest") or _decision_digest(conn, run_id)),
                financial=_financial_bundle(site_inputs or {}, config),
            )
            finance = stored.get("financial_assessment", {}) or {}
            assessment = finance.get("assessment", {}) or {}
            results[config] = {
                "complete": stored["complete"],
                "missing": stored.get("missing", []),
                "financial_complete": bool(assessment.get("complete", False)),
                "financial_verdict": assessment.get("verdict", "insufficient_evidence"),
            }
        link, _ = _link_in_transaction(
            conn, run_id, str(summary.get("dossier_key") or dossier_key or "main")
        )
        if link:
            _ensure_receipt_in_transaction(
                conn, run_id, str(link.get("dossier_key", "main")),
                dossier_id=UUID(str(link["dossier_id"])),
                version=int(link.get("custody_version", 1)),
                kind="forward", from_agent="onesimus", to_agent="michael",
                principal=principal, note="scenarios ready for financial assessment",
            )
        advance_stage_in_transaction(conn, run_id, "raphael", "onesimus")
        summary["scenarios"] = results
        run = get_run_in_transaction(conn, run_id)
        assert run is not None
    if run.stage == "onesimus":
        target = str(summary.get("dossier_key") or dossier_key or "main")
        link, _ = _link_in_transaction(conn, run_id, target)
        if not link:
            raise WorkflowError("no custody link to package from")
        scenarios: dict[str, Any] = {}
        for config in ("bench", "rack3"):
            record = get_record_in_transaction(conn, run_id, "scenario", f"scenario-{config}")
            if record is not None:
                scenarios[config] = dict(record.payload or {})
        if not scenarios:
            raise WorkflowError("no scenarios to package")
        assessments = {
            config: ((payload.get("financial_assessment", {}) or {}).get("assessment", {}) or {})
            for config, payload in scenarios.items()
        }
        if not any(bool(item.get("complete", False)) for item in assessments.values()):
            advance_stage_in_transaction(conn, run_id, "onesimus", "done", status="complete_stopped")
            summary["stage"] = "done"
            summary["finance"] = "withheld: no complete Michael assessment"
            return summary
        dossier_uuid = UUID(str(link["dossier_id"]))
        custody_version = int(link.get("custody_version", 1))
        receipt_id = _ensure_receipt_in_transaction(
            conn, run_id, target, dossier_id=dossier_uuid,
            version=custody_version,
            kind="forward", from_agent="michael", to_agent="uriel",
            principal=principal, note="financial assessment ready for packaging",
        )
        if link.get("package"):
            package = dict(link["package"])
        else:
            dossier_record = get_record_in_transaction(conn, run_id, "dossier_version", target)
            dossier_dump = (
                (dossier_record.payload or {}).get("dossier", {})
                if dossier_record is not None else {}
            )
            unknowns: list[str] = []
            for payload in scenarios.values():
                for item in payload.get("missing", []) or []:
                    if item not in unknowns:
                        unknowns.append(str(item))
            package = prepare_package_in_transaction(
                conn, dossier_id=dossier_uuid, version=custody_version,
                receipt_id=receipt_id, prepared_by=principal,
                dossier=dict(dossier_dump) if isinstance(dossier_dump, dict) else {},
                engineering={
                    config: {
                        "complete": payload.get("complete"),
                        "outputs": payload.get("outputs", {}),
                    }
                    for config, payload in scenarios.items()
                },
                economics={
                    config: assessment for config, assessment in assessments.items()
                },
                unknowns=unknowns,
                blockers=[],
                next_actions=(
                    ["operator review required before outreach preparation"]
                    if not reviewed_by else []
                ),
                review={"reviewed_by": reviewed_by} if reviewed_by else None,
            )
            link["package"] = {
                "entry_id": package["entry_id"],
                "package_state": package["package_state"],
            }
            latest, latest_version = _link_in_transaction(conn, run_id, target)
            latest["package"] = link["package"]
            _save_link_in_transaction(conn, run_id, target, latest, latest_version)
        summary["package"] = link.get("package")
        advance_stage_in_transaction(conn, run_id, "onesimus", "uriel")
        run = get_run_in_transaction(conn, run_id)
        assert run is not None
    if run.stage == "uriel":
        target = str(summary.get("dossier_key") or dossier_key or "main")
        link, _ = _link_in_transaction(conn, run_id, target)
        package = dict((link or {}).get("package") or {})
        if run_simulation:
            if package.get("package_state") != "prepared":
                advance_stage_in_transaction(conn, run_id, "uriel", "done", status="complete_stopped")
                summary["stage"] = "done"
                summary["simulation"] = "withheld: simulation requires a prepared package"
                return summary
            key = simulation_key or f"{run_id}:{target}"
            sim = run_simulation_in_transaction(
                conn, idempotency_key=key, cohort=f"mvp:{run_id}",
                package_entries=[package["entry_id"]],
                actor=principal, seed=str(run_id),
            )
            summary["simulation"] = {
                "sim_id": sim["sim_id"],
                "replayed": sim["replayed"],
                "counts": sim["counts"],
            }
        else:
            summary["simulation"] = "not requested"
        advance_stage_in_transaction(conn, run_id, "uriel", "done", status="complete")
        summary["stage"] = "done"
        return summary
    summary["stage"] = run.stage
    return summary


def _decision_digest(conn: Any, run_id: UUID) -> str:
    decisions = list_records_in_transaction(conn, run_id, "raphael_decision")
    if not decisions:
        raise WorkflowError("no Raphael decision to resume from")
    return str(decisions[0].record_key)


def stop_run(conn: Any, run_id: UUID) -> None:
    """Stop further execution of a run (status only; records preserved)."""
    run = get_run_in_transaction(conn, run_id)
    if run is None:
        raise WorkflowError(f"unknown workflow run {run_id}")
    set_status_in_transaction(conn, run_id, "stopped")


def _evaluation_event(assignment_id: str, query: str, idempotency_key: str) -> Any:
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity

    return AgentEvent.model_validate(
        {
            "agent": AgentId.WONDRUE.value,
            "run_id": idempotency_key,
            "event_type": "wondrue.evaluation.completed",
            "severity": Severity.INFO.value,
            "summary": f"real-property evaluation executed for {assignment_id}",
            "details": {
                "assignment_id": assignment_id,
                "query": query,
                "idempotency_key": idempotency_key,
            },
            "evidence_refs": [],
            "retention_class": RetentionClass.PERMANENT.value,
        }
    )


def start_evaluation_audited(
    assignment_id: str,
    query: str,
    site_inputs: dict[str, Any] | None = None,
    *,
    approved: bool = False,
    timeout_seconds: float = 12.0,
    reviewed_by: str | None = None,
    run_simulation: bool = False,
    simulation_key: str | None = None,
    property_id: str | None = None,
) -> dict[str, Any]:
    """Create a run and evaluate it with its permanent audit event, atomically."""
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    from app.workflow_db import create_run_in_transaction

    _require_enabled()
    if not assignment_id or not query:
        raise WorkflowError("assignment_id and query are required")
    key = f"mvp-{digest_payload({'a': assignment_id, 'q': query})[:16]}"
    event = _evaluation_event(assignment_id, query, key)

    def _mutate(conn: Any) -> dict[str, Any]:
        run = create_run_in_transaction(
            conn, idempotency_key=key, limits={"brave_attempts": 9}
        )
        return evaluate_real(
            conn, run.run_id, assignment_id, query,
            site_inputs=site_inputs, approved=approved,
            timeout_seconds=timeout_seconds, reviewed_by=reviewed_by,
            run_simulation=run_simulation, simulation_key=simulation_key,
            property_id=property_id,
        )

    return run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event,
        mutate=_mutate,
    )


def stop_run_audited(run_id: UUID) -> None:
    """Stop a run with its permanent audit event, atomically."""
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction

    event = AgentEvent.model_validate(
        {
            "agent": AgentId.WONDRUE.value,
            "run_id": str(run_id),
            "event_type": "wondrue.evaluation.stopped",
            "severity": Severity.INFO.value,
            "summary": f"real-property evaluation stopped for run {run_id}",
            "details": {"run_id": str(run_id)},
            "evidence_refs": [],
            "retention_class": RetentionClass.PERMANENT.value,
        }
    )

    def _mutate(conn: Any) -> None:
        stop_run(conn, run_id)

    run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event,
        mutate=_mutate,
    )


def record_model_execution(
    conn: Any, run_id: UUID, record_key: str, result: Any
) -> dict[str, Any]:
    """Persist one model execution with live/replay provenance."""
    payload = result.as_record() if hasattr(result, "as_record") else dict(result)
    stored = put_record_in_transaction(
        conn, run_id, kind=KIND_MODEL_EXECUTION, record_key=record_key,
        version=1, payload=payload, digest=digest_payload(payload),
    )
    return stored.payload
