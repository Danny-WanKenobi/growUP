from copy import deepcopy
import pytest
from app import elmer_intake
from app.agents.elmer import assess
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError,get_record_in_transaction
from app.workflow import digest_payload
from tests.test_solomon_project import prepared,record,observation
from tests.test_handoff_demo import KEY


def source(conn,rid): return get_record_in_transaction(conn,rid,'dossier_version','main')


def accounting_fixture(monkeypatch,entries=None,coverage='reported_records'):
    from app import handoff_demo as demo
    original=demo._fixture
    def fixture(key,fail_at):
        payload=original(key,fail_at)
        payload['dossier']['specialist_inputs']['elmer']['transactions']['value'].update(
            entries=entries or [],coverage=coverage)
        return demo.sealed({k:v for k,v in payload.items() if k!='digest'})
    monkeypatch.setattr(demo,'_fixture',fixture)
    return prepared(monkeypatch)


def transaction(id,kind,amount,**extra):
    return {'id':id,'kind':kind,'amount_usd':amount,'currency':'USD','occurred_on':'2026-11-03',
            'evidence_ref':'demo:fictional-'+id,**extra}


def test_separate_cost_buckets_and_partial_commitment_arithmetic(monkeypatch):
    rows=[transaction('c1','commitment','1000'),transaction('e1','expense','250',commitment_id='c1'),
          transaction('e2','expense','50')]
    conn,rid=accounting_fixture(monkeypatch,rows)
    with scope(conn,KEY):
        record(conn,rid,0,{**observation('change_order','proposed'),'cost_delta_usd':'100.25','schedule_delta_days':3})
        preview=elmer_intake.preview_in_transaction(conn,run_id=rid,dossier_key='main')
        assessment=preview['report']['accounting_assessment']
        assert not preview['report']['missing']
        assert assessment['baseline_estimates']['total_usd']=='3000.00'
        assert assessment['pending_changes']['proposed_total_usd']=='3100.25'
        assert assessment['pending_changes']['applied'] is False
        assert assessment['reported_commitments']['total_usd']=='1000.00'
        assert assessment['reported_commitments']['unexpensed_linked_balance_usd']=='750.00'
        assert assessment['reported_actual_expenses']['total_usd']=='300.00'
        assert not assessment['reported_actual_expenses']['payments_verified']
        assert preview['records_written']==0
        parent=source(conn,rid); inherited=deepcopy(parent.payload['dossier']['reports'])
        args={'principal':'owner','run_id':rid,'dossier_key':'main','operation_id':'one','expected_digest':parent.digest}
        result=elmer_intake.prepare_in_transaction(conn,**args)
        assert result['current_holder']=='elmer' and result['status']=='elmer_accounting_review'
        assert elmer_intake.prepare_in_transaction(conn,**args)['replayed']
        with pytest.raises(WorkflowError,match='replay conflict'):
            elmer_intake.prepare_in_transaction(conn,**{**args,'principal':'foreign'})
        current=source(conn,rid).payload['dossier']
        assert all(current['reports'][key]==value for key,value in inherited.items())
        assert current['solomon_project']==parent.payload['dossier']['solomon_project']
        assert not current['reports']['elmer']['accounting_assessment']['payments_authorized']
        from app.custody import dossier_id_for_property
        from app.custody_db import verify_chain_in_transaction,current_custody_in_transaction
        did=dossier_id_for_property(parent.payload['property_id'])
        verify_chain_in_transaction(conn,did)
        assert current_custody_in_transaction(conn,did)['holder']=='elmer'


def test_unknown_transactions_are_not_zero_and_gaps_return(monkeypatch):
    conn,rid=accounting_fixture(monkeypatch,[transaction('dup','expense','10'),transaction('dup','expense','10')])
    with scope(conn,KEY):
        parent=source(conn,rid)
        result=elmer_intake.prepare_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='gap',expected_digest=parent.digest)
        assert result['current_holder']=='solomon' and result['status']=='returned_to_solomon'
        assert result['report']['accounting_assessment']['reported_actual_expenses']['total_usd'] is None


