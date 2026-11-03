"""Offline re-assessment of the same dossier using retained authenticated evidence.

No transport is imported. An enrolled amendment authorizer approves exact preview
inputs; historical records stay immutable. A filename or submitted verdict is not
source authentication.
"""
from copy import deepcopy
from uuid import UUID
import re

from app.workflow import digest_payload
from app.workflow_db import (WorkflowError,get_record_in_transaction,list_records_in_transaction,
    create_run_in_transaction,get_run_by_key_in_transaction,put_record_in_transaction)
from app.custody import dossier_id_for_property,check_amendment_permission
from app.custody_db import (verify_chain_in_transaction,current_custody_in_transaction,
    latest_version_in_transaction,lock_dossier_in_transaction,amend_in_transaction,
    snapshot_in_transaction,issue_receipt_in_transaction)


def _integrity(record,label):
    if record is None or record.digest != digest_payload(record.payload):
        raise WorkflowError(f"retained {label} failed integrity verification")
    return record.payload


def preview_in_transaction(conn, *, parent_run_id, dossier_key, collection_run_id,
                           site_inputs=None, financial_inputs=None):
    from app.agents.gabriel.sources import dcad
    from app.agents.gabriel.attribution import CategoryObservation,evaluate_category_evidence
    from app.agents.raphael.inspection import build_inspection_report
    from app.agents.raphael.service import evaluate_dossier
    parent_id=UUID(str(parent_run_id)); collection_id=UUID(str(collection_run_id))
    parent=get_record_in_transaction(conn,parent_id,"dossier_version",dossier_key)
    if parent is None or parent.digest != digest_payload({k:v for k,v in parent.payload.items() if k!="digest"}):
        raise WorkflowError("parent dossier failed digest verification")
    payload=deepcopy(parent.payload)
    prop_id=UUID(str(payload.get("property_id") or ""))
    did=dossier_id_for_property(prop_id)
    verify_chain_in_transaction(conn,did)
    latest=latest_version_in_transaction(conn,did)
    holder=current_custody_in_transaction(conn,did)
    if latest is None or not holder or holder["version"]>latest.version or latest.payload.get("dossier_digest")!=parent.digest or latest.payload.get("dossier")!=payload.get("dossier"):
        raise WorkflowError("parent is not the current custody version; reselect latest dossier")
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE id = %(id)s",{"id":str(prop_id)})
        row=cur.fetchone()
    if row is None:
        raise WorkflowError("operational property is missing")
    prop=dict(row)
    dossier=payload.get("dossier") or {}
    from app.agents.gabriel.extractor import normalize_text
    if not dossier.get("street_address") or normalize_text(dossier.get("street_address")) != normalize_text(prop.get("street_address")):
        raise WorkflowError("dossier and operational street identity disagree")
    collections=list_records_in_transaction(conn,collection_id,"dcad_collection")
    if len(collections)!=1:
        raise WorkflowError("select one retained collector-minted DCAD collection")
    record=collections[0]; collection=_integrity(record,"DCAD collection")
    account=str(collection.get("account") or "")
    assessments=list_records_in_transaction(conn,collection_id,"dcad_assessment")
    if len(assessments)!=1:
        raise WorkflowError("retained collection needs its original property-bound assessment")
    original_assessment=_integrity(assessments[0],"original DCAD assessment")
    if (original_assessment.get("property_id")!=str(prop_id)
            or original_assessment.get("dossier_key")!=dossier_key
            or original_assessment.get("account")!=account
            or original_assessment.get("collection_digest")!=record.digest):
        raise WorkflowError("retained collection was not assessed for this property/dossier")
    for field in ("city","state"):
        if (normalize_text(dossier.get(field))!=normalize_text(prop.get(field))):
            raise WorkflowError(f"dossier and operational {field} identity disagree")
    retained_lineage=[]
    inherited={entry["record_key"]:entry for entry in
               (payload.get("retained_evidence_lineage") or []) if isinstance(entry,dict)
               and entry.get("record_key")}
    # Older successor evaluations already carry explicit source run/key/digests.
    from app.mvp import _successor_artifact_lineage
    inherited={**_successor_artifact_lineage(payload),**inherited}
    for ref in payload.get("artifact_refs") or []:
        source_run=parent_id
        artifact=get_record_in_transaction(conn,parent_id,"research_artifact",str(ref))
        locator=inherited.get(str(ref))
        if artifact is None and locator:
            source_run=UUID(str(locator.get("run_id") or ""))
            artifact=get_record_in_transaction(conn,source_run,"research_artifact",str(ref))
        original=_integrity(artifact,"original discovery artifact")
        if locator and artifact.digest!=locator.get("digest"):
            raise WorkflowError("inherited artifact lineage digest disagrees")
        retained_lineage.append({"run_id":str(source_run),"record_key":str(ref),"digest":artifact.digest})
        if original.get("collection_provenance")=="official_retained":
            external=str(original.get("external_id") or "")
            if external.startswith("dcad:") and external!="dcad:"+account:
                raise WorkflowError("retained account disagrees with the original discovered account")
    claim=get_record_in_transaction(conn,collection_id,"dcad_claim",f"dcad:{account}:claim",1)
    claimed=_integrity(claim,"DCAD transmission claim")
    if (claimed.get("account")!=account or claimed.get("principal")!=collection.get("principal")
            or claimed.get("collector_revision")!=dcad.DCAD_COLLECTOR_REVISION
            or claimed.get("state")!="started"):
        raise WorkflowError("collection does not bind its original transmission claim")
    if (collection.get("status_code")!=200 or "text/html" not in str(collection.get("content_type","")).lower()
            or collection.get("body_bytes")!=len(str(collection.get("body_text","")).encode("utf-8"))):
        raise WorkflowError("retained source response metadata is invalid")
    # Derive recorded classification from the actual retained bytes. Reuse the
    # policy without expanding a source registry or asserting current occupancy.
    parsed=dcad.parse_account_page(collection.get("body_text",""),expected_account=account)
    uses={u for entry in parsed["improvements"] for u in dcad._description_buckets(entry["description_raw"])}
    classification={"warehouse":"warehouse_industrial","office":"commercial_property","retail":"commercial_property"}
    prior_category=dossier.get("category_attribution") or {}
    requested=prior_category.get("verified") or prior_category.get("observed") or ""
    if not requested and len(uses)==1:
        requested=classification.get(next(iter(uses)),"")
    assessment=dcad.build_dcad_assessment(collection=collection,collection_digest=record.digest,
        property_record=prop,dossier_key=dossier_key,claim_scope=requested,retained_only=True)
    observations=[CategoryObservation.model_validate(o) for o in prior_category.get("observations",[])]
    if len(uses)==1:
        observed=classification.get(next(iter(uses)))
        if observed:
            observations.append(CategoryObservation(value=observed,origin="authenticated_dcad_recorded_class",
                                                   source_url=collection["url"]))
    attribution=evaluate_category_evidence(observations).model_dump(mode="json")
    if prior_category.get("status")=="contradictory":
        attribution=deepcopy(prior_category)  # never erase an unresolved contradiction
    dossier["category_attribution"]=attribution
    previous_reports=dossier.pop("reports",None)
    if previous_reports:
        dossier.setdefault("report_history",[]).append({"run_id":str(parent_id),"dossier_digest":parent.digest,
                                                       "reports":previous_reports})
    dossier["sources"]=list(dict.fromkeys([*(dossier.get("sources") or []),collection["url"]]))
    refs={"run_id":str(collection_id),"record_key":record.record_key,"digest":record.digest,
          "claim_record_key":claim.record_key,"claim_digest":claim.digest,
          "assessment_record_key":assessments[0].record_key,"assessment_digest":assessments[0].digest,"account":account,
          "scope":"recorded appraisal category/account only; not building capacity or current use"}
    dossier["authenticated_category_evidence"]=refs
    candidate={**payload,"version":1,"retained_evidence_lineage":retained_lineage,"dossier":dossier,"candidate_verification":assessment["dcad_only_verification"]}
    candidate.pop("digest",None)
    candidate["digest"]=digest_payload(candidate)
    inspection=build_inspection_report(candidate,site_inputs=site_inputs or {})
    result={"parent_run_id":str(parent_id),"dossier_key":dossier_key,"property_id":str(prop_id),
        "parent_digest":parent.digest,"parent_version":parent.version,"custody_version":latest.version,
        "holder":holder["holder"],"collection":refs,"assessment":assessment,"dossier":candidate,
        "decision":evaluate_dossier(candidate),"inspection_preview":inspection,
        "site_inputs":deepcopy(site_inputs or {}),"financial_inputs":deepcopy(financial_inputs or {}),
        "provider_requests":0,"discovery_requests":0,"outreach_actions":0}
    # Validate money before any writes; neither units nor currency may silently change.
    from app.michael import financial_inputs_from_raw
    from app.michael import FINANCIAL_NAMES
    if set(financial_inputs or {})-set(FINANCIAL_NAMES):
        raise WorkflowError("unknown financial input name; physical inputs belong in site_inputs")
    if set(site_inputs or {}) & set(FINANCIAL_NAMES):
        raise WorkflowError("financial inputs belong in financial_inputs")
    financial_inputs_from_raw(financial_inputs or {},scenario="bench")
    result["preview_digest"]=digest_payload(result)
    return result


