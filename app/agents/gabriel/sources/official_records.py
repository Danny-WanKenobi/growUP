"""Thin official-record admission bridge (Milestone 2 continuation).

Retained official discovery (currently the staged ``dcad_bulk``
exchange) already created operational properties and permanent
``gabriel.discovery.decision`` events. The default Brave evaluation
path cannot consume those records without disguising them as search
snippets, so this bridge admits them honestly:

- It loads ONE server-stored discovery decision by exact
  discovery-run / event / property identity and validates it.
- It preserves the raw observed fields, source, retrieval time and a
  digest computed over the retained payload. Caller-supplied
  provenance or ``verified`` flags are never accepted.
- It builds only observed facts (address, category hint as an
  *observed* claim) through the existing ``PropertyDossier`` /
  attribution contracts. A staged CSV row is an official-source
  claim, not authenticated publisher evidence merely because its
  filename says DCAD, so no ``candidate_verification`` is minted:
  Raphael therefore returns ``needs_evidence`` unless genuine
  authenticated retained evidence later supports verification
  through the existing shared policy (not this bridge).
- Unknown occupancy / distress / zoning / county stay unknown.
  County credit requires the existing Census collector claim;
  staged filename, bbox or operational county never mint it.

Persistence reuses ``persist_dossier_versions`` /
``persist_qualifications`` and the workflow record store, so
property identity, dossier identity, custody and pilot lineage stay
shared with the Brave path. Same-key replay is digest-checked by
``put_record_in_transaction``: a mismatched evidence digest or a
property/address mismatch fails closed.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
from app.workflow_db import WorkflowError, get_record_in_transaction, put_record_in_transaction

OFFICIAL_SOURCES = frozenset({"dcad_bulk"})
# Generic retained discovery is admitted with clearly labeled
# provenance: a generic open-data source is *not* thereby official
# or authenticated, and no candidate_verification is minted for it.
GENERIC_SOURCES = frozenset({"generic_socrata", "generic_arcgis", "generic_ckan"})
ADMITTED_SOURCES = OFFICIAL_SOURCES | GENERIC_SOURCES
BRIDGE_VERSION = "official-bridge-1"
DECISION_EVENT_TYPE = "gabriel.discovery.decision"


class OfficialRecordError(WorkflowError):
    """Retained official evidence is missing, mismatched or untrusted."""


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def evidence_digest_for(discovery_run_id: str, event_id: str, details: dict[str, Any]) -> str:
    return digest_payload({
        "discovery_run_id": str(discovery_run_id),
        "event_id": str(event_id),
        "details": details,
    })


def load_retained_official_in_transaction(
    conn: Any, *, discovery_run_id: str, event_id: str, property_id: str,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Load and validate one retained discovery decision (read-only).

    Exact identity is required: run, event and property must all
    match the stored permanent Gabriel decision, its recorded result
    must name the same property, and the operational property row's
    address must match the observed address. Any mismatch refuses.
    """
    run_id_text = str(discovery_run_id or "").strip()
    event_id_text = str(event_id or "").strip()
    if not run_id_text or not event_id_text:
        raise OfficialRecordError("discovery_run_id and event_id are required")
    try:
        prop_uuid = UUID(str(property_id or "").strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise OfficialRecordError("property_id must be an operational UUID") from exc
    if str(prop_uuid).startswith("demo-"):
        raise OfficialRecordError("demo identifiers cannot use official admission")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM agent_events WHERE run_id = %(run)s "
            "AND event_id = %(event)s AND property_id = %(prop)s "
            "AND event_type = %(etype)s",
            {"run": run_id_text, "event": event_id_text,
             "prop": str(prop_uuid), "etype": DECISION_EVENT_TYPE},
        )
        row = cur.fetchone()
    if row is None:
        raise OfficialRecordError("retained discovery decision not found for exact run/event/property")
    event = dict(row)
    if event.get("agent") != "gabriel" or event.get("retention_class") != "permanent":
        raise OfficialRecordError("retained event is not a Gabriel discovery decision")
    details = event.get("details") or {}
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError as exc:
            raise OfficialRecordError("retained discovery details are malformed") from exc
    if not isinstance(details, dict):
        raise OfficialRecordError("retained discovery details are malformed")
    if details.get("outcome") != "created" or details.get("mutation_status") != "COMMITTED":
        raise OfficialRecordError("official admission requires a committed discovery creation")
    result = details.get("result") or {}
    if str(result.get("property_id", "") or "") != str(prop_uuid):
        raise OfficialRecordError("retained discovery decision is not bound to this property")
    observed = details.get("observed") or {}
    if not isinstance(observed, dict) or not observed:
        raise OfficialRecordError("retained discovery decision carries no observed fields")
    source = str(observed.get("source", "") or "")
    if source not in ADMITTED_SOURCES:
        raise OfficialRecordError(f"source {source!r} is not an admitted retained discovery source")
    if not str(observed.get("street_address", "") or "").strip():
        raise OfficialRecordError("retained observed fields carry no street address")
    digest = evidence_digest_for(run_id_text, event_id_text, details)
    if expected_digest is not None and str(expected_digest) != digest:
        raise OfficialRecordError("official evidence digest mismatch; refusing")
    # Operational property row must be the same property/address.
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE id = %(id)s", {"id": str(prop_uuid)})
        prop_row = cur.fetchone()
    if prop_row is None:
        raise OfficialRecordError("operational property row missing for retained discovery")
    prop = dict(prop_row)
    if _norm(prop.get("street_address")) != _norm(observed.get("street_address")):
        raise OfficialRecordError("dossier address does not match original property identity")
    for field in ("city", "state"):
        if _norm(observed.get(field)) and _norm(prop.get(field)) and _norm(observed.get(field)) != _norm(prop.get(field)):
            raise OfficialRecordError(f"retained {field} does not match operational property")
    return {
        "discovery_run_id": run_id_text,
        "event_id": event_id_text,
        "property_id": str(prop_uuid),
        "source": source,
        "source_kind": "official" if source in OFFICIAL_SOURCES else "generic_open_data",
        "collection_provenance": (
            "official_retained" if source in OFFICIAL_SOURCES else "generic_retained"),
        "external_id": str(observed.get("external_id", "") or ""),
        "element_url": str(observed.get("element_url", "") or ""),
        "retrieved_at": str(observed.get("retrieved_at", "") or ""),
        "observed": dict(observed),
        "structured": dict(details.get("structured") or {}) if isinstance(details.get("structured"), dict) else {},
        "details": details,
        "evidence_digest": digest,
        "property": prop,
    }


