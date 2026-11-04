"""Critical evidence validity/conflict gates; no live data or providers."""
from copy import deepcopy
from uuid import uuid4
import base64,os
import pytest
from tests.test_owner_milestones import retained,attach
from app import evidence_quality as quality,evidence_workbench as work,owner_evidence as evidence,wondrue_workspace as ws
from app.workflow_db import WorkflowError


def policy(conn,ref,v,**updates):
    args=dict(run_id=ref['run_id'],evidence_id=v['id'],expected_state_digest=v['state_digest'],expected_policy_digest=None,
        expires_on='2020-01-01',reconfirm_after_days=None,date_basis='document_date',note='TEST explicit validity window')
    return quality.save_validity(conn,**dict(args,**updates))


def pair(conn,ref):
    a=attach(conn,ref)
    b=evidence.intake(conn,evidence_id=uuid4(),source=ref,filename='TEST-competing.txt',content_base64=base64.b64encode(b'TEST conflicting rate 12 USD/kg').decode(),
        issuer='TEST alternate source',source_url=None,document_date=None,obtained_on=None,claim_scope=['financial'],note='TEST competing unverified rate')
    return a,work.state(conn,ref['run_id'],b['id'])


def test_dates_are_explicit_and_receipt_time_is_not_evidence():
    metadata={'document_date':'2026-10-01','obtained_on':'2026-11-04','received_at':'2026-11-04T00:00:00Z'}
    p={'expires_on':'2026-11-04','date_basis':'document_date','reconfirm_after_days':None}
    assert quality.freshness(metadata,p,'2026-11-04')['status']=='within_declared_window'
    assert quality.freshness(metadata,p,'2026-11-05')['status']=='expired'
    p.update(expires_on=None,reconfirm_after_days=30)
    assert quality.freshness(metadata,p,'2026-11-04')['status']=='reconfirmation_due'
    metadata['document_date']=None
    assert quality.freshness(metadata,p,'2026-11-04')['status']=='date_unknown'
    metadata['document_date']='2026-12-01'
    assert quality.freshness(metadata,p,'2026-11-04')['status']=='future_dated'


