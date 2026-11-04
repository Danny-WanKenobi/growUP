"""Evidence-bound owner review rehearsal using the existing canonical services.

Only growup_handoff_demo/growup_demo. No operational connector, transports,
discovery or models. Each approved step and its checkpoint commit together.
"""
from copy import deepcopy
from uuid import UUID
import os

from app import handoff_demo as demo, manny_review, owner_work
from app.agents.stages import _load
from app.communications import instant
from app.custody import dossier_id_for_property
from app.custody_db import current_custody_in_transaction,verify_chain_in_transaction
from app.handoff_demo_domain import assert_database,scope,current_key
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction

CHOICES={'supplied_update':'Use the supplied fictional update and reassess Nicole',
         'keep_unknown':'Keep the fictional zoning evidence unknown and reassess Nicole'}
PRINCIPAL='wondrue-demo-review-owner'


def _guard(conn):
    assert_database(conn)
    if current_key() is None:raise WorkflowError('Explicit isolated synthetic scope required')


def _run(conn,case_id):
    _guard(conn)
    return demo._get(conn,'demo-handoff-review-'+UUID(str(case_id)).hex,lock=True)


def _read(conn,rid,key):
    row=get_record_in_transaction(conn,rid,'research_artifact',key)
    if row and row.digest!=digest_payload(row.payload):raise WorkflowError('guided-review integrity failure')
    return row


def _write(conn,rid,key,payload,prior=None):
    return put_record_in_transaction(conn,rid,kind='research_artifact',record_key=key,
        version=prior.version+1 if prior else 1,payload=demo.marked(payload),digest=digest_payload(demo.marked(payload)))


def _source(conn,run):return _load(conn,run.run_id,'main')[0]


def _assessment_content(report):
    return {k:v for k,v in report.items() if k not in ('digest','source_dossier_digest','rework_input_digests')}


def _assess(source,inputs):
    from app.linked_reassessments import effective_dossier
    from app.dossier_coordinator import _build
    candidate=deepcopy(source.payload)
    candidate['dossier']=effective_dossier(candidate['dossier'])
    candidate['dossier'].setdefault('specialist_inputs',{})['nicole']=deepcopy(inputs)
    candidate['digest']=digest_payload({k:v for k,v in candidate.items() if k!='digest'})
    report,eligible,reasons=_build('nicole',candidate)
    return report,eligible,reasons


def create_in_transaction(conn,case_id):
    _guard(conn)
    case_id=str(UUID(str(case_id)));key='demo-handoff-review-'+UUID(case_id).hex
    demo.start_in_transaction(conn,run_key=key)
    run=_run(conn,case_id)
    existing=_read(conn,run.run_id,'guided-review-case')
    if existing:return inspect_in_transaction(conn,case_id)
    for _ in range(demo.MAX_STEPS):
        state=demo._state(conn,run).payload
        if state['phase']=='complete_go':break
        if state['phase']=='returned_to_gabriel':raise WorkflowError('practice fixture did not reach owner intake')
        demo.advance_in_transaction(conn,run_key=key,expected_sequence=state['sequence'])
    else:raise WorkflowError('bounded practice fixture did not finish')
    source=_source(conn,run);did=dossier_id_for_property(source.payload['property_id'])
    if (current_custody_in_transaction(conn,did) or {}).get('holder')!='manny':raise WorkflowError('Manny must hold the synthetic fixture')
    task_key='guided-review-task:'+case_id
    task=demo.marked({'agent':'nicole','property_id':source.payload['property_id'],'dossier_id':str(did),
        'requirement':'Review a superseding FICTIONAL zoning screening note',
        'reason':'A fictional authority supplied a revised note. Reassess it without treating supplied claims as authenticated permits.',
        'status':'pending','authority':'human_owner','grants_permission':False})
    put_record_in_transaction(conn,run.run_id,kind='owner_review_task',record_key=task_key,version=1,payload=task,digest=digest_payload(task))
    _write(conn,run.run_id,'guided-review-case',{'id':case_id,'run_key':key,'task_key':task_key,
        'original_version':source.version,'original_digest':source.digest,
        'original_report_digests':{a:r['digest'] for a,r in source.payload['dossier']['reports'].items()},
        'initial_receipts':verify_chain_in_transaction(conn,did),'created_at':instant().isoformat()})
    return inspect_in_transaction(conn,case_id)


