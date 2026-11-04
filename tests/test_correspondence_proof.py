from uuid import uuid4
import httpx
import pytest
from app import correspondence_proof as proof
from app import mailbox_routing
from app.workflow_db import WorkflowError
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db


@pytest.fixture
def environment(monkeypatch):
    for name,value in {'CORRESPONDENCE_PROOF_APPROVED':'1','DEMO_MODE':'yes','RESEND_API_KEY':'re_fixture',
        'SABBATH_BLACKOUT':'0','RESEND_RECEIVING_DOMAIN':proof.DOMAIN,'DOSSIER_COORDINATOR_EMERGENCY_STOP':'0'}.items():
        monkeypatch.setenv(name,value)
    conn=Conn(_new_db())
    monkeypatch.setattr(proof,'transaction',lambda cfg,action,mutate:mutate(conn))
    return conn


def test_owner_only_transport_and_failed_request_is_not_retransmitted(environment,monkeypatch):
    calls=[]
    def handle(req):
        calls.append(req)
        return httpx.Response(403,json={'error':'private provider details not returned'})
    monkeypatch.setattr(proof,'HTTP_CLIENT',lambda **kw:httpx.Client(transport=httpx.MockTransport(handle),**kw))
    with pytest.raises(WorkflowError,match='HTTP 403'): proof.discover()
    with pytest.raises(WorkflowError,match='uncertain prior'): proof.discover()
    assert len(calls)==1
    with pytest.raises(WorkflowError,match='cannot initiate'):
        proof.request(proof.config(),'bad','POST','/emails',{'from':'simeon@'+proof.DOMAIN,
            'to':[proof.DEMO_RECIPIENT],'reply_to':'simeon@'+proof.DOMAIN})
    with pytest.raises(WorkflowError,match='owner-only'):
        proof.request(proof.config('solomon'),'bad','POST','/emails',{'from':'solomon@'+proof.DOMAIN,
            'to':['stranger@example.com'],'reply_to':'solomon@'+proof.DOMAIN})


def test_retrieved_content_binds_envelope_and_reply_threads_without_duplicate_send(environment,monkeypatch):
    mid=str(uuid4()); calls=[]
    detail={'id':mid,'from':proof.DEMO_RECIPIENT,'to':['simeon@'+proof.DOMAIN],
        'subject':'Controlled proof','message_id':'<owner-test@protonmail.com>','text':'Please explain.'}
    def handle(req):
        import json
        calls.append(req)
        if req.method=='GET': return httpx.Response(200,json=detail)
        body=json.loads(req.content)
        assert body['headers']['In-Reply-To']==detail['message_id']
        assert body['subject']=='Re: Controlled proof'
        return httpx.Response(200,json={'id':str(uuid4())})
    monkeypatch.setattr(proof,'HTTP_CLIENT',lambda **kw:httpx.Client(transport=httpx.MockTransport(handle),**kw))
    assert proof.fetch(mid,'simeon')['mailbox']=='simeon'
    response=proof.reply(mid,'simeon','Conditional information only.')
    assert proof.reply(mid,'simeon','Conditional information only.')==response
    assert len(calls)==2
    with pytest.raises(WorkflowError,match='identity conflict'):
        proof.reply(mid,'simeon','Different message cannot reuse a claimed response.')
    with pytest.raises(WorkflowError,match='actor'):
        proof.reply(mid,'solomon','Wrong agent')
    with pytest.raises(WorkflowError,match='envelope mismatch'):
        mailbox_routing.retain_content(environment,conversation_id=mid,
            detail={**detail,'from':'other@example.com'},response_digest='x')


def test_send_and_read_limits_are_durable(environment):
    cfg=proof.config('solomon')
    for i in range(3): proof.reserve(environment,cfg,'send-'+str(i),'POST','/emails',{})
    with pytest.raises(WorkflowError,match='allowance'): proof.reserve(environment,cfg,'send-4','POST','/emails',{})
    for i in range(16): proof.reserve(environment,cfg,'read-'+str(i),'GET','/emails/receiving',None)
    with pytest.raises(WorkflowError,match='allowance'): proof.reserve(environment,cfg,'read-17','GET','/emails/receiving',None)


