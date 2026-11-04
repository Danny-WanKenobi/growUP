"""Focused evidence-to-owner proof. Real properties are never fabricated."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
import base64,io,os
import pytest
from app import evidence_workbench as work,owner_evidence as evidence,wondrue_workspace as ws,wondrue_retained_review as review,refresh_workflow as refresh
from app.workflow_db import WorkflowError,get_record_in_transaction
from app.workflow import digest_payload
from tests.test_handoff_demo import Conn
from tests.test_funnel_acceptance import _new_db
from tests.workbench_fixtures import retained_fixture


@pytest.fixture
def retained(monkeypatch):
    monkeypatch.setenv('GROWUP_PG_INTEGRATION','1');monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1');monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')
    monkeypatch.setenv('CUSTODY_HMAC_KEY','milestone-test-only-key-0123456789abcd')
    conn=Conn(dict(_new_db(),database='growup_mvp_gate',role='growup_app'));run,row=retained_fixture(conn)
    ref={'run_id':str(run.run_id),'key':'main','version':1,'digest':row.digest}
    return conn,ref


def attach(conn,ref):
    result=evidence.intake(conn,evidence_id=uuid4(),source=ref,filename='TEST-rates.txt',content_base64=base64.b64encode(b'TEST unverified price 9 USD/kg\nSpacing 0.3 m\n').decode(),
        issuer='TEST source',source_url=None,document_date=None,obtained_on=None,claim_scope=['financial'],note='Unverified supplied rate, not a market fact')
    return work.state(conn,ref['run_id'],result['id'])


def test_extraction_correction_and_rejection_preserve_original(retained):
    conn,ref=retained;v=attach(conn,ref)
    extracted=work.extract(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'])
    assert extracted['extraction']['status']=='extracted' and extracted['extraction']['resource_limits_enforced']
    assert extracted['extraction']['measurements'][0]['unit_text']=='USD/kg'
    assert extracted['extraction']['segments'][1]['locator']=={'line':2}
    metadata={k:deepcopy(v[k]) for k in work.FIELDS};metadata['note']='Later declared correction, still unverified'
    args=dict(run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'],metadata=metadata,reason='TEST correction',operation_id=uuid4())
    revised=work.revise(conn,**args)
    assert revised['state_digest']!=v['state_digest'] and revised['sha256']==v['sha256']
    assert work.revise(conn,**args)==revised
    assert evidence.inspect(conn,ref['run_id'],v['id'])['note']==v['note']
    with pytest.raises(WorkflowError,match='changed'):work.extract(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'])
    rejected=work.admit(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=revised['state_digest'],choice='reject',note='Contradictory unsupported claim')
    assert rejected['admission']['status']=='rejected_claim' and not rejected['admission']['authentication_granted']
    with pytest.raises(WorkflowError,match='rejected'):work.resolve_links(conn,ref['run_id'],'main',[{'id':v['id'],'state_digest':revised['state_digest']}])


def test_admission_decision_compare_and_swap(retained):
    conn,ref=retained;v=attach(conn,ref)
    args=dict(run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'],choice='hold',note='TEST hold')
    first=work.admit(conn,**args)
    assert work.admit(conn,**args)==first
    with pytest.raises(WorkflowError,match='decision changed'):
        work.admit(conn,**dict(args,choice='reject'))
    latest=ws.read(conn,ref['run_id'],'evidence-admission:'+v['id'])
    second=work.admit(conn,**dict(args,choice='reject',expected_admission_digest=latest.digest))
    assert second['admission']['status']=='rejected_claim'


def test_campaign_suppression_remains_a_readiness_blocker(retained,monkeypatch):
    from app import uriel,owner_analysis
    conn,ref=retained
    monkeypatch.setattr(uriel,'prepare_campaign',lambda _: {'status':'suppressed','missing':[]})
    result=owner_analysis.campaign_readiness(conn,ref)
    assert result['status']=='needs_evidence' and 'campaign_suppressed' in result['blockers']
    assert result['external_delivery_authorized'] is False


def test_superseding_document_cycle_refused(retained):
    conn,ref=retained;a=attach(conn,ref)
    b=evidence.intake(conn,evidence_id=uuid4(),source=ref,filename='TEST-revised.txt',content_base64=base64.b64encode(b'TEST different revision').decode(),
        issuer='TEST source',source_url=None,document_date=None,obtained_on=None,claim_scope=['financial'],note='TEST replacement, unverified')
    b=work.state(conn,ref['run_id'],b['id'])
    def replace(v,parent):
        return work.revise(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'],metadata={k:v[k] for k in work.FIELDS},reason='TEST superseding link',operation_id=uuid4(),supersedes=parent['id'])
    replace(a,b)
    with pytest.raises(WorkflowError,match='cycle'):replace(b,a)


def test_portfolio_uses_recorded_priorities(retained,monkeypatch):
    from app import owner_analysis
    conn,ref=retained
    row=ws.record(conn,ref['run_id'],'dossier_version','main')
    monkeypatch.setattr(owner_analysis,'latest_rows',lambda *a,**k: ([{'run_id':row['run_id'],'record_key':'main','kind':'dossier_version','version':row['version'],'digest':row['digest'],'payload':row['data']}],False))
    result=owner_analysis.portfolio(conn,'2026-11-04')
    assert len(result['items'])==1 and not result['items'][0].get('claims_quarantined')
    assert result['provider_calls']==0 and result['records_written']==0


def test_pdf_pages_and_encrypted_refusal():
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    w=PdfWriter();p=w.add_blank_page(width=200,height=200)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):w._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 10 100 Td (TEST area 100 m2) Tj ET');p[NameObject('/Contents')]=w._add_object(stream)
    buf=io.BytesIO();w.write(buf);result=work.run_extractor(buf.getvalue(),'application/pdf')
    assert result['status']=='extracted' and result['segments'][0]['locator']['page']==1
    assert result['measurements'][0]['value_text']=='100'
    w.encrypt('TEST');buf=io.BytesIO();w.write(buf)
    assert work.run_extractor(buf.getvalue(),'application/pdf')['status']=='parser_refused'


def test_cited_rate_reassessment_diff_and_frozen_links(retained):
    from app.owner_analysis import compare_versions
    conn,ref=retained;v=attach(conn,ref);v=work.extract(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'])
    rid=uuid4();preview=review.prepare(conn,rid,ref,v['review_task_key'],'michael','Use this exact extracted rate as an assumption only',
        evidence_links=[{'id':v['id'],'state_digest':v['state_digest']}],changes=[{'evidence_id':v['id'],'measurement_index':0,'name':'price_per_kg','unit':'USD/kg'}])
    assert preview['review']['manifest']['approved_assessment_inputs']['financial']['price_per_kg']['classification']=='assumed'
    review.decide(conn,rid,preview['review']['review_digest'],'approve','TEST owner approval of exact cited assumption')
    for _ in range(3):result=review.advance(conn,rid)
    assert result['review']['status']=='complete'
    latest=ws.record(conn,ref['run_id'],'dossier_version','main');entry=latest['data']['dossier']['agent_reassessments'][-1]
    assert entry['supporting_evidence'][0]['sha256']==v['sha256']
    difference=compare_versions(conn,ref,{k:latest[k] for k in ('run_id','key','version','digest')})
    assert any('price_per_kg.value' in c['path'] for c in difference['changes'])
    assert difference['remaining_gaps'] is not None


def test_refresh_scope_cas_recovery_and_readiness(retained):
    from app.owner_analysis import campaign_readiness
    conn,ref=retained;rid=uuid4();p=review.prepare(conn,rid,ref,'test-retained-task','nicole','Refresh retained evidence')
    review.decide(conn,rid,p['review']['review_digest'],'approve','TEST explicit correction')
    for _ in range(3):review.advance(conn,rid)
    row=ws.record(conn,ref['run_id'],'dossier_version','main');current={k:row[k] for k in ('run_id','key','version','digest')}
    assert campaign_readiness(conn,current)['external_delivery_authorized'] is False
    id=uuid4();p=refresh.prepare(conn,id=id,source=current,note='Refresh only existing stale report dependencies')
    assert p['agents']==['theresia_keys','simeon','solomon','elmer']
    with pytest.raises(WorkflowError,match='approved'):refresh.advance(conn,id=id)
    refresh.decide(conn,id=id,expected_digest=p['review_digest'],choice='approve',note='TEST owner scope approval')
    for _ in range(8):
        result=refresh.advance(conn,id=id)
        if result['status']!='approved':break
    assert result['status']=='complete' and len(result['timeline'])==4
    before=deepcopy(conn._db);assert refresh.advance(conn,id=id)==result and before==conn._db


def test_stress_arithmetic_and_unknowns():
    from tests.test_michael import _engineering,_finance
    from app.michael import operating_stress_cases
    from app.michael import FinancialInput
    f=_finance();assert not operating_stress_cases('bench',_engineering(),f)['complete']
    for name in ('occupancy_usd_per_m2_year','maintenance_usd_per_year'):
        f[name]=FinancialInput(name=name,value=Decimal('0'),classification='assumed',source='TEST explicit zero')
    result=operating_stress_cases('bench',_engineering(),f);base=result['baseline_outputs'];delay=result['cases']['construction_delay_3_months']['outputs']
    assert Decimal(delay['year_one_operating_net_usd'])==Decimal(delay['year_one_revenue_usd'])-Decimal(base['opex_usd_per_year'])
    assert Decimal(result['cases']['crop_price_down_20pct']['outputs']['revenue_usd_per_year'])==Decimal(base['revenue_usd_per_year'])*Decimal('.8')


def test_new_routes_require_owner_and_origin(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv('SERVER_MODE','LIVE_SERVER');monkeypatch.setenv('WONDRUE_OPERATOR_KEY','milestone-owner');monkeypatch.setenv('WONDRUE_READER_KEY','milestone-reader')
    with TestClient(app,base_url='https://growup.securecodeusa.com:8443') as client:
        path='/wondrue/api/evidence/'+str(uuid4())+'/'+str(uuid4())+'/extract'
        assert client.post(path,json={'expected_digest':'ab'*32}).status_code==401
        assert client.post(path,json={'expected_digest':'ab'*32},headers={'X-Wondrue-Key':'milestone-reader','Origin':'https://growup.securecodeusa.com:8443'}).status_code==403
        assert client.post(path,json={'expected_digest':'ab'*32},headers={'X-Wondrue-Key':'milestone-owner','Origin':'https://bad.invalid'}).status_code==403


def test_transport_authenticated_correspondence_stays_unverified(retained,monkeypatch):
    from app.mailbox_routing import receive,retain_content
    from app.owner_analysis import correspondence_proposal,correspondence_listing
    monkeypatch.setenv('RESEND_RECEIVING_DOMAIN','messages.example.invalid')
    conn,ref=retained;mid=str(uuid4());text='TEST sender claims the room is 100 m2; confirm independently.'
    metadata={'email_id':mid,'from':'sender@example.invalid','to':['simeon@messages.example.invalid'],'subject':'TEST evidence proposal'}
    receive(conn,metadata=metadata,event_id='TEST',body_digest=digest_payload(metadata),authentication='resend_authenticated_api')
    detail={**metadata,'id':mid,'text':text,'message_id':'<test@example.invalid>'}
    retain_content(conn,conversation_id=mid,detail=detail,response_digest=digest_payload(detail))
    args=dict(source=ref,conversation_id=mid,operation_id=uuid4(),start=0,end=len(text),claim_scope=['dimensions'],note='Explicit TEST owner correlation')
    result=correspondence_proposal(conn,**args)
    assert result['authentication_status']=='sender_claim_unverified' and not result['external_actions']
    assert correspondence_proposal(conn,**args)==result
    assert evidence.inspect(conn,ref['run_id'],result['evidence_id'])['source_authentication_granted'] is False
    with pytest.raises(WorkflowError,match='excerpt'):correspondence_proposal(conn,**dict(args,operation_id=uuid4(),end=9000))


def test_campaign_refuses_stale_engineering_inputs(retained):
    from app.uriel import prepare_campaign,UrielError
    conn,ref=retained;p=deepcopy(ws.record(conn,ref['run_id'],'dossier_version','main')['data'])
    engineering=p['dossier']['reports']['onesimus'];engineering['changed_assumption']='TEST';engineering['digest']=digest_payload({k:v for k,v in engineering.items() if k!='digest'})
    p['digest']=digest_payload({k:v for k,v in p.items() if k!='digest'})
    with pytest.raises(UrielError,match='stale'):prepare_campaign(p)


def test_official_byte_mismatch_cannot_authenticate(retained,monkeypatch):
    from app import mvp
    conn,ref=retained;v=attach(conn,ref)
    monkeypatch.setattr(mvp,'_load_verified_successor_inputs',lambda *a,**k:({'collection':{'body_sha256':'ff'*32}},[]))
    monkeypatch.setattr(mvp,'preview_successor_in_transaction',lambda *a,**k:pytest.fail('must not preview mismatched official bytes'))
    result=work.admit(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'],choice='check_official_match',note='TEST exact retained comparison',collection_run_id=uuid4())
    assert result['admission']['status']=='needs_evidence' and not result['admission']['authentication_granted']


def test_refresher_rejects_runtime_drift(retained,monkeypatch):
    conn,ref=retained;p=refresh.prepare(conn,id=uuid4(),source=ref,note='TEST no scope expansion')
    monkeypatch.setattr(refresh,'runtime_digest',lambda:'changed-runtime')
    with pytest.raises(WorkflowError,match='runtime changed'):refresh.decide(conn,id=p['id'],expected_digest=p['review_digest'],choice='approve',note='TEST stale plan')


def test_official_match_only_previews_shared_policy(retained,monkeypatch):
    from app import mvp
    conn,ref=retained;v=attach(conn,ref);called=[]
    monkeypatch.setattr(mvp,'_load_verified_successor_inputs',lambda *a,**k:({'collection':{'body_sha256':v['sha256']}},[]))
    def preview(*a,**k):
        called.append(k);return {'ready':True,'intended':{'decision':'needs_evidence'}}
    monkeypatch.setattr(mvp,'preview_successor_in_transaction',preview)
    result=work.admit(conn,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest'],choice='check_official_match',note='TEST shared preview only',collection_run_id=uuid4())
    assert called and result['admission']['status']=='shared_policy_preview'
    assert result['authentication_status']=='owner_supplied_unverified'
    assert result['admission']['authentication_granted'] is False
    assert ws.record(conn,ref['run_id'],'dossier_version','main')['digest']==ref['digest']


def test_real_operating_proof_refuses_test_material(retained):
    from app.real_operating_proof import inspect
    conn,ref=retained
    with pytest.raises(WorkflowError,match='test fixtures'):inspect(conn,ref)


@pytest.mark.skipif(os.getenv('GROWUP_PG_INTEGRATION')!='1',reason='Dedicated gate proof explicitly disabled')
def test_pg_evidence_rework_refresh_and_interruption(monkeypatch):
    from tests.test_dcad_admission import _require_gate_database
    from app.properties_db import get_connection
    _require_gate_database();monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    run,row=ws.transaction('milestone_fixture',retained_fixture);ref={'run_id':str(run.run_id),'key':'main','version':1,'digest':row.digest}
    v=ws.transaction('milestone_attachment',lambda c:attach(c,ref))
    v=ws.transaction('milestone_extract',lambda c:work.extract(c,run_id=ref['run_id'],evidence_id=v['id'],expected_digest=v['state_digest']))
    rid=uuid4();p=review.service('prepare',review_id=rid,ref=ref,task_key=v['review_task_key'],target='nicole',note='TEST retained policy refresh',evidence_links=[{'id':v['id'],'state_digest':v['state_digest']}])
    review.service('decide',review_id=rid,expected_digest=p['review']['review_digest'],choice='approve',note='TEST approved exact linked preview')
    for _ in range(3):review.service('advance',review_id=rid)
    with get_connection() as conn:source=ws.record(conn,ref['run_id'],'dossier_version','main')
    current={k:source[k] for k in ('run_id','key','version','digest')};id=uuid4()
    p=ws.transaction('milestone_refresh_prepare',lambda c:refresh.prepare(c,id=id,source=current,note='TEST bounded refresh'))
    ws.transaction('milestone_refresh_approve',lambda c:refresh.decide(c,id=id,expected_digest=p['review_digest'],choice='approve',note='TEST plan approval'))
    original=ws.write
    def interrupt(conn,rid,key,*a,**kw):
        if key=='refresh-workflow':raise KeyboardInterrupt('interrupted after canonical stage mutation')
        return original(conn,rid,key,*a,**kw)
    monkeypatch.setattr(ws,'write',interrupt)
    with pytest.raises(KeyboardInterrupt):ws.transaction('milestone_refresh_interrupt',lambda c:refresh.advance(c,id=id))
    monkeypatch.setattr(ws,'write',original)
    with get_connection() as conn:
        assert refresh.inspect(conn,id)['timeline']==[]
        assert ws.record(conn,ref['run_id'],'dossier_version','main')['digest']==source['digest']
    for _ in range(8):
        result=ws.transaction('milestone_refresh_step',lambda c:refresh.advance(c,id=id))
        if result['status']!='approved':break
    assert result['status']=='complete' and len(result['timeline'])==4
