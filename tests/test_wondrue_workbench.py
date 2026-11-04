"""Focused owner-workbench contracts. No providers or operational fixtures."""
from copy import deepcopy
from uuid import uuid4
import os
import pytest
from app import wondrue_insights as views,wondrue_workspace as ws,wondrue_retained_review as retained
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,put_record_in_transaction,create_run_in_transaction
from tests.test_handoff_demo import Conn,KEY
from tests.test_funnel_acceptance import _new_db


def sealed(value):return dict(value,digest=digest_payload(value))


def test_transitive_minimum_reassessment_order_and_corruption():
    r={'nicole':sealed({'agent':'nicole'}),'theresia_keys':sealed({'agent':'theresia_keys','inherited_report_digests':{'nicole':'old'}})}
    r['simeon']=sealed({'agent':'simeon','inherited_report_digests':{'theresia_keys':r['theresia_keys']['digest']}})
    plan=views.dependency_plan({'reports':r})
    assert [s['agent'] for s in plan['steps']]==['theresia_keys','simeon']
    assert plan['steps'][0]['can_preview_now'] and not plan['steps'][1]['can_preview_now']
    assert not plan['automatic_execution']
    r['nicole']['invented']=True
    with pytest.raises(WorkflowError,match='integrity'):views.dependency_plan({'reports':r})


def test_comparison_preserves_unknown_and_rejects_same_property(monkeypatch):
    def snapshot(conn,ref):
        return {'run_id':ref['run_id'],'kind':'dossier_version','key':'main','version':1,'digest':'ab'*32,'data':{'property_id':ref['run_id']}},{'name':'TEST fixture'},{}
    monkeypatch.setattr(views,'snapshot',snapshot)
    refs=[{'run_id':str(uuid4())},{'run_id':str(uuid4())}]
    result=views.compare(None,refs)
    assert result['properties'][0]['finances'] is None and result['ranking'] is None and result['totals'] is None
    with pytest.raises(WorkflowError,match='distinct'):views.compare(None,[refs[0],refs[0]])


def test_coordinator_review_never_accepts_stage_execution():
    args={'run_id':str(uuid4()),'dossier_key':'main','expected_digest':'ab'*32,'operation_id':str(uuid4()),'expected_sequence':2}
    for action in ('step','run',None):
        with pytest.raises(WorkflowError,match='stop/resume'):ws.intent_for_review({'intent':'coordinate_dossier','args':dict(args,action=action)})
    assert ws.intent_for_review({'intent':'coordinate_dossier','args':dict(args,action='stop')})['args']['action']=='stop'


def test_failed_or_replayed_pause_is_held_for_reconciliation():
    conn=Conn(_new_db())
    for outcome in ({'outcome':'error'},{'status':'stopped','sequence':3,'replayed':True}):
        rid=str(uuid4());run=create_run_in_transaction(conn,idempotency_key='wondrue-review:'+rid)
        ws.write(conn,run.run_id,'review',{'status':'dispatch_claimed','intent':{'intent':'coordinate_dossier','args':{'action':'stop','expected_sequence':2}}})
        assert ws.finish(conn,rid,outcome)['status']=='needs_reconciliation'


def test_review_emergency_stop_refuses_before_connection(monkeypatch):
    monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','1')
    monkeypatch.setattr(ws,'transaction',lambda *a:pytest.fail('must not connect'))
    with pytest.raises(WorkflowError,match='emergency-stopped'):retained.service('advance',review_id=uuid4())


def test_retained_connector_refuses_demo_and_synthetic(monkeypatch):
    db=_new_db();conn=Conn(db)
    with pytest.raises(WorkflowError,match='connector'):retained.guard(conn)
    db.update(database='growup_dev',role='growup_app')
    from types import SimpleNamespace
    with pytest.raises(WorkflowError,match='synthetic'):retained.guard(conn,SimpleNamespace(payload={'synthetic':True}))
    with pytest.raises(WorkflowError,match='fixtures'):retained.guard(conn,SimpleNamespace(payload={'test_material':True}))


@pytest.mark.parametrize('target',['nicole','onesimus'])
def test_retained_review_exact_approval_three_steps_and_replay(monkeypatch,target):
    from tests.workbench_fixtures import retained_fixture
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1');monkeypatch.setenv('GROWUP_PG_INTEGRATION','1')
    monkeypatch.setenv('CUSTODY_HMAC_KEY','gate-test-hmac-key-distinct-0123456789')
    conn=Conn(dict(_new_db(),database='growup_mvp_gate',role='growup_app'))
    run,row=retained_fixture(conn);rid=uuid4();ref={'run_id':str(run.run_id),'key':'main','version':row.version,'digest':row.digest}
    initial=deepcopy(row.payload)
    preview=retained.prepare(conn,rid,ref,'test-retained-task',target,'TEST retained-only correction')
    with pytest.raises(WorkflowError,match='approval'):retained.advance(conn,rid)
    with pytest.raises(WorkflowError,match='changed'):retained.decide(conn,rid,'ff'*32,'approve','Stale decision')
    retained.decide(conn,rid,preview['review']['review_digest'],'approve','TEST fixture explicit approval')
    for _ in range(3):result=retained.advance(conn,rid)
    assert result['review']['status']=='complete' and len(result['review']['timeline'])==3
    assert result['owner_brief']['custody']['current_holder']=='manny'
    assert ws.record(conn,run.run_id,'dossier_version','main',1)['data']==initial
    before=deepcopy(conn._db)
    assert retained.advance(conn,rid)==result and before==conn._db


