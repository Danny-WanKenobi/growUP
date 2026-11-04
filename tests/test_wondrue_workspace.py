"""Bounded owner-cockpit proofs. No providers, outreach or property acquisition."""
from copy import deepcopy
from uuid import uuid4
import hashlib
import hmac
import os
import time

import pytest
from fastapi.testclient import TestClient
from app import wondrue_workspace as ws
from app.main import app
from app.routes import wondrue_cockpit as routes
from app.workflow_db import WorkflowError,create_run_in_transaction,put_record_in_transaction
from app.workflow import digest_payload
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db

ORIGIN='https://growup.securecodeusa.com:8443'


@pytest.fixture
def store(monkeypatch):
    conn=Conn(_new_db())
    monkeypatch.setattr(ws,'transaction',lambda action,fn:fn(conn))
    monkeypatch.setenv('SERVER_MODE','LIVE_SERVER')
    monkeypatch.setenv('WONDRUE_OPERATOR_KEY','operator-test')
    monkeypatch.setenv('WONDRUE_READER_KEY','reader-test')
    monkeypatch.setenv('PUBLIC_SESSION_SECRET','workspace-test-secret-'+'x'*40)
    monkeypatch.setenv('WONDRUE_SANDBOX_ENABLED','0')
    return conn


def dossier(conn):
    run=create_run_in_transaction(conn,idempotency_key='workspace-test:'+uuid4().hex)
    payload={'property_id':str(uuid4()),'dossier':{'street_address':'Fictional test address','reports':{}}}
    row=put_record_in_transaction(conn,run.run_id,kind='dossier_version',record_key='main',version=1,
                                  payload=payload,digest=digest_payload(payload))
    return run,row


def proposal(run,row):
    return {'intent':'prepare_owner_brief','args':{'run_id':str(run.run_id),'dossier_key':'main',
            'operation_id':str(uuid4()),'expected_digest':row.digest}}


def test_history_replay_dedup_and_conflicting_text(store):
    cid=str(uuid4());mid=str(uuid4())
    ws.chat(store,cid,True)
    original=ws.append_turn(store,cid,mid,'Show my Library',{'text':'Retained records only.'})
    assert ws.append_turn(store,cid,mid,'Show my Library',{'text':'different'})==original
    assert len(ws.turns(store,cid)['turns'])==1
    with pytest.raises(WorkflowError,match='conflict'):
        ws.append_turn(store,cid,mid,'Changed request',{})


def test_review_is_bound_to_exact_evidence_and_decision(store):
    run,row=dossier(store);rid=str(uuid4());intent=proposal(run,row)
    state=ws.propose(store,rid,intent,'Refresh a retained brief')
    assert state['status']=='pending'
    assert ws.propose(store,rid,intent,'Refresh a retained brief')==state
    assert ws.decide(store,rid,digest_payload(state),'defer','Check supporting records')['status']=='deferred'
    with pytest.raises(WorkflowError,match='changed'):
        ws.decide(store,rid,digest_payload(state),'approve','Proceed')


def test_changed_dossier_refuses_review_execution(store):
    run,row=dossier(store);rid=str(uuid4());state=ws.propose(store,rid,proposal(run,row),'Check')
    payload=deepcopy(row.payload);payload['correction']='later'
    put_record_in_transaction(store,run.run_id,kind='dossier_version',record_key='main',version=2,
                              payload=payload,digest=digest_payload(payload))
    with pytest.raises(WorkflowError,match='evidence changed'):
        ws.decide(store,rid,digest_payload(state),'approve','Proceed')


def test_interrupted_dispatch_cannot_be_reexecuted(store):
    run,row=dossier(store);rid=str(uuid4());state=ws.propose(store,rid,proposal(run,row),'Check')
    claimed=ws.decide(store,rid,digest_payload(state),'approve','Proceed')
    assert claimed['status']=='dispatch_claimed'
    with pytest.raises(WorkflowError,match='already decided'):
        ws.decide(store,rid,digest_payload(claimed),'approve','Retry')
    assert ws.finish(store,rid)['status']=='needs_reconciliation'


