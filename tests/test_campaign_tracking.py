from uuid import uuid4
import pytest
from app.priscilla import ingest_tracking_in_transaction, timeline_in_transaction, PriscillaError
from tests.test_priscilla import _FakeConn, _package
from app.uriel import prepare_campaign, UrielError
from app import handoff_demo as demo
from app.handoff_demo_domain import scope
from tests.test_handoff_demo import Conn, KEY, drive
from tests.test_funnel_acceptance import _new_db


def inputs(package):
    return dict(package_entry=package,provider='Owner import',provider_event_id='evt-1',message_id='msg-1',
        status='replied',occurred_at='2026-11-03T10:00:00Z',evidence_refs=['owner-provided:reply-1'])


def test_event_replay_conflict_and_message_dedup():
    package=str(uuid4()); data=inputs(package)
    result=ingest_tracking_in_transaction(_FakeConn([None,None,_package(),{'created_at':'now'}]),**data)
    existing={'event_id':result['event_id'],'package_entry':package,'payload':result['payload']}
    assert ingest_tracking_in_transaction(_FakeConn([existing]),**data)['replayed']
    with pytest.raises(PriscillaError,match='conflicts'):
        ingest_tracking_in_transaction(_FakeConn([existing]),**{**data,'package_entry':str(uuid4())})
    duplicate=ingest_tracking_in_transaction(_FakeConn([None,existing,_package(),{'created_at':'now'}]),**{**data,'provider_event_id':'evt-2'})
    assert duplicate['duplicate_message_status']
    alias={'event_id':duplicate['event_id'],'package_entry':package,'payload':duplicate['payload']}
    with pytest.raises(PriscillaError,match='conflicts'):
        ingest_tracking_in_transaction(_FakeConn([alias]),**{**data,'provider_event_id':'evt-2','message_id':'other'})


def test_timeline_preserves_out_of_order_and_domain_separation():
    data=inputs(str(uuid4()))
    def event(status,simulated=False):
        return {'event_id':uuid4(),'kind':'delivery','simulated':simulated,'label':'test','created_at':'2026-11-03',
            'payload':{'evidence_refs':['owner-provided:evidence'],
                'detail':{'provider':'owner','message_id':'message','lifecycle_status':status,'occurred_at':'2026-11-01T00:00:00+00:00'}}}
    timeline=timeline_in_transaction(_FakeConn([_package(),[event('replied'),event('delivered'),event('bounced'),event('interested',True)]]),package_entry=data['package_entry'])
    assert timeline['actual']['reply_rate_of_delivered']==1
    assert timeline['actual']['conflicting_delivery_messages']==1
    assert timeline['actual']['message_status_counts']['interested']==0
    assert timeline['simulated']['message_status_counts']['interested']==1
    assert len(timeline['events'])==4
    journey=timeline['actual']['message_journeys'][0]
    assert journey['stage']=='conflicting_delivery_evidence'
    assert journey['delivery_evidence_conflict'] and not journey['contact_authorized']
    assert timeline['simulated']['message_journeys'][0]['stage']=='interest_recorded'
    unmatched=timeline_in_transaction(_FakeConn([_package(),[event('replied'),event('replied')]]),package_entry=data['package_entry'])
    assert unmatched['actual']['messages']==1 and unmatched['actual']['reply_rate_of_delivered'] is None
    assert unmatched['actual']['message_journeys'][0]['reply_without_delivery_record']


@pytest.mark.parametrize('changes',[{'message_id':''},{'occurred_at':'2026-11-03'},{'evidence_refs':[]},{'simulated':'false'}])
def test_uncorrelated_or_invalid_observation_refuses(changes):
    with pytest.raises(PriscillaError):
        ingest_tracking_in_transaction(_FakeConn([]),**{**inputs(str(uuid4())),**changes})


def test_campaign_is_evidence_linked_and_never_sent(monkeypatch):
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    conn=Conn(_new_db())
    with scope(conn,KEY):
        result=drive(conn,'demo-handoff-campaign')
    payload=result['dossier']
    campaign=payload['dossier']['reports']['uriel']['campaign']
    assert campaign['status']=='draft_not_sent' and not campaign['execution']['enabled']
    assert campaign['claims'] and all(c['evidence']['report_digest'] for c in campaign['claims'])
    assert 'dallas' not in campaign['drafts']['introduction_email']['body'].lower()
    assert payload['dossier']['reports']['priscilla']['tracking_readiness']['status']=='awaiting_observations'
    damaged={**payload,'digest':'wrong'}
    with pytest.raises(UrielError): prepare_campaign(damaged)

@pytest.mark.skipif(__import__('os').getenv('GROWUP_PG_INTEGRATION')!='1',reason='PostgreSQL integration explicitly disabled')
def test_live_tracking_concurrency_and_lifecycle(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tests.test_dcad_admission import _require_gate_database
    from tests.test_priscilla_sim import _live_seed_packages
    from app.properties_db import get_connection
    from app.priscilla import ingest_tracking_audited, metrics_in_transaction
    _require_gate_database()
    monkeypatch.setenv('CUSTODY_HMAC_KEY','campaign-pg-test-key-0123456789')
    with get_connection() as conn:
        package=_live_seed_packages(conn,1)[0]; conn.commit()
    data=inputs(str(package)); data.update(provider='pg-test-'+str(uuid4()),status='bounced')
    barrier=Barrier(2)
    def attempt():
        barrier.wait(timeout=10)
        return ingest_tracking_audited(principal='pg-test',**data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:attempt(),range(2)))
    assert sum(bool(r.get('replayed')) for r in results)==1
    with get_connection() as conn:
        assert metrics_in_transaction(conn,package_entry=package)['actual']['delivery']==0
    ingest_tracking_audited(principal='pg-test',**{**data,'provider_event_id':'reply','status':'replied'})
    with get_connection() as conn:
        view=timeline_in_transaction(conn,package_entry=package)
        assert view['actual']['unmatched_delivery_replies']==1
        assert view['actual']['reply_rate_of_delivered'] is None
    ingest_tracking_audited(principal='pg-test',**{**data,'provider_event_id':'delivered','status':'delivered'})
    with get_connection() as conn:
        view=timeline_in_transaction(conn,package_entry=package)
        assert view['actual']['reply_rate_of_delivered']==1
        assert view['actual']['conflicting_delivery_messages']==1
        assert metrics_in_transaction(conn,package_entry=package)['actual']['delivery']==1


def test_tracking_import_permissions_and_actor_bound_approval(monkeypatch):
    from app.wondrue import handle_command,WondrueError,WondrueAuthError
    from app import priscilla
    monkeypatch.setattr(priscilla,'ingest_tracking_audited',lambda **kw:pytest.fail('must not import'))
    intent={'intent':'import_campaign_tracking','args':{'package_entry':str(uuid4()),'status':'delivered','evidence_ref':'owner:receipt'}}
    with pytest.raises(WondrueError,match='read-only'):
        handle_command(mode='read-only',max_mode='full',target='real',text='',principal='owner',intent_override=intent)
    preview=handle_command(mode='ask',max_mode='full',target='real',text='',principal='owner',intent_override=intent)
    with pytest.raises(WondrueAuthError,match='different operator'):
        handle_command(mode='ask',max_mode='full',target='real',text='',principal='foreign',intent_override=intent,
            approval_token=preview['approval_token'])