@pytest.mark.parametrize('patch',[{'currency':'EUR'},{'amount_usd':'NaN'},{'amount_usd':'-1'},
    {'amount_usd':'1.001'},{'occurred_on':'2025-01-01'},{'commitment_id':'missing'},{'evidence_ref':''}])
def test_invalid_expenses_refuse_totals(monkeypatch,patch):
    conn,rid=accounting_fixture(monkeypatch,[{**transaction('e','expense','10'),**patch}])
    with scope(conn,KEY):
        result=assess(source(conn,rid).payload['property_id'],source(conn,rid).payload['dossier'])
        assert result['reported_actual_expenses']['total_usd'] is None and result['missing']


def test_explicit_empty_records_and_excess_commitment_review(monkeypatch):
    conn,rid=accounting_fixture(monkeypatch,[],coverage='none_reported')
    with scope(conn,KEY):
        payload=source(conn,rid).payload
        result=assess(payload['property_id'],payload['dossier'])
        assert result['reported_actual_expenses']['total_usd']=='0.00'
        dossier=deepcopy(payload['dossier'])
        dossier['specialist_inputs']['elmer']['transactions']['value']['entries']=[
            transaction('c','commitment','100'),transaction('e','expense','150',commitment_id='c')]
        dossier['specialist_inputs']['elmer']['transactions']['value']['coverage']='reported_records'
        result=assess(payload['property_id'],dossier)
        assert result['intake_ready'] and result['exceptions']
        assert result['reported_commitments']['unexpensed_linked_balance_usd']=='0.00'
        dossier['specialist_inputs']['elmer'].pop('transactions')
        assert assess(payload['property_id'],dossier)['reported_actual_expenses']['total_usd'] is None


def test_stale_digest_and_emergency_stop_no_release(monkeypatch):
    conn,rid=accounting_fixture(monkeypatch)
    with scope(conn,KEY):
        args={'principal':'owner','run_id':rid,'dossier_key':'main','operation_id':'stale','expected_digest':'wrong'}
        with pytest.raises(WorkflowError,match='changed'): elmer_intake.prepare_in_transaction(conn,**args)
        monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','1')
        with pytest.raises(WorkflowError,match='emergency'): elmer_intake.prepare_in_transaction(conn,**args)


def test_wondrue_permissions_and_actor_bound_approval(monkeypatch):
    from app.wondrue import handle_command,WondrueError,WondrueAuthError
    override={'intent':'prepare_elmer_intake','args':{'run_id':'unused','dossier_key':'main','operation_id':'one','expected_digest':'parent'}}
    with pytest.raises(WondrueError):
        handle_command(mode='read-only',max_mode='full',text='',target='real',principal='owner',intent_override=override)
    preview=handle_command(mode='ask',max_mode='full',text='',target='real',principal='owner',intent_override=override)
    with pytest.raises(WondrueAuthError):
        handle_command(mode='ask',max_mode='full',text='',target='real',principal='foreign',intent_override=override,approval_token=preview['approval_token'])
    captured={}
    monkeypatch.setattr(elmer_intake,'prepare_audited',lambda **kw:captured.update(kw) or {'external_actions':0})
    handle_command(mode='full',max_mode='full',text='',target='real',principal='owner',intent_override=override)
    assert captured['principal']=='owner'


def test_invalid_period_does_not_produce_zero_reported_expenses(monkeypatch):
    conn,rid=accounting_fixture(monkeypatch,[],coverage='none_reported')
    with scope(conn,KEY):
        payload=source(conn,rid).payload
        dossier=deepcopy(payload['dossier'])
        dossier['specialist_inputs']['elmer']['accounting_period']['value']['start']='invalid'
        result=assess(payload['property_id'],dossier)
        assert result['reported_actual_expenses']['total_usd'] is None