def test_task_annotations_are_cas_and_never_close_source():
    conn=Conn(_new_db());run=create_run_in_transaction(conn,idempotency_key='annotation-test')
    payload={'requirement':'TEST support request','status':'pending'}
    row=put_record_in_transaction(conn,run.run_id,kind='owner_review_task',record_key='csr:test',version=1,payload=payload,digest=digest_payload(payload))
    ref={'run_id':str(run.run_id),'key':row.record_key,'version':1,'digest':row.digest}
    result=views.task_note(conn,ref,'defer','Explicit administrative note')
    assert not result['source_workflow_changed']
    assert ws.record(conn,run.run_id,'owner_review_task',row.record_key)['data']==payload
    with pytest.raises(WorkflowError,match='note changed'):views.task_note(conn,ref,'acknowledge','Stale choice')
    assert views.task_note(conn,ref,'acknowledge','Reviewed',result['note_digest'])['choice']=='acknowledge'


def test_private_routes_reject_reader_mutations_and_foreign_origin(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv('SERVER_MODE','LIVE_SERVER');monkeypatch.setenv('WONDRUE_OPERATOR_KEY','workbench-owner-test');monkeypatch.setenv('WONDRUE_READER_KEY','workbench-reader-test')
    with TestClient(app,base_url='https://growup.securecodeusa.com:8443') as client:
        assert client.get('/wondrue/api/workbench/execution').status_code==401
        ref={'run_id':str(uuid4()),'key':'main','version':1,'digest':'ab'*32}
        body={'id':str(uuid4()),'source':ref,'target':'nicole','task_key':'task','note':'test'}
        headers={'X-Wondrue-Key':'workbench-reader-test','Origin':'https://growup.securecodeusa.com:8443'}
        assert client.post('/wondrue/api/retained-review',json=body,headers=headers).status_code==403
        headers={'X-Wondrue-Key':'workbench-owner-test','Origin':'https://foreign.invalid'}
        assert client.post('/wondrue/api/retained-review',json=body,headers=headers).status_code==403
        assert client.post('/wondrue/api/workbench/compare',json={'sources':[ref,ref]},headers=headers).status_code==403


def test_full_isolated_operating_rehearsal_preserves_original_and_replays(monkeypatch):
    from app.handoff_demo_domain import scope
    from app.operating_rehearsal import step
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1');monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')
    conn=Conn(_new_db());cid=uuid4()
    with scope(conn,KEY):
        for _ in range(12):
            result=step(conn,cid)
            if result['phase']=='complete':break
        assert result['phase']=='complete' and result['original_preserved']
        assert result['refreshed']==['theresia_keys','simeon','solomon','elmer']
        assert result['owner_lifecycle']['vault_preserved_after_new_evidence']
        assert result['owner_lifecycle']['overdue_task_count'] and result['owner_lifecycle']['reopened_task_count']
        before=deepcopy(conn._db)
        assert step(conn,cid)==result and before==conn._db


@pytest.mark.skipif(os.getenv('GROWUP_GUIDED_REVIEW_PG')!='1',reason='Explicit isolated demo PG rehearsal disabled')
def test_pg_rehearsal_rollback_recovery_and_final_replay(monkeypatch):
    from app import operating_rehearsal as rehearsal
    from app.properties_db import get_connection
    from app.handoff_demo import configuration
    from app.handoff_demo_domain import scope
    cid=uuid4();cfg,key=configuration()
    with get_connection(cfg) as conn:
        with scope(conn,key):
            first=rehearsal.step(conn,cid);conn.commit()
    # Simulate loss after mutations and before checkpoint. Context rollback
    # preserves the committed source and approval; retry consumes no extra step.
    original=rehearsal.guided._write
    def interrupted(conn,rid,record_key,*a,**kw):
        if record_key=='operating-rehearsal':raise KeyboardInterrupt('simulated interruption')
        return original(conn,rid,record_key,*a,**kw)
    monkeypatch.setattr(rehearsal.guided,'_write',interrupted)
    with pytest.raises(KeyboardInterrupt):
        with get_connection(cfg) as conn:
            with scope(conn,key):rehearsal.step(conn,cid)
    monkeypatch.setattr(rehearsal.guided,'_write',original)
    result=rehearsal.run(cid)
    assert result['phase']=='complete' and result['owner_lifecycle']['vault_preserved_after_new_evidence']
    assert rehearsal.run(cid)==result