def commit_in_transaction(conn, *, principal, reason, preview_digest, **inputs):
    from app import mvp
    mvp._require_enabled()
    check_amendment_permission(principal,reason)
    if not re.fullmatch("[0-9a-f]{64}",str(preview_digest)):
        raise WorkflowError("exact preview digest is required")
    key="retained-assessment:"+digest_payload({"principal":principal,"preview":preview_digest})
    prior=get_run_by_key_in_transaction(conn,key)
    if prior:
        saved=get_record_in_transaction(conn,prior.run_id,"research_artifact","assessment-result",1)
        result=_integrity(saved,"assessment replay")
        if result["inputs"]!=inputs or result["reason"]!=reason or result["principal"]!=principal:
            raise WorkflowError("reassessment replay changed approved inputs/authorizer/reason")
        return result["result"]
    # Serialize concurrent previews/commits on the same canonical dossier.
    parent=get_record_in_transaction(conn,UUID(str(inputs["parent_run_id"])),"dossier_version",inputs["dossier_key"])
    if parent is None:
        raise WorkflowError("parent dossier missing")
    did=dossier_id_for_property(parent.payload["property_id"])
    lock_dossier_in_transaction(conn,did)
    # A concurrent identical commit may have completed while we waited.
    prior=get_run_by_key_in_transaction(conn,key)
    if prior:
        saved=get_record_in_transaction(conn,prior.run_id,"research_artifact","assessment-result",1)
        result=_integrity(saved,"assessment replay")
        if result["inputs"]!=inputs or result["reason"]!=reason or result["principal"]!=principal:
            raise WorkflowError("reassessment replay changed approved inputs/authorizer/reason")
        return result["result"]
    preview=preview_in_transaction(conn,**inputs)
    if preview["preview_digest"]!=preview_digest:
        raise WorkflowError("evidence/custody/inputs changed; preview again")
    run=create_run_in_transaction(conn,idempotency_key=key,stage="gabriel",
        limits={"stop_after_michael":True,"retained_assessment":preview_digest})
    old_holder=preview["holder"]
    if old_holder!="gabriel":
        issue_receipt_in_transaction(conn,dossier_id=did,version=preview["custody_version"],
            kind="return",from_agent=old_holder,to_agent="gabriel",issued_by=principal,note=reason)
    payload=preview["dossier"]
    stored=put_record_in_transaction(conn,run.run_id,kind="dossier_version",record_key=inputs["dossier_key"],
        version=1,payload=payload,digest=payload["digest"])
    canonical={"dossier":payload["dossier"],"dossier_digest":stored.digest,"run_id":str(run.run_id),
               "dossier_key":inputs["dossier_key"],"parent_run_id":inputs["parent_run_id"],"preview_digest":preview_digest}
    version=amend_in_transaction(conn,property_id=preview["property_id"],payload=canonical,
                                 authorized_by=principal,reason=reason)
    snapshot_in_transaction(conn,dossier_id=did,version=version.version)
    mvp._save_link_in_transaction(conn,run.run_id,inputs["dossier_key"],
        {"dossier_id":str(did),"custody_version":version.version,"property_id":preview["property_id"],
         "dossier_key":inputs["dossier_key"],"receipts":{}},0)
    merged={**(inputs.get("site_inputs") or {}),**(inputs.get("financial_inputs") or {})}
    result=mvp.evaluate_real(conn,run.run_id,key,"retained evidence only",site_inputs=merged,
                            dossier_key=inputs["dossier_key"],principal=principal)
    approved={"principal":principal,"reason":reason,"inputs":inputs,"result":result,
              "preview_digest":preview_digest,"parent_digest":preview["parent_digest"]}
    put_record_in_transaction(conn,run.run_id,kind="research_artifact",record_key="assessment-result",version=1,
                              payload=approved,digest=digest_payload(approved))
    return result


def commit_audited(**kwargs):
    from app.observability.transactions import run_consequential_transaction
    from app.observability.policy import OperationClass
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    event=AgentEvent(agent=AgentId.WONDRUE,event_type="wondrue.retained_assessment",summary="Approved retained-evidence reassessment",
        details={"preview_digest":kwargs.get("preview_digest"),"parent_run_id":kwargs.get("parent_run_id")},
        retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,
                                        mutate=lambda conn:commit_in_transaction(conn,**kwargs))
