from copy import deepcopy
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import pytest
from app import communications as router
from app.workflow_db import WorkflowError
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db

NOW=datetime(2026,11,3,18,tzinfo=timezone.utc)


def setup(monkeypatch):
    monkeypatch.setattr(router,'get_package_in_transaction',lambda *a:{'package_state':'prepared','simulated':False,'demo_mode':False})
    conn=Conn(_new_db()); package=str(uuid4()); mid=str(uuid4())
    enrollment={'email_id':str(uuid4())}
    metadata={'from':'owner@example.com','to':['reply@test.resend.app'],'subject':'Question','text':'Please explain the proposal.'}
    return conn,package,mid,enrollment,metadata


def receive(conn,package,mid,enrollment,metadata):
    return router.receive(conn,package_entry=package,provider_message_id=mid,enrollment=enrollment,
        metadata=metadata,authentication='resend_authenticated_api',received_at=NOW)


def action(conn,package,name,revision,**kwargs):
    return router.control(conn,package_entry=package,action=name,request_id=str(uuid4()),expected_revision=revision,
        principal='owner',now=NOW,**kwargs)


def test_inbound_defaults_to_simeon_and_pauses_campaign_with_durable_replay(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    first=receive(conn,package,mid,enrollment,metadata)
    assert first['owner']=='simeon' and first['campaign_status']=='paused'
    before=deepcopy(conn._db)
    assert receive(conn,package,mid,enrollment,metadata)['replayed']
    assert conn._db==before
    view=router.inspect(Conn(conn._db),package)
    assert view['latest_inbound']['text']==metadata['text'] and not view['delivery_enabled']


def test_only_inbound_drafts_and_uriel_is_never_responder(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    initialized=action(conn,package,'initialize',0)
    claimed=action(conn,package,'claim',initialized['revision'])
    with pytest.raises(WorkflowError,match='incoming'):
        action(conn,package,'draft',claimed['revision'],lease_id=claimed['lease']['id'],text='prospecting')
    receive(conn,package,mid,enrollment,metadata)
    with pytest.raises(WorkflowError,match='Uriel'):
        action(conn,package,'claim',router.inspect(conn,package)['revision'],actor='uriel')


def test_only_simeon_workers_can_reclaim_and_old_lease_is_fenced(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    received=receive(conn,package,mid,enrollment,metadata)
    primary=action(conn,package,'claim',received['revision'])
    for actor in ('priscilla','uriel'):
        with pytest.raises(WorkflowError,match='Only Simeon'):
            action(conn,package,'takeover',primary['revision'],actor=actor)
    later=NOW+timedelta(minutes=6)
    replacement=router.control(conn,package_entry=package,action='claim',request_id='replacement',
        expected_revision=primary['revision'],principal='owner',now=later)
    with pytest.raises(WorkflowError,match='lease'):
        router.control(conn,package_entry=package,action='draft',request_id='stale',expected_revision=replacement['revision'],
            principal='owner',lease_id=primary['lease']['id'],text='old draft',now=later)
    router.control(conn,package_entry=package,action='draft',request_id='new',expected_revision=replacement['revision'],
        principal='owner',lease_id=replacement['lease']['id'],text='Draft only.',now=later)
    assert router.inspect(conn,package)['latest_draft']['current_context']
    assert router.inspect(conn,package)['owner']=='simeon'


def test_no_backup_actions_stop_monotonic_and_idempotency(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    result=action(conn,package,'initialize',0)
    for retired in ('takeover','mark_simeon_unavailable','return_to_simeon'):
        with pytest.raises(WorkflowError,match='retired'): action(conn,package,retired,result['revision'])
    args=dict(package_entry=package,action='stop',request_id='stop',expected_revision=result['revision'],principal='owner',now=NOW)
    first=router.control(conn,**args);before=deepcopy(conn._db)
    assert router.control(conn,**args)['replayed'] and conn._db==before
    with pytest.raises(WorkflowError): action(conn,package,'pause',first['revision'])
    receive(conn,package,mid,enrollment,metadata)
    assert router.inspect(conn,package)['campaign_status']=='stopped'


def test_metadata_can_be_enriched_without_duplicate_inbound_or_overwrite(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    receive(conn,package,mid,enrollment,{**metadata,'text':None})
    assert receive(conn,package,mid,enrollment,metadata)['enriched']
    with pytest.raises(WorkflowError,match='differs'):
        receive(conn,package,mid,enrollment,{**metadata,'text':'changed original'})


def test_wondrue_permissions_and_same_operator_approval(monkeypatch):
    from app.wondrue import handle_command,WondrueError,WondrueAuthError
    calls=[]; monkeypatch.setattr(router,'audited',lambda **kw:calls.append(kw) or {'draft_only':True})
    intent={'intent':'control_communications','args':{'package_entry':str(uuid4()),'action':'stop','request_id':'one','expected_revision':0}}
    kwargs=dict(max_mode='full',target='real',text='',principal='owner',intent_override=intent)
    with pytest.raises(WondrueError,match='read-only'): handle_command(mode='read-only',**kwargs)
    preview=handle_command(mode='ask',**kwargs)
    with pytest.raises(WondrueAuthError):
        handle_command(mode='ask',approval_token=preview['approval_token'],**{**kwargs,'principal':'foreign'})
    preview=handle_command(mode='ask',**kwargs)
    handle_command(mode='ask',approval_token=preview['approval_token'],**kwargs)
    assert calls[0]['principal']=='owner' and calls[0]['action']=='stop'


@pytest.mark.skipif(__import__('os').getenv('GROWUP_PG_INTEGRATION')!='1',reason='PostgreSQL integration explicitly disabled')
def test_live_router_concurrent_claims_and_durable_drafts(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tests.test_dcad_admission import _require_gate_database
    from tests.test_priscilla_sim import _live_seed_packages
    from app.properties_db import get_connection
    _require_gate_database()
    monkeypatch.setenv('DEMO_MODE','no')
    monkeypatch.setenv('CUSTODY_HMAC_KEY','communications-pg-custody-0123456789')
    with get_connection() as conn:
        package=str(_live_seed_packages(conn,1)[0]); conn.commit()
    mid=str(uuid4()); enrollment={'email_id':str(uuid4())}
    metadata={'from':'owner@example.com','to':['reply@test.resend.app'],'subject':'Gate fixture','text':'Test-only inquiry'}
    now=datetime.now(timezone.utc)
    with get_connection() as conn:
        received=router.receive(conn,package_entry=package,provider_message_id=mid,enrollment=enrollment,
            metadata=metadata,authentication='resend_authenticated_api',received_at=now)
        conn.commit()
    barrier=Barrier(2)
    def claim():
        barrier.wait(timeout=10)
        try:
            return router.audited(principal='pg-test',package_entry=package,action='claim',
                request_id=str(uuid4()),expected_revision=received['revision'],actor='simeon')
        except WorkflowError: return {'rejected':True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:claim(),range(2)))
    assert sum(bool(r.get('rejected')) for r in results)==1
    won=next(r for r in results if not r.get('rejected'))
    args=dict(principal='pg-test',package_entry=package,action='draft',request_id='pg-draft-'+uuid4().hex,
        expected_revision=won['revision'],actor='simeon',lease_id=won['lease']['id'],text='Draft only, no transmission.')
    router.audited(**args)
    assert router.audited(**args)['replayed']
    with get_connection() as conn:
        view=router.inspect(conn,package)
        assert view['owner']=='simeon' and view['campaign_status']=='paused'
        assert view['latest_draft']['text']==args['text'] and not view['latest_draft']['send_enabled']
        release_revision=view['revision']
    router.audited(principal='pg-test',package_entry=package,action='release',request_id='pg-release-'+uuid4().hex,
        expected_revision=release_revision,lease_id=won['lease']['id'])
    with get_connection() as conn:
        packages=[str(p) for p in _live_seed_packages(conn,4)]
        for package in packages:
            router.receive(conn,package_entry=package,provider_message_id=str(uuid4()),enrollment=enrollment,
                metadata=metadata,authentication='resend_authenticated_api',received_at=datetime.now(timezone.utc))
        conn.commit()
    dispatched=router.dispatch_pending(principal='pg-test',package_entries=packages,request_id='pg-dispatch-'+uuid4().hex[:12])
    assert all(row['status']=='draft_prepared' for row in dispatched['results'])
    assert dispatched['provider_calls']==dispatched['external_actions']==0

    assert 'id' not in view['lease']


def test_bounded_parallel_slots_queue_fifo_and_release(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    received=receive(conn,package,mid,enrollment,metadata)
    claims=[]
    for i in range(router.MAX_SIMEON_WORKERS):
        pkg=package if i==0 else str(uuid4())
        rev=received['revision'] if i==0 else receive(conn,pkg,str(uuid4()),enrollment,metadata)['revision']
        claims.append((pkg,action(conn,pkg,'claim',rev)))
    assert len({c['lease']['worker_id'] for _,c in claims})==4
    extra=str(uuid4());rev=receive(conn,extra,str(uuid4()),enrollment,metadata)['revision']
    with pytest.raises(WorkflowError,match='capacity'): action(conn,extra,'claim',rev)
    pkg,claim=claims[0]
    second_mid=str(uuid4());receive(conn,pkg,second_mid,enrollment,metadata)
    drafted=action(conn,pkg,'draft',router.inspect(conn,pkg)['revision'],lease_id=claim['lease']['id'],text='First message draft')
    assert router.inspect(conn,pkg)['latest_draft']['in_reply_to']==mid
    released=action(conn,pkg,'release',drafted['revision'],lease_id=claim['lease']['id'])
    assert router.inspect(conn,pkg)['pending_message_ids']==[second_mid]
    assert action(conn,extra,'claim',rev)['lease']['worker_id']==claim['lease']['worker_id']


def test_existing_campaign_suppression_is_inherited(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    monkeypatch.setattr(router,'get_package_in_transaction',lambda *a:{'package_state':'prepared','simulated':False,
        'demo_mode':False,'payload':{'campaign':{'status':'suppressed'}}})
    result=action(conn,package,'initialize',0)
    assert result['campaign_status']=='stopped'
    with pytest.raises(WorkflowError): action(conn,package,'resume_campaign',result['revision'])


def test_legacy_backup_is_readable_but_cannot_act_and_pending_queue_stays_empty(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    received=receive(conn,package,mid,enrollment,metadata)
    run,row,state=router.campaign(conn,package)
    state.update(owner='priscilla',lease={'id':'old','actor':'priscilla','expires_at':(NOW+timedelta(minutes=5)).isoformat()})
    router.write(conn,run.run_id,'conversation',state,row)
    view=router.inspect(conn,package)
    assert view['owner']=='simeon' and view['historical_owner']=='priscilla'
    claim=action(conn,package,'claim',received['revision'])
    draft=action(conn,package,'draft',claim['revision'],lease_id=claim['lease']['id'],text='Draft only')
    release=action(conn,package,'release',draft['revision'],lease_id=claim['lease']['id'])
    assert router.inspect(conn,package)['pending_message_ids']==[]
    next_claim=action(conn,package,'claim',release['revision'])
    assert next_claim['lease']['message_id'] is None