@pytest.mark.skipif(os.getenv('GROWUP_PG_INTEGRATION')!='1',reason='Dedicated gate PostgreSQL proof explicitly disabled')
def test_pg_retained_review_projections_and_coordinator_controls(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_dcad_admission import _require_gate_database
    from tests.workbench_fixtures import retained_fixture
    from app.properties_db import get_connection
    from app.agents.stages import _load
    from app.dossier_coordinator import step_in_transaction
    _require_gate_database()
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1');monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')
    run,row=ws.transaction('test_retained_fixture',retained_fixture)
    ref={'run_id':str(run.run_id),'key':'main','version':1,'digest':row.digest};rid=uuid4()
    prepared=retained.service('prepare',review_id=rid,ref=ref,task_key='test-retained-task',target='nicole',note='TEST gate-only exact preview')
    retained.service('decide',review_id=rid,expected_digest=prepared['review']['review_digest'],choice='approve',note='TEST gate-only owner choice')
    original=retained._save
    def interrupt(*a,**kw):raise KeyboardInterrupt('simulated interruption before checkpoint')
    monkeypatch.setattr(retained,'_save',interrupt)
    with pytest.raises(KeyboardInterrupt):retained.service('advance',review_id=rid)
    monkeypatch.setattr(retained,'_save',original)
    assert retained.service('inspect',review_id=rid)['source_current_digest']==row.digest
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:retained.service('advance',review_id=rid),range(2)))
    result=retained.service('advance',review_id=rid)
    assert result['review']['status']=='complete' and len(result['review']['timeline'])==3
    with get_connection() as conn:
        current=_load(conn,run.run_id,'main')[0]
        assert views.plan(conn,dict(ref,version=current.version,digest=current.digest))['steps']
        assert ws.record(conn,run.run_id,'dossier_version','main',1)['data']==row.payload
        assert views.inbox(conn,as_of='2026-11-04')['items'] is not None
        assert views.briefing(conn,as_of='2026-11-04')['provider_calls']==0
        assert any(j['source']['run_id']==str(run.run_id) for j in views.execution(conn)['jobs'])
    second,other=ws.transaction('test_distinct_comparison_fixture',retained_fixture)
    with get_connection() as conn:
        comparison=views.compare(conn,[ref,{'run_id':str(second.run_id),'key':'main','version':1,'digest':other.digest}])
        assert len(comparison['properties'])==2 and comparison['ranking'] is None
    # A pause/resume review controls stored state, not a worker or agent step.
    checkpoint=ws.transaction('test_stop',lambda c:step_in_transaction(c,run_id=run.run_id,dossier_key='main',principal='wondrue-operator',expected_sequence=0,action='stop'))
    args={'run_id':str(run.run_id),'dossier_key':'main','expected_digest':current.digest,'operation_id':str(uuid4()),'expected_sequence':checkpoint['sequence'],'action':'resume'}
    review_id=uuid4();proposal=ws.transaction('test_resume_proposal',lambda c:ws.propose(c,review_id,{'intent':'coordinate_dossier','args':args},'TEST resume checkpoint only'))
    claimed=ws.transaction('test_resume_claim',lambda c:ws.decide(c,review_id,digest_payload(proposal),'approve','TEST explicit resume approval'))
    assert claimed['status']=='dispatch_claimed'
    resumed=ws.transaction('test_resume',lambda c:step_in_transaction(c,run_id=run.run_id,dossier_key='main',principal='wondrue-operator',expected_sequence=checkpoint['sequence'],action='resume'))
    assert resumed['status']=='running' and resumed['sequence']==checkpoint['sequence']+1
    ws.transaction('test_resume_complete',lambda c:ws.finish(c,str(review_id),resumed))


@pytest.mark.skipif(os.getenv('GROWUP_SANDBOX_PROOF')!='1',reason='Explicit isolated scenario PostgreSQL proof disabled')
def test_pg_named_linked_alternatives_and_unknowns():
    from app import wondrue_sandbox as sandbox
    source={'kind':'dossier_version','run_id':str(uuid4()),'key':'TEST reference only','version':1,'digest':'ab'*32}
    first=sandbox.save(uuid4(),source,{'config':'bench','site':{'usable_area_m2':100},'crop':{}},'TEST compact alternative')
    second=sandbox.save(uuid4(),source,{'config':'rack3','site':{'usable_area_m2':100},'crop':{}},'TEST vertical alternative',first['id'],digest_payload(first))
    refs=[{'id':r['id'],'digest':digest_payload(r)} for r in (first,second)]
    result=sandbox.compare_scenarios(refs)
    assert result['changed_input_groups']==['config'] and result['totals'] is None
    assert all(r['result']['engineering']['missing'] for r in result['scenarios'])
    assert result['scenarios'][1]['parent']['digest']==digest_payload(first)
    with pytest.raises(WorkflowError,match='reference changed'):sandbox.compare_scenarios([refs[0],dict(refs[1],digest='ff'*32)])
    with pytest.raises(WorkflowError,match='same exact source'):sandbox.save(uuid4(),dict(source,version=2),first['inputs'],'TEST wrong parent',first['id'],digest_payload(first))
    assert sandbox.save(first['id'],source,first['inputs'],first['name'])==first
