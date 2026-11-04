from datetime import datetime,timedelta,timezone
from uuid import uuid4
from copy import deepcopy
import importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import customer_service as csr
from app.public_faq import FAQS
from app.routes import public
from app.main import app
from app.workflow_db import WorkflowError
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db


@pytest.fixture
def store(monkeypatch):
    conn=Conn(_new_db())
    monkeypatch.setattr(csr,'transaction',lambda action,mutate:mutate(conn))
    monkeypatch.setenv('CSR_PUBLIC_ENABLED','1');monkeypatch.setenv('SERVER_MODE','LIVE_SERVER')
    monkeypatch.setenv('SABBATH_BLACKOUT','0')
    public._rates.clear()
    return conn


def test_durable_pool_queues_and_reclaims_without_duplicate_completion(store):
    jobs=[]
    # Distinct conversations may run concurrently; one conversation is ordered.
    for index in range(5):
        identity='test-session-'+str(index);csr.session(store,identity,True)
        rid,mid=csr.enqueue(store,identity,str(uuid4()),'Does Grow Up guarantee a profitable conversion?')
        jobs.append((rid,mid,csr.claim(store,rid,mid)))
    assert len({j[2].get('worker') for j in jobs[:4]})==4 and jobs[4][2]['queued']
    rid,mid,lease=jobs[0]
    result=csr.complete(store,rid,mid,lease['token'],{'text':'No guaranteed profit.','escalate':None})
    assert csr.complete(store,rid,mid,lease['token'],{'text':'changed'})==result
    assert not csr.claim(store,jobs[4][0],jobs[4][1]).get('queued')
    rid,mid,old=jobs[1]
    reclaimed=csr.claim(store,rid,mid,datetime.now(timezone.utc)+timedelta(minutes=3))
    assert reclaimed['token']!=old['token']
    with pytest.raises(WorkflowError,match='expired'):csr.complete(store,rid,mid,old['token'],{})


def test_message_replay_and_appointment_conflict(store):
    csr.session(store,'guest',True);mid=str(uuid4())
    first=csr.respond('guest',mid,'What is Grow Up?')
    assert first['status']=='complete' and first['faq']==1 and first['escalate'] is None
    assert csr.respond('guest',mid,'What is Grow Up?')==first
    with pytest.raises(WorkflowError,match='conflict'):csr.respond('guest',mid,'different')
    when=datetime.now(timezone.utc).replace(hour=16,minute=0,second=0,microsecond=0)+timedelta(days=2)
    while when.weekday()>4:when+=timedelta(days=1)
    args=('guest',str(uuid4()),'simeon',when.isoformat(),'visitor@example.com')
    appointment=csr.reserve_appointment(store,*args)
    assert appointment['status']=='tentative_reserved' and not appointment['external_invitation_sent']
    assert csr.reserve_appointment(store,*args)==appointment
    csr.session(store,'other',True)
    with pytest.raises(WorkflowError,match='already reserved'):
        csr.reserve_appointment(store,'other',str(uuid4()),'simeon',when.isoformat(),'other@example.com')


def test_public_page_csr_origin_and_cookie_isolation(store):
    with TestClient(app,base_url='https://growup.securecodeusa.com') as client:
        page=client.get('/')
        assert page.status_code==200 and page.text.count('class="faq-number"')==15
        assert 'help-drawer' in page.text and 'login-drawer' in page.text
        assert client.get('/public-assets/unknown').status_code==404
        assert client.post('/support/start',headers={'Origin':'https://evil.example'}).status_code==403
        origin={'Origin':'https://growup.securecodeusa.com'}
        assert client.post('/support/start',headers=origin,json={}).status_code==200
        body={'request_id':str(uuid4()),'text':'Construction contractor question'}
        response=client.post('/support/message',headers=origin,json=body)
        assert response.status_code==200 and response.json()['conversation_owner']=='solomon'
        assert response.json()['handoff']['to']=='solomon' and response.json()['escalate'] is None
        token=client.cookies.get(public.COOKIE)
        with TestClient(app,base_url='https://growup.securecodeusa.com') as stranger:
            assert stranger.post('/support/message',headers=origin,json=body).status_code==401
        assert client.post('/wondrue/command',json={}).status_code in (401,422,503)


def test_login_requires_bridge_and_valid_session_signature(store,monkeypatch):
    monkeypatch.setenv('PUBLIC_LOGIN_BRIDGE_SECRET','b'*48)
    monkeypatch.setenv('PUBLIC_SESSION_SECRET','s'*48)
    origin={'Origin':'https://growup.securecodeusa.com'}
    with TestClient(app,base_url='https://growup.securecodeusa.com') as client:
        assert client.post('/owner-auth',headers=origin,json={}).status_code==401
        headers={**origin,'X-Growup-Authenticated-User':'existing-owner','X-Growup-Login-Bridge':'b'*48}
        assert client.post('/owner-auth',headers=headers,json={}).status_code==200
        assert client.get('/public/session-check').status_code==204
        client.cookies.clear();client.cookies.set(public.OWNER_COOKIE,'forged')
        assert client.get('/public/session-check').status_code==401


