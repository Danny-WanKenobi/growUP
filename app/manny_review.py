"""Consolidated owner packages and administrative exception decisions only."""
from copy import deepcopy
from uuid import UUID
import os
from app.agents.stages import _load,build_report
from app.agents.manny import assess
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,list_records_in_transaction,put_record_in_transaction


def _latest(conn,run_id,kind):
    latest={}
    for row in list_records_in_transaction(conn,run_id,kind):
        if row.digest!=digest_payload(row.payload): raise WorkflowError('owner-review record integrity failure')
        if row.record_key not in latest or row.version>latest[row.record_key].version: latest[row.record_key]=row
    return latest


def evidence_context(payload):
    dossier=deepcopy(payload.get('dossier') or {})

    for section in ('owner_briefs','owner_decisions','owner_rework','owner_vault_entries'): dossier.pop(section,None)
    (dossier.get('reports') or {}).pop('manny',None)
    return digest_payload({'property_id':payload['property_id'],'dossier':dossier})


def _belongs(task,prop,did):
    if task.get('property_id') is not None and str(task['property_id'])!=str(prop): return False
    if task.get('dossier_id') is not None and str(task['dossier_id'])!=str(did): return False
    return task.get('property_id') is not None or task.get('dossier_id') is not None


def _context(conn,run_id,dossier_key,source):
    advisory=[]
    for key,row in _latest(conn,run_id,'research_artifact').items():
        if key.startswith('advisory:') and row.payload.get('dossier_key')==dossier_key:
            advisory.append({'record_key':key,'record_digest':row.digest,'requester':row.payload.get('requester'),
                'adviser':row.payload.get('adviser'),'joint_status':row.payload.get('joint_status'),
                'source_dossier_digest':row.payload.get('source_dossier_digest'),
                'source_is_current':row.payload.get('source_dossier_digest')==source.digest,'grants_permission':False})
    advisory.sort(key=lambda r:r['record_key'])
    context=digest_payload({'evidence_digest':evidence_context(source.payload),
        'advisory_digests':[r['record_digest'] for r in advisory]})
    return context,advisory


def _task_view(row,resolution,context):
    status='pending';decision=None
    if resolution:
        decision=deepcopy(resolution.payload)
        if decision.get('task_digest')!=row.digest: status='needs_revalidation'
        elif context is None: status='requires_context_check'
        elif decision.get('evidence_context_digest')!=context: status='needs_revalidation'
        else: status=decision['review_status']
    return {**deepcopy(row.payload),'record_key':row.record_key,'task_digest':row.digest,'original_task':deepcopy(row.payload),
        'review_status':status,'resolution_sequence':resolution.payload['sequence'] if resolution else 0,
        'latest_decision':decision,'grants_permission':False}


def queue_in_transaction(conn,*,run_id,dossier_key=None):
    run_id=UUID(str(run_id));tasks=_latest(conn,run_id,'owner_review_task');artifacts=_latest(conn,run_id,'research_artifact')
    resolutions={r.payload.get('task_key'):r for key,r in artifacts.items() if key.startswith('owner-resolution:')}
    context=None;source=None;did=None
    if dossier_key is not None:
        source,did=_load(conn,run_id,dossier_key);context,_=_context(conn,run_id,dossier_key,source)
    result=[];unscoped=[];contexts={};sources={}
    for key,row in sorted(tasks.items()):
        resolution=resolutions.get(key)
        if source is not None and not _belongs(row.payload,source.payload['property_id'],did):
            if row.payload.get('property_id') is None and row.payload.get('dossier_id') is None: unscoped.append(key)
            continue
        current=context
        if dossier_key is None and resolution:
            target=resolution.payload.get('dossier_key')
            if target not in contexts:
                retained,_=_load(conn,run_id,target);contexts[target]=_context(conn,run_id,target,retained)[0];sources[target]=retained
            current=contexts[target]
        if resolution:
            bound_source=source if source is not None else sources[resolution.payload['dossier_key']]
            canonical=[item for item in (bound_source.payload.get('dossier',{}).get('owner_decisions') or []) if item.get('task_key')==key]
            if (not canonical or canonical[-1]!=resolution.payload or resolution.version!=resolution.payload.get('sequence')
                    or resolution.payload.get('digest')!=digest_payload({k:v for k,v in resolution.payload.items() if k!='digest'})):
                raise WorkflowError('owner resolution index does not bind the latest canonical decision')
        result.append(_task_view(row,resolution,current))
    return {'tasks':result,'unscoped_task_keys':unscoped,'external_actions':0,
        'resolution_scope':'Administrative review only; underlying evidence, gates and permissions are unchanged.'}


