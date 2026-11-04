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


def test_backup_takeover_fences_previous_owner_and_drafts_stay_local(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    received=receive(conn,package,mid,enrollment,metadata)
    primary=action(conn,package,'claim',received['revision'])
    with pytest.raises(WorkflowError,match='active'):
        action(conn,package,'takeover',primary['revision'],actor='priscilla')
    later=NOW+timedelta(minutes=6)
    backup=router.control(conn,package_entry=package,action='takeover',request_id='backup',
        expected_revision=primary['revision'],principal='owner',actor='priscilla',now=later)
    with pytest.raises(WorkflowError,match='lease'):
        router.control(conn,package_entry=package,action='draft',request_id='stale',expected_revision=backup['revision'],
            principal='owner',actor='simeon',lease_id=primary['lease']['id'],text='old draft',now=later)
    result=router.control(conn,package_entry=package,action='draft',request_id='draft',expected_revision=backup['revision'],
        principal='owner',actor='priscilla',lease_id=backup['lease']['id'],text='Draft acknowledgement only.',now=later)
    view=router.inspect(conn,package)
    assert view['latest_draft']['status']=='draft_only' and not view['latest_draft']['send_enabled']
    assert view['latest_draft']['current_context']
    changed={**metadata,'text':'Another question'}
    receive(conn,package,str(uuid4()),enrollment,changed)
    assert not router.inspect(conn,package)['latest_draft']['current_context']


def test_unavailable_takeover_stop_monotonic_and_command_idempotency(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    result=action(conn,package,'initialize',0)
    with pytest.raises(WorkflowError,match='takeover'):
        action(conn,package,'takeover',result['revision'],actor='priscilla')
    result=action(conn,package,'mark_simeon_unavailable',result['revision'])
    result=action(conn,package,'takeover',result['revision'],actor='priscilla')
    args=dict(package_entry=package,action='stop',request_id='stop',expected_revision=result['revision'],principal='owner',now=NOW)
    first=router.control(conn,**args); before=deepcopy(conn._db)
    assert router.control(conn,**args)['replayed'] and conn._db==before
    with pytest.raises(WorkflowError): action(conn,package,'pause',first['revision'])
    receive(conn,package,mid,enrollment,metadata)
    assert router.inspect(conn,package)['campaign_status']=='stopped'
    with pytest.raises(WorkflowError): action(conn,package,'resume_campaign',router.inspect(conn,package)['revision'])


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
        assert 'id' not in view['lease']


def test_backup_can_take_unclaimed_inbound_after_bounded_wait(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    received=receive(conn,package,mid,enrollment,metadata)
    result=router.control(conn,package_entry=package,action='takeover',request_id='unclaimed-timeout',
        expected_revision=received['revision'],principal='owner',actor='priscilla',now=NOW+timedelta(minutes=6))
    assert result['owner']=='priscilla' and result['epoch']==1


def test_existing_campaign_suppression_is_inherited(monkeypatch):
    conn,package,mid,enrollment,metadata=setup(monkeypatch)
    monkeypatch.setattr(router,'get_package_in_transaction',lambda *a:{'package_state':'prepared','simulated':False,
        'demo_mode':False,'payload':{'campaign':{'status':'suppressed'}}})
    result=action(conn,package,'initialize',0)
    assert result['campaign_status']=='stopped'
    with pytest.raises(WorkflowError): action(conn,package,'resume_campaign',result['revision'])
