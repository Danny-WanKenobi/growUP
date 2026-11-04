from copy import deepcopy
import pytest
from app import owner_work,manny_review
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError
from tests.test_handoff_demo import KEY
from tests.test_manny_review import ready,scoped_tasks,decision_args
from tests.test_elmer import source


def call(conn,rid,action,op,inputs=None):
    return owner_work.mutate_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id=op,
        expected_digest=source(conn,rid).digest,action=action,inputs=inputs,operator_authorized=True)


def request(conn,rid,target='nicole'):
    task=scoped_tasks(conn,rid)[0]
    manny_review.mutate_in_transaction(conn,**decision_args(conn,rid,task,disposition='request_evidence'))
    return call(conn,rid,'dispatch','dispatch',dict(task_key=task['record_key'],target_agent=target))


def test_targeted_reassessment_preserves_original_and_returns_linked_package(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        original=deepcopy(source(conn,rid).payload['dossier']['reports'])
        job=request(conn,rid)
        assert job['current_holder']=='nicole'
        args=dict(principal='owner',run_id=rid,dossier_key='main',operation_id='reassess',expected_digest=source(conn,rid).digest,
            action='reassess',inputs={'job_key':job['job_key']},operator_authorized=True)
        result=owner_work.mutate_in_transaction(conn,**args)
        assert result['current_holder']=='manny'
        dossier=source(conn,rid).payload['dossier']
        assert dossier['reports']==original and dossier['agent_reassessments'][-1]['agent']=='nicole'
        assert dossier['owner_briefs'][-1]['source_dossier_digest']
        assert dossier['owner_briefs'][-1]['rework_history'][-1]['status']==result['status']
        assert dossier['owner_briefs'][-1]['rework_history'][-1]['report_digest']==result['report_digest']
        before=deepcopy(conn._db)
        assert owner_work.mutate_in_transaction(conn,**args)['replayed'] and before==conn._db
        assert any(t['review_status']=='needs_revalidation' for t in scoped_tasks(conn,rid))


def test_rework_requires_explicit_request_and_target_bound_inputs(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        task=scoped_tasks(conn,rid)[0]
        with pytest.raises(WorkflowError,match='evidence-request'): call(conn,rid,'dispatch','no-request',dict(task_key=task['record_key'],target_agent='nicole'))
        job=request(conn,rid,'raphael')
        with pytest.raises(WorkflowError,match='belong'): call(conn,rid,'reassess','bad',dict(job_key=job['job_key'],assessment_inputs={'financial':{}}))
        with pytest.raises(WorkflowError,match='operator'): owner_work.mutate_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='unauth',expected_digest=source(conn,rid).digest,action='vault')


def test_vault_refuses_unresolved_evidence(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        before=deepcopy(conn._db)
        with pytest.raises(WorkflowError,match='unresolved'): call(conn,rid,'vault','blocked')
        assert before==conn._db


def test_vault_files_exact_snapshot_and_replay_does_not_duplicate(monkeypatch):
    from app import handoff_demo as demo
    from app.agents.onesimus import crops
    from app.agents.onesimus.service import ClassifiedInput
    from app.agents.onesimus.engineering import CROP_UNITS
    profile=crops.crop_profile
    def quantified():
        result=profile()
        for name,value in {'ppfd_umol_m2_s':200,'labor_min_per_site_cycle':2,'nutrient_ml_per_site_cycle':10}.items():
            result[name]=ClassifiedInput(name=name,value=value,unit=CROP_UNITS[name],classification='assumed',provenance='demo:fictional-planning-input')
        return result
    monkeypatch.setattr(crops,'crop_profile',quantified)
    original=demo._fixture
    def fixture(key,fail):
        payload=original(key,fail);prop=payload['property_id']
        payload['dossier']['specialist_inputs']['manny']={name:{'value':{'property_id':prop,**value},'evidence_refs':['demo:owner-record']} for name,value in {
            'asset_ownership':{'status':'unknown'},'operating_mandate':{'scope':'review_only'},'asset_obligations':{'coverage':'none_reported','entries':[]}}.items()}
        return demo.sealed(payload)
    monkeypatch.setattr(demo,'_fixture',fixture)
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        for index,task in enumerate(scoped_tasks(conn,rid)):
            manny_review.mutate_in_transaction(conn,**{**decision_args(conn,rid,task),'operation_id':'close-'+str(index)})
        args=dict(principal='owner',run_id=rid,dossier_key='main',operation_id='vault',expected_digest=source(conn,rid).digest,action='vault',operator_authorized=True)
        view=manny_review.inspect_in_transaction(conn,run_id=rid,dossier_key='main')['brief']
        from app.linked_reassessments import stale_dependencies,effective_reports
        assert not view['unresolved_task_keys'] and not view['overview']['missing'] and not stale_dependencies(effective_reports(source(conn,rid).payload['dossier'])), (view['exceptions'],view['overview']['missing'],stale_dependencies(effective_reports(source(conn,rid).payload['dossier'])))
        assert not {a:r.get('missing') for a,r in effective_reports(source(conn,rid).payload['dossier']).items() if a!='manny' and r.get('missing')}
        result=owner_work.mutate_in_transaction(conn,**args)
        entry=result['vault_entry'];archived=conn._db['vault_archive'][0]
        assert entry['snapshot_digest']==archived['digest'] and not entry['business_approval']
        assert str(archived['snapshot_id'])==entry['snapshot_id']
        before=deepcopy(conn._db)
        assert owner_work.mutate_in_transaction(conn,**args)['replayed'] and before==conn._db
        assert manny_review.inspect_in_transaction(conn,run_id=rid,dossier_key='main')['brief']['vault_entries']==[entry]
        copies=owner_work.read_vault_in_transaction(conn,run_id=rid,dossier_key='main')
        assert copies['entries'][0]['payload']==archived['payload'] and copies['records_written']==0
        conn._db['vault_archive'][0]['digest']='tampered'
        with pytest.raises(WorkflowError,match='integrity'): owner_work.read_vault_in_transaction(conn,run_id=rid,dossier_key='main')


def test_reassessment_lineage_and_transitive_staleness(monkeypatch):
    from app.linked_reassessments import effective_reports,stale_dependencies
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        job=request(conn,rid,'nicole');call(conn,rid,'reassess','again',{'job_key':job['job_key']})
        dossier=source(conn,rid).payload['dossier'];reports=effective_reports(dossier)
        assert reports['nicole']['digest']!=dossier['reports']['nicole']['digest']
        assert 'theresia_keys' in stale_dependencies(reports)
        damaged=deepcopy(dossier);damaged['agent_reassessments'][0]['parent_report_digest']='tampered'
        with pytest.raises(WorkflowError,match='lineage'): effective_reports(damaged)
        with pytest.raises(WorkflowError,match='unresolved'): call(conn,rid,'vault','stale-vault')


def test_rework_audit_rolls_back_interruption(monkeypatch):
    from app.observability import transactions
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        task=scoped_tasks(conn,rid)[0]
        manny_review.mutate_in_transaction(conn,**decision_args(conn,rid,task,disposition='request_evidence'))
        before=deepcopy(conn._db);called=[]
        monkeypatch.setattr(transactions,'get_connection',lambda config:conn)
        monkeypatch.setattr(conn,'rollback',lambda:called.append('rollback'),raising=False)
        monkeypatch.setattr(conn,'close',lambda:called.append('close'))
        original=owner_work.append_owner_material_in_transaction
        def interrupted(*args,**kwargs):
            original(*args,**kwargs);raise KeyboardInterrupt('controlled interruption after snapshot')
        monkeypatch.setattr(owner_work,'append_owner_material_in_transaction',interrupted)
        with pytest.raises(KeyboardInterrupt): owner_work.audited(principal='owner',run_id=rid,dossier_key='main',operation_id='interrupt',expected_digest=source(conn,rid).digest,action='dispatch',inputs=dict(task_key=task['record_key'],target_agent='nicole'),operator_authorized=True)
        assert called==['rollback','close']
        # The fake connection only records rollback; the PostgreSQL gate proves transactional restoration.


@pytest.mark.parametrize('intent_name',['dispatch_owner_rework','reassess_owner_rework','file_owner_vault'])
def test_wondrue_owner_work_requires_explicit_operator_intent(monkeypatch,intent_name):
    from app import wondrue
    intent={'intent':intent_name,'args':{'run_id':'stored','operation_id':'one','expected_digest':'source','inputs_json':'{}'}}
    options={'max_mode':'full','target':'real','text':'','principal':'owner','intent_override':intent}
    with pytest.raises(wondrue.WondrueError): wondrue.handle_command(mode='read-only',**options)
    preview=wondrue.handle_command(mode='ask',**options)
    with pytest.raises(wondrue.WondrueAuthError): wondrue.handle_command(mode='ask',approval_token=preview['approval_token'],**{**options,'principal':'foreign'})
    monkeypatch.setattr(wondrue,'interpret_command',lambda *a,**k:(intent,None))
    with pytest.raises(wondrue.WondrueAuthError,match='explicit operator intent'): wondrue.handle_command(mode='full',max_mode='full',target='real',text='file this',principal='owner')
    seen={};monkeypatch.setattr(owner_work,'audited',lambda **kw:seen.update(kw) or {'external_actions':0})
    wondrue.handle_command(mode='full',**options)
    assert seen['operator_authorized'] is True and seen['principal']=='owner'
