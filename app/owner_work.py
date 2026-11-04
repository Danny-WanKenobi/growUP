"""Source-bound internal owner rework and immutable filing; no network transports."""
from copy import deepcopy
from uuid import UUID
import os
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction
from app.agents.stages import _load
from app import manny_review
from app.custody_db import latest_version_in_transaction,issue_receipt_in_transaction,_snapshot_row_for_version
from app.dossier_reports import append_owner_material_in_transaction,append_reassessment_in_transaction
from app.linked_reassessments import effective_dossier,effective_reports,stale_dependencies


def seal(value):
    value=deepcopy(value);value.pop('digest',None);value['digest']=digest_payload(value);return value


def _job(conn,rid,key):
    row=get_record_in_transaction(conn,rid,'research_artifact',key)
    if row is None or row.digest!=digest_payload(row.payload): raise WorkflowError('rework job missing or corrupt')
    return row


def mutate_in_transaction(conn,*,principal,run_id,dossier_key,operation_id,expected_digest,action,inputs=None,operator_authorized=False):
    from app.mvp import _require_enabled
    from app.agents.registry import PIPELINE_IDS
    _require_enabled()
    if not principal or operator_authorized is not True: raise WorkflowError('explicit operator grant required')
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('owner work emergency-stopped')
    if action not in ('dispatch','reassess','vault'): raise WorkflowError('unsupported owner work action')
    inputs=inputs or {}
    if not isinstance(inputs,dict): raise WorkflowError('structured inputs required')
    run_id=UUID(str(run_id));source,did=manny_review._locked(conn,run_id,dossier_key)
    request=dict(principal=principal,action=action,inputs=inputs,expected_digest=expected_digest,dossier_key=dossier_key)
    op,replay=manny_review._operation(conn,run_id,dossier_key,operation_id,request)
    if replay: return replay
    if source.digest!=expected_digest: raise WorkflowError('source dossier changed; inspect again')
    if action=='dispatch':
        source,_=_load(conn,run_id,dossier_key,'manny')
        if set(inputs)-{'task_key','target_agent'}: raise WorkflowError('unsupported dispatch input')
        target=inputs.get('target_agent')
        if target not in PIPELINE_IDS[:-1]: raise WorkflowError('target must be a pipeline upstream agent')
        tasks=manny_review.queue_in_transaction(conn,run_id=run_id,dossier_key=dossier_key)['tasks']
        task=next((t for t in tasks if t['record_key']==inputs.get('task_key')),None)
        if not task or task['review_status']!='evidence_requested': raise WorkflowError('current evidence-request decision required')
        job_key='owner-rework:'+digest_payload({'decision':task['latest_decision']['digest'],'target':target})
        if get_record_in_transaction(conn,run_id,'research_artifact',job_key): raise WorkflowError('evidence request already dispatched')
        material=seal(dict(schema='owner-rework-1',job_key=job_key,task_key=task['record_key'],decision_digest=task['latest_decision']['digest'],target_agent=target,
            source_dossier_digest=source.digest,requirement=task.get('requirement'),status='dispatched',external_actions=0))
        stored,version=append_owner_material_in_transaction(conn,run_id,dossier_key,section='owner_rework',material=material)
        receipt=issue_receipt_in_transaction(conn,dossier_id=did,version=version.version,kind='return',from_agent='manny',to_agent=target,issued_by=principal,note='Targeted evidence rework; original reports retained')
        job=dict(material,dossier_key=dossier_key,property_id=source.payload['property_id'],receipt_id=str(receipt.receipt_id),principal=principal)
        put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=job_key,version=1,payload=job,digest=digest_payload(job))
        result=dict(job_key=job_key,current_holder=target,dossier_digest=stored.digest,status='dispatched')
    elif action=='reassess':
        if set(inputs)-{'job_key','specialist_inputs','assessment_inputs','correction'}: raise WorkflowError('unsupported reassessment input')
        row=_job(conn,run_id,inputs.get('job_key'));job=row.payload;target=job['target_agent']
        if job.get('dossier_key')!=dossier_key or str(job.get('property_id'))!=str(source.payload['property_id']) or job['status']!='dispatched': raise WorkflowError('job scope/state mismatch')
        manifest=next((m for m in source.payload['dossier'].get('owner_rework',[]) if m.get('job_key')==row.record_key),None)
        if not manifest or manifest.get('digest')!=digest_payload({k:v for k,v in manifest.items() if k!='digest'}) or any(job.get(k)!=v for k,v in manifest.items() if k!='status') or job.get('principal')!=principal:
            raise WorkflowError('rework canonical binding/principal mismatch')
        if source.payload['dossier']['owner_rework'][-1]['job_key']!=row.record_key: raise WorkflowError('rework job was superseded')
        source,_=_load(conn,run_id,dossier_key,target)
        if target=='elmer':
            if set(inputs)-{'job_key','correction'}: raise WorkflowError('Elmer requires linked accounting correction')
            from app.elmer_intake import reconcile_in_transaction
            outcome=reconcile_in_transaction(conn,principal=principal,run_id=run_id,dossier_key=dossier_key,operation_id='rework-'+operation_id,expected_digest=source.digest,correction=inputs.get('correction'))
            if outcome['current_holder']!='manny': raise WorkflowError('accounting remains incomplete; retain job for corrected inputs')
            report=outcome['assessment'];eligible=True;reasons=[]
        else:
            candidate=deepcopy(source.payload);dossier=effective_dossier(candidate['dossier']);candidate['dossier']=dossier
            if 'correction' in inputs: raise WorkflowError('accounting correction only applies to Elmer')
            updates=inputs.get('assessment_inputs')
            if updates is not None:
                allowed={'raphael':'site','onesimus':'crop','michael':'financial'}
                if target not in allowed or not isinstance(updates,dict) or set(updates)!={allowed[target]}: raise WorkflowError('assessment changes must belong to target agent')
                dossier.setdefault('assessment_inputs',{}).update(deepcopy(updates))
            if 'specialist_inputs' in inputs:
                if target in ('gabriel','raphael','onesimus','michael') or not isinstance(inputs['specialist_inputs'],dict): raise WorkflowError('target does not accept specialist inputs')
                dossier.setdefault('specialist_inputs',{})[target]=deepcopy(inputs['specialist_inputs'])
            # The effective view is already resolved. Avoid resolving the history twice.
            dossier.pop('agent_reassessments',None)
            candidate['digest']=digest_payload({k:v for k,v in candidate.items() if k!='digest'})
            if target=='gabriel':
                from app.agents.gabriel.persist import qualify_dossier
                qualification=qualify_dossier(dossier)
                report=seal(dict(agent=target,qualification=qualification,missing=[],source_dossier_digest=source.digest))
                eligible=False;reasons=['Retained-evidence research reassessed; Raphael authorization remains separate.']
            else:
                from app.dossier_coordinator import _build
                report,eligible,reasons=_build(target,candidate)
            prior=effective_reports(source.payload['dossier']).get(target)
            report=seal({**report,'source_dossier_digest':source.digest,'rework_input_digests':{a:r['digest'] for a,r in effective_reports(source.payload['dossier']).items() if a in PIPELINE_IDS[:PIPELINE_IDS.index(target)]}})
            material=seal(dict(agent=target,report=report,parent_report_digest=(prior or {}).get('digest'),source_dossier_digest=source.digest,job_key=row.record_key,
                assessment_inputs=dossier.get('assessment_inputs'),specialist_inputs=(dossier.get('specialist_inputs') or {}).get(target)))
            stored,version=append_reassessment_in_transaction(conn,run_id,dossier_key,material)
            issue_receipt_in_transaction(conn,dossier_id=did,version=version.version,kind='forward',from_agent=target,to_agent='manny',issued_by=principal,note='Linked reassessment returned for owner review; no permission granted')
        job={**job,'status':'reassessed' if eligible else 'needs_evidence','report_digest':report['digest'],'reasons':reasons}
        put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=row.record_key,version=row.version+1,payload=job,digest=digest_payload(job))
        current,_=_load(conn,run_id,dossier_key,'manny')
        manny_review.prepare_on_arrival_in_transaction(conn,principal=principal,run_id=run_id,dossier_key=dossier_key,assessment_digest=report['digest'],dossier_digest=current.digest)
        result=dict(job_key=row.record_key,status=job['status'],current_holder='manny',report_digest=report['digest'],dossier_digest=_load(conn,run_id,dossier_key)[0].digest)
    else:
        if inputs: raise WorkflowError('vault accepts no fact overrides')
        source,_=_load(conn,run_id,dossier_key,'manny')
        brief=manny_review.inspect_in_transaction(conn,run_id=run_id,dossier_key=dossier_key)['brief']
        reports=effective_reports(source.payload['dossier'])
        if (brief.get('asset_tracking') or {}).get('exceptions'): raise WorkflowError('vault refused: unresolved tracked asset obligations')
        if brief['unresolved_task_keys'] or brief['unscoped_task_keys'] or brief['overview']['missing'] or stale_dependencies(reports): raise WorkflowError('vault refused: unresolved review/evidence/dependencies')
        if any(not item['retained'] for item in brief['overview']['report_manifest'] if item['agent']!='gabriel'): raise WorkflowError('vault refused: missing pipeline report')
        if any(r.get('missing') for a,r in reports.items() if a!='manny'): raise WorkflowError('vault refused: report evidence gaps')
        for material in source.payload['dossier'].get('owner_rework') or []:
            if _job(conn,run_id,material['job_key']).payload['status']=='dispatched': raise WorkflowError('vault refused: active rework')
        version=latest_version_in_transaction(conn,did)
        snap=archive_exact_in_transaction(conn,did,version.version)
        material=seal(dict(schema='manny-vault-entry-1',source_dossier_digest=source.digest,dossier_id=str(did),custody_version=version.version,snapshot_id=str(snap['snapshot_id']),snapshot_digest=snap['digest'],business_approval=False,external_actions=0))
        stored,version=append_owner_material_in_transaction(conn,run_id,dossier_key,section='owner_vault_entries',material=material)
        result=dict(vault_entry=material,dossier_digest=stored.digest,current_holder='manny',status='filed')
    result.update(external_actions=0,replayed=False)
    record=dict(request=request,result=result)
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=op,version=1,payload=record,digest=digest_payload(record))
    return result


