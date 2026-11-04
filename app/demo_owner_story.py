"""Bounded, explicitly synthetic owner-register stories using canonical services."""
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction
from app.handoff_demo_domain import assert_database
from app import asset_tracking,manny_review,owner_work
from app.agents.stages import _load


def exercise(conn,run,*,full=False):
    assert_database(conn)
    from app.handoff_demo import FIXTURE_VERSION
    if not run.limits.get('scenario_id') or run.limits.get('fixture_version')!=FIXTURE_VERSION: raise WorkflowError('choose a current catalogue GO property for the owner story')
    state=get_record_in_transaction(conn,run.run_id,'research_artifact','handoff-state')
    if state.payload['phase']!='complete_go': raise WorkflowError('owner story requires a completed synthetic GO dossier')
    marker='showcase-owner-story' if full else 'showcase-owner-seed'
    previous=get_record_in_transaction(conn,run.run_id,'research_artifact',marker)
    if previous:
        if previous.digest!=digest_payload(previous.payload): raise WorkflowError('owner story integrity failure')
        return previous.payload
    def source(): return _load(conn,run.run_id,'main','manny')[0]
    def track(id,as_of,event=None):
        return asset_tracking.mutate_in_transaction(conn,principal='synthetic-demo-controller',run_id=run.run_id,dossier_key='main',operation_id=id,
            expected_digest=source().digest,as_of=as_of,event=event,operator_authorized=True)
    seed=get_record_in_transaction(conn,run.run_id,'research_artifact','showcase-owner-seed')
    if seed is None:
        event=dict(kind='obligation',entity_id='fictional-site-review',property_id=source().payload['property_id'],observed_on='2026-11-03',
            status='open',category='inspection',description='FICTIONAL planning evidence review, not a real inspection obligation',due_on='2026-11-10',
            amount_usd='125.50',currency='USD',note='SYNTHETIC invented evidence only',evidence_refs=['https://records.fiction.example/site-review'],previous_entity_digest=None)
        seeded=track('demo-seed-obligation','2026-11-03',event)
        seed_payload=dict(synthetic=True,domain='synthetic_handoff_demo',event_digest=seeded['asset_tracking']['latest_event_digest'],due_on='2026-11-10')
        seed=put_record_in_transaction(conn,run.run_id,kind='research_artifact',record_key='showcase-owner-seed',version=1,payload=seed_payload,digest=digest_payload(seed_payload))
    if not full: return seed.payload
    due=track('demo-due-today','2026-11-10')
    overdue=track('demo-overdue','2026-11-11')
    event=dict(source().payload['dossier']['owner_asset_events'][0]['event'])
    event.update(status='reported_satisfied',observed_on='2026-11-12',note='SYNTHETIC follow-up report; no payment, inspection or legal approval verified',previous_entity_digest=seed.payload['event_digest'])
    corrected=track('demo-correction','2026-11-12',event)
    # Review closure is explicit fixture input, not software self-approval in live mode.
    for index,task in enumerate(manny_review.queue_in_transaction(conn,run_id=run.run_id,dossier_key='main')['tasks']):
        manny_review.mutate_in_transaction(conn,principal='synthetic-demo-controller',run_id=run.run_id,dossier_key='main',operation_id='demo-review-'+str(index),expected_digest=source().digest,
            action='resolve',operator_authorized=True,decision=dict(task_key=task['record_key'],expected_sequence=task['resolution_sequence'],disposition='close_review',
                note='SYNTHETIC administrative review only; never factual verification or real authority.',evidence_refs=['task:'+task['task_digest']]))
    filed=owner_work.mutate_in_transaction(conn,principal='synthetic-demo-controller',run_id=run.run_id,dossier_key='main',operation_id='demo-vault',expected_digest=source().digest,action='vault',operator_authorized=True)
    protected=owner_work.read_vault_in_transaction(conn,run_id=run.run_id,dossier_key='main')['entries'][-1]
    event.update(status='disputed',observed_on='2026-11-13',previous_entity_digest=corrected['asset_tracking']['latest_event_digest'],note='SYNTHETIC later evidence disputes the earlier completion claim')
    reopened=track('demo-reopen','2026-11-13',event)
    from app.agents.elmer import read_vault_in_transaction
    preserved=read_vault_in_transaction(conn,run_id=run.run_id,dossier_key='main')['entries'][-1]
    if protected['payload']!=preserved['payload']: raise WorkflowError('synthetic vault preservation failed')
    result=dict(synthetic=True,domain='synthetic_handoff_demo',result='PASS',due_today_overdue=due['asset_tracking']['obligations'][0]['overdue'],
        overdue_task_count=len(overdue['created_task_keys']),reported_correction_only=not corrected['asset_tracking']['payments_verified'],
        vault_snapshot_id=filed['vault_entry']['snapshot_id'],vault_preserved_after_new_evidence=True,
        reopened_task_count=len(reopened['created_task_keys']),actual_payments=0,provider_calls=0,external_actions=0,
        automatic_monitor_scope='Synthetic story uses fixed dates and shared register services; operational timer excludes these records.')
    if result['due_today_overdue'] or not result['overdue_task_count'] or not result['reopened_task_count']: raise WorkflowError('synthetic owner lifecycle assertion failed')
    put_record_in_transaction(conn,run.run_id,kind='research_artifact',record_key=marker,version=1,payload=result,digest=digest_payload(result))
    return result