def inspect_in_transaction(conn,*,run_id,dossier_key):
    run_id=UUID(str(run_id));source,did=_load(conn,run_id,dossier_key)
    context,advisory=_context(conn,run_id,dossier_key,source)
    queue=queue_in_transaction(conn,run_id=run_id,dossier_key=dossier_key)
    overview=assess(source.payload['property_id'],source.payload.get('dossier') or {})
    from app.custody_db import current_custody_in_transaction,latest_version_in_transaction
    latest=latest_version_in_transaction(conn,did);holder=current_custody_in_transaction(conn,did)
    statuses={}
    for task in queue['tasks']: statuses[task['review_status']]=statuses.get(task['review_status'],0)+1
    unresolved=[r['record_key'] for r in queue['tasks'] if r['review_status'] not in ('closed_by_owner','closed_as_duplicate')]
    from app.owner_work import rework_views_in_transaction
    brief={'version':'manny-owner-brief-1','run_id':str(run_id),'dossier_key':dossier_key,'property_id':source.payload['property_id'],'source_dossier_digest':source.digest,
        'evidence_context_digest':context,'overview':overview,'exceptions':queue['tasks'],'unscoped_task_keys':queue['unscoped_task_keys'],
        'exception_counts':statuses,'unresolved_task_keys':unresolved,'advisory_threads':advisory,
        'custody':{'dossier_id':str(did),'version':latest.version,'current_holder':holder['holder'] if holder else 'gabriel',
            'last_transfer_receipt_id':holder.get('receipt_id') if holder else None,
            'integrity_scope':'Canonical snapshot/receipt integrity only; not verification of property claims.',
            'latest_version_digest':latest.digest,'scope':'source dossier before this package is appended'},
        'rework_history':rework_views_in_transaction(conn,run_id,source.payload['dossier']),
        'vault_entries':deepcopy(source.payload['dossier'].get('owner_vault_entries') or []),
        'decision_package_status':'owner_review_required','business_approval':False,'external_actions':0,
        'synthetic':source.payload.get('synthetic') is True,
        'limitations':['Closing an administrative task never verifies a property fact or authorizes execution.',
            'Consultations and reports retain their original scopes; Manny cannot grant human approval.']}
    brief['digest']=digest_payload(brief)
    history=(source.payload.get('dossier') or {}).get('owner_briefs') or []
    return {'brief':brief,'latest_retained_brief':deepcopy(history[-1]) if history else None,'records_written':0}


def _locked(conn,run_id,dossier_key):
    from app.custody import dossier_id_for_property
    from app.custody_db import lock_dossier_in_transaction
    identity=get_record_in_transaction(conn,run_id,'dossier_version',dossier_key)
    if identity is None: raise WorkflowError('stored dossier not found')
    lock_dossier_in_transaction(conn,dossier_id_for_property(identity.payload.get('property_id')))
    return _load(conn,run_id,dossier_key)


def _operation(conn,run_id,dossier_key,operation_id,request):
    if not isinstance(operation_id,str) or not operation_id.strip() or len(operation_id)>100: raise WorkflowError('bounded operation_id required')
    key='owner-operation:'+digest_payload({'dossier_key':dossier_key,'id':operation_id})
    row=get_record_in_transaction(conn,run_id,'research_artifact',key)
    if row:
        if row.digest!=digest_payload(row.payload) or row.payload.get('request')!=request: raise WorkflowError('owner operation replay conflict')
        return key,{**deepcopy(row.payload['result']),'replayed':True}
    return key,None


