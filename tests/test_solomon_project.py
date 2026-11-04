from copy import deepcopy
import pytest
from app import dossier_coordinator as coordinator
from app import solomon_project as project
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError, get_record_in_transaction, list_records_in_transaction
from tests.test_dossier_coordinator import specialist_seed
from tests.test_handoff_demo import KEY


def prepared(monkeypatch):
    conn,rid=specialist_seed(monkeypatch,agent='solomon')
    with scope(conn,KEY):
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=0)
        assert result['status']=='solomon_planning_review'
    return conn,rid


def observation(kind='progress',status='reported_complete',phase='survey'):
    return {'kind':kind,'status':status,'phase':phase,'note':'FICTIONAL progress observation',
            'evidence_refs':['demo:synthetic-observation']}


def record(conn,rid,seq,event=None,event_id=None,principal='owner'):
    return project.record_in_transaction(conn,run_id=rid,dossier_key='main',principal=principal,
        event_id=event_id or 'event-'+str(seq),expected_sequence=seq,event=event or observation())


def test_progress_change_and_inspection_preserve_reports_custody_and_replay(monkeypatch):
    conn,rid=prepared(monkeypatch)
    with scope(conn,KEY):
        before=get_record_in_transaction(conn,rid,'dossier_version','main')
        reports=deepcopy(before.payload['dossier']['reports'])
        first=record(conn,rid,0)
        assert first['project']['phases']['survey']=='reported_complete'
        assert first['project']['classification']=='synthetic'
        assert record(conn,rid,0)['replayed']
        with pytest.raises(WorkflowError,match='replay conflict'): record(conn,rid,0,principal='different')
        with pytest.raises(WorkflowError,match='sequence mismatch'): record(conn,rid,0,event_id='new')
        change={**observation('change_order','proposed'),'cost_delta_usd':'100.25','schedule_delta_days':3}
        second=record(conn,rid,1,change)
        assert second['project']['proposed_total_usd']=='3100.25'
        assert second['project']['proposed_changes'][0]['applied'] is False
        failed=record(conn,rid,2,observation('inspection','failed'))
        assert failed['project']['phases']['survey']=='blocked'
        with pytest.raises(WorkflowError,match='failed inspection'): record(conn,rid,3)
        after=get_record_in_transaction(conn,rid,'dossier_version','main')
        assert after.payload['dossier']['reports']==reports
        assert before.payload['dossier'].get('solomon_project') is None
        assert len(list_records_in_transaction(conn,rid,project.KIND))==3
        assert project.inspect_in_transaction(conn,run_id=rid,dossier_key='main')['project']['sequence']==3
        assert not failed['project']['construction_authorized'] and failed['external_actions']==0
        from app.custody import dossier_id_for_property
        from app.custody_db import current_custody_in_transaction,verify_chain_in_transaction
        did=dossier_id_for_property(after.payload['property_id'])
        assert current_custody_in_transaction(conn,did)['holder']=='solomon'
        verify_chain_in_transaction(conn,did)
        tasks=list_records_in_transaction(conn,rid,'owner_review_task')
        assert any(t.payload.get('requirement')=='project:event-1' for t in tasks)
        assert any(t.payload.get('requirement')=='project:event-2' for t in tasks)


def test_dependencies_regression_emergency_and_negative_change(monkeypatch):
    conn,rid=prepared(monkeypatch)
    with scope(conn,KEY):
        with pytest.raises(WorkflowError,match='preceding phase'): record(conn,rid,0,observation(phase='design'))
        record(conn,rid,0)
        record(conn,rid,1,observation(phase='design'))
        state=record(conn,rid,2,observation(status='blocked'))['project']
        assert state['phases']['design']=='blocked'
        monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','1')
        with pytest.raises(WorkflowError,match='emergency'): record(conn,rid,3)
        monkeypatch.delenv('DOSSIER_COORDINATOR_EMERGENCY_STOP')
        with pytest.raises(WorkflowError,match='negative'):
            record(conn,rid,3,{**observation('change_order','proposed'),'cost_delta_usd':'-999999','schedule_delta_days':1})


@pytest.mark.parametrize('patch',[{'kind':'approve'},{'status':'verified_complete'},{'cost_delta_usd':'NaN'},
    {'evidence_refs':[]},{'phase':'arbitrary'},{'construction_authorized':True},{'phase':[]},{'kind':{}}])
def test_malformed_events_refuse(patch):
    with pytest.raises(WorkflowError): project.validate_event({**observation(),**patch})


def test_wondrue_permissions_preview_and_principal_binding(monkeypatch):
    from app.wondrue import handle_command,WondrueError,WondrueAuthError
    override={'intent':'record_solomon_project','args':{'run_id':'unused','dossier_key':'main',
        'event_id':'one','expected_sequence':0,'event_json':__import__('json').dumps(observation())}}
    with pytest.raises(WondrueError):
        handle_command(mode='read-only',max_mode='full',text='',target='real',intent_override=override,principal='owner')
    preview=handle_command(mode='ask',max_mode='full',text='',target='real',intent_override=override,principal='owner')
    assert preview['outcome']=='preview'
    with pytest.raises(WondrueAuthError):
        handle_command(mode='ask',max_mode='full',text='',target='real',intent_override=override,
            principal='foreign',approval_token=preview['approval_token'])
    called={}
    monkeypatch.setattr(project,'record_audited',lambda **kwargs: called.update(kwargs) or {'external_actions':0})
    result=handle_command(mode='full',max_mode='full',text='',target='real',intent_override=override,principal='owner')
    assert called['principal']=='owner' and result['data']['external_actions']==0


def test_missing_plan_and_wrong_holder_cannot_record(monkeypatch):
    conn,rid=specialist_seed(monkeypatch,agent='solomon')
    with scope(conn,KEY):
        with pytest.raises(WorkflowError,match='planning-ready'): record(conn,rid,0)
    conn,rid=specialist_seed(monkeypatch,agent='simeon')
    with scope(conn,KEY):
        with pytest.raises(WorkflowError,match='hold canonical custody'): record(conn,rid,0)
