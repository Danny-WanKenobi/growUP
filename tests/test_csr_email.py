from uuid import uuid4
import pytest
from app import csr_email,mailbox_routing,customer_service as csr
from app.workflow_db import get_run_by_key_in_transaction,WorkflowError
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db


@pytest.fixture
def store(monkeypatch):
    conn=Conn(_new_db());monkeypatch.setenv('RESEND_RECEIVING_DOMAIN','growup.securecodeusa.com')
    monkeypatch.setenv('DEMO_MODE','yes')
    monkeypatch.setattr(csr,'transaction',lambda action,mutate:mutate(conn))
    return conn


def detail():
    return {'id':str(uuid4()),'from':'visitor@example.com','to':['csr@growup.securecodeusa.com'],
        'subject':'Question','text':'Does Grow Up guarantee a profitable conversion?',
        'message_id':'<test@example.com>','headers':{'auto-submitted':'no'},'authentication':{'dmarc':'pass'}}


def test_inbound_csr_process_retains_draft_and_never_sends_in_demo(store,monkeypatch):
    source=detail()
    source['text']='Hello, CSR!!!\n\nSent with [Proton Mail](https://proton.me/mail/home) secure email.'
    result=mailbox_routing.receive(store,metadata={**source,'email_id':source['id']},event_id='test',body_digest='x',authentication='resend_authenticated_api')
    assert result['mailbox']=='csr'
    csr_email.enqueue(store,{**source,'email_id':source['id']})
    run=get_run_by_key_in_transaction(store,'direct-mail:'+source['id'])
    calls=[]
    def fake(rid,mid,phase,body=None):
        calls.append(phase)
        assert phase=='fetch'
        return source
    monkeypatch.setattr(csr_email,'http',fake)
    assert csr_email.process(run.run_id)['status']=='draft_only'
    assert 'Hello!' in csr.read(store,run.run_id,'csr-reply-draft').payload['text']
    assert csr_email.process(run.run_id)['status']=='already_complete_or_leased'
    assert calls==['fetch']


def test_auto_reply_suppression_and_html_sanitizing(store):
    source=detail();assert csr_email.eligible(source)
    for changes in ({'headers':{'auto-submitted':'auto-replied'}},{'headers':{'list-id':'marketing'}},
                    {'from':'csr@growup.securecodeusa.com'},{'authentication':None}):
        assert not csr_email.eligible({**source,**changes})
    assert csr_email.plain({'text':None,'html':'<script>danger</script><p>Hello</p>'}).strip()=='Hello'
    from app.public_faq import answer
    assert answer('What is Grow Up?\n\nSent with Proton Mail secure email.')['faq']==1


def test_send_claim_is_durable_and_sender_cooldown_prevents_loops(store):
    from app.workflow_db import create_run_in_transaction
    one=create_run_in_transaction(store,idempotency_key='csr-test-one')
    body={'to':['visitor@example.com']}
    assert csr_email.reserve(store,one.run_id,'send',body)[0]
    assert not csr_email.reserve(store,one.run_id,'send',body)[0]
    two=create_run_in_transaction(store,idempotency_key='csr-test-two')
    with pytest.raises(WorkflowError,match='cooldown'):csr_email.reserve(store,two.run_id,'send',body)


def test_live_reply_transport_is_bound_to_inbound_and_not_repeated(store,monkeypatch):
    import httpx,json
    for key,value in {'DEMO_MODE':'no','CSR_EMAIL_ENABLED':'1','CSR_EMAIL_DELIVERY_APPROVED':'1',
        'RESEND_API_KEY':'re_test','RESEND_RECEIVING_API_KEY':'re_test','SABBATH_BLACKOUT':'0'}.items():monkeypatch.setenv(key,value)
    source=detail();calls=[]
    mailbox_routing.receive(store,metadata={**source,'email_id':source['id']},event_id='fixture',body_digest='x',authentication='resend_authenticated_api')
    csr_email.enqueue(store,{**source,'email_id':source['id']})
    run=get_run_by_key_in_transaction(store,'direct-mail:'+source['id'])
    def handle(request):
        calls.append(request.method)
        if request.method=='GET':return httpx.Response(200,json=source)
        body=json.loads(request.content)
        assert body['to']==[source['from']] and body['from']=='csr@growup.securecodeusa.com'
        assert body['headers']['In-Reply-To']==source['message_id']
        assert body['headers']['Auto-Submitted']=='auto-replied'
        return httpx.Response(200,json={'id':str(uuid4())})
    monkeypatch.setattr(csr_email,'HTTP_CLIENT',lambda **kw:httpx.Client(transport=httpx.MockTransport(handle),**kw))
    assert csr_email.process(run.run_id)['status']=='provider_accepted'
    assert csr_email.process(run.run_id)['status']=='already_complete_or_leased'
    assert calls==['GET','POST']
