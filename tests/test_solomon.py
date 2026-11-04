from copy import deepcopy
from uuid import uuid4
import pytest
from app.agents.solomon import assess
from app import handoff_demo as demo
from app.handoff_demo_domain import DOMAIN,scope
from app.solomon_simulation import build_vendor_round,evaluate_quotes,PROFILES
from app.workflow import digest_payload
from app.workflow_db import WorkflowError


def inputs():
    prop=str(uuid4()); reports={}
    for agent in ('onesimus','nicole','simeon'):
        report={'agent':agent,'property_id':prop}
        if agent=='nicole': report['regulatory_assessment']={'ready_for_preliminary_underwriting':True,'regulatory_clearance':False}
        if agent=='simeon': report['closing_assessment']={'ready_for_coordination_review':True,'ready_to_close':False}
        report['digest']=digest_payload(report); reports[agent]=report
    wrap=lambda value:{'value':{'property_id':prop,**value},'evidence_refs':['https://records.example/planning']}
    supplied={'approved_design':wrap({'scope':'preconstruction_planning','engineering_report_digest':reports['onesimus']['digest']}),
        'permits':wrap({'status':'pending'}),
        'contractor_and_safety_plan':wrap({'coordinator':'Provided coordinator','site_access_plan':'Planning only','review_status':'pending'}),
        'construction_budget':wrap({'currency':'USD','limit_usd':'100000','line_items':[{'phase':'survey','estimated_cost_usd':'2000'}]})}
    return prop,supplied,reports


def test_planning_is_not_construction_or_closing_authority():
    prop,supplied,reports=inputs(); before=deepcopy(supplied)
    result=assess(prop,supplied,reports)
    assert result['planning_ready'] and result['budget']['estimated_total_usd']=='2000.00'
    assert not any(result[k] for k in ('construction_authorized','site_safety_verified','permits_authenticated','deal_closed','signing_authorized'))
    assert all(p['execution_status']=='not_authorized' for p in result['work_breakdown'])
    assert result['communications']['planned_channels']==['email','phone','chat'] and not result['communications']['active_transports']
    assert before==supplied


@pytest.mark.parametrize('change',['property','engineering','budget','nonfinite','scope','simeon'])
def test_preconstruction_gaps_refuse_readiness(change):
    prop,supplied,reports=inputs()
    if change=='property': supplied['permits']['value']['property_id']=str(uuid4())
    if change=='engineering': supplied['approved_design']['value']['engineering_report_digest']='wrong'
    if change=='budget': supplied['construction_budget']['value']['limit_usd']='1'
    if change=='nonfinite': supplied['construction_budget']['value']['line_items'][0]['estimated_cost_usd']='NaN'
    if change=='scope': supplied['approved_design']['value']['scope']='construction_approved'
    if change=='simeon': reports.pop('simeon')
    assert not assess(prop,supplied,reports)['planning_ready']


def fixture():
    return demo._fixture('demo-handoff-solomon-vendor-test',None)


def test_vendor_round_is_distinct_tailored_synthetic_and_has_no_delivery():
    result=build_vendor_round(fixture())
    assert len(result['proposals'])==len(PROFILES)==7
    assert len({p['body'] for p in result['proposals']})==7
    assert len({p['proposal_id'] for p in result['proposals']})==7
    assert all(p['email']['to']=='werenotrandom@protonmail.com' and not p['email']['sent'] for p in result['proposals'])
    assert result['evaluation']['selected_vendor_id']
    assert result['final_contract_package']['signature_blocks']==[]
    assert result['provider_calls']==result['external_deliveries']==result['real_vendor_responses']==result['actual_contracts']==0
    assert result['digest']==digest_payload({k:v for k,v in result.items() if k!='digest'})


def test_real_dossier_cannot_generate_or_inherit_fake_vendor_responses():
    payload=fixture(); payload.pop('synthetic')
    with pytest.raises(WorkflowError): build_vendor_round(payload)


def test_scoring_is_deterministic_ties_and_no_eligible_bid():
    def quote(vendor): return {'vendor_id':vendor,'synthetic':True,'price_usd':'100','duration_days':10,
        'scope_score':80,'supervision_score':80,'safety_plan_provided':True}
    quotes=[quote(PROFILES[1][0]),quote(PROFILES[0][0])]
    first=evaluate_quotes(quotes,'1000')
    assert first==evaluate_quotes(list(reversed(quotes)),'1000')
    assert first['selected_vendor_id']==min(q['vendor_id'] for q in quotes)
    assert evaluate_quotes(quotes,'1')['selected_vendor_id'] is None
    with pytest.raises(WorkflowError): evaluate_quotes([quotes[0],quotes[0]],'1000')


def test_unknown_simulated_channel_is_refused():
    quote={'vendor_id':PROFILES[0][0],'synthetic':True,'price_usd':'100','duration_days':10,
        'scope_score':80,'supervision_score':80,'safety_plan_provided':True,'channel':'external_delivery'}
    with pytest.raises(WorkflowError): evaluate_quotes([quote],'1000')