def prepare_in_transaction(conn,case_id,review_id,choice,expected_digest):
    if choice not in CHOICES:raise WorkflowError('Select an explicit preplanned synthetic choice')
    run=_run(conn,case_id);case=_read(conn,run.run_id,'guided-review-case')
    if case is None:raise WorkflowError('practice case missing')
    key='guided-review:'+str(UUID(str(review_id)))
    previous=_read(conn,run.run_id,key)
    if previous:
        if previous.payload['manifest']['choice']!=choice or previous.payload['manifest']['source_digest']!=expected_digest:
            raise WorkflowError('preview identity conflict')
        return inspect_in_transaction(conn,case_id,review_id)
    # Only one active correction can own this case at a time.
    active=case.payload.get('active_review')
    if active:
        old=_read(conn,run.run_id,'guided-review:'+active)
        if old and old.payload['status'] not in ('rejected','complete'):
            raise WorkflowError('finish or reject the existing review first')
    source=_source(conn,run)
    if source.digest!=expected_digest:raise WorkflowError('dossier changed; refresh before preparing a preview')
    _load(conn,run.run_id,'main','manny')
    task=get_record_in_transaction(conn,run.run_id,'owner_review_task',case.payload['task_key'])
    if task is None or task.digest!=digest_payload(task.payload):raise WorkflowError('practice issue integrity failure')
    from app.linked_reassessments import effective_dossier
    before=deepcopy(effective_dossier(source.payload['dossier'])['specialist_inputs']['nicole'])
    after=deepcopy(before)
    rows=after['official_requirements']['value']
    zoning=next((r for r in rows if r['topic']=='zoning_use'),None)
    if zoning is None:raise WorkflowError('practice zoning input is unavailable')
    zoning.update(record_id='fiction-guided-'+str(UUID(str(review_id))),record_date='2026-11-04',
        requirement='FICTIONAL superseding screening note; not real law or authenticated permission')
    if choice=='keep_unknown':zoning.update(state='unknown',disposition='pending')
    report,eligible,reasons=_assess(source,after)
    manifest=demo.marked({'version':'guided-review-1','choice':choice,'explanation':CHOICES[choice],
        'source_version':source.version,'source_digest':source.digest,'task_key':task.record_key,'task_digest':task.digest,
        'target_agent':'nicole','before_inputs':before,'after_inputs':after,
        'assessment_content_digest':digest_payload(_assessment_content(report)),
        'preview_assessment':_assessment_content(report),'expected_rework_status':'reassessed' if eligible else 'needs_evidence',
        'expected_remaining_reasons':reasons,'close_target_administrative_review':eligible,
        'steps':['Record the exact evidence request','Return custody to Nicole','Append linked reassessment and return to Manny',
                 'Close only this administrative task if the previewed assessment is complete; preserve all other gaps'],
        'preserves':['Original dossier versions','Inherited reports','Snapshots and custody receipts'],
        'provider_calls':0,'external_actions':0,'operational_writes':0,'grants_permission':False})
    manifest['digest']=digest_payload(manifest)
    state={'id':str(UUID(str(review_id))),'case_id':str(UUID(str(case_id))),'status':'pending','manifest':manifest,
        'expected_source_digest':source.digest,'timeline':[],'created_at':instant().isoformat()}
    _write(conn,run.run_id,key,state)
    _write(conn,run.run_id,'guided-review-case',{**case.payload,'active_review':state['id']},case)
    return inspect_in_transaction(conn,case_id,review_id)


def decide_in_transaction(conn,case_id,review_id,expected_digest,choice,note):
    if choice not in ('approve','defer','reject'):raise WorkflowError('explicit review choice required')
    if not isinstance(note,str) or not note.strip() or len(note)>1000:raise WorkflowError('bounded owner decision note required')
    run=_run(conn,case_id);key='guided-review:'+str(UUID(str(review_id)));row=_read(conn,run.run_id,key)
    if row is None or row.digest!=expected_digest:raise WorkflowError('preview changed; refresh before deciding')
    if row.payload['status'] not in ('pending','deferred'):raise WorkflowError('review already decided; inspect or resume the existing approval')
    state=deepcopy(row.payload)
    manifest=state['manifest']
    if manifest['digest']!=digest_payload({k:v for k,v in manifest.items() if k!='digest'}):raise WorkflowError('preview manifest is corrupt')
    if choice=='approve':
        source=_source(conn,run)
        if source.digest!=manifest['source_digest']:raise WorkflowError('dossier changed since preview; reject it and prepare a new preview')
        task=get_record_in_transaction(conn,run.run_id,'owner_review_task',manifest['task_key'])
        if not task or task.digest!=manifest['task_digest']:raise WorkflowError('issue changed since preview')
    state.update(status={'approve':'approved','defer':'deferred','reject':'rejected'}[choice],
        decision=choice,decision_note=note.strip(),decided_at=instant().isoformat(),principal=PRINCIPAL)
    _write(conn,run.run_id,key,state,row)
    return inspect_in_transaction(conn,case_id,review_id)


