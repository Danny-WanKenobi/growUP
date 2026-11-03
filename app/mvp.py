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

import json
import os
import re
from typing import Any
from uuid import UUID

from app.agents.gabriel.dossier import DossierRequest, build_dossiers
from app.agents.gabriel.extractor import extract_research_intelligence
from app.agents.gabriel.persist import (
    persist_dossier_versions,
    persist_qualifications,
    persist_research_artifacts,
)
from app.agents.gabriel.producers import build_candidate_verifications
from app.agents.gabriel.ranking import rank_intelligence
from app.agents.gabriel.sources.brave import BraveSearchResult
from app.agents.onesimus.crops import crop_profile
from app.agents.onesimus.service import ClassifiedInput, run_scenario_in_transaction
from app.agents.raphael.acquire import (
    DEFAULT_ATTEMPT_LIMIT,
    MAX_ATTEMPT_LIMIT,
    ensure_artifact_owned,
    replay_artifact,
)
from app.agents.raphael.service import authorize_in_transaction
from app.custody_db import (
    create_version_in_transaction,
    current_custody_in_transaction,
    issue_receipt_in_transaction,
    snapshot_in_transaction,
    verify_chain_in_transaction,
    verify_receipt_in_transaction,
)
from app.priscilla import run_simulation_in_transaction
from app.uriel import prepare_package_in_transaction
from app.workflow import (
    DEMO_ID_PREFIX,
    KIND_CENSUS_CLAIM,
    KIND_CENSUS_COLLECTION,
    KIND_CENSUS_COUNTY,
    KIND_CENSUS_REFUSAL,
    KIND_CUSTODY_LINK,
    KIND_DCAD_ASSESSMENT,
    KIND_DCAD_CLAIM,
    KIND_DCAD_COLLECTION,
    KIND_DCAD_REFUSAL,
    KIND_DOSSIER_VERSION,
    KIND_IDENTITY_REVIEW,
    KIND_MODEL_EXECUTION,
    KIND_RESEARCH_ARTIFACT,
    digest_payload,
    is_demo_id,
)
from app.workflow_db import (
    WorkflowError,
    add_usage_in_transaction,
    advance_stage_in_transaction,
    append_error_in_transaction,
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
    from app.agents.gabriel.dedup import normalize_state, normalize_text

    return (
        normalize_text(getattr(dossier, "street_address", None)),
        normalize_text(getattr(dossier, "city", None)),
        normalize_state(getattr(dossier, "state", None)),
    )


def associate_dossiers_with_property(
    dossiers: list[Any], target_id: str
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Associate at most one dossier with an operational property.

    The search target is context, not identity proof: a dossier is
    associated only when its own normalized address uniquely supports
    the target â€” exactly one dossier in the set matches, and the
    target address is unambiguous in storage (checked with the
    existing ``find_by_address``, which never selects among multiple
    matches). Anything else leaves every dossier unassociated;
    genuine ambiguity additionally returns a review request so an
    operator can resolve it instead of the pipeline guessing.
    """
    from app.agents.gabriel.dedup import normalize_state, normalize_text
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
        normalize_state(target_row.get("state")),
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


def _apply_census_in_stage(
    conn: Any,
    run_id: UUID,
    dossier_set: Any,
    association: dict[str, str],
    target: str,
    census: dict[str, Any],
) -> None:
    """Apply retained Census county evidence to the associated dossier.

    Reads only (no transmit): loads the bound admission run's
    collection and county records, verifies integrity and
    digest/binding matches, and applies the admitted county to
    the one dossier associated with the targeted property. A
    successful application also persists the Census evidence
    locator (admission run, record keys, digests) on the
    dossier; without a valid locator the attribution is
    restored to its prior state. A refused application
    (conflicts, changed address, binding mismatch) is recorded
    as a run error entry and the dossier stays
    county-unresolved; tampered rows raise loudly like every
    other stored record. Never aborts the evaluation for
    a data-level refusal.
    """
    from uuid import UUID as _UUID

    from app.agents.gabriel.attribution import CountyAttribution
    from app.agents.gabriel.sources import census as census_module
    from app.properties_db import get_property

    try:
        admission_run = _UUID(str(census.get("collection_run_id", "") or ""))
    except (ValueError, AttributeError, TypeError):
        append_error_in_transaction(
            conn, run_id,
            {"census_county": "not_applied", "reason": "admission run id is invalid"},
        )
        return
    collection_record = None
    county_record = None
    for record in list_records_in_transaction(
        conn, admission_run, KIND_CENSUS_COLLECTION
    ):
        collection_record = record
        break
    for record in list_records_in_transaction(conn, admission_run, KIND_CENSUS_COUNTY):
        county_record = record
        break
    if collection_record is None or county_record is None:
        append_error_in_transaction(
            conn, run_id,
            {
                "census_county": "not_applied",
                "reason": "admission run carries no collection/county pair",
            },
        )
        return
    _require_record_integrity(collection_record, "Census collection")
    _require_record_integrity(county_record, "Census county result")
    if str(collection_record.digest or "") != str(
        census.get("collection_digest", "") or ""
    ) or str(county_record.digest or "") != str(census.get("county_digest", "") or ""):
        append_error_in_transaction(
            conn, run_id,
            {
                "census_county": "not_applied",
                "reason": "admission digests do not match the evaluation bindings",
            },
        )
        return
    try:
        target_uuid = _UUID(target)
    except (ValueError, AttributeError, TypeError):
        append_error_in_transaction(
            conn, run_id,
            {"census_county": "not_applied", "reason": "target is not an identity"},
        )
        return
    target_row = get_property(target_uuid)
    if target_row is None:
        append_error_in_transaction(
            conn, run_id,
            {"census_county": "not_applied", "reason": "target property row missing"},
        )
        return
    current_address = {
        "street": str(target_row.get("street_address", "") or ""),
        "city": str(target_row.get("city", "") or ""),
        "state": str(target_row.get("state", "") or ""),
        "zip": str(target_row.get("zip_code", "") or ""),
    }
    county_payload = dict(county_record.payload or {})
    for dossier in getattr(dossier_set, "dossiers", []) or []:
        if association.get(getattr(dossier, "key", ""), "") != target:
            continue
        outcome = census_module.apply_county_to_dossier(
            dossier.model_dump(mode="json"),
            county_payload,
            collection_digest=str(collection_record.digest or ""),
            property_id=target,
            current_address=current_address,
        )
        if outcome.get("applied"):
            prior_attribution = dossier.county_attribution
            dossier.county_attribution = CountyAttribution.model_validate(
                outcome["county_attribution"]
            )
            try:
                dossier.census_evidence = census_module.census_evidence_refs(
                    admission_run_id=str(admission_run),
                    collection_record_key=str(collection_record.record_key or ""),
                    county_record_key=str(county_record.record_key or ""),
                    collection_digest=str(collection_record.digest or ""),
                    county_digest=str(county_record.digest or ""),
                    county_result=county_payload,
                )
            except census_module.CensusError as exc:
                # No verified attribution without its locator: restore
                # the prior attribution and record the refusal.
                dossier.county_attribution = prior_attribution
                dossier.census_evidence = None
                append_error_in_transaction(
                    conn, run_id,
                    {
                        "census_county": "not_applied",
                        "dossier_key": str(getattr(dossier, "key", "") or ""),
                        "reason": f"census evidence locator refused: {exc}",
                    },
                )
        else:
            append_error_in_transaction(
                conn, run_id,
                {
                    "census_county": "not_applied",
                    "dossier_key": str(getattr(dossier, "key", "") or ""),
                    "reason": str(outcome.get("reason", "") or ""),
                },
            )


def load_census_evidence_for_dossier(
    conn: Any, dossier: dict[str, Any] | None
) -> dict[str, Any]:
    """Load and verify a dossier's referenced Census evidence (read-only).

    Follows only the dossier's persisted ``census_evidence``
    locator (admission run id, record keys, digests) â€” never URL
    text â€” then verifies stored-row integrity, digest equality,
    collector revisions, and the collection/county cross-binding.
    Raises WorkflowError when the locator is absent, malformed,
    or disagrees with the stored rows.
    """
    from app.agents.gabriel.sources import census as census_module

    refs = (dossier or {}).get("census_evidence") or {}
    if not isinstance(refs, dict):
        raise WorkflowError("dossier carries no census evidence locator")
    try:
        admission_run = UUID(str(refs.get("admission_run_id", "") or ""))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkflowError("census evidence locator has no admission run") from exc
    collection_key = str(refs.get("collection_record_key", "") or "")
    county_key = str(refs.get("county_record_key", "") or "")
    if not collection_key or not county_key:
        raise WorkflowError("census evidence locator has no record keys")
    collection_record = get_record_in_transaction(
        conn, admission_run, KIND_CENSUS_COLLECTION, collection_key
    )
    county_record = get_record_in_transaction(
        conn, admission_run, KIND_CENSUS_COUNTY, county_key
    )
    if collection_record is None or county_record is None:
        raise WorkflowError("census evidence rows are missing for this dossier")
    _require_record_integrity(collection_record, "Census collection")
    _require_record_integrity(county_record, "Census county result")
    if str(collection_record.digest or "") != str(
        refs.get("collection_digest", "") or ""
    ):
        raise WorkflowError("census collection digest does not match the locator")
    if str(county_record.digest or "") != str(refs.get("county_digest", "") or ""):
        raise WorkflowError("census county digest does not match the locator")
    county_payload = dict(county_record.payload or {})
    if (
        str(county_payload.get("collection_provenance", "") or "")
        != census_module.CENSUS_PROVENANCE
        or str(county_payload.get("collector_revision", "") or "")
        != census_module.CENSUS_COLLECTOR_REVISION
        or str(county_payload.get("assessment_version", "") or "")
        != census_module.CENSUS_COUNTY_VERSION
    ):
        raise WorkflowError("census evidence is not a supported collector revision")
    if str(county_payload.get("collection_digest", "") or "") != str(
        collection_record.digest or ""
    ):
        raise WorkflowError("census county does not match the stored collection")
    county_geo = county_payload.get("county", {}) or {}
    return {
        "admission_run_id": str(admission_run),
        "collection_record_key": collection_key,
        "county_record_key": county_key,
        "collection_digest": str(collection_record.digest or ""),
        "county_digest": str(county_record.digest or ""),
        "county": str(county_geo.get("canonical", "") or ""),
        "territory": str(county_geo.get("territory", "") or ""),
    }


def _official_gabriel_stage(
    conn: Any, run_id: UUID, official_ref: dict[str, Any], property_id: str | None = None,
    census: dict[str, Any] | None = None,
) -> tuple[list[str], list[str], dict[str, Any] | None]:
    """Admit retained official discovery without any Brave call."""
    from app.agents.gabriel.sources.official_records import (
        admit_official_in_transaction, load_retained_official_in_transaction)

    target = str(official_ref.get("property_id") or property_id or "")
    if property_id and target and str(property_id) != target:
        raise WorkflowError("official evidence property does not match evaluation property")
    retained = load_retained_official_in_transaction(
        conn, discovery_run_id=str(official_ref.get("discovery_run_id", "")),
        event_id=str(official_ref.get("event_id", "")),
        property_id=target,
        expected_digest=official_ref.get("evidence_digest"))
    keys, outcomes = admit_official_in_transaction(conn, run_id, retained, census=census)
    return keys, outcomes, None


def _gabriel_stage(
    conn: Any,
    run_id: UUID,
    assignment_id: str,
    query: str,
    approved: bool,
    timeout_seconds: float,
    property_id: str | None = None,
    census: dict[str, Any] | None = None,
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
        # Owned acquisition: both budgets commit and the provider
        # transmits with no business transaction open, so the caller's
        # transaction never spans an external call. The helper
        # re-checks committed state authoritatively (the run must be
        # durably committed before this stage runs with a fresh id).
        ensure_artifact_owned(
            run_id, assignment_id, query,
            approved=approved, timeout_seconds=timeout_seconds,
        )
        replayed = replay_artifact(conn, run_id, assignment_id)
    intelligence = extract_research_intelligence(_reconstruct_results(replayed))
    ranking = rank_intelligence(intelligence)
    if str(replayed.get("collection_provenance", "")) == "adapter_collected":
        # Shared verification policy, same as the research path: genuine
        # adapter provenance only, never listing claims as facts.
        annotations = build_candidate_verifications(ranking, intelligence)
        for candidate in ranking.candidates:
            candidate.category_verification = annotations.get(candidate.key)
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
    if census is not None and target is not None and association:
        _apply_census_in_stage(
            conn, run_id, dossier_set, association, target, census
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
    """Only declared physical inputs; money remains owned by Michael."""
    from app.agents.onesimus.engineering import SITE_UNITS
    site = {}
    for name, unit in SITE_UNITS.items():
        entry = (raw or {}).get(name)
        if isinstance(entry, dict):
            site[name] = ClassifiedInput.model_validate(
                {"name": name, "unit": unit,
                 "classification": "assumed" if entry.get("value") is not None else "unknown", **entry})
    if "usable_area_m2" not in site:
        site["usable_area_m2"] = ClassifiedInput(name="usable_area_m2", value=None, unit="m2")
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
    verify_chain_in_transaction(conn, UUID(str(dossier_id)))
    link, link_version = _link_in_transaction(conn, run_id, dossier_key)
    receipts = dict(link.get("receipts") or {})
    slot = f"{kind}:{from_agent}->{to_agent}"
    if receipts.get(slot):
        cached = verify_receipt_in_transaction(conn, UUID(str(receipts[slot])))
        if (cached.dossier_id != UUID(str(dossier_id)) or cached.version != version
                or cached.from_agent != from_agent or cached.to_agent != to_agent or cached.kind != kind):
            raise WorkflowError("cached custody receipt does not match the requested handoff")
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
    census: dict[str, Any] | None = None,
    stop_after_raphael: bool = False,
    official_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run or resume the real-property evaluation (caller owns commit).

    Chain: Gabriel finalize -> custody version/snapshot -> Raphael
    authorize -> Onesimus scenarios (+Michael finance) -> Uriel
    package -> optional approved Priscilla simulation. ``reviewed_by``
    is an operator-supplied review attestation (never invented);
    without it packages stay draft and simulation is refused.
    ``property_id`` binds built dossiers to the operational property
    under evaluation (explicit caller targeting context).

    The run must already be durably committed when a fresh assignment
    still needs acquisition: the Gabriel stage acquires through
    owned connections (provider outside any transaction), which can
    only see committed runs. :func:`start_evaluation_audited`
    commits the run and the artifact before opening the coordinated
    transaction; direct callers on PostgreSQL must do the same.
    """
    _require_enabled()
    if not isinstance(stop_after_raphael, bool):
        raise WorkflowError("stop_after_raphael must be a boolean")
    run = get_run_in_transaction(conn, run_id)
    if run is None:
        raise WorkflowError(f"unknown workflow run {run_id}")
    if run.status == "stopped":
        raise WorkflowError("run is stopped")
    summary: dict[str, Any] = {"run_id": str(run_id), "synthetic": False}
    if official_ref is not None and run.stage != "created":
        from app.agents.gabriel.sources.official_records import load_retained_official_in_transaction
        artifacts = [record for record in list_records_in_transaction(conn, run_id, KIND_RESEARCH_ARTIFACT)
                     if (record.payload or {}).get("collection_provenance")
                     in ("official_retained", "generic_retained")]
        if len(artifacts) != 1:
            raise WorkflowError("official replay lacks one retained evidence binding")
        artifact = artifacts[0]
        bound = artifact.payload
        for name in ("discovery_run_id", "event_id", "property_id"):
            supplied = official_ref.get(name) or (property_id if name == "property_id" else None)
            if str(supplied or "") != str(bound.get(name) or ""):
                raise WorkflowError("official replay cannot change its evidence identity")
        if (property_id and str(property_id) != str(bound["property_id"])) or (
            official_ref.get("evidence_digest") and official_ref["evidence_digest"] != bound["evidence_digest"]
        ):
            raise WorkflowError("official replay evidence digest/property mismatch")
        _require_record_integrity(artifact, "official artifact")
        load_retained_official_in_transaction(
            conn, discovery_run_id=bound["discovery_run_id"], event_id=bound["event_id"],
            property_id=bound["property_id"], expected_digest=bound["evidence_digest"])
        summary["official"] = {name: bound[name] for name in
                               ("discovery_run_id", "event_id", "property_id", "evidence_digest")}
    if (run.stage == "raphael" and run.status == "complete_inspected") or (
        run.stage == "done" and run.status == "complete_stopped"
    ):
        target = _select_custody_target(conn, run_id, dossier_key)
        decision_record = next((record for record in list_records_in_transaction(
            conn, run_id, "raphael_decision") if (record.payload or {}).get("dossier_key") == target), None)
        decision = decision_record.payload if decision_record is not None else {}
        link, _ = _link_in_transaction(conn, run_id, target)
        summary.update(stage=run.stage, dossier_key=target,
                       dossier_id=(link or {}).get("dossier_id"),
                       decision=decision.get("outcome"), decision_basis=decision.get("basis", []))
        summary["qualification"] = {r.record_key: (r.payload or {}).get("outcome")
                                    for r in list_records_in_transaction(conn, run_id, "qualification")}
        if link and link.get("dossier_id"):
            custody = current_custody_in_transaction(conn, UUID(str(link["dossier_id"])))
            summary["current_holder"] = (custody or {}).get("holder")
        summary["feedback_reason"] = next(iter(decision.get("basis") or []), decision.get("outcome"))
        return summary
    if run.stage == "created":
        if official_ref is not None:
            keys, outcomes, review = _official_gabriel_stage(
                conn, run_id, official_ref, property_id=property_id, census=census)
            summary["official"] = {
                "discovery_run_id": str(official_ref.get("discovery_run_id", "")),
                "event_id": str(official_ref.get("event_id", "")),
                "property_id": str(official_ref.get("property_id") or property_id or ""),
            }
        else:
            keys, outcomes, review = _gabriel_stage(
                conn, run_id, assignment_id, query, approved, timeout_seconds,
                property_id=property_id, census=census,
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
        verify_chain_in_transaction(conn, dossier_uuid)
        from app.agents.raphael.inspection import build_inspection_report
        from app.dossier_reports import append_agent_report_in_transaction
        current_record = get_record_in_transaction(conn, run_id, "dossier_version", target)
        if current_record is None:
            raise WorkflowError("missing dossier for inspection report")
        if not (current_record.payload.get("dossier", {}).get("reports") or {}).get("raphael"):
            inspection = build_inspection_report(current_record.payload, site_inputs=site_inputs)
            current_record, enhanced = append_agent_report_in_transaction(
                conn, run_id, target, agent="raphael", report=inspection)
            custody_version = enhanced.version
        else:
            custody_version = int(_link_in_transaction(conn, run_id, target)[0]["custody_version"])
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
            summary["current_holder"] = "gabriel"
            summary["feedback_reason"] = decision["basis"][0] if decision["basis"] else decision["outcome"]
            return summary
        if stop_after_raphael or (getattr(run, "limits", {}) or {}).get("stop_after_raphael"):
            advance_stage_in_transaction(conn, run_id, "gabriel", "raphael", status="complete_inspected")
            summary["stage"] = "raphael"
            summary["current_holder"] = "raphael"
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
        target = str(summary.get("dossier_key") or dossier_key or "main")
        link, _ = _link_in_transaction(conn, run_id, target)
        if not link:
            raise WorkflowError("missing shared custody for engineering")
        verify_chain_in_transaction(conn, UUID(str(link["dossier_id"])))
        incoming = current_custody_in_transaction(conn, UUID(str(link["dossier_id"])))
        if not incoming or incoming["holder"] != "onesimus":
            raise WorkflowError("Onesimus must receive authenticated custody before engineering")
        current_record = get_record_in_transaction(conn, run_id, "dossier_version", target)
        inspection = ((current_record.payload.get("dossier") or {}).get("reports") or {}).get("raphael") if current_record else None
        if not inspection:
            raise WorkflowError("structured Raphael inspection report required")
        site = _site_inputs(inspection.get("physical_inputs") or {})
        crop = crop_profile()
        results = {}
        for config in ("bench", "rack3"):
            stored = run_scenario_in_transaction(
                conn, run_id, scenario_key=f"scenario-{config}",
                config_name=config, crop=crop, site=site,
                decision_digest=str(summary.get("dossier_digest") or _decision_digest(conn, run_id)),
                observed_occupancy=str(inspection.get("observed_occupancy") or ""),
            )
            finance = stored.get("financial_assessment", {}) or {}
            assessment = finance.get("assessment", {}) or {}
            results[config] = {
                "complete": stored["complete"],
                "missing": stored.get("missing", []),
                "financial_complete": bool(assessment.get("complete", False)),
                "financial_verdict": assessment.get("verdict", "insufficient_evidence"),
            }
        from app.agents.onesimus.report import build_engineering_report
        from app.dossier_reports import append_agent_report_in_transaction
        scenario_records = {config: get_record_in_transaction(conn, run_id, "scenario", f"scenario-{config}").payload
                            for config in ("bench", "rack3")}
        engineering_report = build_engineering_report(
            scenario_records, inspection=inspection,
            options=inspection.get("engineering_options") or {})
        append_agent_report_in_transaction(conn, run_id, target, agent="onesimus", report=engineering_report)
        link, _ = _link_in_transaction(conn, run_id, target)
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
        verify_chain_in_transaction(conn, UUID(str(link["dossier_id"])))
        holder = current_custody_in_transaction(conn, UUID(str(link["dossier_id"])))
        if not holder or holder["holder"] != "michael":
            raise WorkflowError("Michael must receive the enhanced dossier before assessment")
        from app.michael import assess as michael_assess
        assessments = {}
        for config, payload in scenarios.items():
            assessment = (payload.get("financial_assessment", {}) or {}).get("assessment")
            if assessment is None:
                assessment = michael_assess(config, payload, _financial_bundle(site_inputs or {}, config))
                updated = dict(payload)
                updated["financial_assessment"] = {"assessed": True, "assessment": assessment}
                previous_scenario = get_record_in_transaction(conn, run_id, "scenario", f"scenario-{config}")
                put_record_in_transaction(conn, run_id, kind="scenario", record_key=f"scenario-{config}",
                                          version=previous_scenario.version+1, payload=updated, digest=digest_payload(updated))
                scenarios[config] = updated
                from app.pilot import cell_from_dossier, canonical_quota_id, record_transition_in_transaction
                current_dossier = get_record_in_transaction(conn, run_id, "dossier_version", target)
                cell = cell_from_dossier(current_dossier.payload.get("dossier") or {}) if current_dossier else None
                if cell:
                    record_transition_in_transaction(
                        conn, county=cell[0], category=cell[1],
                        dossier_id=canonical_quota_id(current_dossier.payload["property_id"]),
                        transition="financially_assessed" if assessment.get("complete") else "incomplete",
                        run_id=run_id, record_key=target)
            assessments[config] = assessment
            if config in (summary.get("scenarios") or {}):
                summary["scenarios"][config].update(financial_complete=assessment.get("complete", False),
                                                     financial_verdict=assessment.get("verdict", "insufficient_evidence"))
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


def evaluation_key(assignment_id: str, query: str) -> str:
    """Deterministic idempotency key for one evaluation (resume-safe)."""
    return f"mvp-{digest_payload({'a': assignment_id, 'q': query})[:16]}"


def validate_attempt_limit(value: Any) -> int:
    """Validate an explicit per-run Brave attempt bound (1..18)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowError("brave_attempt_limit must be an integer 1..18")
    if value < 1 or value > MAX_ATTEMPT_LIMIT:
        raise WorkflowError("brave_attempt_limit must be an integer 1..18")
    return value


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
    brave_attempt_limit: int = DEFAULT_ATTEMPT_LIMIT,
    census_attempt_limit: int = 0,
    census_principal: str | None = None,
    stop_after_raphael: bool = False,
    official_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one assignment in committed phases plus an audited chain.

    Phase 1 commits the run (durably visible before any other
    connection accounts an attempt). Phase 2 commits the evidence
    artifact: budget charges and the started-marker commit before the
    provider transmits, and transmission happens with no business
    transaction open. Phase 2b optionally collects Census county
    evidence for the targeted property (committed claim that
    reserves per-run usage BEFORE transmission, transmit outside
    any transaction, retained collection/county result); a
    refused or ambiguous Census attempt never aborts the
    evaluation and never triggers a replacement request. Only
    the claim winner charges usage, exactly once, so burst
    accounting reads durable deltas that no crash can undercount
    and no recovery can double-count. Phase 3
    runs the remaining chain inside the coordinated transaction,
    where the Gabriel stage replays the committed artifacts
    instead of transmitting. The permanent audit event commits if
    and only if the coordinated chain commits, so it always
    describes actually committed stages.

    ``brave_attempt_limit`` (default 9, ordinary allowance) bounds
    Brave attempts for a newly created run; a resumed run keeps its
    persisted limit (re-invocation never widens it). A crash between
    phases resumes losslessly: a completed artifact replays without a
    new charge, while a started-marker without an artifact (an
    uncertain provider request) refuses the same assignment id.
    ``census_attempt_limit`` (default 0, disabled) allows at most
    one Census address request for the targeted property; enabling
    it requires ``census_principal`` attribution.
    """
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    from app.workflow_db import create_run

    _require_enabled()
    if not isinstance(stop_after_raphael, bool):
        raise WorkflowError("stop_after_raphael must be a boolean")
    if not assignment_id or (not query and official_ref is None):
        raise WorkflowError("assignment_id and query are required")
    if official_ref is not None and not query:
        query = assignment_id
    limit = validate_attempt_limit(brave_attempt_limit)
    census_limit = validate_census_attempt_limit(census_attempt_limit)
    if census_limit > 0 and not str(census_principal or "").strip():
        raise WorkflowError("census collection requires an authenticated principal")
    key = evaluation_key(assignment_id, query)
    event = _evaluation_event(assignment_id, query, key)
    limits = {"brave_attempts": limit}
    if stop_after_raphael:
        limits["stop_after_raphael"] = True
    if official_ref is not None:
        from app.properties_db import get_connection as _official_connection
        from app.agents.gabriel.sources.official_records import load_retained_official_in_transaction
        with _official_connection() as conn:
            retained = load_retained_official_in_transaction(
                conn, discovery_run_id=str(official_ref.get("discovery_run_id") or ""),
                event_id=str(official_ref.get("event_id") or ""),
                property_id=str(property_id or official_ref.get("property_id") or ""),
                expected_digest=official_ref.get("evidence_digest"))
        official_ref = {name: retained[name] for name in
                        ("discovery_run_id", "event_id", "property_id", "evidence_digest")}
        limits["official_ref"] = official_ref
    run = create_run(idempotency_key=key, limits=limits)
    if bool((run.limits or {}).get("stop_after_raphael", False)) != stop_after_raphael:
        raise WorkflowError("evaluation replay cannot change its inspection boundary")
    if (run.limits or {}).get("official_ref") != official_ref:
        raise WorkflowError("evaluation replay cannot change its official evidence binding")
    if official_ref is None:
        # Official admission replays retained evidence: no Brave call,
        # no provider budget charge, no snippet reconstruction.
        ensure_artifact_owned(
            run.run_id, assignment_id, query,
            approved=approved, timeout_seconds=timeout_seconds,
        )
    census_status: dict[str, Any] | None = None
    census_bindings: dict[str, Any] | None = None
    if census_limit > 0 and property_id is not None:
        census_status = _collect_census_for_evaluation(
            str(property_id), principal=str(census_principal).strip(),
            spend_run_id=run.run_id,
        )
        if census_status["outcome"] in ("collected", "replayed"):
            census_bindings = {
                "collection_run_id": census_status["admission_run_id"],
                "collection_digest": census_status["collection_digest"],
                "county_digest": census_status["county_digest"],
            }

    def _mutate(conn: Any) -> dict[str, Any]:
        return evaluate_real(
            conn, run.run_id, assignment_id, query,
            site_inputs=site_inputs, approved=approved,
            timeout_seconds=timeout_seconds, reviewed_by=reviewed_by,
            run_simulation=run_simulation, simulation_key=simulation_key,
            property_id=property_id, census=census_bindings, stop_after_raphael=stop_after_raphael,
            official_ref=official_ref,
        )

    result = run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event,
        mutate=_mutate,
    )
    if census_status is not None:
        result["census"] = {
            name: census_status.get(name)
            for name in (
                "outcome", "charged", "county", "territory",
                "reason", "admission_run_id",
            )
            if name in census_status
        }
    return result


def release_engineering_in_transaction(conn: Any, run_id: UUID, *, principal: str,
                                      site_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Explicit release of one authorized inspection hold; no acquisition.

    The original inspection-only run limits stay immutable. This separate
    approval is retained with a principal and exact inputs; replay is read-only.
    """
    _require_enabled()
    if not str(principal or "").strip():
        raise WorkflowError("downstream release requires an authenticated operator")
    run = get_run_in_transaction(conn, run_id, for_update=True)
    if run is None:
        raise WorkflowError("unknown inspected run")
    approval = {"principal": principal, "site_inputs": site_inputs or {},
                "original_limits": run.limits, "run_id": str(run_id)}
    prior = get_record_in_transaction(conn, run_id, KIND_RESEARCH_ARTIFACT, "engineering-release", 1)
    if prior is not None:
        if (prior.digest != digest_payload(prior.payload)
                or prior.payload.get("approval") != approval):
            raise WorkflowError("engineering release replay changed its operator or inputs")
        return prior.payload["result"]
    if run.stage != "raphael" or run.status != "complete_inspected":
        raise WorkflowError("only an authorized inspection-only hold can be released")
    target = _select_custody_target(conn, run_id, None)
    if not target:
        raise WorkflowError("held run has no dossier")
    link, _ = _link_in_transaction(conn, run_id, target)
    if not link:
        raise WorkflowError("held run has no custody")
    dossier_id = UUID(str(link["dossier_id"]))
    verify_chain_in_transaction(conn, dossier_id)
    custody = current_custody_in_transaction(conn, dossier_id)
    if not custody or custody["holder"] != "raphael":
        raise WorkflowError("Raphael must hold the inspected dossier")
    record = get_record_in_transaction(conn, run_id, KIND_DOSSIER_VERSION, target)
    if record is None:
        raise WorkflowError("held dossier is missing")
    from app.agents.raphael.inspection import build_inspection_report
    from app.dossier_reports import append_agent_report_in_transaction
    inspection = (record.payload.get("dossier", {}).get("reports") or {}).get("raphael")
    if inspection is None:
        record, _ = append_agent_report_in_transaction(
            conn, run_id, target, agent="raphael",
            report=build_inspection_report(record.payload, site_inputs=site_inputs))
    else:
        from app.agents.onesimus.engineering import SITE_UNITS
        supplied = {k:v for k,v in (site_inputs or {}).items() if k in SITE_UNITS}
        if supplied:
            proposed = build_inspection_report(record.payload, site_inputs=supplied)["physical_inputs"]
            for name, value in proposed.items():
                frozen = (inspection.get("physical_inputs") or {}).get(name, {})
                if any(value.get(k) != frozen.get(k) for k in ("value", "unit", "classification")):
                    raise WorkflowError("physical inputs changed since inspection; a new assessment is required")
        if "engineering_options" in (site_inputs or {}) and site_inputs["engineering_options"] != inspection.get("engineering_options", {}):
            raise WorkflowError("engineering options changed since inspection")
    decision = authorize_in_transaction(conn, run_id, target)
    if decision["outcome"] != "authorized":
        raise WorkflowError("held dossier no longer satisfies Raphael authorization")
    link, _ = _link_in_transaction(conn, run_id, target)
    _ensure_receipt_in_transaction(conn, run_id, target, dossier_id=dossier_id,
                                  version=int(link["custody_version"]), kind="forward",
                                  from_agent="raphael", to_agent="onesimus", principal=principal,
                                  note="explicit operator release of authorized inspection hold")
    set_status_in_transaction(conn, run_id, "open")
    result = evaluate_real(conn, run_id, f"release:{run_id}", "retained inspection release",
                           site_inputs=site_inputs, dossier_key=target, principal=principal)
    payload = {"approval": approval, "result": result}
    put_record_in_transaction(conn, run_id, kind=KIND_RESEARCH_ARTIFACT,
                              record_key="engineering-release", version=1,
                              payload=payload, digest=digest_payload(payload))
    return result


def release_engineering_audited(run_id: UUID, *, principal: str,
                                site_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event = AgentEvent(agent=AgentId.WONDRUE, run_id=str(run_id),
                       event_type="wondrue.engineering.released", severity=Severity.INFO,
                       retention_class=RetentionClass.PERMANENT,
                       summary="Explicit release of one authorized dossier to engineering")
    return run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL, event=event,
        mutate=lambda conn: release_engineering_in_transaction(
            conn, run_id, principal=principal, site_inputs=site_inputs))


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


# --- DCAD collection/admission + same-dossier successor ------------------------

SUCCESSOR_REVISION = "successor-1"


def _dcad_claim_scope(dossier: dict[str, Any]) -> str:
    """Claim scope from retained category attribution ("" when not single)."""
    category = dossier.get("category_attribution") or {}
    status = str(category.get("status", "")).lower()
    if status == "verified":
        return str(category.get("verified", "") or "")
    if status == "observed":
        return str(category.get("observed", "") or "")
    return ""


def _dcad_record_key(account: str) -> str:
    return f"dcad:{account}"


def _require_operational_uuid(value: Any, name: str) -> UUID:
    text = str(value or "").strip()
    if text.startswith(DEMO_ID_PREFIX):
        raise WorkflowError(f"demo identifiers cannot target {name}")
    try:
        return UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkflowError(f"{name} must be an operational UUID") from exc


def _dcad_run_key(
    *,
    account: str,
    revision: str,
    property_id: str,
    parent_run_id: str,
    dossier_key: str,
    attempt: str,
) -> str:
    """Deterministic admission-run key binding account, scope, and attempt."""
    return "dcad-" + digest_payload(
        {
            "account": str(account),
            "revision": str(revision),
            "property_id": str(property_id),
            "parent_run_id": str(parent_run_id),
            "dossier_key": str(dossier_key),
            "attempt": str(attempt),
        }
    )[:16]


def preview_dcad_collection_in_transaction(
    conn: Any,
    *,
    account: str,
    property_id: str,
    parent_run_id: str,
    dossier_key: str,
    attempt: str = "1",
) -> dict[str, Any]:
    """Read-only collection/admission preview (no writes, no network)."""
    from app.agents.gabriel.sources import dcad as dcad_module
    from app.properties_db import get_property
    from app.workflow_db import get_run_by_key_in_transaction

    try:
        url = dcad_module.dcad_account_url(account)
    except dcad_module.DcadError as exc:
        raise WorkflowError(str(exc)) from exc
    prop_uuid = _require_operational_uuid(property_id, "property_id")
    parent_uuid = _require_operational_uuid(parent_run_id, "parent_run_id")
    if not str(dossier_key or "").strip() or is_demo_id(str(dossier_key)):
        raise WorkflowError("dossier_key must be an operational key")
    attempt_label = str(attempt or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,2}", attempt_label):
        raise WorkflowError("attempt must be an integer 1..999")
    parent = get_record_in_transaction(
        conn, parent_uuid, KIND_DOSSIER_VERSION, str(dossier_key)
    )
    if parent is None:
        raise WorkflowError("parent dossier version not found")
    if str((parent.payload or {}).get("property_id", "") or "") != str(prop_uuid):
        raise WorkflowError("parent dossier is not bound to this property")
    target_row = get_property(prop_uuid)
    dossier = dict((parent.payload or {}).get("dossier", {}) or {})
    key = _dcad_run_key(
        account=str(account).strip(),
        revision=dcad_module.DCAD_COLLECTOR_REVISION,
        property_id=str(prop_uuid),
        parent_run_id=str(parent_uuid),
        dossier_key=str(dossier_key),
        attempt=attempt_label,
    )
    prior: dict[str, Any] | None = None
    existing_run = get_run_by_key_in_transaction(conn, key)
    if existing_run is not None:
        state = _dcad_collect_state(conn, existing_run.run_id, str(account).strip())
        if state["collection"] is not None and state["assessment"] is not None:
            prior_state = "completed"
        elif state["refusal"] is not None and state["collection"] is not None:
            prior_state = "refused"
        elif state["collection"] is not None:
            prior_state = "retained-awaiting-assessment"
        elif state["claim"] is not None:
            prior_state = "uncertain"
        else:
            prior_state = "created"
        prior = {
            "run_id": str(existing_run.run_id),
            "stage": existing_run.stage,
            "state": prior_state,
        }
    return {
        "account": str(account).strip(),
        "url": url,
        "attempt": attempt_label,
        "collection_enabled": dcad_module.collection_enabled(),
        "account_allowlisted": str(account).strip() in dcad_module.allowed_accounts(),
        "limits": {
            "requests": 1,
            "timeout_seconds": dcad_module.DCAD_TIMEOUT_SECONDS,
            "max_body_bytes": dcad_module.DCAD_MAX_BODY_BYTES,
            "retries": 0,
            "redirects": "refused",
        },
        "property": {
            "id": str(prop_uuid),
            "street_address": (target_row or {}).get("street_address", ""),
        },
        "parent": {
            "run_id": str(parent_uuid),
            "dossier_key": str(dossier_key),
            "version": parent.version,
            "digest": parent.digest,
            "claim_scope": _dcad_claim_scope(dossier),
        },
        "prior_attempt": prior,
        "would_store": ["dcad_collection", "dcad_assessment"],
        "collection_provenance": "adapter_collected (collector-minted only)",
        "network": "one HTTPS GET on approved collect only",
        "writes": "none in preview",
    }


def _dcad_event(
    verb: str,
    account: str,
    property_id: str,
    parent_run_id: str,
    dossier_key: str,
    key: str,
    extra: dict[str, Any] | None = None,
) -> Any:
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity

    summaries = {
        "collected": f"DCAD account {account} collected for property {property_id}",
        "assessed": f"DCAD account {account} assessed for property {property_id}",
        "refused": f"DCAD account {account} refused for property {property_id}",
    }
    return AgentEvent.model_validate(
        {
            "agent": AgentId.WONDRUE.value,
            "run_id": key,
            "event_type": f"wondrue.dcad.{verb}",
            "severity": Severity.INFO.value,
            "summary": summaries[verb],
            "details": {
                "account": account,
                "property_id": property_id,
                "parent_run_id": parent_run_id,
                "dossier_key": dossier_key,
                "idempotency_key": key,
                **(extra or {}),
            },
            "evidence_refs": [],
            "retention_class": RetentionClass.PERMANENT.value,
        }
    )


def _dcad_claim_key(account: str) -> str:
    return f"dcad:{account}:claim"


def _dcad_collect_state(conn: Any, run_id: UUID, account: str) -> dict[str, Any]:
    """Read the durable collection state for one admission run."""
    record_key = _dcad_record_key(account)
    return {
        "claim": get_record_in_transaction(conn, run_id, KIND_DCAD_CLAIM, _dcad_claim_key(account)),
        "collection": get_record_in_transaction(conn, run_id, KIND_DCAD_COLLECTION, record_key),
        "assessment": get_record_in_transaction(conn, run_id, KIND_DCAD_ASSESSMENT, record_key),
        "refusal": get_record_in_transaction(conn, run_id, KIND_DCAD_REFUSAL, record_key),
    }


def _require_record_integrity(record: Any, what: str) -> None:
    """Recompute a stored payload digest; refuse tampered rows loudly.

    Comparing a stored digest only against a supplied string cannot
    catch a row whose payload and digest were changed together (or
    a payload edited in place). Dossier-family payloads carry an
    embedded ``digest`` that is excluded from the recomputation,
    matching the writer convention.
    """
    payload = dict(record.payload or {})
    content = {key: value for key, value in payload.items() if key != "digest"}
    if digest_payload(content) != str(record.digest or ""):
        raise WorkflowError(f"stored {what} fails digest verification")


def _dcad_replay_summary(run_id: UUID, state: dict[str, Any]) -> dict[str, Any]:
    _require_record_integrity(state["collection"], "DCAD collection")
    _require_record_integrity(state["assessment"], "DCAD assessment")
    assessment = dict(state["assessment"].payload or {})
    verdict = assessment.get("dcad_only_verification", {}) or {}
    return {
        "run_id": str(run_id),
        "replayed": True,
        "collection_digest": str(state["collection"].digest or ""),
        "assessment_digest": str(state["assessment"].digest or ""),
        "aligned": bool(assessment.get("alignment", {}).get("aligned")),
        "dcad_only_outcome": verdict.get("outcome", ""),
    }


def _dcad_raise_stored_refusal(state: dict[str, Any]) -> Any:
    _require_record_integrity(state["collection"], "DCAD collection")
    _require_record_integrity(state["refusal"], "DCAD refusal")
    refusal = dict(state["refusal"].payload or {})
    raise WorkflowError(
        f"prior collection refused at {refusal.get('stage', 'unknown')}: "
        f"{refusal.get('reason', 'unknown')}"
    )


def _dcad_insert_claim(
    conn: Any, run_id: UUID, account: str, principal: str, revision: str
) -> bool:
    """Insert the exclusive transmission claim; False when already held.

    Exactly one caller per admission run wins the claim and may
    transmit. Losers re-read the durable state and replay, surface
    the stored refusal, or refuse on uncertainty -- they never
    transmit. Insert-or-nothing (not put_record): racers carry
    distinct timestamps, so a digest comparison would be meaningless.
    """
    from datetime import datetime, timezone

    from app.workflow_db import insert_record_if_absent_in_transaction

    payload = {
        "state": "started",
        "account": account,
        "collector_revision": revision,
        "principal": principal,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return (
        insert_record_if_absent_in_transaction(
            conn, run_id, kind=KIND_DCAD_CLAIM,
            record_key=_dcad_claim_key(account), version=1,
            payload=payload, digest=digest_payload(payload),
        )
        is not None
    )


def collect_dcad_audited(
    *,
    account: str,
    property_id: str,
    parent_run_id: str,
    dossier_key: str,
    attempt: str = "1",
    principal: str,
    approved: bool = False,
    config: Any = None,
) -> dict[str, Any]:
    """Collect and assess one DCAD account in committed phases.

    Validation (enabled, allowlisted, bound parent/property)
    refuses before any claim or transmission. A committed
    exclusive claim then serializes transmission: concurrent
    identical calls permit at most one request, completed
    evidence replays without a request, and an uncertain
    started claim cannot silently retransmit (recovery is an
    explicit new attempt, approval-bound). The bounded response
    is retained before parsing, so a parser/assessment refusal
    is recorded inspectably without erasing the bytes. No
    business transaction stays open during HTTP. Crash between
    transmit and retention loses the bytes and stays uncertain
    for that key (documented limitation).
    """
    from app.agents.gabriel.sources import dcad as dcad_module
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    from app.properties_db import get_connection, get_property
    from app.workflow_db import create_run

    _require_enabled()
    if not approved:
        raise WorkflowError("DCAD collection requires explicit approval")
    if not str(principal or "").strip():
        raise WorkflowError("DCAD collection requires an authenticated principal")
    try:
        dcad_module.dcad_account_url(account)
        dcad_module.require_collection_allowed(account)
    except dcad_module.DcadError as exc:
        raise WorkflowError(str(exc)) from exc
    prop_uuid = _require_operational_uuid(property_id, "property_id")
    parent_uuid = _require_operational_uuid(parent_run_id, "parent_run_id")
    if not str(dossier_key or "").strip() or is_demo_id(str(dossier_key)):
        raise WorkflowError("dossier_key must be an operational key")
    attempt_label = str(attempt or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,2}", attempt_label):
        raise WorkflowError("attempt must be an integer 1..999")
    cleaned = str(account).strip()
    key = _dcad_run_key(
        account=cleaned,
        revision=dcad_module.DCAD_COLLECTOR_REVISION,
        property_id=str(prop_uuid),
        parent_run_id=str(parent_uuid),
        dossier_key=str(dossier_key),
        attempt=attempt_label,
    )
    run = create_run(idempotency_key=key, config=config)
    conn = get_connection(config)
    try:
        parent = get_record_in_transaction(
            conn, parent_uuid, KIND_DOSSIER_VERSION, str(dossier_key)
        )
        if parent is None:
            raise WorkflowError("parent dossier version not found")
        if str((parent.payload or {}).get("property_id", "") or "") != str(prop_uuid):
            raise WorkflowError("parent dossier is not bound to this property")
        target_row = get_property(prop_uuid)
        if target_row is None:
            raise WorkflowError("unknown operational property")
        _require_record_integrity(parent, "parent dossier version")
        state = _dcad_collect_state(conn, run.run_id, cleaned)
        if state["collection"] is not None and state["assessment"] is not None:
            return _dcad_replay_summary(run.run_id, state)
        if state["refusal"] is not None and state["collection"] is not None:
            _dcad_raise_stored_refusal(state)
        if state["collection"] is not None:
            _require_record_integrity(state["collection"], "DCAD collection")
            resume_collection = state["collection"]
        else:
            if state["claim"] is not None:
                raise WorkflowError(
                    "uncertain prior collection attempt for this key; "
                    "approve an explicit new attempt to retry"
                )
            resume_collection = None
        claim_scope = _dcad_claim_scope(dict((parent.payload or {}).get("dossier", {}) or {}))
        street = str(target_row.get("street_address", "") or "")
        property_id_text = str(target_row.get("id", "") or prop_uuid)
        if resume_collection is None:
            claimed = _dcad_insert_claim(
                conn, run.run_id, cleaned, str(principal),
                dcad_module.DCAD_COLLECTOR_REVISION,
            )
            conn.commit()
            if not claimed:
                state = _dcad_collect_state(conn, run.run_id, cleaned)
                if state["collection"] is not None and state["assessment"] is not None:
                    return _dcad_replay_summary(run.run_id, state)
                if state["refusal"] is not None and state["collection"] is not None:
                    _dcad_raise_stored_refusal(state)
                raise WorkflowError(
                    "another caller holds the collection claim for this key"
                )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if resume_collection is None:
        try:
            collected = dcad_module.collect_account(cleaned, principal=str(principal))
        except dcad_module.DcadError as exc:
            raise WorkflowError(str(exc)) from exc
        collected_payload = collected.model_dump(mode="json")
        collect_event = _dcad_event(
            "collected", cleaned, str(prop_uuid), str(parent_uuid), str(dossier_key), key
        )

        def _retain(mutate_conn: Any) -> dict[str, Any]:
            live = get_run_in_transaction(mutate_conn, run.run_id)
            if live is None:
                raise WorkflowError(f"unknown workflow run {run.run_id}")
            if live.status == "stopped":
                raise WorkflowError("run is stopped")
            stored = put_record_in_transaction(
                mutate_conn, run.run_id, kind=KIND_DCAD_COLLECTION,
                record_key=_dcad_record_key(cleaned), version=1,
                payload=collected_payload, digest=digest_payload(collected_payload),
            )
            return {
                "collection_digest": str(stored.digest or ""),
                "collection": dict(stored.payload or {}),
            }

        retained = run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=collect_event,
            mutate=_retain,
        )
    else:
        retained = {
            "collection_digest": str(resume_collection.digest or ""),
            "collection": dict(resume_collection.payload or {}),
        }
    try:
        assessment = dcad_module.build_dcad_assessment(
            collection=retained["collection"],
            collection_digest=retained["collection_digest"],
            property_record={"id": property_id_text, "street_address": street},
            dossier_key=str(dossier_key),
            claim_scope=claim_scope,
        )
    except dcad_module.DcadError as exc:
        refusal = {
            "account": cleaned,
            "collection_digest": retained["collection_digest"],
            "stage": "assess",
            "reason": str(exc),
            "collector_revision": dcad_module.DCAD_COLLECTOR_REVISION,
            "principal": str(principal).strip(),
        }
        refuse_event = _dcad_event(
            "refused", cleaned, str(prop_uuid), str(parent_uuid), str(dossier_key), key,
            extra={"reason": str(exc)},
        )

        def _refuse(mutate_conn: Any) -> dict[str, Any]:
            from app.workflow_db import WorkflowConflictError

            live = get_run_in_transaction(mutate_conn, run.run_id)
            if live is None:
                raise WorkflowError(f"unknown workflow run {run.run_id}")
            if live.stage == "done":
                state = _dcad_collect_state(mutate_conn, run.run_id, cleaned)
                if (
                    state["refusal"] is None
                    or state["refusal"].digest != digest_payload(refusal)
                ):
                    raise WorkflowConflictError(
                        "admission run finished with different evidence"
                    )
                _dcad_raise_stored_refusal(state)
            put_record_in_transaction(
                mutate_conn, run.run_id, kind=KIND_DCAD_REFUSAL,
                record_key=_dcad_record_key(cleaned), version=1,
                payload=refusal, digest=digest_payload(refusal),
            )
            advance_stage_in_transaction(
                mutate_conn, run.run_id, "created", "done", status="complete_stopped"
            )
            return {"refused": True}

        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=refuse_event,
            mutate=_refuse,
        )
        raise WorkflowError(f"DCAD assessment refused: {exc}") from exc
    assess_event = _dcad_event(
        "assessed", cleaned, str(prop_uuid), str(parent_uuid), str(dossier_key), key
    )

    def _assess(mutate_conn: Any) -> dict[str, Any]:
        from app.workflow_db import WorkflowConflictError

        live = get_run_in_transaction(mutate_conn, run.run_id)
        if live is None:
            raise WorkflowError(f"unknown workflow run {run.run_id}")
        if live.stage == "done":
            state = _dcad_collect_state(mutate_conn, run.run_id, cleaned)
            if (
                state["assessment"] is None
                or state["assessment"].digest != digest_payload(assessment)
            ):
                raise WorkflowConflictError(
                    "admission run finished with different evidence"
                )
            return _dcad_replay_summary(run.run_id, state)
        stored_assessment = put_record_in_transaction(
            mutate_conn, run.run_id, kind=KIND_DCAD_ASSESSMENT,
            record_key=_dcad_record_key(cleaned), version=1,
            payload=assessment, digest=digest_payload(assessment),
        )
        advance_stage_in_transaction(mutate_conn, run.run_id, "created", "done", status="complete")
        verdict = assessment.get("dcad_only_verification", {}) or {}
        return {
            "run_id": str(run.run_id),
            "replayed": False,
            "collection_digest": retained["collection_digest"],
            "assessment_digest": str(stored_assessment.digest or ""),
            "aligned": bool(assessment.get("alignment", {}).get("aligned")),
            "dcad_only_outcome": verdict.get("outcome", ""),
        }

    return run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=assess_event,
        mutate=_assess,
    )


def validate_census_attempt_limit(value: Any) -> int:
    """Validate an explicit per-run Census attempt bound (0..1).

    One evaluation targets one property, so at most one Census
    address request per run is structurally meaningful; 0 keeps
    county admission disabled for the run.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowError("census_attempt_limit must be an integer 0..1")
    if value < 0 or value > 1:
        raise WorkflowError("census_attempt_limit must be an integer 0..1")
    return value


class _CensusChargedError(WorkflowError):
    """A Census attempt failed after its allowance was consumed.

    The claim commit reserves the attempt's allowance before
    transmission, so every exit past the claim â€” ambiguous
    transmit, assessment refusal, or a concurrent completion of
    this caller's own claim â€” reports through this subclass.
    ``charged`` tells whether THIS call consumed the allowance
    (claim winners) as opposed to reusing an earlier claim
    (resumes and replays). Subclasses WorkflowError so direct
    callers see a uniform refusal; the evaluation phase catches
    this subclass to record the outcome without aborting.
    """

    def __init__(self, message: str, *, charged: bool = True) -> None:
        super().__init__(message)
        self.charged = bool(charged)


def _census_event(
    verb: str,
    property_id: str,
    address: str,
    attempt: str,
    key: str,
    extra: dict[str, Any] | None = None,
) -> Any:
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity

    summaries = {
        "collected": f"Census address collected for property {property_id}",
        "assessed": f"Census county assessed for property {property_id}",
        "refused": f"Census county refused for property {property_id}",
    }
    return AgentEvent.model_validate(
        {
            "agent": AgentId.WONDRUE.value,
            "run_id": key,
            "event_type": f"wondrue.census.{verb}",
            "severity": Severity.INFO.value,
            "summary": summaries[verb],
            "details": {
                "property_id": property_id,
                "address": address,
                "attempt": attempt,
                "idempotency_key": key,
                **(extra or {}),
            },
            "evidence_refs": [],
            "retention_class": RetentionClass.PERMANENT.value,
        }
    )


def _census_collect_state(
    conn: Any, run_id: UUID, record_key: str
) -> dict[str, Any]:
    """Read the durable collection state for one admission run."""
    return {
        "claim": get_record_in_transaction(
            conn, run_id, KIND_CENSUS_CLAIM, f"{record_key}:claim"
        ),
        "collection": get_record_in_transaction(
            conn, run_id, KIND_CENSUS_COLLECTION, record_key
        ),
        "county": get_record_in_transaction(
            conn, run_id, KIND_CENSUS_COUNTY, record_key
        ),
        "refusal": get_record_in_transaction(
            conn, run_id, KIND_CENSUS_REFUSAL, record_key
        ),
    }


def _census_insert_claim(
    conn: Any,
    run_id: UUID,
    record_key: str,
    property_id: str,
    principal: str,
    revision: str,
) -> bool:
    """Insert the exclusive transmission claim; False when already held.

    Exactly one caller per admission run wins the claim and may
    transmit. Losers re-read the durable state and replay, surface
    the stored refusal, or refuse on uncertainty -- they never
    transmit. Insert-or-nothing (not put_record): racers carry
    distinct timestamps, so a digest comparison would be meaningless.
    """
    from datetime import datetime, timezone

    from app.workflow_db import insert_record_if_absent_in_transaction

    payload = {
        "state": "started",
        "property_id": property_id,
        "collector_revision": revision,
        "principal": principal,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return (
        insert_record_if_absent_in_transaction(
            conn, run_id, kind=KIND_CENSUS_CLAIM,
            record_key=f"{record_key}:claim", version=1,
            payload=payload, digest=digest_payload(payload),
        )
        is not None
    )


def _census_replay_summary(
    run_id: UUID, state: dict[str, Any], *, charged: bool = False
) -> dict[str, Any]:
    _require_record_integrity(state["collection"], "Census collection")
    _require_record_integrity(state["county"], "Census county result")
    county = dict((state["county"].payload or {}).get("county", {}) or {})
    return {
        "run_id": str(run_id),
        "replayed": True,
        "charged": bool(charged),
        "collection_digest": str(state["collection"].digest or ""),
        "county_digest": str(state["county"].digest or ""),
        "county": str(county.get("canonical", "") or ""),
        "territory": str(county.get("territory", "") or ""),
    }


def _census_raise_stored_refusal(state: dict[str, Any]) -> Any:
    _require_record_integrity(state["collection"], "Census collection")
    _require_record_integrity(state["refusal"], "Census refusal")
    refusal = dict(state["refusal"].payload or {})
    raise WorkflowError(
        f"prior collection refused at {refusal.get('stage', 'unknown')}: "
        f"{refusal.get('reason', 'unknown')}"
    )


def collect_census_audited(
    *,
    property_id: str,
    attempt: str = "1",
    principal: str,
    approved: bool = False,
    config: Any = None,
    spend_run_id: Any = None,
) -> dict[str, Any]:
    """Collect and assess one Census address in committed phases.

    Validation (enabled adapter, explicit approval, authenticated
    principal, operational property with a usable Texas street
    address) refuses before any claim or transmission. A committed
    exclusive claim then serializes transmission: concurrent
    identical calls permit at most one request, completed
    evidence replays without a request, and an uncertain
    started claim cannot silently retransmit (recovery is an
    explicit new attempt, approval-bound). The claim commit also
    reserves the attempt's allowance on ``spend_run_id`` (when
    given) BEFORE the request is sent, so interruption after
    claiming, transmission, retention, or before runner
    checkpointing can neither undercount the attempt nor reopen
    the allowance; an uncertain transmitted attempt
    conservatively consumes its allowance, and recovery never
    double-counts the same attempt. The bounded response is
    retained before assessment, so a parse/assessment refusal
    is recorded inspectably without erasing the bytes. No
    business transaction stays open during HTTP. Exits past the
    claim (ambiguous transmit, assessment refusal) raise
    :class:`_CensusChargedError`; pre-claim refusals raise
    uncharged WorkflowError. Crash between transmit and
    retention loses the bytes and stays uncertain for that key
    (documented limitation); the allowance stays consumed.
    """
    import re

    from app.agents.gabriel.sources import census as census_module
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    from app.properties_db import get_connection, get_property
    from app.workflow_db import create_run

    _require_enabled()
    if not approved:
        raise WorkflowError("Census collection requires explicit approval")
    if not str(principal or "").strip():
        raise WorkflowError("Census collection requires an authenticated principal")
    spend_uuid: UUID | None = None
    if spend_run_id is not None:
        try:
            spend_uuid = (
                spend_run_id
                if isinstance(spend_run_id, UUID)
                else UUID(str(spend_run_id))
            )
        except (ValueError, AttributeError, TypeError) as exc:
            raise WorkflowError("spend_run_id must be an operational UUID") from exc
    try:
        census_module.require_collection_allowed()
    except census_module.CensusError as exc:
        raise WorkflowError(str(exc)) from exc
    prop_uuid = _require_operational_uuid(property_id, "property_id")
    attempt_label = str(attempt or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,2}", attempt_label):
        raise WorkflowError("attempt must be an integer 1..999")
    target_row = get_property(prop_uuid)
    if target_row is None:
        raise WorkflowError("unknown operational property")
    street = str(target_row.get("street_address", "") or "")
    city = str(target_row.get("city", "") or "")
    state = str(target_row.get("state", "") or "")
    zip_code = str(target_row.get("zip_code", "") or "")
    try:
        census_module.census_request_url(street, city, state, zip_code)
    except census_module.CensusError as exc:
        raise WorkflowError(f"no usable Texas street address: {exc}") from exc
    key = census_module.census_admission_run_key(
        property_id=str(prop_uuid),
        street=street,
        city=city,
        state=state,
        zip_code=zip_code,
        revision=census_module.CENSUS_COLLECTOR_REVISION,
        attempt=attempt_label,
    )
    record_key = census_module.census_record_key(
        street=street, city=city, state=state, zip_code=zip_code
    )
    run = create_run(idempotency_key=key, config=config)
    address_label = ", ".join(part for part in (street, city, state, zip_code) if part)
    conn = get_connection(config)
    try:
        collect_state = _census_collect_state(conn, run.run_id, record_key)
        if collect_state["collection"] is not None and collect_state["county"] is not None:
            return _census_replay_summary(run.run_id, collect_state)
        if collect_state["refusal"] is not None and collect_state["collection"] is not None:
            _census_raise_stored_refusal(collect_state)
        if collect_state["collection"] is not None:
            _require_record_integrity(collect_state["collection"], "Census collection")
            resume_collection = collect_state["collection"]
        else:
            if collect_state["claim"] is not None:
                raise WorkflowError(
                    "uncertain prior collection attempt for this key; "
                    "approve an explicit new attempt to retry"
                )
            resume_collection = None
        charged_here = False
        if resume_collection is None:
            if spend_uuid is not None and get_run_in_transaction(conn, spend_uuid) is None:
                raise WorkflowError("spend run is unknown; refusing uncounted claim")
            claimed = _census_insert_claim(
                conn, run.run_id, record_key, str(prop_uuid),
                str(principal).strip(), census_module.CENSUS_COLLECTOR_REVISION,
            )
            if claimed:
                charged_here = True
                if spend_uuid is not None:
                    # Allowance reservation rides the claim commit:
                    # only the winner charges, exactly once, before
                    # any transmission. No refund path reopens it.
                    add_usage_in_transaction(
                        conn, spend_uuid, "census_attempts", 1
                    )
            conn.commit()
            if not claimed:
                collect_state = _census_collect_state(conn, run.run_id, record_key)
                if (
                    collect_state["collection"] is not None
                    and collect_state["county"] is not None
                ):
                    return _census_replay_summary(run.run_id, collect_state)
                if (
                    collect_state["refusal"] is not None
                    and collect_state["collection"] is not None
                ):
                    _census_raise_stored_refusal(collect_state)
                raise WorkflowError(
                    "another caller holds the collection claim for this key"
                )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if resume_collection is None:
        try:
            collected = census_module.collect_address(
                street, city, state, zip_code, principal=str(principal).strip()
            )
        except census_module.CensusFetchError as exc:
            if not exc.transmitted:
                raise WorkflowError(
                    f"census collection unreachable: {exc}"
                ) from exc
            try:
                err_conn = get_connection(config)
            except Exception:
                err_conn = None
            if err_conn is not None:
                try:
                    append_error_in_transaction(
                        err_conn, run.run_id,
                        {"census_attempt": "ambiguous", "error": str(exc)},
                    )
                    err_conn.commit()
                except Exception:
                    pass
                finally:
                    try:
                        err_conn.close()
                    except Exception:
                        pass
            raise _CensusChargedError(
                f"census collection ambiguous: {exc}", charged=charged_here
            ) from exc
        except census_module.CensusError as exc:
            raise WorkflowError(str(exc)) from exc
        collected_payload = collected.model_dump(mode="json")
        collect_event = _census_event(
            "collected", str(prop_uuid), address_label, attempt_label, key
        )

        def _retain(mutate_conn: Any) -> dict[str, Any]:
            live = get_run_in_transaction(mutate_conn, run.run_id)
            if live is None:
                raise WorkflowError(f"unknown workflow run {run.run_id}")
            if live.status == "stopped":
                raise WorkflowError("run is stopped")
            stored = put_record_in_transaction(
                mutate_conn, run.run_id, kind=KIND_CENSUS_COLLECTION,
                record_key=record_key, version=1,
                payload=collected_payload, digest=digest_payload(collected_payload),
            )
            return {
                "collection_digest": str(stored.digest or ""),
                "collection": dict(stored.payload or {}),
            }

        retained = run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=collect_event,
            mutate=_retain,
        )
    else:
        retained = {
            "collection_digest": str(resume_collection.digest or ""),
            "collection": dict(resume_collection.payload or {}),
        }
    try:
        county = census_module.assess_county(
            collection=retained["collection"],
            collection_digest=retained["collection_digest"],
            property_record={"id": str(prop_uuid)},
        )
    except census_module.CensusError as exc:
        refusal = {
            "property_id": str(prop_uuid),
            "address": address_label,
            "attempt": attempt_label,
            "collection_digest": retained["collection_digest"],
            "stage": "assess",
            "reason": str(exc),
            "collector_revision": census_module.CENSUS_COLLECTOR_REVISION,
            "principal": str(principal).strip(),
        }
        refuse_event = _census_event(
            "refused", str(prop_uuid), address_label, attempt_label, key,
            extra={"reason": str(exc)},
        )

        def _refuse(mutate_conn: Any) -> dict[str, Any]:
            from app.workflow_db import WorkflowConflictError

            live = get_run_in_transaction(mutate_conn, run.run_id)
            if live is None:
                raise WorkflowError(f"unknown workflow run {run.run_id}")
            if live.stage == "done":
                state = _census_collect_state(mutate_conn, run.run_id, record_key)
                if (
                    state["refusal"] is None
                    or state["refusal"].digest != digest_payload(refusal)
                ):
                    raise WorkflowConflictError(
                        "admission run finished with different evidence"
                    )
                try:
                    _census_raise_stored_refusal(state)
                except WorkflowError as exc:
                    raise _CensusChargedError(
                        str(exc), charged=charged_here
                    ) from exc
            put_record_in_transaction(
                mutate_conn, run.run_id, kind=KIND_CENSUS_REFUSAL,
                record_key=record_key, version=1,
                payload=refusal, digest=digest_payload(refusal),
            )
            advance_stage_in_transaction(
                mutate_conn, run.run_id, "created", "done", status="complete_stopped"
            )
            return {"refused": True}

        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=refuse_event,
            mutate=_refuse,
        )
        raise _CensusChargedError(
            f"census assessment refused: {exc}", charged=charged_here
        ) from exc
    assess_event = _census_event(
        "assessed", str(prop_uuid), address_label, attempt_label, key
    )

    def _assess(mutate_conn: Any) -> dict[str, Any]:
        from app.workflow_db import WorkflowConflictError

        live = get_run_in_transaction(mutate_conn, run.run_id)
        if live is None:
            raise WorkflowError(f"unknown workflow run {run.run_id}")
        if live.stage == "done":
            state = _census_collect_state(mutate_conn, run.run_id, record_key)
            if (
                state["county"] is None
                or state["county"].digest != digest_payload(county)
            ):
                raise WorkflowConflictError(
                    "admission run finished with different evidence"
                )
            return _census_replay_summary(run.run_id, state, charged=charged_here)
        stored_county = put_record_in_transaction(
            mutate_conn, run.run_id, kind=KIND_CENSUS_COUNTY,
            record_key=record_key, version=1,
            payload=county, digest=digest_payload(county),
        )
        advance_stage_in_transaction(
            mutate_conn, run.run_id, "created", "done", status="complete"
        )
        county_geo = county.get("county", {}) or {}
        return {
            "run_id": str(run.run_id),
            "replayed": False,
            "charged": charged_here,
            "collection_digest": retained["collection_digest"],
            "county_digest": str(stored_county.digest or ""),
            "county": str(county_geo.get("canonical", "") or ""),
            "territory": str(county_geo.get("territory", "") or ""),
        }

    return run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=assess_event,
        mutate=_assess,
    )


def _collect_census_for_evaluation(
    property_id: str,
    *,
    principal: str,
    config: Any = None,
    spend_run_id: Any = None,
) -> dict[str, Any]:
    """Collect county evidence for one evaluation (never raises).

    Attempt 1 only: a refused or ambiguous attempt never causes a
    replacement request under a new identifier. Returns a status
    dict with ``charged`` telling whether this call newly consumed
    Census allowance (claim winners only; replays, resumes, and
    pre-claim refusals consume nothing new). The allowance itself
    is reserved inside the claim commit on ``spend_run_id``,
    before transmission â€” never marked after the fact.
    """
    try:
        summary = collect_census_audited(
            property_id=property_id, attempt="1",
            principal=principal, approved=True, config=config,
            spend_run_id=spend_run_id,
        )
    except _CensusChargedError as exc:
        return {
            "outcome": "ambiguous",
            "charged": bool(exc.charged),
            "reason": str(exc),
        }
    except WorkflowError as exc:
        return {"outcome": "refused", "charged": False, "reason": str(exc)}
    replayed = bool(summary.get("replayed"))
    return {
        "outcome": "replayed" if replayed else "collected",
        "charged": bool(summary.get("charged", False)),
        "admission_run_id": str(summary.get("run_id", "")),
        "collection_digest": str(summary.get("collection_digest", "") or ""),
        "county_digest": str(summary.get("county_digest", "") or ""),
        "county": str(summary.get("county", "") or ""),
        "territory": str(summary.get("territory", "") or ""),
    }


def successor_run_key(
    *,
    parent_run_id: str,
    parent_version: int,
    parent_digest: str,
    collection_digest: str,
    assessment_digest: str,
) -> str:
    """Deterministic idempotency key binding parent, evidence, revision."""
    return "successor-" + digest_payload(
        {
            "parent_run_id": str(parent_run_id),
            "parent_version": int(parent_version),
            "parent_digest": str(parent_digest),
            "collection_digest": str(collection_digest),
            "assessment_digest": str(assessment_digest),
            "revision": SUCCESSOR_REVISION,
        }
    )[:32]


def _dcad_collection_for_assessment(conn: Any, run_id: UUID) -> Any:
    """The single DCAD collection record in a collection run, if present."""
    for record in list_records_in_transaction(conn, run_id, KIND_DCAD_COLLECTION):
        return record
    return None


def _dcad_assessment_for_successor(conn: Any, run_id: UUID) -> Any:
    """The single DCAD assessment record in a collection run, if present."""
    for record in list_records_in_transaction(conn, run_id, KIND_DCAD_ASSESSMENT):
        return record
    return None


def _census_collection_for_successor(conn: Any, run_id: UUID) -> Any:
    """The single Census collection record in a collection run, if present."""
    for record in list_records_in_transaction(conn, run_id, KIND_CENSUS_COLLECTION):
        return record
    return None


def _census_county_for_successor(conn: Any, run_id: UUID) -> Any:
    """The single Census county record in a collection run, if present."""
    for record in list_records_in_transaction(conn, run_id, KIND_CENSUS_COUNTY):
        return record
    return None


def _dossier_content_changes(prior: dict[str, Any], rebuilt: dict[str, Any]) -> list[dict[str, Any]]:
    """Material field diffs between parent and rebuilt dossier content."""
    changes: list[dict[str, Any]] = []
    for field in (
        "name", "street_address", "city", "state", "zip_code", "unit_designator",
        "census_evidence",
    ):
        old, new = prior.get(field), rebuilt.get(field)
        if old != new:
            changes.append({"field": field, "parent": old, "rebuilt": new})
    for field in ("category_attribution", "county_attribution"):
        old, new = prior.get(field) or {}, rebuilt.get(field) or {}
        for sub in ("status", "observed", "verified", "value", "evidence_status"):
            if old.get(sub) != new.get(sub):
                changes.append(
                    {"field": f"{field}.{sub}", "parent": old.get(sub), "rebuilt": new.get(sub)}
                )
    old_conflicts = sorted(
        json.dumps(item, sort_keys=True, default=str)
        for item in (prior.get("conflicts", []) or [])
    )
    new_conflicts = sorted(
        json.dumps(item, sort_keys=True, default=str)
        for item in (rebuilt.get("conflicts", []) or [])
    )
    if old_conflicts != new_conflicts:
        changes.append(
            {"field": "conflicts", "parent": old_conflicts, "rebuilt": new_conflicts}
        )
    if (prior.get("identity_disputes", {}) or {}) != (rebuilt.get("identity_disputes", {}) or {}):
        changes.append(
            {
                "field": "identity_disputes",
                "parent": prior.get("identity_disputes", {}),
                "rebuilt": rebuilt.get("identity_disputes", {}),
            }
        )
    return changes


def _successor_artifact_lineage(
    parent_payload: dict[str, Any]
) -> dict[str, dict[str, str]]:
    """Explicit run/key/digest lineage from a committed successor block.

    Returns record-key -> {run_id, digest} for well-formed
    entries; malformed entries are ignored (a ref that then
    resolves nowhere becomes an ordinary missing-artifact
    blocker, never a guess).
    """
    lineage: dict[str, dict[str, str]] = {}
    successor = parent_payload.get("successor") or {}
    if not isinstance(successor, dict):
        return lineage
    entries = successor.get("retained_artifacts") or []
    if not isinstance(entries, list):
        return lineage
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("record_key", "") or "")
        run_id = str(entry.get("run_id", "") or "")
        digest = str(entry.get("digest", "") or "")
        if key and run_id and digest and key not in lineage:
            lineage[key] = {"run_id": run_id, "digest": digest}
    return lineage


