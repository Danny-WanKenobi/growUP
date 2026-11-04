"""Focused synthetic review proof; the canonical owner/custody services are real."""
from copy import deepcopy
from uuid import uuid4
import os
import pytest
from app import guided_demo_review as guided
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError
from tests.test_handoff_demo import Conn,KEY
from tests.test_funnel_acceptance import _new_db


@pytest.fixture
def practice(monkeypatch):
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')
    return Conn(_new_db()),str(uuid4()),str(uuid4())


@pytest.mark.parametrize('choice,expected',[('supplied_update','reassessed'),('keep_unknown','needs_evidence')])
def test_exact_preview_approval_linked_reassessment_and_preserved_history(practice,choice,expected):
    conn,cid,rid=practice
    with scope(conn,KEY):
        initial=guided.create_in_transaction(conn,cid)
        preview=guided.prepare_in_transaction(conn,cid,rid,choice,initial['source_digest'])
        assert preview['review']['status']=='pending'
        assert preview['verified_receipts']==initial['verified_receipts']
        before=deepcopy(conn._db)
        with pytest.raises(WorkflowError,match='approval'):
            guided.advance_in_transaction(conn,cid,rid)
        assert before==conn._db
        guided.decide_in_transaction(conn,cid,rid,preview['review']['review_digest'],'approve','Explicit synthetic test approval')
        for _ in range(3):result=guided.advance_in_transaction(conn,cid,rid)
        assert result['review']['status']=='complete'
        assert result['review']['result']['status']==expected
        assert result['current_holder']=='manny'
        assert result['original_preserved'] and result['inherited_reports_preserved']
        assert result['verified_receipts']==initial['verified_receipts']+2
        assert result['review']['report_digest']
        assert result['remaining_review_items'] or result['stale_dependencies']
        before=deepcopy(conn._db)
        assert guided.advance_in_transaction(conn,cid,rid)==result
        assert before==conn._db


def test_reject_and_stale_preview_do_not_dispatch(practice):
    conn,cid,rid=practice
    with scope(conn,KEY):
        initial=guided.create_in_transaction(conn,cid)
        preview=guided.prepare_in_transaction(conn,cid,rid,'supplied_update',initial['source_digest'])
        with pytest.raises(WorkflowError,match='preview changed'):
            guided.decide_in_transaction(conn,cid,rid,'00'*32,'approve','Wrong identity')
        rejected=guided.decide_in_transaction(conn,cid,rid,preview['review']['review_digest'],'reject','Do not execute')
        assert rejected['source_digest']==initial['source_digest']
        with pytest.raises(WorkflowError,match='approval'):
            guided.advance_in_transaction(conn,cid,rid)


def test_stale_pending_preview_can_be_rejected_and_replaced(practice):
    conn,cid,rid=practice
    with scope(conn,KEY):
        initial=guided.create_in_transaction(conn,cid)
        preview=guided.prepare_in_transaction(conn,cid,rid,'supplied_update',initial['source_digest'])
        run=guided._run(conn,cid)
        guided.manny_review.mutate_in_transaction(conn,principal=guided.PRINCIPAL,run_id=run.run_id,dossier_key='main',
            operation_id='separate-owner-brief',expected_digest=initial['source_digest'],action='prepare')
        with pytest.raises(WorkflowError,match='dossier changed'):
            guided.decide_in_transaction(conn,cid,rid,preview['review']['review_digest'],'approve','Old evidence')
        rejected=guided.decide_in_transaction(conn,cid,rid,preview['review']['review_digest'],'reject','Replace stale preview')
        assert rejected['review']['status']=='rejected'
        replacement=guided.prepare_in_transaction(conn,cid,uuid4(),'supplied_update',rejected['source_digest'])
        assert replacement['review']['manifest']['source_digest']==rejected['source_digest']


@pytest.mark.parametrize('database,role',[('growup_dev','growup_demo'),('growup_prod','growup_demo'),('growup_handoff_demo','growup_app')])
def test_operational_domains_are_refused_without_writes(database,role):
    db=_new_db();db.update(database=database,role=role);before=deepcopy(db)
    with pytest.raises(ValueError,match='operational databases are forbidden'):
        with scope(Conn(db),KEY):guided.create_in_transaction(Conn(db),uuid4())
    assert db==before