def find_retained_official_in_transaction(conn: Any, *, property_id: str) -> dict[str, Any] | None:
    """Latest retained official decision for one property, or None."""
    try:
        prop_uuid = UUID(str(property_id or "").strip())
    except (ValueError, AttributeError, TypeError):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM agent_events WHERE property_id = %(prop)s "
            "AND event_type = %(etype)s ORDER BY occurred_at DESC, event_id DESC LIMIT 20",
            {"prop": str(prop_uuid), "etype": DECISION_EVENT_TYPE},
        )
        rows = cur.fetchall() or []
    for row in rows:
        event = dict(row)
        details = event.get("details") or {}
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                continue
        observed = (details or {}).get("observed") or {}
        if str(observed.get("source", "") or "") not in ADMITTED_SOURCES:
            continue
        try:
            return load_retained_official_in_transaction(
                conn, discovery_run_id=str(event.get("run_id", "")),
                event_id=str(event.get("event_id", "")), property_id=str(prop_uuid))
        except OfficialRecordError:
            continue
    return None


def build_official_dossier(retained: dict[str, Any]) -> Any:
    """Build one observed-only PropertyDossier from retained fields."""
    from app.agents.gabriel.attribution import (
        CategoryObservation, CountyAttribution, evaluate_category_evidence)
    from app.agents.gabriel.dossier import ConsolidatedClaim, PropertyDossier
    from app.agents.gabriel.ranking import QCStatus

    observed = retained["observed"]
    prop = retained["property"]
    generic = retained.get("source_kind") == "generic_open_data"
    origin = "generic_record_claim" if generic else "official_record_claim"
    category_hint = str(observed.get("category_hint", "") or "other")
    observations = [CategoryObservation(value=category_hint, origin=origin,
                                       source_url=retained.get("element_url", ""))]
    structured = retained.get("structured") or {}
    if generic and structured.get("platform_record"):
        # Preserve each observed category-bearing field separately so
        # mixed/contradictory observations stay contradictory.
        from app.agents.gabriel.sources.generic_open_data import category_from_observed

        try:
            record = json.loads(structured["platform_record"])
            if not isinstance(record, dict):
                raise ValueError("invalid platform record")
        except (ValueError, TypeError) as exc:
            raise OfficialRecordError("retained platform record is malformed") from exc
        for field in ("land_use", "use_code", "property_class", "classification",
                      "building_class", "building", "building_type"):
            value = str(record.get(field) or "").strip()
            if not value:
                continue
            mapped = category_from_observed({field: value,
                                             "num_stories": record.get("num_stories", ""),
                                             "stories": record.get("stories", "")})
            if mapped != "other":
                observations.append(CategoryObservation(
                    value=mapped, origin=origin,
                    source_url=str(structured.get("dataset_url") or "")))
    if structured.get("improvements"):
        from app.agents.gabriel.sources.dcad_bulk import map_land_use
        try:
            improvements = json.loads(structured["improvements"])
            if not isinstance(improvements, list) or not all(isinstance(i, dict) for i in improvements):
                raise ValueError("invalid improvement rows")
        except (ValueError, TypeError) as exc:
            raise OfficialRecordError("retained improvement detail is malformed or truncated") from exc
        for improvement in improvements:
            value = map_land_use("", str(improvement.get("BLDG_CLASS_DESC") or ""),
                                 str(improvement.get("NUM_STORIES") or ""))
            if value != "other":
                observations.append(CategoryObservation(value=value, origin="official_record_claim",
                                                       source_url=retained.get("element_url", "")))
    attribution = evaluate_category_evidence(observations)
    source_ref = (structured.get("dataset_url") or retained.get("element_url")
                  or f"{retained['source']}:{retained.get('external_id', '')}")
    facts = []
    statement_bits = [f"{key}={observed[key]}" for key in
                      ("street_address", "city", "state", "zip_code", "category_hint", "external_id")
                      if observed.get(key)]
    structured = retained.get("structured") or {}
    for key in sorted(structured):
        if structured.get(key) not in (None, ""):
            statement_bits.append(f"{key}={structured[key]}")
    facts.append(ConsolidatedClaim(
        topic="official_record" if not generic else "generic_record",
        statement=("Observed generic open-data claim (unverified; not official/authenticated): "
                   if generic else "Observed official-source claim (unverified): ")
        + "; ".join(statement_bits)[:280],
        evidence=[]))
    missing = ["occupancy unknown", "vacancy/distress unknown", "zoning unknown",
               "county unverified (no Census collector claim)", "category not verified by authenticated publisher evidence"]
    return PropertyDossier(
        key="main",
        name=(str(observed.get("name", "") or "") or str(prop.get("name", "") or "")) or None,
        street_address=str(prop.get("street_address", "") or "") or None,
        city=str(prop.get("city", "") or "") or None,
        state=str(prop.get("state", "") or "") or None,
        qc_status=QCStatus.PASS,
        supported_facts=facts,
        sources=[source_ref] if source_ref else [],
        missing=missing,
        remaining_questions=list(missing),
        summary=("Generic retained discovery admitted as observed claim only; "
                 "not official/authenticated; verification pending." if generic else
                 "Official retained discovery admitted as observed claim only; verification pending."),
        county_attribution=CountyAttribution(),
        category_attribution=attribution,
    )


