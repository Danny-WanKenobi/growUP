from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app import conversion_owner_demo as owner
from app.workflow import digest_payload
from app.workflow_db import WorkflowError


def source():
    value={'synthetic':True,'domain':'synthetic_handoff_demo','property_id':str(uuid4())}
    value['digest']=digest_payload(value)
    return value


def test_changed_preference_preserves_source_and_links_three_agent_briefs():
    original=source();before=deepcopy(original)
    first=owner.assessment(original,'owner_funded','Cash and control are important.')
    second=owner.assessment(original,'participation','Explore shared project funding.',first)
    assert original==before
    assert second['revision']==2 and second['previous_assessment_digest']==first['digest']
    assert first['pathway']=='owner_funded'
    assert second['theresia']['document_type']=='illustrative_nonbinding_project_structure'
    assert second['manny']['business_approval'] is False
    assert second['elmer']['ledger_postings']==0
    assert second['simeon']['real_actions_enabled'] is False


def test_append_retry_stale_update_and_retained_history(monkeypatch):
    rows={};writes=[]
    monkeypatch.setattr(owner,'assert_database',lambda conn:None)
    monkeypatch.setattr(owner,'current_key',lambda:b'demo-key')
    monkeypatch.setattr(owner,'lock',lambda *args:None)
    monkeypatch.setattr(owner,'read',lambda conn,rid,key:rows.get(key))
    def write(conn,rid,key,value,prior=None):
        rows[key]=SimpleNamespace(payload=deepcopy(value));writes.append(key)
    monkeypatch.setattr(owner,'write',write)
    original=source()
    def append(pathway,concerns,previous):
        return owner.append(None,'run',original,pathway=pathway,concerns=concerns,expected_previous=previous)
    first=append('owner_funded','Cost concern',None)
    assert append('owner_funded','Cost concern',None)['replayed'] is True
    assert len(writes)==2
    second=append('participation','Control concern',first['assessment']['digest'])
    with pytest.raises(WorkflowError):append('owner_funded','Stale changed choice',None)
    chain=owner.history(None,'run',original)
    assert chain==[first['assessment'],second['assessment']]
    del rows['conversion-owner:'+first['assessment']['digest']]
    with pytest.raises(WorkflowError):owner.history(None,'run',original)


def test_wrong_source_and_unscoped_mutation_refuse(monkeypatch):
    a=source();first=owner.assessment(a,'owner_funded','Concern')
    with pytest.raises(WorkflowError):owner.assessment(source(),'participation','Concern',first)
    monkeypatch.setattr(owner,'assert_database',lambda conn:None)
    monkeypatch.setattr(owner,'current_key',lambda:None)
    with pytest.raises(WorkflowError):
        owner.append(None,'run',a,pathway='owner_funded',concerns='Concern',expected_previous=None)


def test_api_refuses_unauthenticated_mutation(monkeypatch):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.routes.demo_tour import router
    monkeypatch.setenv('WONDRUE_OPERATOR_KEY','test-secret-key')
    app=FastAPI();app.include_router(router)
    response=TestClient(app).post('/wondrue/api/demo-tour/conversion-preference',json={
        'pathway':'participation','concerns':'Test','expected_previous':None})
    assert response.status_code in (401,403,503)
