from copy import deepcopy
from uuid import uuid4
import pytest
from app.agents.advisers import assess_insurance,assess_legal


def fixture():
    prop=str(uuid4())
    def wrap(entries,**extra): return dict(value=dict(property_id=prop,entries=entries,**extra),evidence_refs=['fixture:retained'])
    insurance=dict(risk_inventory=wrap([dict(id='water',description='Reported water escape concern',severity=5,likelihood=3,evidence_refs=['fixture:risk'])],as_of='2026-11-03'),
        existing_policies=wrap([dict(id='p1',coverage='liability',effective_on='2026-01-01',expires_on='2026-10-31',limit_usd='500000.00',deductible_usd='5000.00',currency='USD',exclusions=['Flood exclusion claimed'],evidence_refs=['fixture:policy'])]),
        coverage_requirements=wrap([dict(coverage='liability',minimum_limit_usd='1000000.00',evidence_refs=['fixture:requirement'])]))
    legal=dict(jurisdiction=dict(value=dict(property_id=prop,name='Fictional jurisdiction'),evidence_refs=['fixture:jurisdiction']),
        legal_question=dict(value=dict(property_id=prop,text='Which document controls the proposed use?'),evidence_refs=['fixture:question']),
        governing_documents=wrap([dict(id='lease',version='1',status='draft',issues=['Use scope unresolved'],evidence_refs=['fixture:lease']),dict(id='lease',version='2',status='signed_claimed',issues=[],evidence_refs=['fixture:lease2'])]))
    return prop,insurance,legal


def test_advisers_prioritize_risk_and_flag_expired_coverage_and_document_conflict():
    prop,insurance,legal=fixture();before=deepcopy((insurance,legal))
    y=assess_insurance(prop,insurance);d=assess_legal(prop,legal)
    assert y['risks'][0]['triage_score']==15 and y['risks'][0]['priority']=='high'
    assert y['reported_coverage_gaps'][0]['reported_limit_gap_usd']=='1000000.00'
    assert 'conflicting_document_versions:lease' in d['review_issues']
    assert not y['coverage_verified'] and not d['legal_clearance'] and before==(insurance,legal)


def test_two_policy_limits_do_not_stack_and_invalid_values_remain_gaps():
    prop,insurance,legal=fixture();policy=insurance['existing_policies']['value']['entries'][0]
    policy.update(expires_on='2026-12-31',exclusions=[])
    insurance['existing_policies']['value']['entries'].append({**policy,'id':'p2'})
    assert assess_insurance(prop,insurance)['reported_coverage_gaps'][0]['reported_limit_gap_usd']=='500000.00'
    insurance['risk_inventory']['value']['entries'][0]['likelihood']=True
    policy['limit_usd']='NaN'
    result=assess_insurance(prop,insurance)
    assert result['missing'] and not result['coverage_verified']
    assert assess_legal(str(uuid4()),legal)['missing']


def test_durable_advisory_screen_replays_without_transfer(monkeypatch):
    from tests.test_solomon_project import prepared
    from tests.test_handoff_demo import KEY
    from app.handoff_demo_domain import scope
    from app import agent_consultations
    from app.workflow_db import get_record_in_transaction
    conn,rid=prepared(monkeypatch)
    with scope(conn,KEY):
        source=get_record_in_transaction(conn,rid,'dossier_version','main');receipts=len(conn._db['receipts'])
        args=dict(principal='owner',run_id=rid,dossier_key='main',requester='solomon',adviser='yoseph',expected_digest=source.digest)
        first=agent_consultations.assess_in_transaction(conn,**args)
        assert first['report']['advisory_assessment']['coverage_verified'] is False and not first['custody_transferred']
        before=deepcopy(conn._db)
        assert agent_consultations.assess_in_transaction(conn,**args)['replayed'] and before==conn._db
        assert len(conn._db['receipts'])==receipts and get_record_in_transaction(conn,rid,'dossier_version','main')==source


def test_requester_can_receive_an_immediate_structured_adviser_reply(monkeypatch):
    from tests.test_solomon_project import prepared
    from tests.test_handoff_demo import KEY
    from app.handoff_demo_domain import scope
    from app import agent_consultations as c
    from app.workflow_db import get_record_in_transaction
    conn,rid=prepared(monkeypatch)
    with scope(conn,KEY):
        source=get_record_in_transaction(conn,rid,'dossier_version','main');receipts=len(conn._db['receipts'])
        args=dict(principal='owner',run_id=rid,dossier_key='main',thread_id='automatic-review',requester='solomon',adviser='deborah',expected_sequence=0,
            message=dict(speaker='solomon',text='Screen the retained documents before planning.',recommendation='needs_evidence',evidence_refs=['fixture:retained'],assessment_requested=True))
        reply=c.exchange_in_transaction(conn,**args)
        assert reply['sequence']==2 and reply['messages'][-1]['request']['message']['speaker']=='deborah'
        assert reply['latest_assessment']['status']=='preliminary_assessment_available'
        assert reply['messages'][-1]['request']['message']['recommendation']=='proceed_conditionally'
        assert reply['joint_status']=='disagreement_owner_review'
        assert c.exchange_in_transaction(conn,**args)['replayed']
        assert get_record_in_transaction(conn,rid,'dossier_version','main')==source and len(conn._db['receipts'])==receipts


def test_adviser_context_survives_owner_brief_refresh(monkeypatch):
    from tests.test_manny_review import ready
    from tests.test_handoff_demo import KEY
    from app.handoff_demo_domain import scope
    from app import agent_consultations as c,manny_review
    from app.workflow_db import get_record_in_transaction
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        source=get_record_in_transaction(conn,rid,'dossier_version','main')
        c.assess_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',requester='manny',adviser='deborah',expected_digest=source.digest)
        manny_review.mutate_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='format-only',expected_digest=source.digest,action='prepare')
        assert all(t['source_is_current'] for t in manny_review.inspect_in_transaction(conn,run_id=rid,dossier_key='main')['brief']['advisory_threads'])
