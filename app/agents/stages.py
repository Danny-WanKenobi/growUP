"""Shared version-one specialist preparation, using canonical custody only.

No discovery, models, legal/financial advice engine, synthetic records or external
execution. Workers are not installed or started. Entrypoints consume stored
records only; missing information produces requests, never invented findings.
"""
from copy import deepcopy
from uuid import UUID

from app.agents.registry import AGENT_SPECS, HANDOFF_CONTRACT
from app.workflow import digest_payload
from app.workflow_db import WorkflowError, get_record_in_transaction, put_record_in_transaction

REPORT_SCHEMA = "agent-intake-report-1"
OWNER_TASK_KIND = "owner_review_task"


def build_report(agent, payload):
    """Pure intake/report preparation; does not create or authorize a dossier."""
    if agent not in AGENT_SPECS:
        raise WorkflowError("unknown downstream agent")
    if not isinstance(payload, dict) or not payload.get("property_id") or not payload.get("digest"):
        raise WorkflowError("a stored property-bound dossier is required")
    if payload["digest"] != digest_payload({k:v for k,v in payload.items() if k!="digest"}):
        raise WorkflowError("input dossier failed digest verification")
    spec = AGENT_SPECS[agent]
    dossier = payload.get("dossier") or {}
    reports = dossier.get("reports") or {}
    supplied = (dossier.get("specialist_inputs") or {}).get(agent) or {}
    if not isinstance(supplied, dict):
        raise WorkflowError("specialist inputs must be a mapping")
    report_refs = {}
    missing = []
    for name in spec.required_reports:
        report = reports.get(name)
        if not isinstance(report, dict) or report.get("digest") != digest_payload({k:v for k,v in report.items() if k!="digest"}):
            missing.append("report:"+name)
        else:
            report_refs[name] = report["digest"]
    inputs = {}
    for name in spec.required_inputs:
        entry = supplied.get(name)
        if not isinstance(entry, dict) or entry.get("value") in (None, "", [], {}):
            missing.append(name)
            continue
        refs = entry.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(r,str) or not r.strip() for r in refs):
            missing.append(name+":evidence_refs")
            continue
        inputs[name] = {"value":deepcopy(entry["value"]), "evidence_refs":list(refs),
                        "evidence_status":"provided_unverified"}
    campaign = (reports.get('uriel') or {}).get('campaign') or {}
    if agent == 'priscilla' and not campaign.get('campaign_id'):
        missing.append('retained_campaign_correlation')
    if agent == 'priscilla' and (dossier.get('tracking_policy') or {}).get('require_owner_review'):
        missing.append('tracking_owner_review')
    requests = [{"requirement":name, "reason":"Required input or report is absent/unresolved.",
                 "status":"pending", "authority":"human_owner", "grants_permission":False} for name in missing]
    result = {
        "schema_version":REPORT_SCHEMA, "agent":agent, "name":spec.name, "role":spec.role,
        "source_dossier_digest":payload["digest"], "property_id":str(payload["property_id"]),
        "status":"needs_evidence" if missing else "prepared_not_approved",
        "deliverable":spec.deliverable, "inherited_report_digests":report_refs,
        "supplied_inputs":inputs, "missing":missing, "owner_review_requests":requests,
        "evidence_refs":deepcopy(dossier.get("sources") or []),
        "next_steps":["Obtain or resolve "+name for name in missing],
        "deferred":list(spec.deferred), "handoff_contract":list(HANDOFF_CONTRACT),
        "execution":{"external_actions":0, "provider_calls":0, "signing_authority":False,
                     "business_approval":False, "simulated_events_created":0},
        "limitations":["Intake preparation only; provided references have not been independently authenticated by this agent.",
                       "A complete checklist is not a permit, underwriting decision, signed contract, closed deal, tax filing, insurance coverage or asset ownership."],
    }
    if agent == "uriel":
        from app.uriel import prepare_campaign
        if any(name.startswith('report:') for name in missing):
            result['campaign'] = {'status':'needs_evidence','campaign_id':None,'missing':list(missing)}
        else:
            result['campaign'] = prepare_campaign(payload)
            for gap in result['campaign']['missing']:
                if gap not in missing:
                    missing.append(gap)
                    result['owner_review_requests'].append({'requirement':gap,'reason':'Campaign input is unresolved.',
                        'status':'pending','authority':'human_owner','grants_permission':False})
            if missing:
                result['status']='needs_evidence'
                result['next_steps']=['Obtain or resolve '+name for name in missing]
        result["opportunity_brief"] = {
            "property":dossier.get("street_address") or "Unknown address",
            "financial_assessments":deepcopy((reports.get("michael") or {}).get("assessments") or {}),
            "claim_scope":"Conditional stored calculations only; no promised savings or profit.",
            "package_runtime":"app.uriel.prepare_package_in_transaction; no duplicate package store"}
    if agent == "priscilla":
        result['tracking_readiness'] = {'campaign_id':campaign.get('campaign_id'),
            'package_entry':(reports.get('uriel') or {}).get('package_entry'),
            'status':('suppressed' if campaign.get('status')=='suppressed' else 'awaiting_observations') if campaign else 'needs_campaign',
            'delivered':None,'replied':None,'interested':None,
            'scope':'No engagement is inferred from campaign preparation; query the event timeline for observations.'}
        result["tracking"] = {"runtime":"app.priscilla.metrics_in_transaction",
                              "actual_engagement":"unknown; read recorded events, never infer from prepared outreach",
                              "simulated_events_created":0}
    if agent == "nicole":
        from app.agents.nicole import assess
        result["regulatory_assessment"] = assess(payload["property_id"], supplied)
        for gap in result["regulatory_assessment"]["missing"]:
            if gap not in missing:
                missing.append(gap)
                result["owner_review_requests"].append({"requirement":gap,
                    "reason":"Nicole regulatory intake remains unresolved.", "status":"pending",
                    "authority":"human_owner", "grants_permission":False})
        result["owner_review_requests"].extend(result["regulatory_assessment"]["owner_review_requests"])
        result["status"] = "needs_evidence" if missing else "prepared_not_approved"
        result["next_steps"] = ["Obtain or resolve "+name for name in missing]
        if not missing:
            result["next_steps"].append("Preliminary underwriting may inspect this report; regulatory clearance remains unestablished.")
    if agent == "theresia_keys":
        from app.agents.theresia import assess
        result["underwriting_assessment"] = assess(payload["property_id"], supplied, reports)
        for gap in result["underwriting_assessment"]["missing"]:
            if gap not in missing:
                missing.append(gap)
                result["owner_review_requests"].append({"requirement":gap,
                    "reason":"Theresia underwriting evidence or planning inputs remain unresolved.",
                    "status":"pending","authority":"human_owner","grants_permission":False})
        result["owner_review_requests"].extend(result["underwriting_assessment"]["owner_review_requests"])
        result["status"] = "needs_evidence" if missing else "prepared_not_approved"
        result["next_steps"] = ["Obtain or resolve "+name for name in missing]
        if not missing:
            result["next_steps"].append("Review the unsigned conditional preparation brief; no execution or financing approval is granted.")
    if agent == "simeon":
        from app.agents.simeon import assess
        result["closing_assessment"] = assess(payload["property_id"], supplied, reports)
        for gap in result["closing_assessment"]["missing"]:
            if gap not in missing:
                missing.append(gap)
        result["owner_review_requests"].extend(result["closing_assessment"]["owner_review_requests"])
        result["status"] = "needs_evidence" if missing else "prepared_not_approved"
        result["next_steps"] = ["Obtain or resolve "+name for name in missing]
        result["next_steps"].append("Resolve closing exceptions through the existing owner queue; no signing or closing authority is granted.")
    if agent == "solomon":
        from app.agents.solomon import assess
        result["preconstruction_assessment"] = assess(payload["property_id"], supplied, reports)
        for gap in result["preconstruction_assessment"]["missing"]:
            if gap not in missing:
                missing.append(gap)
                result["owner_review_requests"].append({"requirement":gap,"reason":"Preconstruction evidence is unresolved.",
                    "route":"simeon","status":"pending","authority":"human_owner","grants_permission":False})
        result["owner_review_requests"].extend(result["preconstruction_assessment"]["owner_review_requests"])
        result["status"] = "needs_evidence" if missing else "prepared_not_approved"
        result["next_steps"] = ["Obtain or resolve "+name for name in missing]
        result["next_steps"].append("Retain for planning review; no mobilization, procurement or construction is authorized.")
        if payload.get("synthetic") is True and payload.get("domain")=="synthetic_handoff_demo":
            from app.solomon_simulation import build_vendor_round
            result["vendor_simulation"] = build_vendor_round(payload)
    result["digest"] = digest_payload(result)
    return result