def _load_verified_successor_inputs(
    conn: Any,
    *,
    property_id: UUID,
    parent_run_id: UUID,
    dossier_key: str,
    collection_run_id: UUID,
) -> tuple[dict[str, Any], list[str]]:
    """Load + verify every successor input; shared by preview and commit.

    Stored payload digests are recomputed against contents (not
    merely compared between strings), cross-bindings are checked,
    and evidence must come from supported collector/parser
    revisions. Returns (bundle, blockers): tamper and structural
    absence raise loudly; resolvable mismatches become blockers
    the preview reports and the commit refuses.
    """
    from app.agents.gabriel.sources import dcad as dcad_module
    from app.custody import dossier_id_for_property
    from app.custody_db import (
        current_custody_in_transaction,
        latest_version_in_transaction,
        verify_chain_in_transaction,
    )

    blockers: list[str] = []
    parent = get_record_in_transaction(
        conn, parent_run_id, KIND_DOSSIER_VERSION, dossier_key
    )
    if parent is None:
        raise WorkflowError("parent dossier version not found")
    if str((parent.payload or {}).get("property_id", "") or "") != str(property_id):
        raise WorkflowError("parent dossier is not bound to this property")
    _require_record_integrity(parent, "parent dossier version")
    parent_payload = dict(parent.payload or {})
    prior_dossier = dict(parent_payload.get("dossier", {}) or {})
    lineage = _successor_artifact_lineage(parent_payload)
    retained: list[dict[str, Any]] = []
    for ref in parent_payload.get("artifact_refs", []) or []:
        artifact = get_record_in_transaction(
            conn, parent_run_id, KIND_RESEARCH_ARTIFACT, str(ref)
        )
        source_run = parent_run_id
        if artifact is None and str(ref) in lineage:
            # Successor-of-successor: the parent run references
            # artifacts it does not hold. Resolve through the
            # parent's own explicit run/key/digest lineage and
            # verify the digest; the committed successor block
            # already carries these entries.
            entry = lineage[str(ref)]
            try:
                source_run = UUID(str(entry.get("run_id", "") or ""))
            except (ValueError, AttributeError, TypeError):
                blockers.append(f"retained artifact lineage invalid: {ref}")
                continue
            artifact = get_record_in_transaction(
                conn, source_run, KIND_RESEARCH_ARTIFACT, str(ref)
            )
            if artifact is not None and str(artifact.digest or "") != str(
                entry.get("digest", "") or ""
            ):
                raise WorkflowError(f"retained artifact digest mismatch: {ref}")
        if artifact is None:
            blockers.append(f"retained artifact missing: {ref}")
            continue
        if (artifact.payload or {}).get("attempt") != "completed":
            blockers.append(f"retained artifact not completed: {ref}")
            continue
        _require_record_integrity(artifact, f"retained artifact {ref}")
        retained.append(
            {
                "run_id": str(source_run),
                "record_key": str(ref),
                "digest": str(artifact.digest or ""),
                "payload": dict(artifact.payload or {}),
            }
        )
    if not retained and not blockers:
        blockers.append("parent dossier references no retained artifact")
    collection_record = _dcad_collection_for_assessment(conn, collection_run_id)
    assessment_record = _dcad_assessment_for_successor(conn, collection_run_id)
    evidence_source = "dcad"
    census_collection = _census_collection_for_successor(conn, collection_run_id)
    census_county = _census_county_for_successor(conn, collection_run_id)
    collection_payload: dict[str, Any] = {}
    assessment_payload: dict[str, Any] = {}
    collection_digest = ""
    assessment_digest = ""
    collection_record_key = ""
    assessment_record_key = ""
    current_address: dict[str, Any] = {}
    if (
        collection_record is None
        and assessment_record is None
        and census_collection is not None
        and census_county is not None
    ):
        evidence_source = "census"
        from app.agents.gabriel.sources import census as census_module
        from app.properties_db import get_property

        _require_record_integrity(census_collection, "Census collection")
        _require_record_integrity(census_county, "Census county result")
        collection_digest = str(census_collection.digest or "")
        assessment_digest = str(census_county.digest or "")
        collection_payload = dict(census_collection.payload or {})
        assessment_payload = dict(census_county.payload or {})
        collection_record_key = str(census_collection.record_key or "")
        assessment_record_key = str(census_county.record_key or "")
        if str(assessment_payload.get("property_id", "") or "") != str(property_id):
            blockers.append("county result binds a different property")
        if str(assessment_payload.get("collection_digest", "") or "") != collection_digest:
            blockers.append("county result does not match the stored collection")
        try:
            expected_url = census_module.census_request_url(
                str(collection_payload.get("submitted_street", "") or ""),
                str(collection_payload.get("submitted_city", "") or ""),
                str(collection_payload.get("submitted_state", "") or ""),
                str(collection_payload.get("submitted_zip", "") or ""),
            )
        except census_module.CensusError:
            expected_url = ""
        if not expected_url or str(collection_payload.get("url", "") or "") != expected_url:
            blockers.append("stored collection URL does not match its submitted address")
        if (
            str(collection_payload.get("collector_revision", "") or "")
            != census_module.CENSUS_COLLECTOR_REVISION
            or str(assessment_payload.get("collector_revision", "") or "")
            != census_module.CENSUS_COLLECTOR_REVISION
            or str(assessment_payload.get("assessment_version", "") or "")
            != census_module.CENSUS_COUNTY_VERSION
            or str(assessment_payload.get("collection_provenance", "") or "")
            != census_module.CENSUS_PROVENANCE
        ):
            blockers.append("evidence was not admitted under supported revisions")
        target_row = get_property(property_id)
        if target_row is None:
            blockers.append("operational property row is missing")
        else:
            current_address = {
                "street": str(target_row.get("street_address", "") or ""),
                "city": str(target_row.get("city", "") or ""),
                "state": str(target_row.get("state", "") or ""),
                "zip": str(target_row.get("zip_code", "") or ""),
            }
    elif collection_record is None or assessment_record is None:
        blockers.append("collection run carries no DCAD collection/assessment pair")
    else:
        _require_record_integrity(collection_record, "DCAD collection")
        _require_record_integrity(assessment_record, "DCAD assessment")
        collection_digest = str(collection_record.digest or "")
        assessment_digest = str(assessment_record.digest or "")
        collection_payload = dict(collection_record.payload or {})
        assessment_payload = dict(assessment_record.payload or {})
        if str(assessment_payload.get("property_id", "") or "") != str(property_id):
            blockers.append("assessment binds a different property")
        if str(assessment_payload.get("dossier_key", "") or "") != dossier_key:
            blockers.append("assessment binds a different dossier key")
        if str(assessment_payload.get("collection_digest", "") or "") != collection_digest:
            blockers.append("assessment does not match the stored collection")
        if str(assessment_payload.get("account", "") or "") != str(
            collection_payload.get("account", "") or ""
        ):
            blockers.append("assessment binds a different account than the collection")
        try:
            expected_url = dcad_module.dcad_account_url(
                str(collection_payload.get("account", "") or "")
            )
        except dcad_module.DcadError:
            expected_url = ""
        if not expected_url or str(collection_payload.get("url", "") or "") != expected_url:
            blockers.append("stored collection URL does not match its account")
        if (
            str(collection_payload.get("collector_revision", "") or "")
            != dcad_module.DCAD_COLLECTOR_REVISION
            or str(assessment_payload.get("collector_revision", "") or "")
            != dcad_module.DCAD_COLLECTOR_REVISION
            or str(assessment_payload.get("assessment_version", "") or "")
            != dcad_module.DCAD_ASSESSMENT_VERSION
        ):
            blockers.append("evidence was not admitted under supported revisions")
    dossier_uuid = dossier_id_for_property(property_id)
    latest = latest_version_in_transaction(conn, dossier_uuid)
    if latest is None:
        blockers.append("dossier has no custody version to supersede")
        custody_version = 0
    else:
        custody_version = latest.version
    receipts_verified = verify_chain_in_transaction(conn, dossier_uuid)
    custody = current_custody_in_transaction(conn, dossier_uuid)
    holder = str((custody or {}).get("holder", "") or "")
    if holder and holder != "gabriel":
        blockers.append(f"custody is held by {holder}, not gabriel")
    return (
        {
            "parent": parent,
            "parent_payload": parent_payload,
            "prior_dossier": prior_dossier,
            "retained": retained,
            "evidence_source": evidence_source,
            "current_address": current_address,
            "collection": collection_payload,
            "collection_digest": collection_digest,
            "collection_record_key": collection_record_key,
            "assessment": assessment_payload,
            "assessment_digest": assessment_digest,
            "assessment_record_key": assessment_record_key,
            "dossier_id": dossier_uuid,
            "latest": latest,
            "custody_version": custody_version,
            "holder": holder,
            "receipts_verified": receipts_verified,
        },
        blockers,
    )


