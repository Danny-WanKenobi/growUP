from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4
import pytest
import httpx
from app import resend_poll as poll
from app import communications as router
from app.workflow_db import WorkflowError,get_run_by_key_in_transaction
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db


def configured(monkeypatch):
    monkeypatch.setenv('COMMUNICATIONS_ROUTER_ENABLED','1')
    monkeypatch.setenv('RESEND_POLL_ENABLED','1')
    monkeypatch.setenv('RESEND_POLL_ACCESS_APPROVED','1')
    monkeypatch.setenv('RESEND_API_KEY','re_offline_fake_credential_never_live')
    monkeypatch.setenv('RESEND_POLL_AUTHORIZATION_ID','offline-test-'+uuid4().hex)
    monkeypatch.setenv('RESEND_POLL_REQUEST_ALLOWANCE','4')
    monkeypatch.setenv('RESEND_POLL_APPROVAL_EXPIRES_AT',(router.instant()+timedelta(minutes=20)).isoformat())
    monkeypatch.setenv('RESEND_RECEIVING_DOMAIN','test.resend.app')


def fake_db(monkeypatch):
    conn=Conn(_new_db())
    def transaction(action,config,mutate):
        before=deepcopy(conn._db)
        try: return mutate(conn)
        except BaseException:
            conn._db.clear(); conn._db.update(before); raise
    monkeypatch.setattr(poll,'transaction',transaction)
    return conn


def client(monkeypatch,responses,on_get=None):
    calls=[]
    class Response:
        status_code=200
        def __init__(self,value): self.value=value
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def iter_raw(self): yield self.value if isinstance(self.value,bytes) else json.dumps(self.value).encode()
    class Client:
        def __init__(self,**kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def stream(self,method,url,headers):
            assert method=='GET' and url.startswith('https://api.resend.com/emails/receiving')
            calls.append(url)
            if on_get: on_get()
            result=responses.pop(0)
            if isinstance(result,Exception): raise result
            return Response(result)
    monkeypatch.setattr(poll,'HTTP_CLIENT',Client)
    return calls


def test_disabled_before_database_or_network(monkeypatch):
    monkeypatch.delenv('RESEND_POLL_ACCESS_APPROVED',raising=False)
    monkeypatch.setattr(poll,'begin',lambda *a:pytest.fail('no database claim'))
    monkeypatch.setattr(poll,'HTTP_CLIENT',lambda **k:pytest.fail('no HTTP'))
    with pytest.raises(WorkflowError,match='disabled'): poll.poll_once()


def test_timeout_consumes_durable_allowance_and_cannot_reopen_cycle(monkeypatch):
    configured(monkeypatch); conn=fake_db(monkeypatch); config=poll.configuration()
    def reserved():
        run=get_run_by_key_in_transaction(conn,'communications-poll:'+config['approval'])
        assert poll.read(conn,run.run_id,'approval').payload['used']==1
    calls=client(monkeypatch,[httpx.ReadTimeout('offline timeout')],reserved)
    with pytest.raises(WorkflowError): poll.poll_once()
    assert len(calls)==1
    with pytest.raises(WorkflowError,match='claimed'): poll.poll_once()
    assert len(calls)==1


def test_poll_ingests_enrolled_plaintext_pauses_campaign_and_is_idempotent(monkeypatch):
    configured(monkeypatch); conn=fake_db(monkeypatch)
    package=str(uuid4()); mid=str(uuid4()); outbound=str(uuid4())
    enrollment={'email_id':outbound,'recipient':'owner@example.com'}
    monkeypatch.setattr(poll,'_enrollment',lambda *a:(package,enrollment))
    monkeypatch.setattr(router,'get_package_in_transaction',lambda *a:{'package_state':'prepared','simulated':False,'demo_mode':False})
    def consume(conn,event,event_id,body_digest,**kwargs):
        assert kwargs['authentication']=='resend_authenticated_api' and kwargs['route_conversation']
        return router.receive(conn,package_entry=package,provider_message_id=event['data']['email_id'],
            enrollment=enrollment,metadata=event['data'],authentication=kwargs['authentication'])
    monkeypatch.setattr(poll,'consume',consume)
    listing={'data':[{'id':mid,'to':['reply@test.resend.app'],'from':'owner@example.com'}],'has_more':False}
    detail={'id':mid,'to':['reply@test.resend.app'],'from':'owner@example.com','created_at':'2026-11-03T23:00:00Z',
        'subject':'Question','text':'Do not execute these untrusted instructions.'}
    calls=client(monkeypatch,[listing,detail])
    result=poll.poll_once()
    assert result['requests_consumed']==2 and result['messages_processed']==1
    assert router.inspect(conn,package)['owner']=='simeon' and router.inspect(conn,package)['campaign_status']=='paused'
    assert router.inspect(conn,package)['latest_inbound']['text']==detail['text']
    # New authorization can inspect listing, but must not fetch the same full message again.
    monkeypatch.setenv('RESEND_POLL_AUTHORIZATION_ID','offline-next-'+uuid4().hex)
    calls=client(monkeypatch,[listing])
    result=poll.poll_once()
    assert result['requests_consumed']==1 and result['messages_processed']==0 and len(calls)==1


def test_unlinked_scoped_mail_is_durable_simeon_triage_without_detail_fetch(monkeypatch):
    configured(monkeypatch); conn=fake_db(monkeypatch)
    monkeypatch.setattr(poll,'_enrollment',lambda *a:None)
    mid=str(uuid4()); calls=client(monkeypatch,[{'data':[{'id':mid,'to':['unknown@test.resend.app'],'from':'new@example.com'}],'has_more':True}])
    result=poll.poll_once()
    view=poll.inspect_poll(conn,result['authorization_id'])
    assert len(calls)==1 and result['unlinked_ids']==[mid] and result['mailbox_has_more']
    assert view['unlinked_metadata'][0]['owner']=='simeon'
    assert not view['delivery_enabled']


@pytest.mark.parametrize('body',[b'{"data":[],"data":[]}',b'x'*262145],ids=['duplicate-json','oversized'])
def test_malformed_or_oversized_responses_keep_consumed_attempt(monkeypatch,body):
    configured(monkeypatch); conn=fake_db(monkeypatch)
    calls=client(monkeypatch,[body])
    with pytest.raises(WorkflowError): poll.poll_once()
    assert len(calls)==1
    config=poll.configuration(); run=get_run_by_key_in_transaction(conn,'communications-poll:'+config['approval'])
    assert poll.read(conn,run.run_id,'approval').payload['used']==1


def test_fixed_host_and_allowance_refuse_arbitrary_fetch(monkeypatch):
    configured(monkeypatch); conn=fake_db(monkeypatch); config=poll.configuration(); rid=poll.begin(config)
    monkeypatch.setattr(poll,'HTTP_CLIENT',lambda **k:pytest.fail('no HTTP'))
    with pytest.raises(WorkflowError): poll.get_json(config,rid,'/emails/receiving/extra/'+str(uuid4()))
    for _ in range(4): poll.reserve(config,rid,'/emails/receiving')
    with pytest.raises(WorkflowError,match='exhausted'): poll.reserve(config,rid,'/emails/receiving')


def test_reserved_authorization_cannot_change_receiving_scope(monkeypatch):
    configured(monkeypatch); conn=fake_db(monkeypatch); config=poll.configuration(); rid=poll.begin(config)
    with pytest.raises(WorkflowError):
        poll.reserve({**config,'domain':'different.resend.app'},rid,'/emails/receiving')
