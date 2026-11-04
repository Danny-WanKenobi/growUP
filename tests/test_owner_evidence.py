"""Bounded private-document retention; never source authentication or providers."""
import base64
from copy import deepcopy
import os
from uuid import uuid4
import pytest
from app import owner_evidence as evidence,wondrue_workspace as ws
from app.workflow import digest_payload
from app.workflow_db import WorkflowError
from tests.test_handoff_demo import Conn
from tests.test_funnel_acceptance import _new_db
from tests.test_wondrue_workspace import dossier


@pytest.fixture
def stored(monkeypatch):
    monkeypatch.setenv('GROWUP_PG_INTEGRATION','1')
    monkeypatch.setenv('SERVER_MODE','LIVE_SERVER')
    monkeypatch.setenv('WONDRUE_OPERATOR_KEY','evidence-owner-test');monkeypatch.setenv('WONDRUE_READER_KEY','evidence-reader-test')
    conn=Conn(dict(_new_db(),database='growup_mvp_gate',role='growup_app'));run,row=dossier(conn)
    return conn,{'run_id':str(run.run_id),'key':'main','version':row.version,'digest':row.digest}


def request(source,**changes):
    return dict(evidence_id=str(uuid4()),source=source,filename='TEST-document.txt',content_base64=base64.b64encode(b'TEST ONLY: unverified dimensions claim').decode(),
        issuer='TEST supplied document',source_url='https://records.example.invalid/document',document_date='2026-11-01',obtained_on=None,
        claim_scope=['dimensions'],note='TEST claim only, not authenticated',**changes)


def test_retention_replay_download_and_no_dossier_mutation(stored):
    conn,ref=stored;args=request(ref);original=deepcopy(ws.record(conn,ref['run_id'],'dossier_version','main'))
    result=evidence.intake(conn,**args)
    assert result['authentication_status']=='owner_supplied_unverified' and not result['source_authentication_granted']
    assert result['document_date']=='2026-11-01' and result['obtained_on'] is None
    assert evidence.intake(conn,**args)['replayed']
    listing=evidence.listing(conn,ref);assert len(listing['items'])==1 and listing['items'][0]['bound_to_selected_version']
    assert 'content_base64' not in str(listing)
    metadata,raw=evidence.download(conn,ref['run_id'],args['evidence_id'])
    assert raw==base64.b64decode(args['content_base64']) and metadata['sha256']==result['sha256']
    task=ws.record(conn,ref['run_id'],'owner_review_task',result['review_task_key'])
    assert not task['data']['grants_permission'] and task['data']['evidence_metadata_digest']==result['metadata_digest']
    assert ws.record(conn,ref['run_id'],'dossier_version','main')==original
    with pytest.raises(WorkflowError,match='identity conflict'):evidence.intake(conn,**dict(args,note='different claim'))
    with pytest.raises(WorkflowError,match='already attached'):evidence.intake(conn,**dict(args,evidence_id=str(uuid4())))


def test_stale_source_and_size_ceiling_refuse_without_partial_records(stored,monkeypatch):
    conn,ref=stored;before=deepcopy(conn._db)
    with pytest.raises(WorkflowError,match='changed'):evidence.intake(conn,**request(dict(ref,digest='ff'*32)))
    assert conn._db==before
    evidence.intake(conn,**request(ref));monkeypatch.setattr(evidence,'MAX_FILES',1)
    with pytest.raises(WorkflowError,match='already has'):evidence.intake(conn,**dict(request(ref),content_base64=base64.b64encode(b'Different TEST bytes').decode()))


@pytest.mark.parametrize('name,raw',[
    ('../secret.txt',b'test'),('script.html',b'<script>bad</script>'),('archive.zip',b'PKxx'),
    ('fake.pdf',b'not a PDF'),('nul.txt',b'bad\x00text'),('bad.txt',b'\xff'),('empty.txt',b''),('large.txt',b'x'*(evidence.MAX_BYTES+1)),
],ids=['path','html','archive','wrong-header','nul-text','non-utf8','empty','oversized'])
def test_unsafe_or_unbounded_formats_refuse(name,raw):
    with pytest.raises(WorkflowError):evidence.validate_document(name,base64.b64encode(raw).decode())


def test_blob_or_index_tampering_refuses(stored):
    conn,ref=stored;result=evidence.intake(conn,**request(ref))
    row=ws.read(conn,ref['run_id'],result['blob_key'])
    ws.write(conn,ref['run_id'],result['blob_key'],dict(row.payload,content_base64=base64.b64encode(b'changed bytes').decode()),row)
    with pytest.raises(WorkflowError,match='corrupt'):evidence.download(conn,ref['run_id'],result['id'])