def _evaluate_successor_evidence(
    *,
    parent_payload: dict[str, Any],
    retained_payloads: list[dict[str, Any]],
    assessment: dict[str, Any],
    collection_url: str,
    dossier_key: str,
    evidence_source: str = "dcad",
    collection_digest: str = "",
    property_id: str = "",
    current_address: dict[str, Any] | None = None,
    collection_run_id: str = "",
    collection_record_key: str = "",
    assessment_record_key: str = "",
    assessment_digest: str = "",
) -> dict[str, Any]:
    """Evaluate cumulative retained-plus-admission evidence (pure, shared).

    Rebuilds retained intelligence deterministically, folds in the
    admitted evidence through the SAME processing the rebuilt
    dossier uses, and derives the intended qualification and
    Raphael outcome. DCAD evidence composes claim-scoped
    alignment (no inherited alignment) and evaluates through the
    shared policy; Census evidence applies county-only
    attribution with the ordinary fail-closed category verdict
    (county admission never authorizes category verification).
    Preview and commit run this identical function over
    identically verified inputs.
    """
    from app.agents.gabriel.extractor import group_by_property
    from app.agents.gabriel.persist import qualify_dossier
    from app.agents.gabriel.producers import build_candidate_verification
    from app.agents.gabriel.sources import dcad as dcad_module
    from app.agents.raphael.service import evaluate_dossier

    results: list[Any] = []
    for payload in retained_payloads:
        results.extend(_reconstruct_results(payload))
    intel = extract_research_intelligence(results)
    ranking = rank_intelligence(intel)
    groups = {group.key: group for group in group_by_property(intel.results)}
    candidate = next((item for item in ranking.candidates if item.key == dossier_key), None)
    group = groups.get(dossier_key)
    if candidate is None or group is None:
        raise WorkflowError("retained evidence does not reproduce the dossier")
    county_application: dict[str, Any] = {}
    if evidence_source == "census":
        from app.agents.gabriel.sources import census as census_module

        verdict = build_candidate_verification(
            candidate,
            list(group.members),
            intel.results,
            basis=group.basis,
            registry=None,
        ).model_dump(mode="json")
        voice_alignment: dict[str, Any] = {}
    else:
        alignment = assessment.get("alignment", {}) or {}
        attestation, voice_alignment = dcad_module.cumulative_attestation(
            dossier_key=dossier_key,
            brave_voice_urls=[str(item.result_url or "") for item in intel.results],
            dcad_voice_url=str(collection_url or ""),
            dcad_aligned=bool(alignment.get("aligned")),
            dcad_basis=str(alignment.get("basis", "") or ""),
            dcad_scope=str(alignment.get("scope", "") or ""),
            basis=group.basis,
            disputes=len(candidate.identity_disputes),
        )
        voice = dcad_module.build_dcad_voice(str(collection_url or ""))
        verdict = build_candidate_verification(
            candidate,
            list(group.members),
            intel.results,
            basis=group.basis,
            registry=None,
            extra_voices=[voice],
            attestation=attestation,
        ).model_dump(mode="json")
    rebuilt_set = build_dossiers(DossierRequest(rounds=[intel]))
    rebuilt = next(
        (item.model_dump(mode="json") for item in rebuilt_set.dossiers if item.key == dossier_key),
        None,
    )
    if rebuilt is None:
        raise WorkflowError("retained evidence does not reproduce the dossier")
    if evidence_source == "census":
        application = census_module.apply_county_to_dossier(
            rebuilt,
            assessment,
            collection_digest=collection_digest,
            property_id=property_id,
            current_address=current_address or {},
        )
        if not application.get("applied"):
            raise WorkflowError(
                "census county not applicable to the rebuilt dossier: "
                f"{application.get('reason', 'unknown')}"
            )
        rebuilt["county_attribution"] = application["county_attribution"]
        try:
            rebuilt["census_evidence"] = census_module.census_evidence_refs(
                admission_run_id=collection_run_id,
                collection_record_key=collection_record_key,
                county_record_key=assessment_record_key,
                collection_digest=collection_digest,
                county_digest=assessment_digest,
                county_result=assessment,
            )
        except census_module.CensusError as exc:
            raise WorkflowError(
                "census evidence locator refused for the rebuilt dossier: "
                f"{exc}"
            ) from exc
        admitted = dict(assessment.get("county", {}) or {})
        county_application = {
            "applied": True,
            "county": str(admitted.get("canonical", "") or ""),
            "territory": str(admitted.get("territory", "") or ""),
            "scope": str(assessment.get("scope_label", "") or ""),
        }
    prior_dossier = dict(parent_payload.get("dossier", {}) or {})
    changes = _dossier_content_changes(prior_dossier, rebuilt)
    qualification = qualify_dossier(rebuilt)
    raphael = evaluate_dossier(
        {"dossier": rebuilt, "candidate_verification": verdict}
    )
    return {
        "rebuilt_dossier": rebuilt,
        "content_changes": changes,
        "verdict": verdict,
        "voice_alignment": voice_alignment,
        "county_application": county_application,
        "qualification": qualification,
        "raphael": raphael,
    }