def _load(conn, run_id, dossier_key, agent=None):
    from app.custody import dossier_id_for_property
    from app.custody_db import (verify_chain_in_transaction, latest_version_in_transaction,
                                current_custody_in_transaction, lock_dossier_in_transaction)
    source = get_record_in_transaction(conn, run_id, "dossier_version", dossier_key)
    if source is None:
        raise WorkflowError("stored dossier not found")
    did = dossier_id_for_property(source.payload.get("property_id"))
    if agent is not None:
        lock_dossier_in_transaction(conn, did)
        source = get_record_in_transaction(conn, run_id, "dossier_version", dossier_key)
    verify_chain_in_transaction(conn, did)
    latest = latest_version_in_transaction(conn, did)
    link = get_record_in_transaction(conn, run_id, "custody_link", dossier_key)
    if (latest is None or link is None or latest.version!=link.payload.get("custody_version")
            or latest.payload.get("dossier_digest")!=source.digest
            or latest.payload.get("dossier")!=source.payload.get("dossier")
            or source.digest!=source.payload.get("digest")
            or source.digest!=digest_payload({k:v for k,v in source.payload.items() if k!="digest"})
            or link.digest!=digest_payload(link.payload)
            or str(link.payload.get("dossier_id"))!=str(did)):
        raise WorkflowError("specialist input is not the current shared dossier")
    holder = current_custody_in_transaction(conn, did)
    if agent is not None and (not holder or holder["holder"]!=agent):
        raise WorkflowError("specialist must hold canonical custody before processing")
    return source, did