@pytest.mark.parametrize('intent',['start_discovery','collect_dcad_evidence','draft_outreach','compare_scenarios','coordinate_dossier'])
def test_provider_or_dispatch_operations_cannot_enter_review_executor(intent):
    with pytest.raises(WorkflowError,match='not enabled'):
        ws.intent_for_review({'intent':intent,'args':{}})


def test_authentication_origin_legacy_and_disabled_sandbox(store,monkeypatch):
    with TestClient(app,base_url=ORIGIN) as client:
        assert client.get('/wondrue').status_code==200
        assert 'RECENT CONVERSATIONS' in client.get('/wondrue').text
        assert 'Wondrue access key' in client.get('/wondrue/legacy').text
        assert client.get('/wondrue/api/chats').status_code==401
        client.cookies.set('growup_owner','forged')
        assert client.get('/wondrue/api/status').status_code==401
        headers={'X-Wondrue-Key':'operator-test','Origin':ORIGIN}
        assert client.post('/wondrue/api/chats',headers={**headers,'Origin':'https://evil.invalid'},json={'id':str(uuid4())}).status_code==403
        assert client.post('/wondrue/api/chats',headers={**headers,'X-Wondrue-Key':'reader-test'},json={'id':str(uuid4())}).status_code==403
        assert client.post('/wondrue/api/sandbox',headers=headers,json={'id':str(uuid4()),'run_id':str(uuid4()),'key':'main','version':1,'inputs':{}}).status_code==409
        assert client.post('/wondrue/command',headers=headers,json={'mode':'full','intent':{'intent':'start_real_evaluation','args':{}}}).status_code==403
        assert client.post('/wondrue/command',headers=headers,json={'mode':'ask','approval_token':'old-token'}).status_code==403
        assert client.post('/wondrue/assist/extract',headers=headers,json={'mode':'full'}).status_code==403
        # Existing signed owner session supplies owner authority only to the cockpit.
        payload=str(int(time.time()))+'.'+uuid4().hex
        signature=hmac.new(os.environ['PUBLIC_SESSION_SECRET'].encode(),payload.encode(),hashlib.sha256).hexdigest()
        client.cookies.set('growup_owner',payload+'.'+signature)
        assert client.get('/wondrue/api/status').json()['sandbox_enabled'] is False
        cid=str(uuid4())
        assert client.post('/wondrue/api/chats',headers={'Origin':ORIGIN},json={'id':cid}).status_code==200
        response=client.post(f'/wondrue/api/chats/{cid}/messages',headers={'Origin':ORIGIN},
            json={'request_id':str(uuid4()),'text':'Hello'})
        assert response.status_code==200 and 'no model' in response.json()['answer']['text']
        forbidden=client.post(f'/wondrue/api/chats/{cid}/messages',headers={'Origin':ORIGIN},
            json={'request_id':str(uuid4()),'text':'change','intent':{'intent':'start_discovery','args':{}}})
        assert forbidden.status_code==409


def test_record_allowlist_prevents_arbitrary_artifact_reads(store):
    with pytest.raises(WorkflowError,match='not in'):
        ws.record(store,uuid4(),'research_artifact','content')


def test_review_route_dispatches_only_once_and_records_uncertain_failures(store,monkeypatch):
    run,row=dossier(store);request_id=str(uuid4())
    state=ws.propose(store,request_id,proposal(run,row),'Refresh retained brief')
    calls=[]
    def execute(**kwargs):
        calls.append(kwargs)
        return {'data':{'external_actions':0,'operation_id':kwargs['intent_override']['args']['operation_id']}}
    monkeypatch.setattr('app.wondrue.handle_command',execute)
    with TestClient(app,base_url=ORIGIN) as client:
        headers={'X-Wondrue-Key':'operator-test','Origin':ORIGIN}
        url='/wondrue/api/reviews/'+request_id+'/decision'
        body={'expected_digest':digest_payload(state),'choice':'approve','note':'Owner reviewed this exact input'}
        assert client.post(url,headers=headers,json=body).json()['status']=='completed'
        assert client.post(url,headers=headers,json=body).status_code==409
        assert len(calls)==1 and calls[0]['use_live'] is False
        assert calls[0]['principal']=='wondrue-operator'
        failed_id=str(uuid4());failed=ws.propose(store,failed_id,proposal(run,row),'Separate failure case')
        def interrupted(**kwargs):raise RuntimeError('simulated interruption')
        monkeypatch.setattr('app.wondrue.handle_command',interrupted)
        result=client.post('/wondrue/api/reviews/'+failed_id+'/decision',headers=headers,
            json={'expected_digest':digest_payload(failed),'choice':'approve','note':'Explicit approval'})
        assert result.json()['status']=='needs_reconciliation'


