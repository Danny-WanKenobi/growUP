"""Narrow current-holder report append; never edits inherited property facts."""
from copy import deepcopy
from uuid import UUID

from app.custody import CustodyAuthError, CustodyTamperError
from app.custody_db import (
    _append_version_in_transaction, current_custody_in_transaction,
    latest_version_in_transaction, lock_dossier_in_transaction,
    snapshot_in_transaction, verify_chain_in_transaction,
)
from app.agents.registry import AGENT_SPECS
from app.workflow import digest_payload
from app.workflow_db import WorkflowError, get_record_in_transaction, put_record_in_transaction


def append_agent_report_in_transaction(conn, run_id, dossier_key, *, agent, report):
    if agent in ("yoseph","deborah"):
        raise CustodyAuthError("shared advisers do not append pipeline reports; use consultations")
    if agent not in ("gabriel", "raphael", "onesimus", "michael", *AGENT_SPECS) or not isinstance(report, dict):
        raise CustodyAuthError("only a structured inspection/engineering/financial report can be appended")
    link_record = get_record_in_transaction(conn, run_id, "custody_link", dossier_key)
    source = get_record_in_transaction(conn, run_id, "dossier_version", dossier_key)
    if link_record is None or source is None:
        raise WorkflowError("report append requires the existing shared dossier/custody link")
    dossier_id = UUID(str(link_record.payload["dossier_id"]))
    lock_dossier_in_transaction(conn, dossier_id)
    verify_chain_in_transaction(conn, dossier_id)
    holder = current_custody_in_transaction(conn, dossier_id)
    latest = latest_version_in_transaction(conn, dossier_id)
    # Canonical custody starts with Gabriel before the first receipt.
    if holder is None and agent=="gabriel" and latest is not None and latest.version==1 and latest.created_by=="gabriel":
        holder={"holder":"gabriel"}
    if not holder or holder["holder"] != agent or latest is None:
        raise CustodyAuthError("report author does not hold the dossier")
    if (latest.version != int(link_record.payload["custody_version"])
            or latest.payload.get("dossier_digest") != source.digest
            or latest.payload.get("dossier") != source.payload.get("dossier")):
        raise CustodyTamperError("report append does not bind the latest inherited dossier")
    if source.digest != digest_payload({k: v for k, v in source.payload.items() if k != "digest"}):
        raise CustodyTamperError("workflow dossier failed digest verification")
    report_digest = digest_payload({k: v for k, v in report.items() if k != "digest"})
    if report.get("digest") != report_digest:
        raise CustodyTamperError("agent report failed digest verification")
    reports = (source.payload.get("dossier") or {}).get("reports") or {}
    if agent in reports:
        if reports[agent] == report:
            return source, latest
        raise WorkflowError("agent report already exists; use an explicit new assessment rather than overwrite")
    schema = {"gabriel":"gabriel-discovery-report-1","raphael":"raphael-inspection-1","onesimus":"onesimus-concept-report-1",
              "michael":"michael-finance-report-1"}.get(agent, "agent-intake-report-1")
    if agent in AGENT_SPECS and report.get("agent") != agent:
        raise CustodyTamperError("specialist report names a different author")
    if report.get("schema_version") != schema or report.get("source_dossier_digest") != source.digest:
        raise CustodyTamperError("agent report does not bind its input dossier/schema")
    if agent == "onesimus":
        inspection = (source.payload.get("dossier", {}).get("reports") or {}).get("raphael")
        if not inspection or report.get("source_inspection_digest") != inspection.get("digest"):
            raise CustodyTamperError("engineering report does not bind its inspection")
    if agent == "michael":
        engineering=(source.payload.get("dossier",{}).get("reports") or {}).get("onesimus")
        if not engineering or report.get("source_engineering_digest") != engineering.get("digest"):
            raise CustodyTamperError("financial report does not bind its engineering report")
    payload = deepcopy(source.payload)
    payload["version"] = source.version + 1
    payload["dossier"].setdefault("reports", {})[agent] = deepcopy(report)
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    stored = put_record_in_transaction(
        conn, run_id, kind="dossier_version", record_key=dossier_key,
        version=source.version+1, payload=payload, digest=payload["digest"])
    custody_payload = deepcopy(latest.payload)
    custody_payload.update(dossier=deepcopy(payload["dossier"]), dossier_digest=stored.digest)
    successor = _append_version_in_transaction(
        conn, property_id=latest.property_id, payload=custody_payload,
        created_by=agent, supersedes=latest.version,
        amendment={"kind": "agent_report", "agent": agent,
                   "parent_digest": latest.digest, "report_digest": report_digest})
    snapshot_in_transaction(conn, dossier_id=dossier_id, version=successor.version)
    link = deepcopy(link_record.payload)
    link["custody_version"] = successor.version
    put_record_in_transaction(conn, run_id, kind="custody_link", record_key=dossier_key,
                              version=link_record.version+1, payload=link, digest=digest_payload(link))
    return stored, successor


