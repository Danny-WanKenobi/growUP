from datetime import datetime
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app import uriel_demo_send as sender
from app.workflow_db import WorkflowError
from tests.test_uriel_campaign_package import campaign
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('DEMO_MODE','yes')
    monkeypatch.setenv('URIEL_DEMO_SEND_APPROVED','1')
    monkeypatch.setenv('URIEL_DEMO_SEND_AUTHORIZATION','owner-demo-test')
    monkeypatch.setenv('RESEND_API_KEY','re_offline_fixture')
    monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')
    monkeypatch.setenv('SABBATH_BLACKOUT','0')
    conn = Conn(_new_db())
    monkeypatch.setattr(sender, 'transaction', lambda cfg, action, mutate: mutate(conn))
    return conn


def send():
    return sender.send_once(campaign(), start_at=datetime.fromisoformat('2026-11-04T10:00:00-06:00'),
        timezone_name='America/Chicago')


def test_exact_transport_and_durable_replay_never_sends_again(configured, monkeypatch):
    calls=[]
    pid=str(uuid4())
    def handle(request):
        import json
        body=json.loads(request.content)
        assert request.url == 'https://api.resend.com/emails'
        assert body['to'] == ['werenotrandom@protonmail.com']
        assert body['from'] == 'uriel@growup.securecodeusa.com'
        assert body['reply_to'] == 'simeon@growup.securecodeusa.com'
        assert not set(body) & {'cc','bcc','scheduled_at'}
        assert request.headers['Idempotency-Key'] == 'growup-demo/owner-demo-test'
        assert body['attachments'][0]['filename'].endswith('.zip')
        calls.append(request)
        return httpx.Response(200,json={'id':pid})
    monkeypatch.setattr(sender,'HTTP_CLIENT',lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handle),**kwargs))
    result=send()
    assert result['provider_id'] == pid and not result['delivered']
    assert send()['replayed'] and len(calls)==1


@pytest.mark.parametrize('failure',['timeout','rejected','bad_json'])
def test_ambiguous_or_refused_attempt_consumes_approval(configured,monkeypatch,failure):
    calls=[]
    def handle(request):
        calls.append(request)
        if failure=='timeout': raise httpx.ReadTimeout('offline fixture')
        return httpx.Response(403 if failure=='rejected' else 200, content=b'not-json')
    monkeypatch.setattr(sender,'HTTP_CLIENT',lambda **kw: httpx.Client(transport=httpx.MockTransport(handle),**kw))
    with pytest.raises(WorkflowError,match='consumed'): send()
    assert send()['new_provider_requests']==0 and len(calls)==1


def test_committed_claim_without_http_blocks_restart_and_changed_material(configured):
    config=sender.configuration()
    first=sender.claim_in_transaction(configured,config,{'body_digest':'a','campaign_id':'campaign:test'})
    before=deepcopy(configured._db)
    assert not sender.claim_in_transaction(configured,config,{'body_digest':'a','campaign_id':'campaign:test'})[1]
    assert configured._db==before
    with pytest.raises(WorkflowError,match='another package'):
        sender.claim_in_transaction(configured,config,{'body_digest':'b','campaign_id':'campaign:test'})
    with pytest.raises(WorkflowError,match='another authorization'):
        sender.claim_in_transaction(configured,{**config,'authorization':'another'},
            {'body_digest':'a','campaign_id':'campaign:test'})


@pytest.mark.parametrize('name,value',[('DEMO_MODE','no'),('URIEL_DEMO_SEND_APPROVED','0'),
    ('RESEND_API_KEY',''),('DOSSIER_COORDINATOR_EMERGENCY_STOP','1')])
def test_disabled_or_invalid_configuration_never_claims(configured,monkeypatch,name,value):
    before=deepcopy(configured._db)
    monkeypatch.setenv(name,value)
    with pytest.raises(WorkflowError): send()
    assert configured._db==before


@pytest.mark.skipif(__import__('os').getenv('URIEL_DEMO_PG_PROOF') != '1',
    reason='Isolated demo PostgreSQL proof explicitly disabled')
def test_pg_claim_commits_before_transport_and_blocks_concurrent_replay(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.handoff_demo import configuration as demo_configuration
    from app.handoff_demo_domain import assert_database
    from app.properties_db import get_connection
    from app.workflow_db import get_run_by_key_in_transaction
    cfg,_ = demo_configuration()
    with get_connection(cfg) as conn: assert_database(conn)
    authorization='pg-test-demo-send-'+uuid4().hex
    monkeypatch.setenv('DEMO_MODE','yes')
    monkeypatch.setenv('URIEL_DEMO_SEND_APPROVED','1')
    monkeypatch.setenv('URIEL_DEMO_SEND_AUTHORIZATION',authorization)
    monkeypatch.setenv('RESEND_API_KEY','re_offline_pg_fixture')
    monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')
    monkeypatch.setenv('SABBATH_BLACKOUT','0')
    source=campaign()
    # Unique fixture identity avoids colliding with permanent prior test claims.
    source['pg_test_fixture']=authorization
    source['campaign_id']='campaign:'+__import__('app.workflow',fromlist=['digest_payload']).digest_payload(
        {k:v for k,v in source.items() if k!='campaign_id'})
    calls=[]
    def handle(request):
        # A fresh connection sees the claim before any mocked request accepts it.
        with get_connection(cfg) as conn:
            run=get_run_by_key_in_transaction(conn,'uriel-demo-send:'+authorization)
            state=sender.read(conn,run.run_id,'delivery').payload
            assert state['status']=='claimed_before_transmission' and state['requests_consumed']==1
        calls.append(request)
        return httpx.Response(200,json={'id':str(uuid4())})
    monkeypatch.setattr(sender,'HTTP_CLIENT',lambda **kw:httpx.Client(transport=httpx.MockTransport(handle),**kw))
    barrier=Barrier(2)
    def attempt():
        barrier.wait(timeout=10)
        return sender.send_once(source,start_at=datetime.fromisoformat('2026-11-05T10:00:00-06:00'),timezone_name='America/Chicago')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:attempt(),range(2)))
    assert len(calls)==1 and sum(r['new_provider_requests'] for r in results)==1
    assert sender.send_once(source,start_at=datetime.fromisoformat('2026-11-05T10:00:00-06:00'),
        timezone_name='America/Chicago')['new_provider_requests']==0