def audited(*,principal,**inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId.MANNY,run_id=str(inputs['run_id']),event_type='manny.rework_vault',summary='Internal linked rework or protected filing',details={'action':inputs['action']},retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try: return mutate_in_transaction(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def archive_exact_in_transaction(conn,did,version):
    """Copy one exact immutable snapshot; never age/archive unrelated dossiers."""
    pair=_snapshot_row_for_version(conn,did,version)
    if pair is None: raise WorkflowError('vault requires canonical snapshot')
    snap=pair[0]
    with conn.cursor() as cur:
        cur.execute('INSERT INTO custody_snapshots_archive (snapshot_id,dossier_id,version,payload,digest,created_at) SELECT snapshot_id,dossier_id,version,payload,digest,created_at FROM custody_snapshots WHERE dossier_id=%(dossier)s AND version=%(version)s ON CONFLICT DO NOTHING',dict(dossier=did,version=version))
        cur.execute('SELECT * FROM custody_snapshots_archive WHERE dossier_id=%(dossier)s AND version=%(version)s',dict(dossier=did,version=version))
        archived=cur.fetchone()
    if not archived or str(archived['snapshot_id'])!=str(snap['snapshot_id']) or archived['digest']!=snap['digest'] or archived['payload']!=snap['payload']: raise WorkflowError('vault archive integrity failure')
    return snap


def read_vault_in_transaction(conn,*,run_id,dossier_key):
    """Return exact protected copies, verifying locators from canonical manifests."""
    from app.custody import snapshot_digest
    source,did=_load(conn,UUID(str(run_id)),dossier_key)
    result=[]
    for entry in source.payload['dossier'].get('owner_vault_entries') or []:
        if entry.get('digest')!=digest_payload({k:v for k,v in entry.items() if k!='digest'}) or entry.get('dossier_id')!=str(did): raise WorkflowError('vault manifest integrity failure')
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM custody_snapshots_archive WHERE dossier_id=%(dossier)s AND version=%(version)s',dict(dossier=did,version=entry['custody_version']))
            row=cur.fetchone()
        if not row or str(row['snapshot_id'])!=entry['snapshot_id'] or row['digest']!=entry['snapshot_digest'] or snapshot_digest(row['payload'])!=row['digest'] or row['payload'].get('dossier_digest')!=entry['source_dossier_digest']: raise WorkflowError('vault copy integrity failure')
        result.append(dict(entry=deepcopy(entry),payload=deepcopy(row['payload']),current_dossier_digest=source.digest))
    return dict(entries=result,records_written=0,external_actions=0)


def rework_views_in_transaction(conn,run_id,dossier):
    """Project the current linked outcome beside the immutable request manifest."""
    views=[]
    assessments=[e.get('report') or {} for e in dossier.get('agent_reassessments') or []]+list(dossier.get('elmer_assessments') or [])
    digests={r.get('digest') for r in assessments}
    for manifest in dossier.get('owner_rework') or []:
        job=_job(conn,run_id,manifest['job_key']).payload
        if manifest.get('digest')!=digest_payload({k:v for k,v in manifest.items() if k!='digest'}) or any(job.get(k)!=v for k,v in manifest.items() if k!='status'): raise WorkflowError('rework outcome canonical binding failure')
        if job['status'] not in ('dispatched','reassessed','needs_evidence'): raise WorkflowError('unknown rework state')
        if job['status']!='dispatched' and job.get('report_digest') not in digests: raise WorkflowError('rework outcome lacks retained assessment')
        views.append(dict(request=deepcopy(manifest),status=job['status'],report_digest=job.get('report_digest'),reasons=deepcopy(job.get('reasons') or [])))
    return views
