"""Isolated full owner cycle, using canonical services and fictional inputs only."""
from copy import deepcopy
import os
from uuid import UUID,uuid5,NAMESPACE_URL
from app import guided_demo_review as guided,manny_review,owner_work,demo_owner_story
from app.agents.stages import _load
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,put_record_in_transaction
from app.wondrue_insights import dependency_plan
from app.linked_reassessments import effective_dossier


def step(conn,case_id):
    """One atomic phase; caller commits. Replays preserve the exact final proof."""
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP')=='1':raise WorkflowError('operating rehearsal emergency-stopped')
    guided._guard(conn)
    cid=str(UUID(str(case_id)))
    guided.create_in_transaction(conn,cid,scenario_id='elm-fork')
    run=guided._run(conn,cid)
    prior=guided._read(conn,run.run_id,'operating-rehearsal')
    state=deepcopy(prior.payload) if prior else {'phase':'created','sequence':0,'timeline':[],
        'synthetic':True,'domain':'synthetic_handoff_demo','case_id':cid,'run_key':run.idempotency_key}
    if state['phase']=='complete':return state
    def source():return _load(conn,run.run_id,'main','manny')[0]
    if state['phase']=='created':
        initial=guided.inspect_in_transaction(conn,cid)
        rid=str(uuid5(NAMESPACE_URL,'operating-rehearsal:'+cid))
        preview=guided.prepare_in_transaction(conn,cid,rid,'supplied_update',initial['source_digest'])
        guided.decide_in_transaction(conn,cid,rid,preview['review']['review_digest'],'approve','SYNTHETIC scripted owner decision; no live authority')
        state.update(phase='correction_approved',review_id=rid)
    elif state['phase']=='correction_approved':
        for _ in range(3):result=guided.advance_in_transaction(conn,cid,state['review_id'])
        if result['review']['status']!='complete' or not result['original_preserved']:raise WorkflowError('correction proof failed')
        state.update(phase='refreshing',original_preserved=True)
    elif state['phase']=='refreshing':
        plan=dependency_plan(source().payload['dossier'])
        if plan['blocked_cycle']:raise WorkflowError('rehearsal dependency cycle')
        if plan['steps']:
            target=plan['steps'][0]['agent']
            if target not in ('theresia_keys','simeon','solomon','elmer'):raise WorkflowError('unexpected rehearsal stale target: '+target)
            op='rehearsal-'+target;task_key=op+'-task';src=source()
            task={'agent':target,'property_id':src.payload['property_id'],'requirement':'SYNTHETIC refresh retained dependencies for '+target,
                'status':'pending','authority':'human_owner','grants_permission':False,'synthetic':True,'domain':'synthetic_handoff_demo'}
            put_record_in_transaction(conn,run.run_id,kind='owner_review_task',record_key=task_key,version=1,payload=task,digest=digest_payload(task))
            t=next(t for t in manny_review.queue_in_transaction(conn,run_id=run.run_id,dossier_key='main')['tasks'] if t['record_key']==task_key)
            manny_review.mutate_in_transaction(conn,principal=guided.PRINCIPAL,run_id=run.run_id,dossier_key='main',operation_id=op+'-request',
                expected_digest=source().digest,action='resolve',operator_authorized=True,
                decision={'task_key':task_key,'expected_sequence':t['resolution_sequence'],'disposition':'request_evidence',
                    'note':'SYNTHETIC fixture decision to refresh retained inputs only','evidence_refs':['task:'+t['task_digest']]})
            dispatched=owner_work.mutate_in_transaction(conn,principal=guided.PRINCIPAL,run_id=run.run_id,dossier_key='main',operation_id=op+'-dispatch',expected_digest=source().digest,
                action='dispatch',operator_authorized=True,inputs={'task_key':task_key,'target_agent':target})
            held=_load(conn,run.run_id,'main')[0];inputs={'job_key':dispatched['job_key']}
            if target=='elmer':inputs['correction']={'reason':'SYNTHETIC linked retained dependency refresh',
                'inputs':deepcopy(effective_dossier(held.payload['dossier'])['specialist_inputs']['elmer'])}
            outcome=owner_work.mutate_in_transaction(conn,principal=guided.PRINCIPAL,run_id=run.run_id,dossier_key='main',operation_id=op+'-reassess',expected_digest=held.digest,
                action='reassess',operator_authorized=True,inputs=inputs)
            if outcome['status']!='reassessed':raise WorkflowError('fictional retained inputs remain incomplete: '+target)
            state.setdefault('refreshed',[]).append(target)
        else:state['phase']='lifecycle_ready'
    else:
        result=demo_owner_story.exercise(conn,run,full=True)
        if result['result']!='PASS' or not result['vault_preserved_after_new_evidence']:raise WorkflowError('owner lifecycle proof failed')
        state.update(phase='complete',owner_lifecycle=result,provider_calls=0,operational_writes=0,external_actions=0)
    current=_load(conn,run.run_id,'main')[0]
    state['sequence']+=1
    state['timeline'].append({'phase':state['phase'],'dossier_version':current.version,'digest':current.digest})
    guided._write(conn,run.run_id,'operating-rehearsal',state,prior)
    return state


def run(case_id):
    from app.properties_db import get_connection
    from app.handoff_demo_domain import scope
    cfg,key=guided.demo.configuration()
    for _ in range(12):
        with get_connection(cfg) as conn:
            with scope(conn,key):
                state=step(conn,case_id)
                from app.observability.events import AgentEvent,AgentId,RetentionClass
                from app.observability.store import insert_event_in_transaction
                insert_event_in_transaction(conn,AgentEvent(agent=AgentId.WONDRUE,run_id=state['run_key'],event_type='demo.operating_rehearsal',
                    summary='SYNTHETIC owner operating rehearsal',details={'phase':state['phase']},retention_class=RetentionClass.PERMANENT))
                conn.commit()
        if state['phase']=='complete':return state
    raise WorkflowError('bounded rehearsal did not complete')
