"""Narrow real-connector review: reassess retained inputs, never invent evidence.

The owner approves each immutable plan. Each step and checkpoint share a single
audited transaction. Synthetic handoff records must use the separate demo path.
"""
from copy import deepcopy
from uuid import UUID
import os

from app import wondrue_workspace as ws,manny_review,owner_work
from app.agents.stages import _load
from app.communications import instant,lock
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,create_run_in_transaction,get_run_by_key_in_transaction
from app.wondrue_insights import RETAINED_TARGETS,dependency_plan
def _assessment_content(report):
    # Runtime source bindings necessarily change at each canonical version.
    # Compare the pure assessment, never a newly minted input-version pointer.
    def clean(value):
        if isinstance(value,list):return [clean(v) for v in value]
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k not in ('source_dossier_digest','dossier_digest','rework_input_digests')}
        return value
    return clean({k:v for k,v in report.items() if k!='digest'})

PREFIX='wondrue-retained-review:'
PRINCIPAL='wondrue-operator'


def guard(conn,source=None):
    with conn.cursor() as cur:
        cur.execute('SELECT current_database() AS db,current_user AS role');identity=cur.fetchone()
    gate=identity['db']=='growup_mvp_gate' and os.getenv('GROWUP_PG_INTEGRATION')=='1'
    if identity['role']!='growup_app' or (identity['db'] not in ('growup_dev','growup_prod') and not gate):
        raise WorkflowError('retained review requires the configured operational connector; demo databases are forbidden')
    if source:
        p=source.payload;d=p.get('dossier') or {}
        if p.get('synthetic') or d.get('synthetic') or p.get('domain')=='synthetic_handoff_demo' or d.get('domain')=='synthetic_handoff_demo':
            raise WorkflowError('synthetic dossiers must use the isolated guided demo')
        if p.get('test_material') and not gate:raise WorkflowError('test fixtures are forbidden in operational review')
        if any(r.get('synthetic') or r.get('domain')=='synthetic_handoff_demo' for r in (d.get('reports') or {}).values()):
            raise WorkflowError('synthetic reports cannot establish real review inputs')


def _get(conn,review_id):
    guard(conn);key=PREFIX+str(UUID(str(review_id)));lock(conn,key)
    run=get_run_by_key_in_transaction(conn,key)
    if run is None:raise WorkflowError('retained review not found')
    state=ws.read(conn,run.run_id,'retained-review')
    if state is None:raise WorkflowError('retained review state missing')
    return run,state


def _save(conn,run,state,prior=None):return ws.write(conn,run.run_id,'retained-review',state,prior)


def prepare(conn,review_id,ref,task_key,target,note):
    guard(conn)
    review_id=str(UUID(str(review_id)));request={'source':ref,'task_key':task_key,'target':target,'note':note}
    key=PREFIX+review_id;lock(conn,key);old=get_run_by_key_in_transaction(conn,key)
    if old:
        row=ws.read(conn,old.run_id,'retained-review')
        if row.payload['request']!=request:raise WorkflowError('review identity conflict')
        return inspect(conn,review_id)
    if target not in RETAINED_TARGETS:raise WorkflowError('target cannot be reassessed by this retained-only path')
    if not isinstance(note,str) or not note.strip() or len(note)>1000:raise WorkflowError('bounded correction reason required')
    source,did=_load(conn,UUID(ref['run_id']),ref['key'],'manny');guard(conn,source)
    if source.version!=ref['version'] or source.digest!=ref['digest']:raise WorkflowError('source version changed; inspect again')
    plan=dependency_plan(source.payload['dossier'])
    target_step=next((s for s in plan['steps'] if s['agent']==target),None)
    if plan['blocked_cycle'] or (target_step and not target_step['can_preview_now']):raise WorkflowError('refresh stale upstream dependencies first')
    tasks=manny_review.queue_in_transaction(conn,run_id=source.run_id,dossier_key=ref['key'])['tasks']
    task=next((t for t in tasks if t['record_key']==task_key),None)
    if task is None or task['review_status'] in ('closed_by_owner','closed_as_duplicate'):
        raise WorkflowError('an unresolved task belonging to this dossier is required')
    from app.dossier_coordinator import _build
    from app.linked_reassessments import effective_dossier
    candidate=deepcopy(source.payload);candidate['dossier']=effective_dossier(candidate['dossier'])
    candidate['digest']=digest_payload({k:v for k,v in candidate.items() if k!='digest'})
    report,eligible,reasons=_build(target,candidate)
    manifest={'version':'retained-review-1','request':request,'task_digest':task['task_digest'],
        'assessment_content_digest':digest_payload(_assessment_content(report)),
        'preview_assessment':_assessment_content(report),'preview_report':report,'expected_status':'reassessed' if eligible else 'needs_evidence',
        'expected_reasons':reasons,'input_policy':'Retained effective inputs only; no caller facts, acquisition or authentication overrides.',
        'steps':['Record the evidence request','Return custody to the selected upstream agent',
                 'Append a linked retained-input reassessment and return the package to Manny'],
        'administrative_task_closure':False,'provider_calls':0,'external_actions':0,'grants_permission':False}
    manifest['digest']=digest_payload(manifest)
    run=create_run_in_transaction(conn,idempotency_key=key)
    state={'id':review_id,'request':request,'manifest':manifest,'status':'pending','principal':PRINCIPAL,
           'expected_source_digest':source.digest,'timeline':[],'created_at':instant().isoformat()}
    _save(conn,run,state)
    return inspect(conn,review_id)


