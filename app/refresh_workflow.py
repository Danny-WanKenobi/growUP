"""One dossier's owner-approved retained-input refresh; no discovery or providers."""
from copy import deepcopy
from pathlib import Path
from uuid import UUID
import hashlib,os
from app import wondrue_workspace as ws,manny_review,owner_work
from app.agents.stages import _load
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,create_run_in_transaction,get_run_by_key_in_transaction,put_record_in_transaction
from app.communications import lock,instant
from app.wondrue_retained_review import guard
from app.wondrue_insights import dependency_plan,RETAINED_TARGETS
from app.linked_reassessments import effective_dossier

PRINCIPAL='wondrue-operator'


def runtime_digest():
    root=Path(__file__).parent
    return digest_payload({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*.py'))})


def inputs_digest(source):
    d=effective_dossier(source.payload['dossier'])
    return digest_payload({'property':source.payload['property_id'],'assessment_inputs':d.get('assessment_inputs'),'specialist_inputs':d.get('specialist_inputs')})


def _get(conn,id):
    guard(conn);key='owner-refresh:'+str(UUID(str(id)));lock(conn,key);run=get_run_by_key_in_transaction(conn,key)
    row=ws.read(conn,run.run_id,'refresh-workflow') if run else None
    if row is None:raise WorkflowError('refresh workflow not found')
    return run,row


def prepare(conn,*,id,source,note):
    guard(conn);key='owner-refresh:'+str(UUID(str(id)));lock(conn,key)
    prior=get_run_by_key_in_transaction(conn,key)
    if prior:
        state=ws.read(conn,prior.run_id,'refresh-workflow').payload
        if state['source']!=source or state['note']!=note:raise WorkflowError('refresh identity conflict')
        return inspect(conn,id)
    retained,_=_load(conn,UUID(source['run_id']),source['key'],'manny');guard(conn,retained)
    if retained.digest!=source['digest'] or retained.version!=source['version']:raise WorkflowError('source changed')
    plan=dependency_plan(retained.payload['dossier']);agents=[s['agent'] for s in plan['steps']]
    if plan['blocked_cycle'] or any(a not in (*RETAINED_TARGETS,'elmer') for a in agents):raise WorkflowError('unsupported/cyclic upstream refresh requires a separate evidence decision')
    if len(agents)>8:raise WorkflowError('refresh exceeds eight stages')
    run=create_run_in_transaction(conn,idempotency_key=key)
    state={'id':str(id),'source':source,'note':ws._text(note,1000,'refresh reason'),'agents':agents,'status':'pending','expected_digest':retained.digest,
        'input_digest':inputs_digest(retained),'runtime_digest':runtime_digest(),'timeline':[],'created_at':instant().isoformat(),
        'policy':'Retained inputs only; no fact overrides. Refresh only this approved stale set in order; stop on needs-evidence. No task closure or external actions.',
        'external_actions':0,'provider_calls':0}
    ws.write(conn,run.run_id,'refresh-workflow',state);return inspect(conn,id)


def inspect(conn,id):
    run,row=_get(conn,id);return dict(row.payload,review_digest=row.digest)


def decide(conn,*,id,expected_digest,choice,note):
    run,row=_get(conn,id);s=deepcopy(row.payload)
    if row.digest!=expected_digest or s['status'] not in ('pending','deferred') or choice not in ('approve','defer','reject'):raise WorkflowError('exact pending refresh decision required')
    source,_=_load(conn,UUID(s['source']['run_id']),s['source']['key'],'manny')
    if choice=='approve' and (source.digest!=s['expected_digest'] or runtime_digest()!=s['runtime_digest']):raise WorkflowError('source/runtime changed; new preview required')
    s.update(status={'approve':'approved','defer':'deferred','reject':'rejected'}[choice],decision_note=ws._text(note,1000,'decision reason'),decided_at=instant().isoformat())
    ws.write(conn,run.run_id,'refresh-workflow',s,row);return inspect(conn,id)


def advance(conn,*,id):
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP')=='1':raise WorkflowError('refresh emergency-stopped')
    run,row=_get(conn,id);s=deepcopy(row.payload)
    if s['status']=='complete':return inspect(conn,id)
    if s['status']!='approved':raise WorkflowError('approved refresh required; blocked evidence requires a new decision')
    ref=s['source'];rid=UUID(ref['run_id']);source,_=manny_review._locked(conn,rid,ref['key']);_load(conn,rid,ref['key'],'manny')
    if source.digest!=s['expected_digest'] or inputs_digest(source)!=s['input_digest'] or runtime_digest()!=s['runtime_digest']:raise WorkflowError('approved source, inputs or runtime changed; reconcile before recovery')
    plan=dependency_plan(source.payload['dossier']);remaining=[a for a in s['agents'] if a in plan['stale']]
    if set(plan['stale'])-set(s['agents']) or plan['blocked_cycle']:raise WorkflowError('refresh scope expanded; new review required')
    if remaining:
        target=remaining[0];step=next(p for p in plan['steps'] if p['agent']==target)
        if not step['can_preview_now']:raise WorkflowError('refresh upstream dependencies first')
        op='refresh-'+s['id']+'-'+target;task_key=op+'-task'
        task={'property_id':source.payload['property_id'],'agent':target,'status':'pending','authority':'human_owner','grants_permission':False,
            'requirement':'Owner-approved retained dependency refresh: '+target,'reason':s['note']}
        put_record_in_transaction(conn,rid,kind='owner_review_task',record_key=task_key,version=1,payload=task,digest=digest_payload(task))
        t=next(t for t in manny_review.queue_in_transaction(conn,run_id=rid,dossier_key=ref['key'])['tasks'] if t['record_key']==task_key)
        def current():return _load(conn,rid,ref['key'])[0]
        manny_review.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=rid,dossier_key=ref['key'],operation_id=op+'-request',expected_digest=current().digest,
            action='resolve',operator_authorized=True,decision={'task_key':task_key,'expected_sequence':t['resolution_sequence'],'disposition':'request_evidence',
            'note':s['decision_note'],'evidence_refs':['task:'+t['task_digest']]})
        job=owner_work.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=rid,dossier_key=ref['key'],operation_id=op+'-dispatch',expected_digest=current().digest,
            action='dispatch',operator_authorized=True,inputs={'task_key':task_key,'target_agent':target})
        inputs={'job_key':job['job_key']}
        if target=='elmer':inputs['correction']={'reason':'Retained accounting dependency refresh; no new transactions','inputs':deepcopy(effective_dossier(current().payload['dossier'])['specialist_inputs']['elmer'])}
        result=owner_work.mutate_in_transaction(conn,principal=PRINCIPAL,run_id=rid,dossier_key=ref['key'],operation_id=op+'-reassess',expected_digest=current().digest,
            action='reassess',operator_authorized=True,inputs=inputs)
        source=current();s['timeline'].append({'agent':target,'result':result,'source_digest':source.digest,'at':instant().isoformat()})
        if result['status']!='reassessed':s['status']='blocked_needs_evidence'
    if s['status']=='approved' and not dependency_plan(source.payload['dossier'])['stale']:s['status']='complete'
    s['expected_digest']=source.digest
    ws.write(conn,run.run_id,'refresh-workflow',s,row);return inspect(conn,id)
