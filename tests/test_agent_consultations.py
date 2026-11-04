import pytest
from app import agent_consultations as consultations
from app.agents.registry import PIPELINE_IDS
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError,get_record_in_transaction,list_records_in_transaction
from tests.test_solomon_project import prepared
from tests.test_handoff_demo import KEY


def message(speaker,recommendation='needs_evidence'):
    return {'speaker':speaker,'text':'Review retained evidence and unresolved conditions.',
        'recommendation':recommendation,'evidence_refs':['demo:retained-claim']}


@pytest.mark.parametrize('requester',(*PIPELINE_IDS,'wondrue'))
def test_all_agents_have_two_way_adviser_access_without_custody(monkeypatch,requester):
    conn,rid=prepared(monkeypatch)
    with scope(conn,KEY):
        args={'principal':'owner','run_id':rid,'dossier_key':'main','thread_id':'review','requester':requester,'adviser':'deborah'}
        source=get_record_in_transaction(conn,rid,'dossier_version','main')
        first=consultations.exchange_in_transaction(conn,**args,expected_sequence=0,message=message(requester))
        assert first['joint_status']=='awaiting_reply'
        second=consultations.exchange_in_transaction(conn,**args,expected_sequence=1,message=message('deborah'))
        assert second['joint_status']=='aligned_advisory_needs_evidence' and not second['grants_permission']
        assert consultations.exchange_in_transaction(conn,**args,expected_sequence=1,message=message('deborah'))['replayed']
        assert get_record_in_transaction(conn,rid,'dossier_version','main')==source
        with pytest.raises(WorkflowError,match='identity conflict'):
            consultations.exchange_in_transaction(conn,**{**args,'principal':'foreign'},expected_sequence=2,message=message(requester))


def test_disagreement_goes_to_owner_without_approval_or_transfer(monkeypatch):
    conn,rid=prepared(monkeypatch)
    with scope(conn,KEY):
        args={'principal':'owner','run_id':rid,'dossier_key':'main','thread_id':'insurance','requester':'solomon','adviser':'yoseph'}
        consultations.exchange_in_transaction(conn,**args,expected_sequence=0,message=message('solomon','proceed_conditionally'))
        result=consultations.exchange_in_transaction(conn,**args,expected_sequence=1,message=message('yoseph','hold'))
        assert result['joint_status']=='disagreement_owner_review'
        assert not result['custody_transferred'] and result['external_actions']==0
        assert any(r.payload.get('adviser')=='yoseph' for r in list_records_in_transaction(conn,rid,'owner_review_task'))


def test_advisers_rejected_from_new_custody_paths_but_historical_meaning_readable():
    from app.custody import check_handoff_routing,interpret_custody_agent,CustodyError
    for adviser in ('yoseph','deborah'):
        assert interpret_custody_agent('canonical-1',adviser)['meaning']
        for kind,sender,recipient in [('forward','elmer',adviser),('return','manny',adviser),('review',adviser,'human_review')]:
            with pytest.raises(CustodyError,match='shared advisers'):check_handoff_routing(kind,sender,recipient)


def test_changed_dossier_does_not_produce_current_joint_approval(monkeypatch):
    from tests.test_solomon_project import record
    conn,rid=prepared(monkeypatch)
    with scope(conn,KEY):
        args={'principal':'owner','run_id':rid,'dossier_key':'main','thread_id':'stale','requester':'solomon','adviser':'deborah'}
        consultations.exchange_in_transaction(conn,**args,expected_sequence=0,message=message('solomon','proceed_conditionally'))
        record(conn,rid,0)
        result=consultations.exchange_in_transaction(conn,**args,expected_sequence=1,message=message('deborah','proceed_conditionally'))
        assert result['joint_status']=='context_changed_requires_new_review' and not result['grants_permission']


@pytest.mark.parametrize('intent', ['consult_adviser','reconcile_elmer','complete_elmer_intake','dispatch_simeon'])
def test_internal_mutations_are_readonly_blocked_and_actor_bound(intent):
    from app.wondrue import handle_command,WondrueError,WondrueAuthError
    override={'intent':intent,'args':{}}
    options={'max_mode':'full','target':'real','text':'','intent_override':override,'principal':'owner'}
    with pytest.raises(WondrueError,match='read-only'): handle_command(mode='read-only',**options)
    preview=handle_command(mode='ask',**options)
    with pytest.raises(WondrueAuthError):
        handle_command(mode='ask',approval_token=preview['approval_token'],**{**options,'principal':'foreign'})
