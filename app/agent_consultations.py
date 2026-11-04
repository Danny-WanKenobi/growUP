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
    if (not isinstance(message,dict) or set(message)-{'speaker','text','recommendation','evidence_refs','assessment_requested'}
            or message.get('speaker') not in (requester,adviser)
            or not isinstance(message.get('text'),str) or not message['text'].strip() or len(message['text'])>1500
            or message.get('recommendation') not in ('proceed_conditionally','needs_evidence','hold')):
        raise WorkflowError('structured bounded advisory message required')
    if 'assessment_requested' in message and (not isinstance(message['assessment_requested'],bool) or message['speaker']!=requester): raise WorkflowError('only a requester may ask for a structured assessment')
    if message.get('assessment_requested') and expected_sequence>=99: raise WorkflowError('insufficient thread capacity for request and adviser reply')
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
    from app.manny_review import evidence_context
    if not prior: state['source_context_digest']=evidence_context(source.payload)
    screening=None
    if message.get('assessment_requested'):
        screening=assess_in_transaction(conn,principal=principal,run_id=run_id,dossier_key=dossier_key,requester=requester,adviser=adviser,expected_digest=source.digest)
        state['latest_assessment']={'report_digest':screening['report']['digest'],'status':screening['joint_status'],'source_context_digest':screening['source_context_digest']}
    state['messages'].append({'request':deepcopy(request),'evidence_status':'supplied_unverified'})
    state['sequence']+=1
    opinions={}
    for item in state['messages']: opinions[item['request']['message']['speaker']]=item['request']['message']['recommendation']
    stale=(state['source_context_digest']!=evidence_context(source.payload)) if state.get('source_context_digest') else state['source_dossier_digest']!=source.digest
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
    if screening:
        assessment=screening['report']['advisory_assessment']
        issues=screening['report']['missing']+assessment['review_issues']
        recommendation='needs_evidence' if screening['report']['missing'] else 'hold' if assessment['review_issues'] else 'proceed_conditionally'
        reply={'speaker':adviser,'text':('Structured retained-evidence screening: '+assessment['status']+'. '+('; '.join(issues[:5]) if issues else 'No screening gaps in supplied records; not professional approval.'))[:1500],
            'recommendation':recommendation,'evidence_refs':['advisory-screen:'+screening['report']['digest']]}
        return exchange_in_transaction(conn,principal=principal,run_id=run_id,dossier_key=dossier_key,thread_id=thread_id,requester=requester,adviser=adviser,expected_sequence=state['sequence'],message=reply)
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


def assess_in_transaction(conn,*,principal,run_id,dossier_key,requester,adviser,expected_digest):
    """Immediate, idempotent retained-evidence screen; no transfer or authority."""
    import os
    from app.agents.stages import build_report
    from app import manny_review
    from app.mvp import _require_enabled
    _require_enabled()
    if not isinstance(principal,str) or not principal.strip(): raise WorkflowError('authenticated adviser principal required')
    if requester not in (*PIPELINE_IDS,*ADVISER_IDS) or adviser not in ADVISER_IDS or requester==adviser: raise WorkflowError('known requesting agent and shared adviser required')
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('adviser assessment emergency-stopped')
    run_id=UUID(str(run_id));source,did=manny_review._locked(conn,run_id,dossier_key)
    if source.digest!=expected_digest: raise WorkflowError('adviser source dossier changed')
    context=manny_review.evidence_context(source.payload)
    key='advisory:screen:'+digest_payload(dict(dossier_key=dossier_key,principal=principal,requester=requester,adviser=adviser,context=context))
    prior=get_record_in_transaction(conn,run_id,'research_artifact',key)
    if prior:
        report=prior.payload.get('report') or {}
        if (prior.digest!=digest_payload(prior.payload) or prior.payload.get('source_context_digest')!=context
            or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'})
            or prior.payload.get('principal')!=principal or prior.payload.get('requester')!=requester or prior.payload.get('adviser')!=adviser): raise WorkflowError('adviser assessment integrity failure')
        return {**prior.payload,'replayed':True}
    report=build_report(adviser,source.payload);assessment=report['advisory_assessment']
    issues=sorted(set(report['missing']+assessment['review_issues']))
    result=dict(principal=principal,requester=requester,adviser=adviser,dossier_key=dossier_key,source_dossier_digest=source.digest,
        source_context_digest=context,report=report,joint_status='assessment_requires_review' if issues else 'preliminary_assessment_available',
        custody_transferred=False,grants_permission=False,external_actions=0,provider_calls=0)
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=key,version=1,payload=result,digest=digest_payload(result))
    for issue in issues:
        task_key='owner-adviser:'+digest_payload(dict(dossier_id=str(did),adviser=adviser,issue=issue,context=context))
        if get_record_in_transaction(conn,run_id,'owner_review_task',task_key) is None:
            task=dict(agent=requester,adviser=adviser,property_id=source.payload['property_id'],dossier_id=str(did),requirement='adviser:'+issue,
                reason='Retained-evidence advisory screening requires review, not permission to act.',status='pending',authority='human_owner',grants_permission=False,assessment_digest=report['digest'])
            put_record_in_transaction(conn,run_id,kind='owner_review_task',record_key=task_key,version=1,payload=task,digest=digest_payload(task))
    return {**result,'replayed':False}


def assess_audited(*,principal,**inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId(inputs['adviser']),run_id=str(inputs['run_id']),event_type='agents.advisory_assessment',summary='Structured retained-evidence adviser screening; no authority or custody transfer',details={'requester':inputs['requester']},retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try: return assess_in_transaction(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)