def test_nginx_public_routes_preserve_private_gateway_and_callback():
    spec=importlib.util.spec_from_file_location('public_site',Path('deployment/configure-public-site.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    text='server {\n    auth_basic "Grow Up - private preview";\n    auth_basic_user_file /etc/nginx/growup.htpasswd;\n    location = /integrations/resend/webhook {\n        auth_basic off;\n    }\n    location / { proxy_pass http://127.0.0.1:8010; }\n}\n'
    changed=module.revised(text)
    assert module.revised(changed)==changed
    assert 'auth_basic "Grow Up - private preview";' in changed
    assert 'auth_request /_growup_session;' in changed
    assert 'location = /owner-auth' in changed and 'internal;' in changed
    assert 'location = /integrations/resend/webhook {\n        auth_basic off;\n        auth_request off;' in changed
    assert 'listen 8080;' in changed
    assert 'https://growup.securecodeusa.com:8443$request_uri' in changed
    assert 'listen 8443 ssl;' in module.development_route()
    assert '127.0.0.1:8001' in module.development_route()


@pytest.mark.skipif(__import__('os').getenv('GROWUP_PG_INTEGRATION')!='1',reason='Dedicated PostgreSQL proof explicitly disabled')
def test_pg_csr_worker_contention_persistence_and_appointments(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_dcad_admission import _require_gate_database
    from app.properties_db import get_connection
    _require_gate_database();monkeypatch.setenv('SABBATH_BLACKOUT','0')
    identity='pg-csr-'+uuid4().hex
    csr.transaction('test_session',lambda conn:csr.session(conn,identity,True))
    identities=[identity+'-'+str(i) for i in range(5)]
    for who in identities:csr.transaction('test_session',lambda conn,who=who:csr.session(conn,who,True))
    jobs=[csr.transaction('test_enqueue',lambda conn,who=who:csr.enqueue(conn,who,str(uuid4()),'What is Grow Up?')) for who in identities]
    def claim(job):return csr.transaction('test_claim',lambda conn:csr.claim(conn,*job))
    with ThreadPoolExecutor(max_workers=5) as pool:leases=list(pool.map(claim,jobs))
    assert sum('worker' in lease for lease in leases)==4
    assert len({lease['worker'] for lease in leases if 'worker' in lease})==4
    index=next(i for i,lease in enumerate(leases) if 'worker' in lease)
    result=csr.transaction('test_complete',lambda conn:csr.complete(conn,*jobs[index],leases[index]['token'],
        {'text':'PostgreSQL support test','escalate':'solomon'}))
    with get_connection() as conn:
        assert csr.claim(conn,*jobs[index])['complete']==result
    waiting=next(i for i,lease in enumerate(leases) if lease.get('queued'))
    assert claim(jobs[waiting])['worker']==leases[index]['worker']
    for i,lease in enumerate(leases):
        if i!=index and 'worker' in lease:
            csr.transaction('test_release',lambda conn,i=i,lease=lease:csr.complete(conn,*jobs[i],lease['token'],{'text':'Done','escalate':None}))
    # Finish the newly reclaimed fifth job as well.
    with get_connection() as conn:
        pool_run=__import__('app.workflow_db',fromlist=['get_run_by_key_in_transaction']).get_run_by_key_in_transaction(conn,'csr-worker-pool')
        slots=csr.read(conn,pool_run.run_id,'workers').payload['slots']
        token=next(v['token'] for v in slots.values() if v['request_id']==jobs[waiting][1])
    csr.transaction('test_release',lambda conn:csr.complete(conn,*jobs[waiting],token,{'text':'Done','escalate':None}))
    request_id=str(uuid4())
    appointment=None
    for day in range(20,50):
        when=datetime.now(timezone.utc).replace(hour=16,minute=0,second=0,microsecond=0)+timedelta(days=day)
        if when.weekday()>4:continue
        try:
            appointment=csr.transaction('test_appointment',lambda conn:csr.reserve_appointment(conn,identity,request_id,'simeon',when.isoformat(),'test@example.test'))
            break
        except WorkflowError as exc:
            if 'already reserved' not in str(exc):raise
    assert appointment is not None
    assert appointment['status']=='tentative_reserved'
    assert csr.transaction('test_appointment',lambda conn:csr.reserve_appointment(conn,identity,request_id,'simeon',when.isoformat(),'test@example.test'))==appointment
    # Exercise the actual parameterized queue-recovery query, not only job helpers.
    csr.transaction('test_queued',lambda conn:csr.enqueue(conn,identity,str(uuid4()),'What is Grow Up?'))
    assert csr.drain_web()['inspected']>=1
