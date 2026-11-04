from copy import deepcopy
from uuid import uuid4
import pytest
from app.agents.theresia import assess
from app.agents.stages import build_report
from app.workflow import digest_payload


def inputs():
    prop=str(uuid4()); source="https://records.example/provided"
    wrap=lambda value:{"value":value,"evidence_refs":[source]}
    supplied={"ownership_and_liens":wrap({"property_id":prop,"owner_name":"Provided owner",
        "lien_status":"none_reported","record_date":"2026-11-03"}),
        "financing_terms":wrap({"property_id":prop,"currency":"USD","additional_project_costs_usd":"5000",
            "contingency_usd":"5000","proposed_funding_usd":"120000","annual_debt_service_usd":"8000","minimum_coverage_ratio":"1.25"}),
        "contract_terms":wrap({"property_id":prop,"parties":[{"name":"Owner","role":"owner"},{"name":"Operator","role":"operator"}],
            "scope":"Preliminary passive Kratky concept","term_months":12,"conditions":["Resolve permits before commitment."]})}
    reports={"michael":{"agent":"michael","property_id":prop,"assessments":{"bench":{"complete":True,
        "outputs":{"capex_usd":"100000","operating_net_usd_per_year":"12000"}}}},
        "nicole":{"agent":"nicole","property_id":prop,"missing":[],"regulatory_assessment":{
            "ready_for_preliminary_underwriting":True,"regulatory_clearance":False}}}
    for report in reports.values(): report["digest"]=digest_payload(report)
    return prop,supplied,reports


def test_explicit_decimal_arithmetic_and_nonbinding_scope():
    prop,supplied,reports=inputs(); before=deepcopy((supplied,reports))
    result=assess(prop,supplied,reports); scenario=result["scenarios"]["bench"]
    assert scenario["capital_needed_usd"]=="110000.00"
    assert scenario["funding_gap_usd"]=="0.00"
    assert scenario["proposed_funding_surplus_usd"]=="10000.00"
    assert scenario["annual_after_debt_proxy_usd"]=="4000.00"
    assert scenario["operating_surplus_debt_coverage_proxy"]=="1.5"
    assert result["status"]=="conditional_preparation_only"
    assert not any(result[k] for k in ("ownership_verified","financing_approved","contract_executable","signing_authority"))
    assert result["contract_preparation"]["signature_blocks"]==[]
    assert result["owner_review_requests"] and (supplied,reports)==before


@pytest.mark.parametrize("value",[None,"NaN","Infinity","-1",True,"1e-999999", "1e1000"])
def test_missing_nonfinite_negative_or_unsafe_money_refuses(value):
    prop,supplied,reports=inputs()
    supplied["financing_terms"]["value"]["contingency_usd"]=value
    result=assess(prop,supplied,reports)
    assert result["status"]=="needs_evidence" and result["scenarios"]=={}


def test_funding_gap_and_zero_debt_are_not_invented_approvals():
    prop,supplied,reports=inputs()
    supplied["financing_terms"]["value"].update(proposed_funding_usd="100000",annual_debt_service_usd="0")
    result=assess(prop,supplied,reports); scenario=result["scenarios"]["bench"]
    assert scenario["funding_gap_usd"]=="10000.00"
    assert scenario["operating_surplus_debt_coverage_proxy"] is None
    assert result["status"]=="needs_evidence"


@pytest.mark.parametrize("change",["property","currency","report_tamper","nicole","liens","parties","duration"])
def test_scope_evidence_and_report_gates(change):
    prop,supplied,reports=inputs()
    if change=="property": supplied["ownership_and_liens"]["value"]["property_id"]=str(uuid4())
    if change=="currency": supplied["financing_terms"]["value"]["currency"]="EUR"
    if change=="report_tamper": reports["michael"]["assessments"]["bench"]["outputs"]["capex_usd"]="1"
    if change=="nicole":
        reports["nicole"]["regulatory_assessment"]["ready_for_preliminary_underwriting"]=False
        reports["nicole"].pop("digest"); reports["nicole"]["digest"]=digest_payload(reports["nicole"])
    if change=="liens": supplied["ownership_and_liens"]["value"]["lien_status"]="unknown"
    if change=="parties": supplied["contract_terms"]["value"]["parties"][1]["name"]="Owner"
    if change=="duration": supplied["contract_terms"]["value"]["term_months"]=True
    assert assess(prop,supplied,reports)["status"]=="needs_evidence"


def test_weak_cashflow_does_not_pass_supplied_coverage_limits():
    prop,supplied,reports=inputs()
    supplied["financing_terms"]["value"]["annual_debt_service_usd"]="13000"
    result=assess(prop,supplied,reports)
    assert result["status"]=="needs_evidence"
    assert result["scenarios"]["bench"]["annual_after_debt_proxy_usd"]=="-1000.00"


def test_shared_report_sealing_and_review_conditions():
    prop,supplied,reports=inputs()
    payload={"property_id":prop,"dossier":{"specialist_inputs":{"theresia_keys":supplied},"reports":reports}}
    payload["digest"]=digest_payload(payload)
    report=build_report("theresia_keys",payload)
    assert report["status"]=="prepared_not_approved"
    assert report["underwriting_assessment"]["contract_executable"] is False
    assert report["digest"]==digest_payload({k:v for k,v in report.items() if k!="digest"})
    assert all(not item["grants_permission"] for item in report["owner_review_requests"])