def test_delivery_evidence_preserves_reply_deduplication(environment,monkeypatch):
    mid=str(uuid4()); pid=str(uuid4()); calls=[]
    detail={'id':mid,'from':proof.DEMO_RECIPIENT,'to':['simeon@'+proof.DOMAIN],
        'subject':'Delivery fixture','message_id':'<delivery@example.test>','text':'Question'}
    def handle(req):
        calls.append(req)
        if req.method=='POST': return httpx.Response(200,json={'id':pid})
        if str(req.url).endswith(mid): return httpx.Response(200,json=detail)
        return httpx.Response(200,json={'id':pid,'from':'simeon@'+proof.DOMAIN,
            'to':[proof.DEMO_RECIPIENT],'last_event':'delivered'})
    monkeypatch.setattr(proof,'HTTP_CLIENT',lambda **kw:httpx.Client(transport=httpx.MockTransport(handle),**kw))
    proof.fetch(mid,'simeon'); proof.reply(mid,'simeon','Reply')
    assert proof.delivery('simeon','delivery-fixture',mid)['delivered']
    assert proof.reply(mid,'simeon','Reply')['id']==pid
    assert sum(req.method=='POST' for req in calls)==1


@pytest.mark.skipif(__import__('os').getenv('CORRESPONDENCE_PG_PROOF')!='1',
    reason='Dedicated demo PostgreSQL proof explicitly disabled')
def test_pg_conversations_and_canonical_followup_with_mock_transport(monkeypatch):
    from app.handoff_demo import configuration, service
    from app.handoff_demo_domain import assert_database
    from app.properties_db import get_connection
    cfg,_=configuration()
    with get_connection(cfg) as conn: assert_database(conn)
    for name,value in {'CORRESPONDENCE_PROOF_APPROVED':'1','DEMO_MODE':'yes','RESEND_API_KEY':'re_offline_fixture',
        'RESEND_RECEIVING_API_KEY':'re_offline_fixture','SABBATH_BLACKOUT':'0','RESEND_RECEIVING_DOMAIN':proof.DOMAIN,
        'DOSSIER_COORDINATOR_EMERGENCY_STOP':'0','MVP_WORKFLOW_ENABLED':'1'}.items(): monkeypatch.setenv(name,value)
    monkeypatch.setattr(proof,'AUTHORIZATION','pg-test-correspondence-'+uuid4().hex)
    monkeypatch.setattr(proof,'RETURN_RUN_KEY','demo-handoff-pg-cold-'+uuid4().hex)
    ids={a:str(uuid4()) for a in proof.ACTORS}; calls=[]
    def handle(req):
        calls.append((req.method,str(req.url)))
        if req.method=='POST': return httpx.Response(200,json={'id':str(uuid4())})
        actor=next(a for a in ids if str(req.url).endswith(ids[a]))
        return httpx.Response(200,json={'id':ids[actor],'from':proof.DEMO_RECIPIENT,
            'to':[actor+'@'+proof.DOMAIN],'subject':'MOCKED PostgreSQL fixture',
            'message_id':'<'+ids[actor]+'@example.test>','text':'Fictional test-only inquiry.'})
    monkeypatch.setattr(proof,'HTTP_CLIENT',lambda **kw:httpx.Client(transport=httpx.MockTransport(handle),**kw))
    proof.fetch(ids['simeon'],'simeon')
    response=proof.reply(ids['simeon'],'simeon','Mocked conditional reply; no delivery.')
    assert proof.reply(ids['simeon'],'simeon','Mocked conditional reply; no delivery.')==response
    proof.solomon_introduction()
    proof.fetch(ids['solomon'],'solomon')
    proof.reply(ids['solomon'],'solomon','Mocked construction reply; not a commitment.')
    returned=proof.cold_return(ids['simeon'])
    assert returned['current_holder']=='uriel' and returned['verified_receipts']>0
    assert proof.cold_return(ids['simeon'])['replayed']
    view=service('inspect',run_key=proof.RETURN_RUN_KEY)
    assert view['phase']=='campaign_rework' and view['current_holder']=='uriel'
    assert view['dossier']['dossier']['agent_reassessments'][-1]['report']['conversation_id']==ids['simeon']
    assert sum(method=='POST' for method,_ in calls)==3