def append_solomon_tracking_in_transaction(conn, run_id, dossier_key, tracking):
    """Append a held Solomon project state without replacing any agent report."""
    from app.agents.stages import _load
    source, did = _load(conn, run_id, dossier_key, 'solomon')
    link_record = get_record_in_transaction(conn, run_id, 'custody_link', dossier_key)
    latest = latest_version_in_transaction(conn, did)
    payload = deepcopy(source.payload)
    payload['version'] = source.version + 1
    payload['dossier']['solomon_project'] = deepcopy(tracking)
    payload['digest'] = digest_payload({k:v for k,v in payload.items() if k!='digest'})
    stored = put_record_in_transaction(conn, run_id, kind='dossier_version', record_key=dossier_key,
        version=source.version+1, payload=payload, digest=payload['digest'])
    custody_payload = deepcopy(latest.payload)
    custody_payload.update(dossier=deepcopy(payload['dossier']), dossier_digest=stored.digest)
    version = _append_version_in_transaction(conn, property_id=latest.property_id, payload=custody_payload,
        created_by='solomon', supersedes=latest.version,
        amendment={'kind':'solomon_project_update','parent_digest':latest.digest,'tracking_digest':digest_payload(tracking)})
    snapshot_in_transaction(conn, dossier_id=did, version=version.version)
    link = deepcopy(link_record.payload)
    link['custody_version'] = version.version
    put_record_in_transaction(conn, run_id, kind='custody_link', record_key=dossier_key,
        version=link_record.version+1, payload=link, digest=digest_payload(link))
    return stored, version


def append_elmer_correction_in_transaction(conn,run_id,dossier_key,report):
    """Append a linked accounting assessment; inherited report slots are immutable."""
    from app.agents.stages import _load
    source,did=_load(conn,run_id,dossier_key,'elmer')
    if report.get('source_dossier_digest')!=source.digest or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'}):
        raise CustodyTamperError('accounting correction does not bind current dossier')
    latest=latest_version_in_transaction(conn,did)
    link_record=get_record_in_transaction(conn,run_id,'custody_link',dossier_key)
    payload=deepcopy(source.payload)
    history=payload['dossier'].setdefault('elmer_assessments',[])
    if len(history)>=100: raise WorkflowError('accounting assessment history limit reached')
    prior=history[-1] if history else payload['dossier']['reports']['elmer']
    if report.get('parent_assessment_digest')!=prior.get('digest'): raise WorkflowError('accounting assessment parent mismatch')
    history.append(deepcopy(report)); payload['version']=source.version+1
    payload['digest']=digest_payload({k:v for k,v in payload.items() if k!='digest'})
    stored=put_record_in_transaction(conn,run_id,kind='dossier_version',record_key=dossier_key,version=source.version+1,payload=payload,digest=payload['digest'])
    custody_payload=deepcopy(latest.payload)
    custody_payload.update(dossier=deepcopy(payload['dossier']),dossier_digest=stored.digest)
    version=_append_version_in_transaction(conn,property_id=latest.property_id,payload=custody_payload,created_by='elmer',supersedes=latest.version,
        amendment={'kind':'accounting_correction','parent_digest':latest.digest,'assessment_digest':report['digest'],'parent_assessment_digest':prior['digest']})
    snapshot_in_transaction(conn,dossier_id=did,version=version.version)
    link=deepcopy(link_record.payload);link['custody_version']=version.version
    put_record_in_transaction(conn,run_id,kind='custody_link',record_key=dossier_key,version=link_record.version+1,payload=link,digest=digest_payload(link))
    return stored,version