def preview_successor_in_transaction(
    conn: Any,
    *,
    property_id: str,
    parent_run_id: str,
    dossier_key: str,
    collection_run_id: str,
) -> dict[str, Any]:
    """Read-only successor preview with intended outcomes (no writes, no network)."""
    from app.custody import CustodyError

    prop_uuid = _require_operational_uuid(property_id, "property_id")
    parent_uuid = _require_operational_uuid(parent_run_id, "parent_run_id")
    collection_uuid = _require_operational_uuid(collection_run_id, "collection_run_id")
    if not str(dossier_key or "").strip() or is_demo_id(str(dossier_key)):
        raise WorkflowError("dossier_key must be an operational key")
    try:
        bundle, blockers = _load_verified_successor_inputs(
            conn,
            property_id=prop_uuid,
            parent_run_id=parent_uuid,
            dossier_key=str(dossier_key),
            collection_run_id=collection_uuid,
        )
    except CustodyError as exc:
        raise WorkflowError(f"custody chain does not verify: {exc}") from exc
    parent = bundle["parent"]
    assessment_payload = bundle["assessment"]
    evidence_source = str(bundle.get("evidence_source", "dcad") or "dcad")
    key = ""
    if parent.digest and bundle["collection_digest"] and bundle["assessment_digest"]:
        key = successor_run_key(
            parent_run_id=str(parent_uuid),
            parent_version=int(parent.version or 0),
            parent_digest=str(parent.digest or ""),
            collection_digest=bundle["collection_digest"],
            assessment_digest=bundle["assessment_digest"],
        )
    assessment_view: dict[str, Any] = {
        "run_id": str(collection_uuid),
        "evidence_source": evidence_source,
        "collection_digest": bundle["collection_digest"],
        "assessment_digest": bundle["assessment_digest"],
    }
    if evidence_source == "census":
        admitted = dict(assessment_payload.get("county", {}) or {})
        assessment_view["county"] = str(admitted.get("canonical", "") or "")
        assessment_view["territory"] = str(admitted.get("territory", "") or "")
        assessment_view["scope"] = str(assessment_payload.get("scope_label", "") or "")
        claim_scope = "county-only"
    else:
        assessment_view["aligned"] = bool(
            assessment_payload.get("alignment", {}).get("aligned")
        )
        assessment_view["dcad_only_outcome"] = (
            assessment_payload.get("dcad_only_verification", {}) or {}
        ).get("outcome", "")
        claim_scope = _dcad_claim_scope(bundle["prior_dossier"])
    base: dict[str, Any] = {
        "ready": not blockers,
        "blockers": blockers,
        "parent": {
            "run_id": str(parent_uuid),
            "dossier_key": str(dossier_key),
            "version": parent.version,
            "digest": parent.digest,
            "claim_scope": claim_scope,
        },
        "retained_artifacts": [
            {
                "run_id": item["run_id"],
                "record_key": item["record_key"],
                "digest": item["digest"],
            }
            for item in bundle["retained"]
        ],
        "assessment": assessment_view,
        "custody": {
            "dossier_id": str(bundle["dossier_id"]),
            "latest_version": bundle["custody_version"],
            "holder": bundle["holder"] or "gabriel (pre-handoff)",
            "receipts_verified": bundle["receipts_verified"],
            "would_append_version": bundle["custody_version"] + 1
            if bundle["latest"] is not None
            else 0,
        },
        "idempotency_key": key,
        "writes": "none in preview",
    }
    if blockers:
        return base
    try:
        evaluated = _evaluate_successor_evidence(
            parent_payload=bundle["parent_payload"],
            retained_payloads=[item["payload"] for item in bundle["retained"]],
            assessment=assessment_payload,
            collection_url=str(bundle["collection"].get("url", "") or ""),
            dossier_key=str(dossier_key),
            evidence_source=evidence_source,
            collection_digest=bundle["collection_digest"],
            property_id=str(prop_uuid),
            current_address=bundle.get("current_address") or {},
            collection_run_id=str(collection_uuid),
            collection_record_key=str(bundle.get("collection_record_key", "") or ""),
            assessment_record_key=str(bundle.get("assessment_record_key", "") or ""),
            assessment_digest=bundle["assessment_digest"],
        )
    except WorkflowError as exc:
        base["ready"] = False
        base["blockers"] = [str(exc)]
        return base
    base["intended"] = {
        "qualification": evaluated["qualification"],
        "decision": evaluated["raphael"].get("outcome", ""),
        "decision_basis": list(evaluated["raphael"].get("basis", []) or []),
        "verification_outcome": evaluated["verdict"].get("outcome", ""),
        "verification_reasons": list(evaluated["verdict"].get("reasons", []) or []),
    }
    base["content_changes"] = evaluated["content_changes"]
    base["voice_alignment"] = evaluated["voice_alignment"]
    base["county_application"] = evaluated.get("county_application", {})
    return base