def mutate_in_transaction(conn,*,principal,run_id,dossier_key,operation_id,expected_digest,action='prepare',decision=None,operator_authorized=False):
    from app.mvp import _require_enabled
    from app.dossier_reports import append_agent_report_in_transaction,append_owner_material_in_transaction
    _require_enabled()
    if not isinstance(principal,str) or not principal.strip(): raise WorkflowError('authenticated principal required')
    if action not in ('prepare','resolve'): raise WorkflowError('unknown owner-review operation')
    if action=='resolve' and operator_authorized is not True: raise WorkflowError('explicit operator grant required for exception decisions')
    run_id=UUID(str(run_id));source,did=_locked(conn,run_id,dossier_key)
    request={'principal':principal,'dossier_key':dossier_key,'expected_digest':expected_digest,'action':action,'decision':decision}
    key,replay=_operation(conn,run_id,dossier_key,operation_id,request)
    if replay: return replay
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('owner review emergency-stopped')
    source,_=_load(conn,run_id,dossier_key,'manny')
    if source.digest!=expected_digest: raise WorkflowError('source dossier changed; inspect again')
    if action=='prepare':
        if (source.payload['dossier'].get('reports') or {}).get('manny') is None:
            report=build_report('manny',source.payload)
            source,_=append_agent_report_in_transaction(conn,run_id,dossier_key,agent='manny',report=report)
        overview=assess(source.payload['property_id'],source.payload['dossier'])
        context,_=_context(conn,run_id,dossier_key,source)
        for gap in overview['missing']:
            task={'agent':'manny','property_id':source.payload['property_id'],'dossier_id':str(did),'requirement':gap,
                'reason':'Owner intake evidence is unresolved.','status':'pending','authority':'human_owner','grants_permission':False,
                'evidence_context_digest':context}
            put_record_in_transaction(conn,run_id,kind='owner_review_task',record_key='owner:'+digest_payload(task),version=1,payload=task,digest=digest_payload(task))
        material=inspect_in_transaction(conn,run_id=run_id,dossier_key=dossier_key)['brief']
        stored,version=append_owner_material_in_transaction(conn,run_id,dossier_key,section='owner_briefs',material=material)
        result={'brief':material,'custody_version':version.version,'dossier_digest':stored.digest,'external_actions':0,'replayed':False}
    else:
        if not isinstance(decision,dict) or set(decision)-{'task_key','expected_sequence','disposition','note','evidence_refs','duplicate_of'}:
            raise WorkflowError('structured exception decision required')
        task_key=decision.get('task_key');sequence=decision.get('expected_sequence');disposition=decision.get('disposition')
        if not isinstance(task_key,str) or not task_key or len(task_key)>500: raise WorkflowError('bounded task key required')
        if isinstance(sequence,bool) or not isinstance(sequence,int) or not 0<=sequence<100: raise WorkflowError('bounded resolution sequence required')
        statuses={'acknowledge':'acknowledged','request_evidence':'evidence_requested','close_review':'closed_by_owner','reopen':'reopened','mark_duplicate':'closed_as_duplicate'}
        if not isinstance(disposition,str) or disposition not in statuses: raise WorkflowError('unknown review disposition; approval is not a supported action')
        note=decision.get('note');refs=decision.get('evidence_refs')
        if not isinstance(note,str) or not note.strip() or len(note)>1000: raise WorkflowError('bounded decision note required')
        if not isinstance(refs,list) or not 1<=len(refs)<=10 or any(not isinstance(r,str) for r in refs): raise WorkflowError('retained evidence references required')
        task=get_record_in_transaction(conn,run_id,'owner_review_task',task_key)
        if task is None or task.digest!=digest_payload(task.payload) or not _belongs(task.payload,source.payload['property_id'],did): raise WorkflowError('task does not bind this dossier')
        resolution_key='owner-resolution:'+digest_payload({'task_key':task_key})
        previous=get_record_in_transaction(conn,run_id,'research_artifact',resolution_key)
        if previous and previous.digest!=digest_payload(previous.payload): raise WorkflowError('resolution integrity failure')
        if sequence!=(previous.payload['sequence'] if previous else 0): raise WorkflowError('resolution sequence changed; inspect again')
        context,advisory=_context(conn,run_id,dossier_key,source)
        allowed={'dossier:'+source.digest,'task:'+task.digest}
        reports=list((source.payload['dossier'].get('reports') or {}).values())+list(source.payload['dossier'].get('elmer_assessments') or [])
        allowed.update('report:'+r['digest'] for r in reports if isinstance(r,dict) and r.get('digest')==digest_payload({k:v for k,v in r.items() if k!='digest'}))
        allowed.update('advisory:'+r['record_digest'] for r in advisory)
        if any(ref not in allowed for ref in refs): raise WorkflowError('decision reference is not retained in this dossier context')
        duplicate=decision.get('duplicate_of')
        if disposition=='mark_duplicate':
            other=get_record_in_transaction(conn,run_id,'owner_review_task',duplicate) if isinstance(duplicate,str) else None
            if other is None or duplicate==task_key or other.digest!=digest_payload(other.payload) or not _belongs(other.payload,source.payload['property_id'],did): raise WorkflowError('duplicate target must be a different task in this dossier')
            duplicate_state=get_record_in_transaction(conn,run_id,'research_artifact','owner-resolution:'+digest_payload({'task_key':duplicate}))
            if duplicate_state and duplicate_state.digest!=digest_payload(duplicate_state.payload): raise WorkflowError('duplicate target resolution integrity failure')
            if duplicate_state and duplicate_state.payload.get('review_status')=='closed_as_duplicate': raise WorkflowError('duplicate target must be a primary task; cycles are refused')
        elif duplicate is not None: raise WorkflowError('duplicate target is only valid for mark_duplicate')
        material={'version':'manny-owner-decision-1','task_key':task_key,'task_digest':task.digest,'dossier_key':dossier_key,
            'sequence':sequence+1,'principal':principal,'disposition':disposition,'review_status':statuses[disposition],
            'note':note,'evidence_refs':list(refs),'duplicate_of':duplicate,'evidence_context_digest':context,
            'source_dossier_digest':source.digest,'parent_resolution_digest':previous.digest if previous else None,
            'grants_permission':False,'underlying_facts_changed':False,'external_actions':0}
        material['digest']=digest_payload(material)
        stored,version=append_owner_material_in_transaction(conn,run_id,dossier_key,section='owner_decisions',material=material)
        put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=resolution_key,version=previous.version+1 if previous else 1,payload=material,digest=digest_payload(material))
        refreshed=inspect_in_transaction(conn,run_id=run_id,dossier_key=dossier_key)['brief']
        stored,version=append_owner_material_in_transaction(conn,run_id,dossier_key,section='owner_briefs',material=refreshed)
        result={'decision':material,'owner_brief_digest':refreshed['digest'],'dossier_digest':stored.digest,'custody_version':version.version,'replayed':False,'external_actions':0}
    record={'request':request,'result':result}
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=key,version=1,payload=record,digest=digest_payload(record))
    return result


def audited(*,principal,**inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId.MANNY,run_id=str(inputs['run_id']),event_type='manny.owner_review',
        summary='Administrative owner brief/decision; no business execution authority',details={'principal':principal,'operation_id':inputs['operation_id']},retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try: return mutate_in_transaction(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def prepare_on_arrival_in_transaction(conn,*,principal,run_id,dossier_key,assessment_digest,dossier_digest):
    """Internal brief preparation only; never resolves human exceptions."""
    return mutate_in_transaction(conn,principal=principal,run_id=run_id,dossier_key=dossier_key,
        operation_id='arrival-'+digest_payload({'assessment':assessment_digest,'source':dossier_digest}),expected_digest=dossier_digest,action='prepare')
