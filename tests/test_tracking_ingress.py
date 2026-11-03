import hashlib
import hmac
import json
import time
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.routes import tracking_ingress as ingress
from app.contact_evidence import assess_contact

SECRET='test-gateway-secret-not-a-real-credential-1234'


def configured(monkeypatch):
    package=str(uuid4())
    monkeypatch.setenv('TRACKING_INGRESS_ENABLED','1')
    monkeypatch.setenv('TRACKING_INGRESS_KEY_TEST',SECRET)
    monkeypatch.setenv('TRACKING_INGRESS_SOURCES',json.dumps({'test':{'secret_env':'TRACKING_INGRESS_KEY_TEST','allowed_packages':[package]}}))
    data={'package_entry':package,'provider_event_id':'event-1','message_id':'message-1','status':'delivered',
        'occurred_at':'2026-11-03T10:00:00Z','evidence_refs':['https://provider.example/receipt/1']}
    return data


def signed(body,offset=0,source='test'):
    timestamp=str(int(time.time())+offset)
    signature=hmac.new(SECRET.encode(),source.encode()+b'.'+timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    return {'X-Tracking-Timestamp':timestamp,'X-Tracking-Signature':signature}


def test_gateway_valid_signature_uses_fixed_source_identity(monkeypatch):
    data=configured(monkeypatch); calls=[]
    monkeypatch.setattr(ingress,'ingest_tracking_audited',lambda **kw:calls.append(kw) or {'event_id':'test-event','replayed':True})
    body=json.dumps(data).encode()
    with TestClient(app) as client:
        response=client.post('/integrations/tracking/test',content=body,headers=signed(body))
    assert response.status_code==200 and response.json()['replayed']
    assert calls[0]['provider']=='gateway:test' and calls[0]['principal']=='tracking-gateway:test'
    assert calls[0]['authentication']=='configured_gateway_hmac_sha256'
    assert SECRET not in response.text


@pytest.mark.parametrize('failure',['stale','tampered','scope','simulated','extra','duplicate','oversized','wrong_source','reused_key'])
def test_gateway_refuses_before_database_dispatch(monkeypatch,failure):
    data=configured(monkeypatch)
    monkeypatch.setattr(ingress,'ingest_tracking_audited',lambda **kw:pytest.fail('must not dispatch'))
    if failure=='scope': data['package_entry']=str(uuid4())
    if failure=='simulated': data['simulated']=True
    if failure=='extra': data['provider']='forged'
    body=json.dumps(data).encode()
    if failure=='duplicate': body=body[:-1]+b',"status":"replied"}'
    if failure=='oversized': body=b'x'*(ingress.MAX_BODY+1)
    if failure=='reused_key': monkeypatch.setenv('WONDRUE_OPERATOR_KEY',SECRET)
    headers=signed(body,-600 if failure=='stale' else 0,source='other' if failure=='wrong_source' else 'test')
    if failure=='tampered': body=body.replace(b'delivered',b'bounced')
    with TestClient(app) as client:
        response=client.post('/integrations/tracking/test',content=body,headers=headers)
    assert response.status_code in (400,401,403,413)


def test_gateway_disabled_by_default(monkeypatch):
    monkeypatch.delenv('TRACKING_INGRESS_ENABLED',raising=False)
    with TestClient(app) as client:
        assert client.post('/integrations/tracking/test',content=b'{}').status_code==404


def contact(prop):
    return {'value':{'name':'Provided Owner','email':'owner@example.com','property_id':prop,'relationship':'owner'},
        'evidence_refs':['https://records.example/property/1']}


def test_contact_syntax_is_not_ownership_authority():
    prop=str(uuid4()); result=assess_contact(contact(prop),property_id=prop)
    assert result['eligible_for_draft'] and not result['ownership_verified'] and not result['channel_authorized']
    assert result['status']=='provided_unverified'


@pytest.mark.parametrize('failure',['other_property','header_injection','multiple','unsupported_assertion','credentials'])
def test_contact_rejects_unsafe_or_unbound_material(failure):
    prop=str(uuid4()); entry=contact(prop)
    if failure=='other_property': entry['value']['property_id']=str(uuid4())
    if failure=='header_injection': entry['value']['email']='owner@example.com\r\nBcc: another@example.com'
    if failure=='multiple': entry['value']['email']='one@example.com,two@example.com'
    if failure=='unsupported_assertion': entry['value']['verified']=True
    if failure=='credentials': entry['evidence_refs']=['https://user:password@records.example/1']
    assert not assess_contact(entry,property_id=prop)['eligible_for_draft']

@pytest.mark.skipif(__import__('os').getenv('GROWUP_PG_INTEGRATION')!='1',reason='PostgreSQL integration explicitly disabled')
def test_live_signed_ingress_replay_and_authentication_scope(monkeypatch):
    from tests.test_dcad_admission import _require_gate_database
    from tests.test_priscilla_sim import _live_seed_packages
    from app.properties_db import get_connection
    from app.priscilla import timeline_in_transaction
    _require_gate_database()
    monkeypatch.setenv('CUSTODY_HMAC_KEY','ingress-pg-test-key-0123456789')
    with get_connection() as conn:
        package=_live_seed_packages(conn,1)[0]; conn.commit()
    data=configured(monkeypatch)
    data.update(package_entry=str(package),provider_event_id=str(uuid4()),message_id=str(uuid4()))
    monkeypatch.setenv('TRACKING_INGRESS_SOURCES',json.dumps({'test':{'secret_env':'TRACKING_INGRESS_KEY_TEST','allowed_packages':[str(package)]}}))
    body=json.dumps(data).encode()
    with TestClient(app) as client:
        first=client.post('/integrations/tracking/test',content=body,headers=signed(body))
        assert first.status_code==200, first.text
        second=client.post('/integrations/tracking/test',content=body,headers=signed(body))
        assert second.status_code==200 and second.json()['replayed']
        assert first.json()['event_id']==second.json()['event_id']
    with get_connection() as conn:
        timeline=timeline_in_transaction(conn,package_entry=package)
    assert timeline['actual']['message_status_counts']['delivered']==1
    assert timeline['events'][0]['authentication']=='configured_gateway_hmac_sha256'