def admit_official_in_transaction(
    conn: Any, run_id: UUID, retained: dict[str, Any], census: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Persist artifact + dossier + qualification; return keys/outcomes.

    The artifact is recorded with its real provider and
    ``official_retained`` provenance (never Brave / adapter_collected
    search results). ``candidate_verification`` stays absent.
    """
    from app.agents.gabriel.dossier import DossierSet
    from app.agents.gabriel.persist import persist_dossier_versions, persist_qualifications

    artifact_key = f"official:{retained['discovery_run_id']}:{retained['event_id']}"
    existing = get_record_in_transaction(conn, run_id, KIND_RESEARCH_ARTIFACT, artifact_key)
    if existing is not None:
        if str((existing.payload or {}).get("evidence_digest", "")) != retained["evidence_digest"]:
            raise OfficialRecordError("official evidence digest mismatch on replay; refusing")
    else:
        payload = {
            "provider": retained["source"],
            "assignment_id": artifact_key,
            "query": "",
            "results": [],
            "structured_observed": retained["observed"],
            "structured": retained.get("structured") or {},
            "discovery_run_id": retained["discovery_run_id"],
            "event_id": retained["event_id"],
            "property_id": retained["property_id"],
            "external_id": retained.get("external_id", ""),
            "retrieved_at": retained.get("retrieved_at", ""),
            "evidence_digest": retained["evidence_digest"],
            "collection_provenance": retained.get("collection_provenance", "official_retained"),
            "source_kind": retained.get("source_kind", "official"),
            "dataset_url": str((retained.get("structured") or {}).get("dataset_url", "") or ""),
            "selection_digest": str((retained.get("structured") or {}).get("selection_digest", "") or ""),
            "resource_digest": str((retained.get("structured") or {}).get("resource_digest", "") or ""),
            "attempt": "completed",
            "error": "",
        }
        put_record_in_transaction(conn, run_id, kind=KIND_RESEARCH_ARTIFACT,
                                  record_key=artifact_key, version=1,
                                  payload=payload, digest=digest_payload(payload))
    dossier = build_official_dossier(retained)
    dossier_set = DossierSet(dossiers=[dossier], dossier_version=BRIDGE_VERSION)
    if census is not None:
        from app.mvp import _apply_census_in_stage
        _apply_census_in_stage(conn, run_id, dossier_set,
                              {"main": retained["property_id"]}, retained["property_id"], census)
    persist_dossier_versions(conn, run_id, dossier_set, None, [artifact_key],
                             property_ids={"main": retained["property_id"]})
    outcomes = persist_qualifications(conn, run_id, ["main"])
    return ["main"], outcomes