def preview_in_transaction(conn, *, run_id, dossier_key, agent):
    source, _ = _load(conn, UUID(str(run_id)), dossier_key)
    return {"preview_only":True, "report":build_report(agent, source.payload),
            "records_written":0, "handoff_executed":False}


def prepare_in_transaction(conn, *, run_id, dossier_key, agent):
    """Worker primitive; caller owns transaction/audit. No worker calls it yet."""
    from app.dossier_reports import append_agent_report_in_transaction
    from app.mvp import _require_enabled
    _require_enabled()
    if agent not in AGENT_SPECS:
        raise WorkflowError("unknown downstream agent")
    run_id = UUID(str(run_id))
    source, did = _load(conn, run_id, dossier_key, agent)
    prior = (source.payload.get("dossier",{}).get("reports") or {}).get(agent)
    if prior is not None:
        if (prior.get("schema_version")!=REPORT_SCHEMA or prior.get("agent")!=agent
                or prior.get("digest")!=digest_payload({k:v for k,v in prior.items() if k!="digest"})):
            raise WorkflowError("prior specialist report failed integrity verification")
        return {"report":prior, "replayed":True, "dossier_id":str(did)}
    report = build_report(agent, source.payload)
    stored, version = append_agent_report_in_transaction(conn, run_id, dossier_key, agent=agent, report=report)
    for issue in report["owner_review_requests"]:
        task = {**issue, "agent":agent, "property_id":report["property_id"],
                "dossier_id":str(did), "source_dossier_digest":source.digest,
                "report_digest":report["digest"], "requirement":issue["requirement"]}
        put_record_in_transaction(conn, run_id, kind=OWNER_TASK_KIND,
            record_key="owner:"+digest_payload(task), version=1, payload=task, digest=digest_payload(task))
    return {"report":report, "replayed":False, "dossier_id":str(did),
            "custody_version":version.version, "dossier_digest":stored.digest,
            "handoff_executed":False}


def prepare_audited(*, principal, **inputs):
    """Inactive until explicitly called by a configured internal worker/operator."""
    from app.observability.events import AgentEvent, AgentId, RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    if not principal or not principal.strip():
        raise WorkflowError("authenticated worker/operator principal is required")
    event = AgentEvent(agent=AgentId(inputs["agent"]), run_id=str(inputs["run_id"]), event_type="agent.intake_prepared",
        summary="Preliminary specialist intake; no external execution",
        details={"principal":principal, "run_id":str(inputs["run_id"]), "dossier_key":inputs["dossier_key"]},
        retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event, mutate=lambda conn:prepare_in_transaction(conn, **inputs))


def owner_queue_in_transaction(conn, *, run_id):
    from app.workflow_db import list_records_in_transaction
    rows = list_records_in_transaction(conn, UUID(str(run_id)), OWNER_TASK_KIND)
    result = []
    for row in rows:
        if row.digest != digest_payload(row.payload):
            raise WorkflowError("owner-review task failed integrity verification")
        result.append({"record_key":row.record_key, **row.payload})
    return {"tasks":result, "resolution_automation":"deferred; pending tasks never grant permissions"}