def _save_step(conn,run,key,state,row):
    """Checkpoint shares the business transaction; interruption rolls both back."""
    return _write(conn,run.run_id,key,state,row)


def advance_in_transaction(conn,case_id,review_id):
    run=_run(conn,case_id);key='guided-review:'+str(UUID(str(review_id)));row=_read(conn,run.run_id,key)
    if row is None:raise WorkflowError('review missing')
    state=deepcopy(row.payload);status=state['status'];m=state['manifest']
    if status=='complete':return inspect_in_transaction(conn,case_id,review_id)
    if status not in ('approved','requested','dispatched'):raise WorkflowError('explicit owner approval is required before execution')
    if m['digest']!=digest_payload({k:v for k,v in m.items() if k!='digest'}):raise WorkflowError('approval manifest integrity failed')
    source=_source(conn,run)
    if source.digest!=state['expected_source_digest']:raise WorkflowError('dossier changed outside the approved step; stop and reconcile')
    prefix='guided-'+state['id']
    if status=='approved':
        task=next(t for t in manny_review.queue_in_transaction(conn,run_id=run.run_id,dossier_key='main')['tasks'] if t['record_key']==m['task_key'])
        result=manny_review.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=run.run_id,dossier_key='main',
            operation_id=prefix+'-request',expected_digest=source.digest,action='resolve',operator_authorized=True,
            decision={'task_key':m['task_key'],'expected_sequence':task['resolution_sequence'],'disposition':'request_evidence',
                'note':'SYNTHETIC owner-approved request bound to preview '+m['digest'],
                'evidence_refs':['task:'+m['task_digest'],'dossier:'+source.digest]})
        state['status']='requested'
    elif status=='requested':
        result=owner_work.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=run.run_id,dossier_key='main',
            operation_id=prefix+'-dispatch',expected_digest=source.digest,action='dispatch',operator_authorized=True,
            inputs={'task_key':m['task_key'],'target_agent':'nicole'})
        state.update(status='dispatched',job_key=result['job_key'])
    else:
        result=owner_work.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=run.run_id,dossier_key='main',
            operation_id=prefix+'-reassess',expected_digest=source.digest,action='reassess',operator_authorized=True,
            inputs={'job_key':state['job_key'],'specialist_inputs':m['after_inputs']})
        returned=_source(conn,run)
        report=returned.payload['dossier']['agent_reassessments'][-1]['report']
        if digest_payload(_assessment_content(report))!=m['assessment_content_digest'] or result['status']!=m['expected_rework_status']:
            raise WorkflowError('actual assessment differs from approved preview; transaction rolled back')
        if m['close_target_administrative_review']:
            task=next(t for t in manny_review.queue_in_transaction(conn,run_id=run.run_id,dossier_key='main')['tasks'] if t['record_key']==m['task_key'])
            manny_review.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=run.run_id,dossier_key='main',
                operation_id=prefix+'-close',expected_digest=returned.digest,action='resolve',operator_authorized=True,
                decision={'task_key':m['task_key'],'expected_sequence':task['resolution_sequence'],'disposition':'close_review',
                    'note':'SYNTHETIC administrative review of the exact previewed correction only; no fact authentication or permission.',
                    'evidence_refs':['task:'+m['task_digest'],'dossier:'+returned.digest]})
        state.update(status='complete',result=result,report_digest=report['digest'])
    current=_source(conn,run);did=dossier_id_for_property(current.payload['property_id'])
    custody=current_custody_in_transaction(conn,did) or {}
    state['expected_source_digest']=current.digest
    state['timeline'].append({'phase':state['status'],'at':instant().isoformat(),'dossier_version':current.version,
        'dossier_digest':current.digest,'holder':(current_custody_in_transaction(conn,did) or {}).get('holder','unknown'),
        'verified_receipts':verify_chain_in_transaction(conn,did),
        'receipt_id':custody.get('receipt_id'),'receipt_version':custody.get('version'),'snapshot_id':custody.get('snapshot_id')})
    _save_step(conn,run,key,state,row)
    return inspect_in_transaction(conn,case_id,review_id)


