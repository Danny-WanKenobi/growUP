"""Channel/personality and ordered specialist handover proofs; no providers."""
from copy import deepcopy
from uuid import uuid4
import os
import pytest
from app import customer_service as csr
from app.personalities import PROFILES,profile,require_channel
from app.workflow_db import WorkflowError
from tests.test_customer_service import store


def test_personality_profiles_do_not_grant_channels_or_change_voice():
    assert len(PROFILES)==17
    assert all(not profile(a)['authority_granted'] and not profile(a)['voice_changed'] for a in PROFILES)
    for agent in PROFILES:
        with pytest.raises(WorkflowError):require_channel(agent,'phone','outbound',human_initiated=True)
    for channel in ('email','chat','postal'):
        with pytest.raises(WorkflowError):require_channel('wondrue',channel,'outbound')
    with pytest.raises(WorkflowError):require_channel('uriel','email','inbound')
    with pytest.raises(WorkflowError):require_channel('uriel','postal','outbound')
    for actor in ('simeon','csr'):
        with pytest.raises(WorkflowError,match='human-initiated'):require_channel(actor,'email','outbound')
        assert require_channel(actor,'email','outbound',human_initiated=True)
    for actor in ('priscilla','gabriel','manny'):
        with pytest.raises(WorkflowError):require_channel(actor,'chat','outbound',human_initiated=True)


def test_ordered_handover_replay_history_and_return_to_csr(store):
    csr.session(store,'visitor',True);first=str(uuid4());second=str(uuid4())
    rid,_=csr.enqueue(store,'visitor',first,'Please connect me to Simeon')
    csr.enqueue(store,'visitor',second,'What information do you need?')
    assert csr.claim(store,rid,second).get('queued')
    result=csr.respond('visitor',first,'Please connect me to Simeon')
    assert result['agent']=='simeon' and result['handoff']=={'from':'csr','to':'simeon','reason':'Customer-requested specialist routing','context_preserved':True}
    before=deepcopy(store._db)
    assert csr.respond('visitor',first,'Please connect me to Simeon')==result and store._db==before
    follow=csr.respond('visitor',second,'What information do you need?')
    assert follow['agent']=='simeon' and not follow['handoff']
    construction=csr.respond('visitor',str(uuid4()),'Please connect me to Solomon')
    assert construction['handoff']['from']=='simeon' and construction['agent']=='solomon'
    returned=csr.respond('visitor',str(uuid4()),'Please return me to customer care')
    assert returned['agent']=='csr' and returned['handoff']['from']=='solomon'
    history=csr.inspect_session(store,'visitor')
    assert len(history['history'])==4 and history['history'][0]['result']['agent']=='simeon'
    csr.session(store,'stranger',True)
    assert csr.inspect_session(store,'stranger')['history']==[]


def test_no_handoff_to_forbidden_agents_and_no_email_role_takeover(store):
    csr.session(store,'visitor',True)
    result=csr.respond('visitor',str(uuid4()),'Please connect me to Uriel')
    assert result['agent']=='csr' and not result['handoff']
    csr.session(store,'email-visitor',True)
    email=csr.respond('email-visitor',str(uuid4()),'Construction contractor question',channel='email')
    assert email['agent']=='csr' and not email['handoff'] and email['escalate']=='solomon'
    assert csr.inspect_session(store,'email-visitor')['history']==[]


def test_commitment_request_is_queued_without_authority(store,monkeypatch):
    csr.session(store,'visitor',True)
    csr.respond('visitor',str(uuid4()),'Please connect me to Simeon')
    result=csr.respond('visitor',str(uuid4()),'Please sign this contract')
    assert result['escalate']=='human_owner' and not result['commitments_made'] and not result['authority_granted']
    monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','1')
    assert csr.respond('visitor',str(uuid4()),'More questions')['status']=='queued'


@pytest.mark.skipif(os.getenv('GROWUP_PG_INTEGRATION')!='1',reason='Dedicated PostgreSQL gate required')
def test_pg_specialist_handover_atomic_recovery_and_history(monkeypatch):
    from tests.test_dcad_admission import _require_gate_database
    from app.properties_db import get_connection
    from app.specialist_chat import respond
    _require_gate_database();monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')
    who='pg-handover-'+uuid4().hex;mid=str(uuid4())
    csr.transaction('test_session',lambda c:csr.session(c,who,True))
    rid,_=csr.transaction('test_enqueue',lambda c:csr.enqueue(c,who,mid,'Please connect me to Solomon'))
    lease=csr.transaction('test_claim',lambda c:csr.claim(c,rid,mid))
    result=respond(lease['owner'],lease['text'],lease['channel']);original=csr.write
    def interrupt(c,r,key,*args,**kw):
        record=original(c,r,key,*args,**kw)
        if key.startswith('support-handoff:'):raise KeyboardInterrupt('TEST after handover record insertion')
        return record
    monkeypatch.setattr(csr,'write',interrupt)
    with pytest.raises(KeyboardInterrupt):csr.transaction('test_interrupt',lambda c:csr.complete(c,rid,mid,lease['token'],result))
    monkeypatch.setattr(csr,'write',original)
    with get_connection() as c:
        assert csr.inspect_session(c,who)['agent']=='csr'
        assert csr.read(c,rid,'support-handoff:'+mid) is None
    complete=csr.transaction('test_recover',lambda c:csr.complete(c,rid,mid,lease['token'],result))
    assert complete['agent']=='solomon'
    assert csr.transaction('test_replay',lambda c:csr.complete(c,rid,mid,lease['token'],result))==complete
    with get_connection() as c:
        history=csr.inspect_session(c,who)
        assert history['agent']=='solomon' and len(history['history'])==1