def test_expired_or_changed_metadata_blocks_reuse_with_history(retained):
    conn,ref=retained;v=attach(conn,ref);before=ws.record(conn,ref['run_id'],'dossier_version','main')['digest']
    result=policy(conn,ref,v)
    assert result['freshness']['status']=='expired'
    with pytest.raises(WorkflowError,match='quality'):work.resolve_links(conn,ref['run_id'],'main',[{'id':v['id'],'state_digest':v['state_digest']}])
    with pytest.raises(WorkflowError,match='policy changed'):policy(conn,ref,v,expires_on='2030-01-01')
    updated=policy(conn,ref,v,expires_on='2030-01-01',expected_policy_digest=result['validity_policy']['digest'])
    assert work.resolve_links(conn,ref['run_id'],'main',[{'id':v['id'],'state_digest':v['state_digest']}])[0]['quality_digest']==updated['review_digest']
    metadata={k:deepcopy(v[k]) for k in work.FIELDS};metadata['document_date']='2026-11-04'
    work.revise(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'],metadata=metadata,reason='TEST new date claim',operation_id=uuid4())
    assert quality.inspect(conn,ref['run_id'],v['id'])['freshness']['status']=='metadata_changed_requires_review'
    assert ws.record(conn,ref['run_id'],'dossier_version','main')['digest']==before


def test_conflict_review_preserves_both_claims_and_does_not_verify(retained):
    conn,ref=retained;a,b=pair(conn,ref);refs=[{'id':v['id'],'state_digest':v['state_digest']} for v in (a,b)]
    args=dict(run_id=ref['run_id'],id=uuid4(),evidence_refs=refs,claim_scope='financial',claims=['TEST 9 USD/kg','TEST 12 USD/kg'],disposition='unresolved',note='TEST competing assumptions')
    first=quality.save_conflict(conn,**args)
    assert quality.save_conflict(conn,**args)['digest']==first['digest']
    with pytest.raises(WorkflowError,match='quality'):work.resolve_links(conn,ref['run_id'],'main',refs)
    with pytest.raises(WorkflowError,match='decision changed'):quality.save_conflict(conn,**dict(args,disposition='reconciled_for_planning'))
    resolved=quality.save_conflict(conn,**dict(args,disposition='reconciled_for_planning',expected_digest=first['digest'],note='TEST treat as independent sensitivity assumptions, neither is verified'))
    assert resolved['version']==2 and resolved['claims']==first['claims'] and not resolved['authentication_granted']
    assert [r['disposition'] for r in quality.conflict_history(conn,ref['run_id'],args['id'])['decisions']]==['unresolved','reconciled_for_planning']
    assert len(work.resolve_links(conn,ref['run_id'],'main',refs))==2
    metadata={k:deepcopy(a[k]) for k in work.FIELDS};metadata['note']='TEST source corrected its rate claim'
    changed=work.revise(conn,run_id=ref['run_id'],evidence_id=a['id'],expected_digest=a['state_digest'],metadata=metadata,reason='TEST revised statement',operation_id=uuid4())
    assert not quality.inspect(conn,ref['run_id'],a['id'])['conflicts'][0]['current']
    with pytest.raises(WorkflowError,match='quality'):work.resolve_links(conn,ref['run_id'],'main',[{'id':a['id'],'state_digest':changed['state_digest']}])
    updated_refs=[{'id':a['id'],'state_digest':changed['state_digest']},refs[1]]
    reviewed=quality.save_conflict(conn,**dict(args,evidence_refs=updated_refs,disposition='reconciled_for_planning',expected_digest=resolved['digest'],note='TEST re-read corrected document; still assumptions'))
    assert reviewed['version']==3 and len(quality.conflict_history(conn,ref['run_id'],args['id'])['decisions'])==3
    critical=dict(args,id=uuid4(),evidence_refs=updated_refs,claim_scope='category',disposition='reconciled_for_planning')
    with pytest.raises(WorkflowError,match='authenticated upstream'):quality.save_conflict(conn,**critical)
    assert evidence.inspect(conn,ref['run_id'],a['id'])['source_authentication_granted'] is False


def test_quality_routes_require_owner_and_same_origin(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv('SERVER_MODE','LIVE_SERVER');monkeypatch.setenv('WONDRUE_OPERATOR_KEY','TEST-quality-owner');monkeypatch.setenv('WONDRUE_READER_KEY','TEST-quality-reader')
    with TestClient(app,base_url='https://growup.securecodeusa.com:8443') as client:
        path='/wondrue/api/evidence/'+str(uuid4())+'/'+str(uuid4())+'/validity'
        body=dict(expected_state_digest='ab'*32,expires_on='2030-01-01',date_basis='document_date',note='TEST')
        assert client.post(path,json=body).status_code==401
        assert client.post(path,json=body,headers={'X-Wondrue-Key':'TEST-quality-reader','Origin':'https://growup.securecodeusa.com:8443'}).status_code==403
        assert client.post(path,json=body,headers={'X-Wondrue-Key':'TEST-quality-owner','Origin':'https://other.invalid'}).status_code==403


@pytest.mark.skipif(os.getenv('GROWUP_PG_INTEGRATION')!='1',reason='Dedicated PostgreSQL gate required')
def test_pg_quality_history_and_atomic_conflict_rollback(monkeypatch):
    from tests.test_dcad_admission import _require_gate_database
    from tests.workbench_fixtures import retained_fixture
    from app.properties_db import get_connection
    _require_gate_database();monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    run,row=ws.transaction('quality_fixture',retained_fixture);ref={'run_id':str(run.run_id),'key':'main','version':row.version,'digest':row.digest}
    a,b=ws.transaction('quality_attachments',lambda c:pair(c,ref))
    args=dict(run_id=str(run.run_id),id=uuid4(),evidence_refs=[{'id':v['id'],'state_digest':v['state_digest']} for v in (a,b)],claim_scope='financial',claims=['TEST nine','TEST twelve'],disposition='unresolved',note='TEST contradiction')
    original=ws.write
    def interrupt(conn,rid,key,*values,**kw):
        result=original(conn,rid,key,*values,**kw)
        if key.startswith('evidence-conflict:'):raise KeyboardInterrupt('TEST after indexes and conflict insertion')
        return result
    monkeypatch.setattr(ws,'write',interrupt)
    with pytest.raises(KeyboardInterrupt):ws.transaction('quality_interruption',lambda c:quality.save_conflict(c,**args))
    monkeypatch.setattr(ws,'write',original)
    with get_connection() as conn:
        assert not quality.inspect(conn,str(run.run_id),a['id'])['conflicts']
    saved=ws.transaction('quality_conflict',lambda c:quality.save_conflict(c,**args))
    assert saved['version']==1
    result=ws.transaction('quality_expiration',lambda c:policy(c,ref,a))
    assert result['freshness']['status']=='expired' and result['conflicts'][0]['claims']==args['claims']
    with get_connection() as conn:
        assert ws.record(conn,str(run.run_id),'dossier_version','main')['digest']==row.digest