def inspect(conn,review_id):
    run,row=_get(conn,review_id);state=deepcopy(row.payload);ref=state['request']['source']
    source,_=_load(conn,UUID(ref['run_id']),ref['key']);guard(conn,source)
    brief=manny_review.inspect_in_transaction(conn,run_id=ref['run_id'],dossier_key=ref['key'])['brief']
    return {'review':dict(state,review_digest=row.digest),'source_current_digest':source.digest,
        'source_current_version':source.version,'owner_brief':brief,'dependency_plan':dependency_plan(source.payload['dossier']),
        'provider_calls':0,'external_actions':0,'scope':'Retained-input internal reassessment; no new source authentication or professional approval.'}


def decide(conn,review_id,expected_digest,choice,note):
    if choice not in ('approve','reject','defer'):raise WorkflowError('explicit decision required')
    if not isinstance(note,str) or not note.strip() or len(note)>1000:raise WorkflowError('bounded owner decision reason required')
    run,row=_get(conn,review_id)
    if row.digest!=expected_digest or row.payload['status'] not in ('pending','deferred'):
        raise WorkflowError('review changed or already decided; refresh')
    state=deepcopy(row.payload);m=state['manifest'];ref=state['request']['source']
    if m['digest']!=digest_payload({k:v for k,v in m.items() if k!='digest'}):raise WorkflowError('approval manifest corrupt')
    if choice=='approve':
        source,_=_load(conn,UUID(ref['run_id']),ref['key'],'manny');guard(conn,source)
        if source.digest!=ref['digest'] or source.version!=ref['version']:raise WorkflowError('source changed; reject and prepare a new preview')
    state.update(status={'approve':'approved','defer':'deferred','reject':'rejected'}[choice],
                 decision=choice,decision_note=note,decided_at=instant().isoformat())
    _save(conn,run,state,row)
    return inspect(conn,review_id)


def advance(conn,review_id):
    run,row=_get(conn,review_id);state=deepcopy(row.payload);m=state['manifest'];request=state['request'];ref=request['source']
    if state['status']=='complete':return inspect(conn,review_id)
    if state['status'] not in ('approved','requested','dispatched'):raise WorkflowError('explicit approval required')
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1':raise WorkflowError('review is emergency-stopped')
    if m['digest']!=digest_payload({k:v for k,v in m.items() if k!='digest'}):raise WorkflowError('approval manifest corrupt')
    source,_=manny_review._locked(conn,UUID(ref['run_id']),ref['key']);guard(conn,source)
    if source.digest!=state['expected_source_digest']:raise WorkflowError('source changed outside this approved step; reconcile before resuming')
    op='retained-'+state['id']
    if state['status']=='approved':
        task=next((t for t in manny_review.queue_in_transaction(conn,run_id=ref['run_id'],dossier_key=ref['key'])['tasks'] if t['record_key']==request['task_key']),None)
        if not task or task['task_digest']!=m['task_digest']:raise WorkflowError('review issue changed')
        manny_review.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=ref['run_id'],dossier_key=ref['key'],
            operation_id=op+'-request',expected_digest=source.digest,action='resolve',operator_authorized=True,
            decision={'task_key':task['record_key'],'expected_sequence':task['resolution_sequence'],'disposition':'request_evidence',
                'note':'Owner-approved retained-only reassessment: '+state['decision_note'][:700],
                'evidence_refs':['task:'+task['task_digest'],'dossier:'+source.digest]})
        state['status']='requested'
    elif state['status']=='requested':
        result=owner_work.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=ref['run_id'],dossier_key=ref['key'],
            operation_id=op+'-dispatch',expected_digest=source.digest,action='dispatch',operator_authorized=True,
            inputs={'task_key':request['task_key'],'target_agent':request['target']})
        state.update(status='dispatched',job_key=result['job_key'])
    else:
        result=owner_work.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=ref['run_id'],dossier_key=ref['key'],
            operation_id=op+'-reassess',expected_digest=source.digest,action='reassess',operator_authorized=True,
            inputs={'job_key':state['job_key']})
        returned,_=_load(conn,UUID(ref['run_id']),ref['key']);report=returned.payload['dossier']['agent_reassessments'][-1]['report']
        if digest_payload(_assessment_content(report))!=m['assessment_content_digest'] or result['status']!=m['expected_status']:
            raise WorkflowError('assessment differs from approved preview; rolled back')
        state.update(status='complete',result=result,report_digest=report['digest'])
    current,_=_load(conn,UUID(ref['run_id']),ref['key'])
    state['expected_source_digest']=current.digest
    state['timeline'].append({'phase':state['status'],'version':current.version,'digest':current.digest,'at':instant().isoformat()})
    _save(conn,run,state,row)
    return inspect(conn,review_id)


def service(action,**kwargs):
    if action!='inspect' and os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1':raise WorkflowError('review is emergency-stopped')
    fn={'prepare':prepare,'inspect':inspect,'decide':decide,'advance':advance}.get(action)
    if fn is None:raise WorkflowError('unsupported retained review action')
    if action=='inspect':
        from app.properties_db import get_connection
        with get_connection() as conn:return fn(conn,**kwargs)
    return ws.transaction('retained_review_'+action,lambda conn:fn(conn,**kwargs))
