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
    if agent not in ("raphael", "onesimus", "michael", *AGENT_SPECS) or not isinstance(report, dict):
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
    schema = {"raphael":"raphael-inspection-1","onesimus":"onesimus-concept-report-1",
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