def test_http_review_requires_owner_and_same_origin(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv('SERVER_MODE','LIVE_SERVER')
    monkeypatch.setenv('WONDRUE_OPERATOR_KEY','review-owner-test')
    monkeypatch.setenv('WONDRUE_READER_KEY','review-reader-test')
    monkeypatch.setenv('PUBLIC_SESSION_SECRET','x'*40)
    calls=[]
    monkeypatch.setattr(guided,'service',lambda action,**inputs:calls.append(action) or {'synthetic':True})
    with TestClient(app,base_url='https://growup.securecodeusa.com:8443') as client:
        assert client.post('/wondrue/api/guided-review',json={'id':str(uuid4())}).status_code==401
        headers={'X-Wondrue-Key':'review-reader-test','Origin':'https://growup.securecodeusa.com:8443'}
        assert client.post('/wondrue/api/guided-review',headers=headers,json={'id':str(uuid4())}).status_code==403
        headers['X-Wondrue-Key']='review-owner-test';headers['Origin']='https://untrusted.invalid'
        assert client.post('/wondrue/api/guided-review',headers=headers,json={'id':str(uuid4())}).status_code==403
        assert not calls
        headers['Origin']='https://growup.securecodeusa.com:8443'
        assert client.post('/wondrue/api/guided-review',headers=headers,json={'id':str(uuid4())}).status_code==200
        assert calls==['create']


def test_emergency_stop_refuses_before_connecting(monkeypatch):
    monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','1')
    monkeypatch.setattr(guided.demo,'configuration',lambda:pytest.fail('must refuse before opening any database'))
    with pytest.raises(WorkflowError,match='emergency-stopped'):
        guided.service('create',case_id=uuid4())


@pytest.mark.skipif(os.getenv('GROWUP_GUIDED_REVIEW_PG')!='1',reason='Explicit isolated demo PostgreSQL proof disabled')
def test_pg_interruption_resume_concurrency_and_both_outcomes(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.handoff_demo import configuration
    from app.properties_db import get_connection
    cfg,key=configuration()
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    for choice in ('supplied_update','keep_unknown'):
        cid=str(uuid4());rid=str(uuid4())
        initial=guided.service('create',case_id=cid)
        preview=guided.service('preview',case_id=cid,review_id=rid,choice=choice,expected_digest=initial['source_digest'])
        guided.service('decide',case_id=cid,review_id=rid,expected_digest=preview['review']['review_digest'],choice='approve',note='SYNTHETIC automated proof of explicit approval')
        # Fail after canonical mutations but before the corresponding checkpoint.
        save=guided._save_step
        def interrupt(*args,**kwargs):raise KeyboardInterrupt('simulated process interruption before checkpoint')
        monkeypatch.setattr(guided,'_save_step',interrupt)
        with pytest.raises(KeyboardInterrupt):guided.service('advance',case_id=cid,review_id=rid)
        monkeypatch.setattr(guided,'_save_step',save)
        unchanged=guided.service('inspect',case_id=cid,review_id=rid)
        assert unchanged['review']['status']=='approved' and unchanged['source_digest']==initial['source_digest']
        # Two resumptions cannot duplicate a step: the canonical run is row-locked.
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _:guided.service('advance',case_id=cid,review_id=rid),range(2)))
        result=guided.service('advance',case_id=cid,review_id=rid)
        assert result['review']['status']=='complete' and len(result['review']['timeline'])==3
        assert result['verified_receipts']==initial['verified_receipts']+2
        assert result['original_preserved'] and result['inherited_reports_preserved']
        assert result['review']['result']['status']==('reassessed' if choice=='supplied_update' else 'needs_evidence')
        assert guided.service('advance',case_id=cid,review_id=rid)==result
        assert any(item['id']==cid for item in guided.service('list')['items'])
        original=guided.service('record',case_id=cid,version=initial['source_version'])
        assert original['digest']==initial['source_digest']
        # Direct identity proof: no operational fallback or privileged role.
        with get_connection(cfg) as conn:
            with scope(conn,key):guided._guard(conn)
