from copy import deepcopy
import json
import pytest
from app import manny_review,elmer_intake
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction
from app.handoff_demo_domain import scope
from tests.test_elmer import accounting_fixture,transaction,source
from tests.test_handoff_demo import KEY


def ready(monkeypatch):
    conn,rid=accounting_fixture(monkeypatch,[],coverage='none_reported')
    with scope(conn,KEY):
        parent=source(conn,rid)
        result=elmer_intake.prepare_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='arrival',expected_digest=parent.digest)
        assert result['current_holder']=='manny' and result['owner_brief_digest']
    return conn,rid


def scoped_tasks(conn,rid): return manny_review.queue_in_transaction(conn,run_id=rid,dossier_key='main')['tasks']


def decision_args(conn,rid,task,**updates):
    decision={'task_key':task['record_key'],'expected_sequence':task['resolution_sequence'],
        'disposition':'close_review','note':'Owner completed administrative review; no fact or permission is changed.',
        'evidence_refs':['task:'+task['task_digest']]}
    decision.update(updates)
    return {'principal':'owner','run_id':rid,'dossier_key':'main','operation_id':'review-'+str(task['resolution_sequence']),
        'expected_digest':source(conn,rid).digest,'action':'resolve','decision':decision,'operator_authorized':True}


def test_owner_package_on_arrival_uses_latest_reports_and_preserves_unknowns(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        retained=source(conn,rid).payload['dossier']
        assert retained['owner_briefs'] and retained['reports']['manny']['owner_overview']
        before=deepcopy(conn._db)
        view=manny_review.inspect_in_transaction(conn,run_id=rid,dossier_key='main')
        brief=view['brief'];overview=brief['overview']
        assert conn._db==before and view['records_written']==0
        assert overview['latest_accounting_digest']==retained['reports']['elmer']['digest']
        assert overview['latest_accounting']['reported_actual_expenses']['total_usd']=='0.00'
        assert overview['property_assessment']['facts']['structural_capacity']['value'] is None
        assert not overview['ownership_verified'] and not brief['business_approval']
        readiness=overview['execution_readiness']
        assert readiness['closing_checklist'] and readiness['regulatory_workplan']
        assert readiness['underwriting_scenarios']
        assert not readiness['external_actions_authorized'] and not readiness['funding_committed'] and not readiness['deal_closed']
        assert readiness['source_report_digests']['simeon']==retained['reports']['simeon']['digest']
        assert not {'yoseph','deborah'} & {r['agent'] for r in overview['report_manifest']}
        assert brief['unresolved_task_keys'] and brief['custody']['current_holder']=='manny'


def test_resolution_is_append_only_idempotent_and_presentation_does_not_reopen_it(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        task=scoped_tasks(conn,rid)[0]
        original=deepcopy(get_record_in_transaction(conn,rid,'owner_review_task',task['record_key']).payload)
        args=decision_args(conn,rid,task)
        result=manny_review.mutate_in_transaction(conn,**args)
        assert not result['decision']['grants_permission'] and not result['decision']['underlying_facts_changed']
        before=deepcopy(conn._db)
        assert manny_review.mutate_in_transaction(conn,**args)['replayed'] and conn._db==before
        with pytest.raises(WorkflowError,match='replay conflict'):
            manny_review.mutate_in_transaction(conn,**{**args,'principal':'foreign'})
        item=next(t for t in scoped_tasks(conn,rid) if t['record_key']==task['record_key'])
        assert item['review_status']=='closed_by_owner' and item['status']=='pending'
        assert get_record_in_transaction(conn,rid,'owner_review_task',task['record_key']).payload==original
        manny_review.mutate_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='refresh',
            expected_digest=source(conn,rid).digest,action='prepare')
        item=next(t for t in scoped_tasks(conn,rid) if t['record_key']==task['record_key'])
        assert item['review_status']=='closed_by_owner'
        reopened=manny_review.mutate_in_transaction(conn,**{**decision_args(conn,rid,item,disposition='reopen'),'operation_id':'reopen'})
        assert reopened['decision']['review_status']=='reopened' and reopened['decision']['parent_resolution_digest']


def test_new_accounting_evidence_revalidates_old_review_and_brief_uses_correction(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        task=scoped_tasks(conn,rid)[0]
        manny_review.mutate_in_transaction(conn,**decision_args(conn,rid,task))
        prop=source(conn,rid).payload['property_id']
        original=deepcopy(source(conn,rid).payload['dossier']['reports']['elmer'])
        correction={'reason':'Later invoice evidence','inputs':{'transactions':{'value':{'property_id':prop,
            'coverage':'reported_records','entries':[transaction('new-invoice','expense','25')]},'evidence_refs':['demo:invoice']}}}
        elmer_intake.reconcile_in_transaction(conn,principal='owner',run_id=rid,dossier_key='main',operation_id='new-evidence',
            expected_digest=source(conn,rid).digest,correction=correction)
        item=next(t for t in scoped_tasks(conn,rid) if t['record_key']==task['record_key'])
        assert item['review_status']=='needs_revalidation'
        overview=manny_review.inspect_in_transaction(conn,run_id=rid,dossier_key='main')['brief']['overview']
        assert overview['latest_accounting']['reported_actual_expenses']['total_usd']=='25.00'
        assert source(conn,rid).payload['dossier']['reports']['elmer']==original
        assert len(source(conn,rid).payload['dossier']['owner_briefs'])==3


def test_worker_cannot_close_and_unknown_reference_or_approval_is_refused(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        task=scoped_tasks(conn,rid)[0];args=decision_args(conn,rid,task)
        with pytest.raises(WorkflowError,match='explicit operator'):
            manny_review.mutate_in_transaction(conn,**{**args,'operator_authorized':False})
        with pytest.raises(WorkflowError,match='not retained'):
            manny_review.mutate_in_transaction(conn,**{**args,'decision':{**args['decision'],'evidence_refs':['https://not-retained.example/']}})
        with pytest.raises(WorkflowError,match='not a supported action'):
            manny_review.mutate_in_transaction(conn,**{**args,'decision':{**args['decision'],'disposition':'approve'}})
        with pytest.raises(WorkflowError,match='changed'):
            manny_review.mutate_in_transaction(conn,**{**args,'expected_digest':'old'})


def test_duplicate_tasks_and_cross_property_scope(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        tasks=scoped_tasks(conn,rid);a,b=tasks[:2]
        args=decision_args(conn,rid,a,disposition='mark_duplicate',duplicate_of=b['record_key'])
        manny_review.mutate_in_transaction(conn,**args)
        b=next(t for t in scoped_tasks(conn,rid) if t['record_key']==b['record_key'])
        with pytest.raises(WorkflowError,match='cycles'):
            manny_review.mutate_in_transaction(conn,**{**decision_args(conn,rid,b,disposition='mark_duplicate',duplicate_of=a['record_key']),'operation_id':'cycle'})
        payload={'agent':'manny','property_id':str(__import__('uuid').uuid4()),'status':'pending','requirement':'foreign','grants_permission':False}
        put_record_in_transaction(conn,rid,kind='owner_review_task',record_key='foreign',version=1,payload=payload,digest=digest_payload(payload))
        with pytest.raises(WorkflowError,match='does not bind'):
            manny_review.mutate_in_transaction(conn,**{**args,'operation_id':'foreign','expected_digest':source(conn,rid).digest,
                'decision':{**args['decision'],'task_key':'foreign','disposition':'close_review','duplicate_of':None}})


def test_wondrue_resolution_requires_explicit_operator_intent_and_principal_bound_approval(monkeypatch):
    from app import wondrue
    intent={'intent':'resolve_owner_exception','args':{'run_id':'stored','operation_id':'one','expected_digest':'source',
        'decision_json':json.dumps({'disposition':'close_review'})}}
    options={'max_mode':'full','target':'real','text':'','principal':'owner','intent_override':intent}
    with pytest.raises(wondrue.WondrueError): wondrue.handle_command(mode='read-only',**options)
    preview=wondrue.handle_command(mode='ask',**options)
    with pytest.raises(wondrue.WondrueAuthError):
        wondrue.handle_command(mode='ask',approval_token=preview['approval_token'],**{**options,'principal':'other'})
    monkeypatch.setattr(wondrue,'interpret_command',lambda *a,**k:(intent,None))
    with pytest.raises(wondrue.WondrueAuthError,match='explicit operator intent'):
        wondrue.handle_command(mode='full',max_mode='full',text='close all exceptions',target='real',principal='owner')
    seen={};monkeypatch.setattr(manny_review,'audited',lambda **kw:seen.update(kw) or {'external_actions':0})
    wondrue.handle_command(mode='full',**options)
    assert seen['operator_authorized'] is True and seen['principal']=='owner'


def test_resolution_index_cannot_roll_back_the_canonical_decision(monkeypatch):
    conn,rid=ready(monkeypatch)
    with scope(conn,KEY):
        task=scoped_tasks(conn,rid)[0]
        first=manny_review.mutate_in_transaction(conn,**decision_args(conn,rid,task))
        current=next(t for t in scoped_tasks(conn,rid) if t['record_key']==task['record_key'])
        manny_review.mutate_in_transaction(conn,**{**decision_args(conn,rid,current,disposition='reopen'),'operation_id':'reopen'})
        rollback=first['decision']
        resolution_key='owner-resolution:'+digest_payload({'task_key':task['record_key']})
        put_record_in_transaction(conn,rid,kind='research_artifact',record_key=resolution_key,version=3,payload=rollback,digest=digest_payload(rollback))
        with pytest.raises(WorkflowError,match='latest canonical decision'): scoped_tasks(conn,rid)