def _successor_summary_from_records(
    conn: Any, run_id: UUID, dossier_key: str, *, bindings: dict[str, Any]
) -> dict[str, Any]:
    """Re-derive a committed successor summary (replay path, no writes).

    The run key already binds the evidence digests, but property,
    parent, and principal identity are re-validated against the
    stored successor block: a replay caller with mismatched
    operation bindings is refused instead of handed another
    operation's result.
    """
    dossier_record = get_record_in_transaction(conn, run_id, KIND_DOSSIER_VERSION, dossier_key)
    link, _ = _link_in_transaction(conn, run_id, dossier_key)
    decision = None
    for record in list_records_in_transaction(conn, run_id, "raphael_decision"):
        decision = record
    if dossier_record is None or decision is None or not link:
        raise WorkflowError("successor run is missing committed outputs")
    _require_record_integrity(dossier_record, "successor dossier version")
    _require_record_integrity(decision, "Raphael decision")
    stored_payload = dict(dossier_record.payload or {})
    stored_successor = dict(stored_payload.get("successor", {}) or {})
    stored_source = str(stored_successor.get("evidence_source", "dcad") or "dcad")
    if stored_source == "census":
        collection_ref = stored_successor.get("census_collection", {}) or {}
        assessment_ref = stored_successor.get("census_county", {}) or {}
    else:
        collection_ref = stored_successor.get("dcad_collection", {}) or {}
        assessment_ref = stored_successor.get("dcad_assessment", {}) or {}
    stored_bindings = {
        "property_id": str(stored_payload.get("property_id", "") or ""),
        "parent_run_id": str(stored_successor.get("parent_run_id", "") or ""),
        "parent_digest": str(stored_successor.get("parent_digest", "") or ""),
        "collection_digest": str(collection_ref.get("digest", "") or ""),
        "assessment_digest": str(assessment_ref.get("digest", "") or ""),
        "principal": str(stored_successor.get("actor", "") or ""),
    }
    for name, stored_value in stored_bindings.items():
        if stored_value != str(bindings.get(name, "") or ""):
            raise WorkflowError(
                f"successor replay binding mismatch: {name} does not match "
                "the committed operation"
            )
    return {
        "run_id": str(run_id),
        "replayed": True,
        "dossier_key": dossier_key,
        "dossier_digest": str(dossier_record.digest or ""),
        "custody_version": int(link.get("custody_version", 0)),
        "decision": str((decision.payload or {}).get("outcome", "")),
        "decision_basis": list((decision.payload or {}).get("basis", []) or []),
        "receipts": dict(link.get("receipts", {}) or {}),
    }