def inspect_in_transaction(conn,case_id,review_id=None):
    run=_run(conn,case_id);case=_read(conn,run.run_id,'guided-review-case')
    if case is None:raise WorkflowError('practice case missing')
    source=_source(conn,run);did=dossier_id_for_property(source.payload['property_id'])
    original=get_record_in_transaction(conn,run.run_id,'dossier_version','main',case.payload['original_version'])
    if original is None or original.digest!=case.payload['original_digest'] or original.digest!=digest_payload({k:v for k,v in original.payload.items() if k!='digest'}):
        raise WorkflowError('original dossier was not preserved')
    preserved={a:r['digest'] for a,r in source.payload['dossier']['reports'].items()}==case.payload['original_report_digests']
    if not preserved:raise WorkflowError('inherited reports were changed')
    selected=str(UUID(str(review_id))) if review_id else case.payload.get('active_review')
    row=_read(conn,run.run_id,'guided-review:'+selected) if selected else None
    from app.linked_reassessments import effective_reports,stale_dependencies
    brief=manny_review.inspect_in_transaction(conn,run_id=run.run_id,dossier_key='main')['brief']
    return demo.marked({'case':case.payload,'source_digest':source.digest,'source_version':source.version,
        'review':dict(row.payload,review_digest=row.digest) if row else None,'choices':CHOICES,
        'original_preserved':True,'inherited_reports_preserved':preserved,
        'current_holder':(current_custody_in_transaction(conn,did) or {}).get('holder','unknown'),
        'verified_receipts':verify_chain_in_transaction(conn,did),'owner_brief':brief,
        'remaining_review_items':brief['unresolved_task_keys'],
        'stale_dependencies':stale_dependencies(effective_reports(source.payload['dossier'])),
        'provider_calls':0,'external_actions':0,'operational_writes':0,'grants_permission':False})


def list_in_transaction(conn):
    _guard(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT payload,digest FROM (SELECT DISTINCT ON(r.run_id) r.payload,r.digest,r.run_id "
                    "FROM workflow_records r JOIN workflow_runs w USING(run_id) "
                    "WHERE w.idempotency_key LIKE %(prefix)s AND r.kind='research_artifact' "
                    "AND r.record_key='guided-review-case' ORDER BY r.run_id,r.version DESC) latest "
                    "ORDER BY payload->>'created_at' DESC,run_id LIMIT 20",{'prefix':'demo-handoff-review-%'})
        rows=cur.fetchall()
    if any(digest_payload(r['payload'])!=r['digest'] for r in rows):raise WorkflowError('practice case index integrity failed')
    return demo.marked({'items':[r['payload'] for r in rows],'limit':20,'provider_calls':0,'operational_writes':0})


def service(action,*,case_id=None,review_id=None,choice=None,expected_digest=None,note=None,version=None):
    if action not in ('inspect','list','record') and os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1':
        raise WorkflowError('guided review is emergency-stopped; inspection remains available')
    from app.properties_db import get_connection
    cfg,key=demo.configuration()
    with get_connection(cfg) as conn:
        with scope(conn,key):
            if action=='list':result=list_in_transaction(conn)
            elif action=='create':result=create_in_transaction(conn,case_id)
            elif action=='preview':result=prepare_in_transaction(conn,case_id,review_id,choice,expected_digest)
            elif action=='decide':result=decide_in_transaction(conn,case_id,review_id,expected_digest,choice,note)
            elif action=='advance':result=advance_in_transaction(conn,case_id,review_id)
            elif action=='inspect':result=inspect_in_transaction(conn,case_id,review_id)
            elif action=='record':
                from app.wondrue_workspace import record
                run=_run(conn,case_id)
                result=demo.marked(record(conn,run.run_id,'dossier_version','main',version))
            else:raise WorkflowError('unsupported guided-review action')
            if action in ('inspect','list','record'):conn.rollback()
            else:
                from app.observability.events import AgentEvent,AgentId,RetentionClass
                from app.observability.store import insert_event_in_transaction
                insert_event_in_transaction(conn,AgentEvent(agent=AgentId.WONDRUE,run_id=result['case']['run_key'],
                    event_type='demo.guided_review',summary='SYNTHETIC evidence-bound review rehearsal',
                    details={'action':action,'case_id':str(case_id),'review_id':str(review_id) if review_id else None},
                    retention_class=RetentionClass.PERMANENT))
                conn.commit()
            return result