def append_owner_material_in_transaction(conn,run_id,dossier_key,*,section,material):
    """Append Manny-owned briefs/decisions without replacing any prior report."""
    from app.agents.stages import _load
    if section not in ('owner_briefs','owner_decisions','owner_rework','owner_vault_entries','owner_asset_events'): raise WorkflowError('unsupported owner material')
    source,did=_load(conn,run_id,dossier_key,'manny')
    if material.get('source_dossier_digest')!=source.digest or material.get('digest')!=digest_payload({k:v for k,v in material.items() if k!='digest'}):
        raise CustodyTamperError('owner material does not bind current dossier')
    latest=latest_version_in_transaction(conn,did);link_record=get_record_in_transaction(conn,run_id,'custody_link',dossier_key)
    payload=deepcopy(source.payload);items=payload['dossier'].setdefault(section,[])
    if len(items)>=100: raise WorkflowError('owner material history limit reached')
    items.append(deepcopy(material));payload['version']=source.version+1
    payload['digest']=digest_payload({k:v for k,v in payload.items() if k!='digest'})
    stored=put_record_in_transaction(conn,run_id,kind='dossier_version',record_key=dossier_key,version=source.version+1,payload=payload,digest=payload['digest'])
    custody_payload=deepcopy(latest.payload);custody_payload.update(dossier=deepcopy(payload['dossier']),dossier_digest=stored.digest)
    version=_append_version_in_transaction(conn,property_id=latest.property_id,payload=custody_payload,created_by='manny',supersedes=latest.version,
        amendment={'kind':section,'parent_digest':latest.digest,'material_digest':material['digest']})
    snapshot_in_transaction(conn,dossier_id=did,version=version.version)
    link=deepcopy(link_record.payload);link['custody_version']=version.version
    put_record_in_transaction(conn,run_id,kind='custody_link',record_key=dossier_key,version=link_record.version+1,payload=link,digest=digest_payload(link))
    return stored,version


def append_reassessment_in_transaction(conn,run_id,dossier_key,material):
    """Append one current-holder reassessment, preserving original report slots."""
    from app.agents.stages import _load
    agent=material['agent'];source,did=_load(conn,run_id,dossier_key,agent)
    if material['source_dossier_digest']!=source.digest or material['digest']!=digest_payload({k:v for k,v in material.items() if k!='digest'}):
        raise CustodyTamperError('reassessment does not bind current dossier')
    from app.linked_reassessments import effective_reports
    prior=effective_reports(source.payload['dossier']).get(agent) or {}
    if material.get('parent_report_digest')!=prior.get('digest'): raise WorkflowError('reassessment parent changed')
    if agent in ('elmer','manny'): raise WorkflowError('agent requires its dedicated assessment path')
    if material['report'].get('source_dossier_digest')!=source.digest or material['report'].get('digest')!=digest_payload({k:v for k,v in material['report'].items() if k!='digest'}): raise WorkflowError('reassessment report integrity failure')
    latest=latest_version_in_transaction(conn,did);link_record=get_record_in_transaction(conn,run_id,'custody_link',dossier_key)
    payload=deepcopy(source.payload);history=payload['dossier'].setdefault('agent_reassessments',[])
    if len(history)>=100: raise WorkflowError('reassessment history limit reached')
    history.append(deepcopy(material));payload['version']=source.version+1
    payload['digest']=digest_payload({k:v for k,v in payload.items() if k!='digest'})
    stored=put_record_in_transaction(conn,run_id,kind='dossier_version',record_key=dossier_key,version=source.version+1,payload=payload,digest=payload['digest'])
    custody_payload=deepcopy(latest.payload);custody_payload.update(dossier=deepcopy(payload['dossier']),dossier_digest=stored.digest)
    version=_append_version_in_transaction(conn,property_id=latest.property_id,payload=custody_payload,created_by=agent,supersedes=latest.version,
        amendment={'kind':'targeted_reassessment','parent_digest':latest.digest,'material_digest':material['digest']})
    snapshot_in_transaction(conn,dossier_id=did,version=version.version)
    link=deepcopy(link_record.payload);link['custody_version']=version.version
    put_record_in_transaction(conn,run_id,kind='custody_link',record_key=dossier_key,version=link_record.version+1,payload=link,digest=digest_payload(link))
    return stored,version