def test_authentication_and_source_url_claims_cannot_mint_authority(stored):
    conn,ref=stored
    for url in ('file:///etc/passwd','https://user:password@example.invalid/x','javascript:alert(1)'):
        with pytest.raises(WorkflowError,match='source URL'):evidence.intake(conn,**dict(request(ref),source_url=url))
    assert evidence.listing(conn,ref)['items']==[]


def test_http_permissions_stream_limits_and_download_headers(stored,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routes import owner_evidence as routes
    conn,ref=stored;calls=[]
    monkeypatch.setattr(routes,'safe_call',lambda action,fn,write=False:calls.append(action) or fn(conn))
    args=request(ref);body=dict(args,id=args['evidence_id']);body.pop('evidence_id')
    headers={'X-Wondrue-Key':'evidence-owner-test','Origin':'https://growup.securecodeusa.com:8443'}
    with TestClient(app,base_url=headers['Origin']) as client:
        assert client.post('/wondrue/api/evidence',json=body).status_code==401
        assert client.post('/wondrue/api/evidence',json=body,headers=dict(headers,**{'X-Wondrue-Key':'evidence-reader-test'})).status_code==403
        assert client.post('/wondrue/api/evidence',json=body,headers=dict(headers,Origin='https://untrusted.invalid')).status_code==403
        assert client.post('/wondrue/api/evidence',json=dict(body,authentication_status='verified'),headers=headers).status_code==422
        assert client.post('/wondrue/api/evidence',content=b'{}',headers=dict(headers,**{'Content-Type':'application/json','Content-Length':str(routes.BODY_LIMIT+1)})).status_code==413
        assert client.post('/wondrue/api/evidence',content=iter([b'x'*800000,b'x'*800000]),headers=dict(headers,**{'Content-Type':'application/json'})).status_code==413
        assert not calls
        response=client.post('/wondrue/api/evidence',json=body,headers=headers)
        assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
        result=response.json()
        path=f"/wondrue/api/evidence/{ref['run_id']}/{result['id']}/download"
        assert client.get(path).status_code==401
        response=client.get(path,headers={'X-Wondrue-Key':'evidence-reader-test'})
        assert response.status_code==200 and response.headers['Content-Type']=='application/octet-stream'
        assert response.headers['Content-Disposition'].startswith('attachment;') and response.headers['X-Content-Type-Options']=='nosniff'
        assert response.headers['X-Content-SHA256']==result['sha256']


@pytest.mark.skipif(os.getenv('GROWUP_PG_INTEGRATION')!='1',reason='Explicit dedicated gate PostgreSQL proof disabled')
def test_pg_atomic_upload_concurrent_duplicates_and_custody_unchanged(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.properties_db import get_connection
    from tests.test_dcad_admission import _require_gate_database
    from tests.workbench_fixtures import retained_fixture
    from app.custody_db import verify_chain_in_transaction
    from app.custody import dossier_id_for_property
    _require_gate_database();monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    run,source=ws.transaction('test_attachment_fixture',retained_fixture)
    ref={'run_id':str(run.run_id),'key':'main','version':1,'digest':source.digest};args=request(ref)
    did=dossier_id_for_property(source.payload['property_id'])
    with get_connection() as conn:receipts=verify_chain_in_transaction(conn,did)
    original=ws.write
    def interrupt(conn,rid,key,*a,**kw):
        if key.startswith('owner-evidence-index:'):raise KeyboardInterrupt('simulated interruption before index checkpoint')
        return original(conn,rid,key,*a,**kw)
    monkeypatch.setattr(ws,'write',interrupt)
    with pytest.raises(KeyboardInterrupt):ws.transaction('test_interrupted_attachment',lambda c:evidence.intake(c,**args))
    monkeypatch.setattr(ws,'write',original)
    with get_connection() as conn:
        assert evidence.listing(conn,ref)['items']==[]
        assert ws.read(conn,run.run_id,'owner-evidence:'+args['evidence_id']) is None
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM workflow_records WHERE run_id=%s AND record_key LIKE 'owner-evidence%%'",(run.run_id,))
            assert cur.fetchone()['n']==0
    def race(_):return ws.transaction('test_attachment_race',lambda c:evidence.intake(c,**args))
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(race,range(2)))
    assert sorted(r['replayed'] for r in results)==[False,True]
    with get_connection() as conn:
        assert len(evidence.listing(conn,ref)['items'])==1
        assert evidence.download(conn,run.run_id,args['evidence_id'])[1]==base64.b64decode(args['content_base64'])
        assert ws.record(conn,run.run_id,'dossier_version','main')['data']==source.payload
        assert verify_chain_in_transaction(conn,did)==receipts
        task=ws.record(conn,run.run_id,'owner_review_task',results[0]['review_task_key'])
        assert task['data']['authentication_status']=='owner_supplied_unverified'