def test_sandbox_reuses_engineering_and_never_imports_contacts():
    from app import wondrue_sandbox as s
    reference={'kind':'dossier_version','run_id':str(uuid4()),'key':'main','version':1,'digest':'ab'*32,
               'data':{'owner_email':'private@example.invalid','secret':'not copied'}}
    copied=s.sanitize_source(reference)
    assert 'private@' not in str(copied) and 'secret' not in str(copied)
    result=s.evaluate({'config':'bench','site':{'usable_area_m2':100},'crop':{}})
    assert result['hypothetical'] and not result['authorization']
    assert result['engineering']['missing']
    assert result['engineering']['input_groups']['site']['usable_area_m2']['classification']=='assumed'
    with pytest.raises(ValueError):s.evaluate({'config':'bench','site':{'usable_area_m2':float('nan')}})
    with pytest.raises(ValueError):s.evaluate({'config':'bench','site':{'usable_area_m2':True}})


@pytest.mark.skipif(os.getenv('GROWUP_PG_INTEGRATION')!='1',reason='Dedicated PostgreSQL proof explicitly disabled')
def test_pg_workspace_history_search_library_review_and_claim_race(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_dcad_admission import _require_gate_database
    from app.properties_db import get_connection
    _require_gate_database()
    monkeypatch.setenv('OBS_ENABLED','0')
    cid=str(uuid4());mid=str(uuid4());needle='pg-cockpit-'+uuid4().hex
    ws.transaction('test_create',lambda c:ws.chat(c,cid,True))
    value=ws.transaction('test_turn',lambda c:ws.append_turn(c,cid,mid,needle,{'text':'Retained answer'}))
    assert ws.transaction('test_replay',lambda c:ws.append_turn(c,cid,mid,needle,{}))==value
    with get_connection() as conn:
        assert ws.turns(conn,cid)['turns']==[value]
        assert ws.conversations(conn,needle)['items'][0]['id']==cid
        assert ws.conversations(conn,'unmatched-'+uuid4().hex)['items']==[]
    run,row=ws.transaction('test_source',dossier)
    rid=str(uuid4());state=ws.transaction('test_proposal',lambda c:ws.propose(c,rid,proposal(run,row),'PostgreSQL review proof'))
    def race(_):
        try:return ws.transaction('test_claim',lambda c:ws.decide(c,rid,digest_payload(state),'approve','One exact claim'))['status']
        except WorkflowError:return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(race,range(2)))==['dispatch_claimed','refused']
    ws.transaction('test_interrupt',lambda c:ws.finish(c,rid))
    with get_connection() as conn:
        assert any(r['id']==rid and r['status']=='needs_reconciliation' for r in ws.review_list(conn)['items'])
        assert ws.catalogue(conn,'Fictional test address')['items']
        assert ws.catalogue(conn,tasks=True)['items'] is not None
        assert ws.record(conn,run.run_id,'dossier_version','main')['digest']==row.digest
    # Historical negative-test rows must be quarantined individually, not hide the Library.
    bad_key='integrity-'+uuid4().hex
    ws.transaction('test_bad_fixture',lambda c:put_record_in_transaction(c,run.run_id,kind='qualification',
        record_key=bad_key,version=1,payload={'probe':bad_key},digest='ff'*32))
    with get_connection() as conn:
        item=ws.catalogue(conn,bad_key)['items'][0]
        assert item['status']=='integrity_failed' and 'probe' not in item
        with pytest.raises(WorkflowError,match='integrity'):
            ws.record(conn,run.run_id,'qualification',bad_key)
