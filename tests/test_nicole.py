from copy import deepcopy
from uuid import uuid4
import pytest
from app.agents.nicole import assess, TOPICS
from app.agents.stages import build_report
from app.workflow import digest_payload


def inputs():
    prop = str(uuid4()); source = "https://records.example/record"
    wrapped = lambda value: {"value":value,"evidence_refs":[source]}
    return prop, {"jurisdiction":wrapped({"property_id":prop,"jurisdiction_id":"city", "authority_name":"Provided authority",
        "city":"Provided City","state":"TX"}),
        "proposed_use":wrapped({"property_id":prop,"scope":"indoor_farming","method":"passive_kratky"}),
        "official_requirements":wrapped([{"property_id":prop,"jurisdiction_id":"city","topic":topic,
            "record_id":topic,"requirement":"Provided screening statement, not authenticated",
            "state":"not_required","disposition":"not_applicable","record_date":"2026-11-03","source_url":source} for topic in TOPICS])}


def test_complete_intake_never_means_regulatory_clearance():
    prop, supplied=inputs(); before=deepcopy(supplied)
    result=assess(prop,supplied)
    assert result["ready_for_preliminary_underwriting"]
    assert not result["regulatory_clearance"] and not result["permit_approval_authenticated"]
    assert all(r["evidence_status"]=="provided_unverified" for r in result["records"])
    assert supplied==before


@pytest.mark.parametrize("change",["property","jurisdiction","url","date","unknown","denied","method","legacy"])
def test_evidence_gaps_and_scope_mismatch_refuse_readiness(change):
    prop,supplied=inputs(); row=supplied["official_requirements"]["value"][0]
    if change=="property": row["property_id"]=str(uuid4())
    if change=="jurisdiction": row["jurisdiction_id"]="other"
    if change=="url": row["source_url"]="https://user:secret@records.example/record"
    if change=="date": row["record_date"]="unknown"
    if change=="unknown": row["state"]="unknown"
    if change=="denied": row["disposition"]="denied"
    if change=="method": supplied["proposed_use"]["value"]["method"]="circulating"
    if change=="legacy": supplied["jurisdiction"]="unsupported string"
    assert not assess(prop,supplied)["ready_for_preliminary_underwriting"]


def test_contradictions_and_claimed_approval_stay_visible():
    prop,supplied=inputs(); rows=supplied["official_requirements"]["value"]
    other={**rows[0],"record_id":"other","state":"required","disposition":"claimed_approved"}
    rows.append(other)
    result=assess(prop,supplied)
    assert result["checklist"][0]["status"]=="conflicting"
    assert len(result["records"])==8
    assert result["owner_review_requests"] and not result["ready_for_preliminary_underwriting"]
    assert not result["records"][-1]["approval_authenticated"]


def test_report_integration_and_digest_preserve_evidence_scope():
    prop,supplied=inputs()
    reports={}
    for agent in ("raphael","onesimus"):
        report={"agent":agent,"property_id":prop}; report["digest"]=digest_payload(report); reports[agent]=report
    payload={"property_id":prop,"dossier":{"reports":reports,"specialist_inputs":{"nicole":supplied}}}
    payload["digest"]=digest_payload(payload)
    report=build_report("nicole",payload)
    assert report["status"]=="prepared_not_approved"
    assert report["regulatory_assessment"]["ready_for_preliminary_underwriting"]
    assert report["digest"]==digest_payload({k:v for k,v in report.items() if k!="digest"})
