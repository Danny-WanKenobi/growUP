from copy import deepcopy
import pytest
from app import asset_tracking as tracking,manny_review
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError
from tests.test_handoff_demo import KEY
from tests.test_manny_review import ready,scoped_tasks,decision_args
from tests.test_elmer import source


def event(conn,rid,**changes):
    result=dict(kind='obligation',entity_id='roof-inspection',property_id=source(conn,rid).payload['property_id'],observed_on='2026-11-03',
        status='open',category='inspection',description='FICTIONAL inspection obligation',due_on='2026-11-04',amount_usd='125.50',currency='USD',
        note='FICTIONAL supplied planning record; no payment or clearance asserted',evidence_refs=['demo:obligation'],previous_entity_digest=None)
    result.update(changes);return result


def record(conn,rid,op='one',as_of='2026-11-03',value=None):
    return tracking.mutate_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id=op,expected_digest=source(conn,rid).digest,
        as_of=as_of,event=value,operator_authorized=True)


def test_obligation_due_monitoring_correction_and_history(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        original=deepcopy(source(conn,rid).payload['dossier']['reports'])
        first=record(conn,rid,value=event(conn,rid));assert not first['asset_tracking']['exceptions']
        before=deepcopy(conn._db)
        view=tracking.inspect_in_transaction(conn,run_id=rid,dossier_key='main',as_of='2026-11-04')
        assert not view['obligations'][0]['overdue'] and before==conn._db
        late=record(conn,rid,'monitor','2026-11-05')
        assert late['asset_tracking']['obligations'][0]['overdue'] and len(late['created_task_keys'])==1
        again=record(conn,rid,'monitor-again','2026-11-06');assert not again['created_task_keys']
        correction=event(conn,rid,status='reported_satisfied',observed_on='2026-11-06',previous_entity_digest=first['asset_tracking']['latest_event_digest'])
        corrected=record(conn,rid,'correct','2026-11-06',correction)
        assert not corrected['asset_tracking']['exceptions'] and not corrected['asset_tracking']['payments_verified']
        dossier=source(conn,rid).payload['dossier'];assert dossier['reports']==original and len(dossier['owner_asset_events'])==2
        assert dossier['owner_briefs'][-1]['asset_tracking']['as_of']=='2026-11-06'
        assert any(t['record_key'] in late['created_task_keys'] for t in scoped_tasks(conn,rid))
        assert manny_review.inspect_in_transaction(conn,run_id=rid,dossier_key='main')['brief']['asset_tracking']['as_of']=='2026-11-06'


def test_replay_conflict_parent_and_missing_money(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        args=dict(principal='owner',run_id=rid,dossier_key='main',operation_id='stable',expected_digest=source(conn,rid).digest,as_of='2026-11-03',event=event(conn,rid,amount_usd=None),operator_authorized=True)
        result=tracking.mutate_in_transaction(conn,**args);before=deepcopy(conn._db)
        assert tracking.mutate_in_transaction(conn,**args)['replayed'] and conn._db==before
        assert result['asset_tracking']['active_obligations_with_unknown_amount']==1
        with pytest.raises(WorkflowError,match='replay conflict'): tracking.mutate_in_transaction(conn,**{**args,'principal':'other'})
        with pytest.raises(WorkflowError,match='entity changed'): record(conn,rid,'bad',value=event(conn,rid))
        damaged=deepcopy(source(conn,rid).payload['dossier']);damaged['owner_asset_events'][0]['event']['amount_usd']='1.00'
        with pytest.raises(WorkflowError,match='integrity'): tracking.project(damaged,property_id=args['event']['property_id'],as_of='2026-11-03')


@pytest.mark.parametrize('patch',[{'amount_usd':'NaN'},{'amount_usd':'-1'},{'amount_usd':'0.001'},{'amount_usd':True},{'currency':'EUR'},
    {'property_id':'foreign'},{'observed_on':'2026-11-04'},{'due_on':'2026-02-30'},{'status':'paid'},{'evidence_refs':[]},{'secret':'unsupported'}])
def test_invalid_observations_refuse_without_writes(monkeypatch,patch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        before=deepcopy(conn._db)
        with pytest.raises(WorkflowError): record(conn,rid,value=event(conn,rid,**patch))
        assert before==conn._db


def test_status_and_revalidation_do_not_rewrite_old_owner_decisions(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        task=scoped_tasks(conn,rid)[0]
        decision=manny_review.mutate_in_transaction(conn,**decision_args(conn,rid,task))['decision']
        status={k:v for k,v in event(conn,rid).items() if k in ('property_id','observed_on','note','evidence_refs','previous_entity_digest')}
        status.update(kind='asset_status',entity_id='asset',status='on_hold')
        result=record(conn,rid,value=status)
        assert result['created_task_keys'] and not result['asset_tracking']['ownership_verified']
        task=next(t for t in scoped_tasks(conn,rid) if t['record_key']==task['record_key'])
        assert task['review_status']=='needs_revalidation' and task['latest_decision']==decision
        with pytest.raises(WorkflowError,match='operator'): tracking.mutate_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='deny',expected_digest=source(conn,rid).digest,as_of='2026-11-03')


@pytest.mark.parametrize('name',['record_asset_observation','monitor_asset_obligations'])
def test_asset_commands_require_operator_and_bind_approval(monkeypatch,name):
    from app import wondrue
    import json
    intent={'intent':name,'args':dict(run_id='stored',operation_id='one',expected_digest='source',as_of='2026-11-03',event_json=json.dumps({'kind':'asset_status'}))}
    options=dict(max_mode='full',target='real',text='',principal='owner',intent_override=intent)
    with pytest.raises(wondrue.WondrueError): wondrue.handle_command(mode='read-only',**options)
    preview=wondrue.handle_command(mode='ask',**options)
    with pytest.raises(wondrue.WondrueAuthError): wondrue.handle_command(mode='ask',approval_token=preview['approval_token'],**{**options,'principal':'foreign'})
    monkeypatch.setattr(wondrue,'interpret_command',lambda *a,**k:(intent,None))
    with pytest.raises(wondrue.WondrueAuthError): wondrue.handle_command(mode='full',max_mode='full',text='change status',target='real',principal='owner')
    seen={};monkeypatch.setattr(tracking,'audited',lambda **kw:seen.update(kw) or {})
    wondrue.handle_command(mode='full',**options);assert seen['operator_authorized'] is True


def test_tracking_exceptions_block_filing_even_after_administrative_closure(monkeypatch):
    from app import owner_work
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        result=record(conn,rid,value=event(conn,rid,status='disputed'))
        task=next(t for t in scoped_tasks(conn,rid) if t['record_key'] in result['created_task_keys'])
        manny_review.mutate_in_transaction(conn,**decision_args(conn,rid,task))
        with pytest.raises(WorkflowError,match='tracked asset obligations'):
            owner_work.mutate_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='no-file',expected_digest=source(conn,rid).digest,action='vault',operator_authorized=True)
