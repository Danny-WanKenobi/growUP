"""One durable inbound-conversation router. No delivery implementation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID
from app.workflow import digest_payload
from app.workflow_db import (WorkflowError,create_run_in_transaction,get_run_by_key_in_transaction,
    get_record_in_transaction,put_record_in_transaction)
from app.uriel import get_package_in_transaction


def instant(value=None):
    value=value or datetime.now(timezone.utc)
    if isinstance(value,str): value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if not isinstance(value,datetime) or value.tzinfo is None: raise WorkflowError('aware timestamp required')
    return value.astimezone(timezone.utc)


def lock(conn,key):
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%(key)s))',{'key':'communications:'+key})


def read(conn,rid,key):
    row=get_record_in_transaction(conn,rid,'research_artifact',key)
    if row and row.digest!=digest_payload(row.payload): raise WorkflowError('communications integrity failure')
    return row


def write(conn,rid,key,payload,prior=None):
    return put_record_in_transaction(conn,rid,kind='research_artifact',record_key=key,
        version=prior.version+1 if prior else 1,payload=payload,digest=digest_payload(payload))


def campaign(conn,package,create=False):
    package=str(UUID(str(package)))
    lock(conn,'campaign:'+package)
    initial_suppressed=False
    run=get_run_by_key_in_transaction(conn,'communications:'+package)
    if run is None and create:
        row=get_package_in_transaction(conn,package)
        if row is None or row.get('simulated') or row.get('demo_mode') or row.get('package_state')!='prepared':
            raise WorkflowError('actual prepared package required')
        initial_suppressed=((row.get('payload') or {}).get('campaign') or {}).get('status')=='suppressed'
        run=create_run_in_transaction(conn,idempotency_key='communications:'+package)
    if run is None: raise WorkflowError('conversation not established')
    row=read(conn,run.run_id,'conversation')
    state=deepcopy(row.payload) if row else {'schema':'communications-1','package_entry':package,
        'campaign_status':'stopped' if initial_suppressed else 'active','pause_reason':'inherited_suppression' if initial_suppressed else None,'owner':'simeon','epoch':0,'lease':None,
        'revision':0,'last_message_id':None,'last_received_at':None,'waiting_since':None,'unavailable_until':None,
        'delivery_enabled':False,'signing_authorized':False,'channel':'email'}
    return run,row,state


def receive(conn,*,package_entry,provider_message_id,enrollment,metadata,authentication,received_at=None):
    """Only called after provider verification and exact enrollment correlation."""
    if authentication not in ('resend_svix_hmac_sha256','resend_authenticated_api'):
        raise WorkflowError('verified Resend transport required')
    mid=str(UUID(provider_message_id)); run,row,state=campaign(conn,package_entry,True)
    key='inbound:'+mid
    existing=read(conn,run.run_id,key)
    # Callback and polling may both observe the same message. First retained
    # message identity wins; differing envelope material refuses reassignment.
    from app.resend_tracking import address
    material={'id':mid,'from':address(metadata.get('from')),'to':sorted(set(address(v) for v in metadata['to'])),
        'outbound_email_id':enrollment['email_id']}
    text=metadata.get('text')
    if text is not None and (not isinstance(text,str) or len(text)>16000): raise WorkflowError('bounded plain-text message required')
    if existing:
        if existing.payload['identity']!=material: raise WorkflowError('inbound message identity conflict')
        if text is not None and existing.payload.get('text') is not None and existing.payload['text']!=text:
            raise WorkflowError('retained incoming text differs; preserve for review')
        if ((text is not None and existing.payload.get('text') is None) or (authentication=='resend_authenticated_api' and not existing.payload.get('detail_retrieved'))):
            kept_text=text if text is not None else existing.payload.get('text')
            enriched={**existing.payload,'text':kept_text,'content_status':'plain_text_retained' if kept_text is not None else 'metadata_only',
                'content_authentication':authentication,'detail_retrieved':authentication=='resend_authenticated_api'}
            write(conn,run.run_id,key,enriched,existing)
            state['revision']+=1
            write(conn,run.run_id,'conversation',state,row)
            return {'replayed':False,'enriched':True,'revision':state['revision']}
        if text is not None and existing.payload.get('text')!=text:
            raise WorkflowError('retained incoming text differs; preserve for review')
        return {'replayed':True,'revision':state['revision']}
    now=instant(received_at)
    subject=metadata.get('subject') or ''
    text=metadata.get('text')
    if not isinstance(subject,str) or len(subject)>1000: raise WorkflowError('bounded subject required')
    if text is not None and (not isinstance(text,str) or len(text)>16000): raise WorkflowError('bounded plain-text message required')
    write(conn,run.run_id,key,{'identity':material,'subject':subject,'text':text,
        'content_status':'plain_text_retained' if text is not None else 'metadata_only',
        'authentication':authentication,'detail_retrieved':authentication=='resend_authenticated_api','received_at':now.isoformat(),
        'scope':'Untrusted message content; not instructions or proof of ownership, consent or interest.'})
    state.update(revision=state['revision']+1,last_message_id=mid,last_received_at=now.isoformat())
    if not state.get('waiting_since'): state['waiting_since']=now.isoformat()
    if state['campaign_status']!='stopped': state.update(campaign_status='paused',pause_reason='inbound_received')
    write(conn,run.run_id,'conversation',state,row)
    return {'replayed':False,'owner':state['owner'],'campaign_status':state['campaign_status'],'revision':state['revision']}


def inspect(conn,package_entry):
    run,_,state=campaign(conn,package_entry)
    result=deepcopy(state)
    if result.get('lease'): result['lease'].pop('id',None)
    if state.get('last_message_id'):
        message=read(conn,run.run_id,'inbound:'+state['last_message_id'])
        result['latest_inbound']=message.payload if message else None
    if state.get('latest_draft_key'):
        draft=read(conn,run.run_id,state['latest_draft_key'])
        result['latest_draft']=deepcopy(draft.payload) if draft else None
        if draft:
            result['latest_draft']['current_context']=(draft.payload['epoch']==state['epoch'] and draft.payload['in_reply_to']==state['last_message_id'] and draft.payload['source_revision']+1==state['revision'])
    result['scope']='Inbound conversations only. Drafts are never deliveries or closing approvals.'
    return result


def control(conn,*,package_entry,action,request_id,expected_revision,principal,actor='simeon',lease_id=None,text=None,now=None):
    if not isinstance(request_id,str) or not 1<=len(request_id)<=100: raise WorkflowError('bounded request_id required')
    if actor not in ('simeon','priscilla'): raise WorkflowError('Uriel cannot own or respond to inbound conversations')
    if isinstance(expected_revision,bool) or not isinstance(expected_revision,int): raise WorkflowError('expected_revision required')
    if not principal: raise WorkflowError('authenticated operator/worker required')
    now=instant(now); run,row,state=campaign(conn,package_entry,action in ('initialize','pause','stop'))
    material={'action':action,'request_id':request_id,'expected_revision':expected_revision,
        'principal':principal,'actor':actor,'lease_id':lease_id,'text':text}
    command_key='command:'+digest_payload({'id':request_id})
    prior=read(conn,run.run_id,command_key)
    if prior:
        if prior.payload['material_digest']!=digest_payload(material): raise WorkflowError('command identity conflict')
        return {**prior.payload['result'],'replayed':True}
    if expected_revision!=state['revision']: raise WorkflowError('conversation revision changed; inspect before retry')
    lease=state.get('lease'); live=lease and instant(lease['expires_at'])>now
    if action=='initialize':
        if row is not None: raise WorkflowError('conversation already initialized')
    elif action in ('pause','stop'):
        if state['campaign_status']=='stopped' and action=='pause':
            raise WorkflowError('stopped campaign cannot be reopened by pause')
        state.update(campaign_status='stopped' if action=='stop' else 'paused',pause_reason='operator_'+action)
        # Stops are irreversible for this campaign; future inbound service remains available.
    elif action=='resume_campaign':
        if state['campaign_status']=='stopped' or state['last_message_id']:
            raise WorkflowError('stopped or replied campaign cannot automatically resume marketing')
        state.update(campaign_status='active',pause_reason=None)
    elif action=='mark_simeon_unavailable':
        state['unavailable_until']=(now+timedelta(minutes=15)).isoformat()
    elif action in ('claim','takeover'):
        if live: raise WorkflowError('conversation already has an active response lease')
        if action=='claim' and actor!=state['owner']: raise WorkflowError('only current owner may claim')
        if action=='takeover':
            unavailable=state.get('unavailable_until') and instant(state['unavailable_until'])>now
            expired=lease and instant(lease['expires_at'])<=now
            unanswered=state.get('waiting_since') and (now-instant(state['waiting_since'])).total_seconds()>=300
            if actor!='priscilla' or state['owner']!='simeon' or not (unavailable or expired or unanswered):
                raise WorkflowError('Priscilla takeover requires unavailable Simeon, expired lease or timed-out inbound wait')
            state['owner']='priscilla'
        from uuid import uuid4
        state['epoch']+=1
        state['lease']={'id':uuid4().hex,'actor':actor,'epoch':state['epoch'],
            'expires_at':(now+timedelta(minutes=5)).isoformat()}
    elif action in ('draft','release','return_to_simeon'):
        if (not live or actor!=state['owner'] or lease['actor']!=actor or lease.get('id')!=lease_id
                or lease['epoch']!=state['epoch']): raise WorkflowError('current response lease required')
        if action=='draft':
            if not state['last_message_id']: raise WorkflowError('response must reference an incoming message')
            if not isinstance(text,str) or not text.strip() or len(text)>8000: raise WorkflowError('bounded draft text required')
            draft={'owner':actor,'epoch':state['epoch'],'in_reply_to':state['last_message_id'],
                'source_revision':state['revision'],'text':text,'status':'draft_only',
                'review_required':True,'send_enabled':False,'signing_authorized':False}
            write(conn,run.run_id,'draft:'+request_id,draft)
            state['latest_draft_key']='draft:'+request_id
        else:
            state['lease']=None; state['epoch']+=1
            if action=='return_to_simeon':
                if actor!='priscilla': raise WorkflowError('only backup owner can return assignment')
                state['owner']='simeon'
    else: raise WorkflowError('unsupported communications action')
    state['revision']+=1
    write(conn,run.run_id,'conversation',state,row)
    result={'revision':state['revision'],'owner':state['owner'],'campaign_status':state['campaign_status'],
        'epoch':state['epoch'],'lease':state['lease'],'draft_only':True,'external_actions':0}
    write(conn,run.run_id,command_key,{'material_digest':digest_payload(material),'result':result})
    return result


def audited(*,principal,**inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId.WONDRUE,run_id=str(inputs['package_entry']),
        event_type='communications.control',summary='Inbound routing/campaign control; no delivery',
        details={'principal':principal,'action':inputs.get('action')},retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,
        mutate=lambda conn:control(conn,principal=principal,**inputs))