def _successor_commit_event(key: str, bindings: dict[str, Any]) -> Any:
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity

    return AgentEvent.model_validate(
        {
            "agent": AgentId.WONDRUE.value,
            "run_id": key,
            "event_type": "wondrue.successor.committed",
            "severity": Severity.INFO.value,
            "summary": (
                f"successor assessment committed for dossier {bindings['dossier_key']}"
            ),
            "details": {**bindings, "idempotency_key": key},
            "evidence_refs": [],
            "retention_class": RetentionClass.PERMANENT.value,
        }
    )


def commit_successor_audited(
    *,
    property_id: str,
    parent_run_id: str,
    dossier_key: str,
    parent_version: int,
    parent_digest: str,
    expected_custody_version: int,
    collection_run_id: str,
    collection_digest: str,
    assessment_digest: str,
    reason: str,
    principal: str,
    approved: bool = False,
    config: Any = None,
) -> dict[str, Any]:
    """Commit one same-dossier successor assessment (audited, idempotent).

    Amendment authority resolves from the backend-authenticated
    principal (enrollment-checked): no caller-supplied name can
    impersonate an enrolled amender, and nobody is auto-enrolled.
    Authority is verified before any commit or replay output, so
    an unenrolled caller cannot retrieve a committed successor
    through the idempotent retry path either.
    Inside one transaction under the dossier serialization lock,
    re-verifies every approval binding: stored digests recomputed
    against contents, parent version/digest, collection/assessment
    digests and cross-bindings, supported revisions, custody chain
    integrity, current holder (gabriel), and the expected custody
    version. Evaluates cumulative evidence through the shared
    policy (same function the preview runs), persists the
    successor dossier rebuilt from that same processing, amends
    custody, snapshots, hands Gabriel -> Raphael, and routes the
    Raphael decision. An identical retry replays stored outputs
    without duplicating versions, receipts, or pilot credit; a
    conflicting retry fails safely. Original history is never
    rewritten.
    """
    from app.custody import check_amendment_permission, dossier_id_for_property
    from app.custody_db import (
        amend_in_transaction,
        lock_dossier_in_transaction,
        snapshot_in_transaction,
    )
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    from app.properties_db import get_connection
    from app.workflow_db import create_run

    _require_enabled()
    if not approved:
        raise WorkflowError("successor commit requires explicit approval")
    if not str(principal or "").strip():
        raise WorkflowError("successor commit requires an authenticated principal")
    # Authority gates every path below, including the completed-run
    # replay shortcuts: an unenrolled principal is refused before
    # reading or returning any committed successor output.
    check_amendment_permission(str(principal).strip(), str(reason or ""))
    prop_uuid = _require_operational_uuid(property_id, "property_id")
    parent_uuid = _require_operational_uuid(parent_run_id, "parent_run_id")
    collection_uuid = _require_operational_uuid(collection_run_id, "collection_run_id")
    if not str(dossier_key or "").strip() or is_demo_id(str(dossier_key)):
        raise WorkflowError("dossier_key must be an operational key")
    try:
        expected_parent_version = int(parent_version)
        expected_version = int(expected_custody_version)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("parent and custody versions must be integers") from exc
    bindings = {
        "property_id": str(prop_uuid),
        "parent_run_id": str(parent_uuid),
        "dossier_key": str(dossier_key),
        "parent_version": expected_parent_version,
        "parent_digest": str(parent_digest or ""),
        "expected_custody_version": expected_version,
        "collection_run_id": str(collection_uuid),
        "collection_digest": str(collection_digest or ""),
        "assessment_digest": str(assessment_digest or ""),
        "principal": str(principal).strip(),
    }
    key = successor_run_key(
        parent_run_id=str(parent_uuid),
        parent_version=expected_parent_version,
        parent_digest=str(parent_digest or ""),
        collection_digest=str(collection_digest or ""),
        assessment_digest=str(assessment_digest or ""),
    )
    run = create_run(idempotency_key=key, config=config)
    conn = get_connection(config)
    try:
        live = get_run_in_transaction(conn, run.run_id)
        if live is not None and live.stage in ("raphael", "done"):
            return _successor_summary_from_records(
                conn, run.run_id, str(dossier_key), bindings=bindings
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    event = _successor_commit_event(key, bindings)

    def _mutate(mutate_conn: Any) -> dict[str, Any]:
        live = get_run_in_transaction(mutate_conn, run.run_id)
        if live is None:
            raise WorkflowError(f"unknown workflow run {run.run_id}")
        if live.status == "stopped":
            raise WorkflowError("run is stopped")
        if live.stage in ("raphael", "done"):
            return _successor_summary_from_records(
                mutate_conn, run.run_id, str(dossier_key), bindings=bindings
            )
        if live.stage != "created":
            raise WorkflowError(f"successor run is at unexpected stage {live.stage}")
        check_amendment_permission(str(principal).strip(), str(reason or ""))
        dossier_uuid = dossier_id_for_property(prop_uuid)
        lock_dossier_in_transaction(mutate_conn, dossier_uuid)
        bundle, blockers = _load_verified_successor_inputs(
            mutate_conn,
            property_id=prop_uuid,
            parent_run_id=parent_uuid,
            dossier_key=str(dossier_key),
            collection_run_id=collection_uuid,
        )
        if blockers:
            raise WorkflowError("; ".join(blockers))
        parent = bundle["parent"]
        if int(parent.version or 0) != expected_parent_version:
            raise WorkflowError("parent dossier version does not match the approval")
        if str(parent.digest or "") != str(parent_digest or ""):
            raise WorkflowError("parent dossier digest does not match the approval")
        if bundle["collection_digest"] != str(collection_digest or ""):
            raise WorkflowError("collection digest does not match the approval")
        if bundle["assessment_digest"] != str(assessment_digest or ""):
            raise WorkflowError("assessment digest does not match the approval")
        commit_source = str(bundle.get("evidence_source", "dcad") or "dcad")
        evaluated = _evaluate_successor_evidence(
            parent_payload=bundle["parent_payload"],
            retained_payloads=[item["payload"] for item in bundle["retained"]],
            assessment=bundle["assessment"],
            collection_url=str(bundle["collection"].get("url", "") or ""),
            dossier_key=str(dossier_key),
            evidence_source=commit_source,
            collection_digest=bundle["collection_digest"],
            property_id=str(prop_uuid),
            current_address=bundle.get("current_address") or {},
            collection_run_id=str(collection_uuid),
            collection_record_key=str(bundle.get("collection_record_key", "") or ""),
            assessment_record_key=str(bundle.get("assessment_record_key", "") or ""),
            assessment_digest=bundle["assessment_digest"],
        )
        verdict = evaluated["verdict"]
        rebuilt_dossier = evaluated["rebuilt_dossier"]
        retained = [
            {
                "run_id": str(item["run_id"]),
                "record_key": item["record_key"],
                "digest": item["digest"],
            }
            for item in bundle["retained"]
        ]
        if commit_source == "census":
            collection_ref = _census_collection_for_successor(mutate_conn, collection_uuid)
            assessment_ref = _census_county_for_successor(mutate_conn, collection_uuid)
        else:
            collection_ref = _dcad_collection_for_assessment(mutate_conn, collection_uuid)
            assessment_ref = _dcad_assessment_for_successor(mutate_conn, collection_uuid)
        if collection_ref is None or assessment_ref is None:
            raise WorkflowError(
                "collection run carries no DCAD or Census evidence pair"
            )
        successor_block = {
            "parent_run_id": str(parent_uuid),
            "parent_record_key": str(dossier_key),
            "parent_version": expected_parent_version,
            "parent_digest": str(parent_digest or ""),
            "retained_artifacts": retained,
            "evidence_source": commit_source,
            "voice_alignment": evaluated["voice_alignment"],
            "county_application": evaluated.get("county_application", {}),
            "content_changes": evaluated["content_changes"],
            "processing_revision": SUCCESSOR_REVISION,
            "actor": str(principal).strip(),
        }
        if commit_source == "census":
            successor_block["census_collection"] = {
                "run_id": str(collection_uuid),
                "record_key": str(collection_ref.record_key or ""),
                "digest": bundle["collection_digest"],
            }
            successor_block["census_county"] = {
                "run_id": str(collection_uuid),
                "record_key": str(assessment_ref.record_key or ""),
                "digest": bundle["assessment_digest"],
            }
        else:
            successor_block["dcad_collection"] = {
                "run_id": str(collection_uuid),
                "record_key": str(collection_ref.record_key or ""),
                "digest": bundle["collection_digest"],
            }
            successor_block["dcad_assessment"] = {
                "run_id": str(collection_uuid),
                "record_key": str(assessment_ref.record_key or ""),
                "digest": bundle["assessment_digest"],
            }
        parent_payload = bundle["parent_payload"]
        payload = {
            "version": 1,
            "builder_version": SUCCESSOR_REVISION,
            "artifact_refs": list(parent_payload.get("artifact_refs", []) or []),
            "dossier": rebuilt_dossier,
            "candidate_verification": verdict,
            "property_id": str(prop_uuid),
            "successor": successor_block,
        }
        payload["digest"] = digest_payload(
            {item: value for item, value in payload.items() if item != "digest"}
        )
        custody_payload = {
            "dossier": rebuilt_dossier,
            "dossier_digest": str(payload["digest"]),
            "run_id": str(run.run_id),
            "dossier_key": str(dossier_key),
            "successor": successor_block,
        }
        latest = bundle["latest"]
        if latest is None:
            raise WorkflowError("dossier has no custody version to supersede")
        if latest.version != expected_version:
            _successor_replay_or_conflict(
                latest, expected_version, intended=custody_payload
            )
            return _successor_summary_from_records(
                mutate_conn, run.run_id, str(dossier_key), bindings=bindings
            )
        stored_dossier = put_record_in_transaction(
            mutate_conn, run.run_id, kind=KIND_DOSSIER_VERSION,
            record_key=str(dossier_key), version=1,
            payload=payload, digest=str(payload["digest"]),
        )
        persist_qualifications(mutate_conn, run.run_id, [str(dossier_key)])
        amended = amend_in_transaction(
            mutate_conn,
            property_id=prop_uuid,
            payload=custody_payload,
            authorized_by=str(principal).strip(),
            reason=str(reason or ""),
        )
        snapshot_in_transaction(
            mutate_conn, dossier_id=dossier_uuid, version=amended.version
        )
        _save_link_in_transaction(
            mutate_conn, run.run_id, str(dossier_key),
            {
                "dossier_id": str(dossier_uuid),
                "custody_version": amended.version,
                "property_id": str(prop_uuid),
                "dossier_key": str(dossier_key),
                "receipts": {},
            },
            0,
        )
        _ensure_receipt_in_transaction(
            mutate_conn, run.run_id, str(dossier_key), dossier_id=dossier_uuid,
            version=amended.version,
            kind="forward", from_agent="gabriel", to_agent="raphael",
            principal=str(principal).strip(),
            note="successor assessment ready for Raphael",
        )
        decision = authorize_in_transaction(mutate_conn, run.run_id, str(dossier_key))
        if decision["outcome"] == "authorized":
            _ensure_receipt_in_transaction(
                mutate_conn, run.run_id, str(dossier_key), dossier_id=dossier_uuid,
                version=amended.version,
                kind="forward", from_agent="raphael", to_agent="onesimus",
                principal=str(principal).strip(),
                note="successor authorized for engineering scenarios",
            )
            advance_stage_in_transaction(mutate_conn, run.run_id, "created", "raphael")
        else:
            basis = decision.get("basis", []) or []
            _ensure_receipt_in_transaction(
                mutate_conn, run.run_id, str(dossier_key), dossier_id=dossier_uuid,
                version=amended.version,
                kind="return", from_agent="raphael", to_agent="gabriel",
                principal=str(principal).strip(),
                note=(
                    f"Raphael {decision['outcome']}: "
                    f"{str(basis[0])[:160] if basis else 'see basis'}"
                ),
            )
            advance_stage_in_transaction(
                mutate_conn, run.run_id, "created", "done", status="complete_stopped"
            )
        link, _ = _link_in_transaction(mutate_conn, run.run_id, str(dossier_key))
        return {
            "run_id": str(run.run_id),
            "replayed": False,
            "dossier_key": str(dossier_key),
            "dossier_digest": str(stored_dossier.digest or ""),
            "custody_version": amended.version,
            "decision": str(decision.get("outcome", "")),
            "decision_basis": list(decision.get("basis", []) or []),
            "receipts": dict(link.get("receipts", {}) or {}),
        }

    return run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event,
        mutate=_mutate,
    )


def _successor_replay_or_conflict(
    latest: Any, expected_version: int, intended: dict[str, Any]
) -> None:
    """Replay an identical committed successor, else fail on conflict.

    When the chain tip already carries this exact successor payload
    (a committed identical attempt), the caller re-derives stored
    outputs. Anything else -- a genuinely different successor or a
    foreign amendment -- raises instead of duplicating or forking.
    """
    from app.workflow_db import WorkflowConflictError

    if latest.version == expected_version + 1 and dict(latest.payload or {}) == intended:
        return
    raise WorkflowConflictError(
        "dossier chain moved since approval; re-preview before committing"
    )
