"""Immediate internal two-way adviser exchange; no custody or external actions."""
from copy import deepcopy
from uuid import UUID
from app.agents.registry import PIPELINE_IDS,ADVISER_IDS
from app.agents.stages import _load
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction


def exchange_in_transaction(conn,*,principal,run_id,dossier_key,thread_id,requester,adviser,expected_sequence,message):
    import os
    from app.mvp import _require_enabled
    _require_enabled()
    if not isinstance(principal,str) or not principal.strip(): raise WorkflowError('authenticated internal principal required')
    if requester not in (*PIPELINE_IDS,*ADVISER_IDS,'wondrue') or adviser not in ADVISER_IDS or requester==adviser: raise WorkflowError('known requester and shared adviser required')
    if not isinstance(thread_id,str) or not thread_id.strip() or len(thread_id)>100: raise WorkflowError('bounded thread_id required')
    if isinstance(expected_sequence,bool) or not isinstance(expected_sequence,int) or expected_sequence<0: raise WorkflowError('expected sequence required')
    if (not isinstance(message,dict) or set(message)-{'speaker','text','recommendation','evidence_refs'}
            or message.get('speaker') not in (requester,adviser)
            or not isinstance(message.get('text'),str) or not message['text'].strip() or len(message['text'])>1500
            or message.get('recommendation') not in ('proceed_conditionally','needs_evidence','hold')):
        raise WorkflowError('structured bounded advisory message required')
    refs=message.get('evidence_refs')
    if not isinstance(refs,list) or not 1<=len(refs)<=10 or any(not isinstance(r,str) or not r.strip() or len(r)>500 for r in refs):
        raise WorkflowError('retained evidence references required')
    run_id=UUID(str(run_id))
    from app.custody import dossier_id_for_property
    from app.custody_db import lock_dossier_in_transaction
    identity=get_record_in_transaction(conn,run_id,'dossier_version',dossier_key)
    if identity is None: raise WorkflowError('stored dossier not found')
    lock_dossier_in_transaction(conn,dossier_id_for_property(identity.payload.get('property_id')))
    with conn.cursor() as cur: cur.execute('SELECT pg_advisory_xact_lock(hashtext(%(key)s))',{'key':'advisory:'+str(run_id)+':'+thread_id})
    source,did=_load(conn,run_id,dossier_key)
    key='advisory:'+digest_payload({'dossier_key':dossier_key,'thread_id':thread_id})
    prior=get_record_in_transaction(conn,run_id,'research_artifact',key)
    request={'principal':principal,'requester':requester,'adviser':adviser,'expected_sequence':expected_sequence,'message':message}
    if prior:
        if prior.digest!=digest_payload(prior.payload): raise WorkflowError('advisory integrity failure')
        state=deepcopy(prior.payload)
        if state['principal']!=principal or state['requester']!=requester or state['adviser']!=adviser: raise WorkflowError('advisory identity conflict')
        if expected_sequence<state['sequence']:
            entry=state['messages'][expected_sequence]
            if entry['request']!=request: raise WorkflowError('advisory replay conflict')
            return {**state,'replayed':True,'external_actions':0}
    else:
        state={'sequence':0,'principal':principal,'requester':requester,'adviser':adviser,
            'source_dossier_digest':source.digest,'messages':[],'thread_id':thread_id,'dossier_key':dossier_key}
        if message['speaker']!=requester: raise WorkflowError('requesting agent opens the consultation')
    if expected_sequence!=state['sequence'] or expected_sequence>=100: raise WorkflowError('advisory sequence mismatch or bounded thread full')
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('internal consultation emergency-stopped')
    state['messages'].append({'request':deepcopy(request),'evidence_status':'supplied_unverified'})
    state['sequence']+=1
    opinions={}
    for item in state['messages']: opinions[item['request']['message']['speaker']]=item['request']['message']['recommendation']
    stale=state['source_dossier_digest']!=source.digest
    if stale: joint='context_changed_requires_new_review'
    elif len(opinions)<2: joint='awaiting_reply'
    elif len(set(opinions.values()))==1: joint='aligned_advisory_'+opinions[requester]
    else: joint='disagreement_owner_review'
    state.update(joint_status=joint,previous_record_digest=prior.digest if prior else None,
        custody_transferred=False,grants_permission=False,external_actions=0,
        scope='Internal supplied advice only; no legal opinion, insurance binder, pipeline approval or provider call.')
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=key,version=prior.version+1 if prior else 1,
        payload=state,digest=digest_payload(state))
    if joint in ('disagreement_owner_review','context_changed_requires_new_review'):
        task={'agent':requester,'adviser':adviser,'dossier_id':str(did),'property_id':source.payload['property_id'],
            'requirement':'consultation:'+thread_id,'reason':joint,'status':'pending','authority':'human_owner',
            'grants_permission':False,'consultation_digest':digest_payload(state)}
        put_record_in_transaction(conn,run_id,kind='owner_review_task',record_key='owner:'+digest_payload(task),version=1,payload=task,digest=digest_payload(task))
    return {**state,'replayed':False}


def inspect_in_transaction(conn,*,run_id,dossier_key,thread_id):
    run_id=UUID(str(run_id));_load(conn,run_id,dossier_key)
    key='advisory:'+digest_payload({'dossier_key':dossier_key,'thread_id':thread_id})
    row=get_record_in_transaction(conn,run_id,'research_artifact',key)
    if row is None: return {'thread':None,'records_written':0}
    if row.digest!=digest_payload(row.payload): raise WorkflowError('advisory integrity failure')
    return {'thread':row.payload,'records_written':0}


def exchange_audited(*,principal,**inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId.WONDRUE,run_id=str(inputs['run_id']),event_type='agents.advisory_exchange',
        summary='Internal adviser exchange; no custody or authority change',details={'principal':principal,'thread_id':inputs['thread_id']},retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try: return exchange_in_transaction(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)
