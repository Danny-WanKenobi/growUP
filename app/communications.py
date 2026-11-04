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
    if state['owner']!='simeon' or (state.get('lease') and not state['lease'].get('worker_id')):
        state.update(owner='simeon',lease=None,epoch=state['epoch']+1,legacy_backup_retired=True)
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
    pending=state.get('pending_message_ids',([state['last_message_id']] if state['last_message_id'] else []))
    if len(pending)>=100: raise WorkflowError('inbound queue limit reached; preserve provider item for retry')
    state.update(revision=state['revision']+1,last_message_id=mid,last_received_at=now.isoformat(),pending_message_ids=pending+[mid])
    if not state.get('waiting_since'): state['waiting_since']=now.isoformat()
    if state['campaign_status']!='stopped': state.update(campaign_status='paused',pause_reason='inbound_received')
    write(conn,run.run_id,'conversation',state,row)
    return {'replayed':False,'owner':state['owner'],'campaign_status':state['campaign_status'],'revision':state['revision']}


def inspect(conn,package_entry):
    run,_,state=campaign(conn,package_entry)
    result=deepcopy(state)
    if result['owner']!='simeon':
        result.update(historical_owner=result['owner'],owner='simeon',legacy_backup_retired=True,lease=None)
    if result.get('lease'): result['lease'].pop('id',None)
    if state.get('last_message_id'):
        message=read(conn,run.run_id,'inbound:'+state['last_message_id'])
        result['latest_inbound']=message.payload if message else None
    if state.get('latest_draft_key'):
        draft=read(conn,run.run_id,state['latest_draft_key'])
        result['latest_draft']=deepcopy(draft.payload) if draft else None
        if draft:
            result['latest_draft']['current_context']=(draft.payload['epoch']==state['epoch'] and draft.payload['in_reply_to'] in state.get('pending_message_ids',[state['last_message_id']]) and state.get('lease') is not None and state['lease'].get('message_id')==draft.payload['in_reply_to'])
    result['scope']='Inbound conversations only. Drafts are never deliveries or closing approvals.'
    return result


MAX_SIMEON_WORKERS=4


def worker_pool(conn, now):
    # Lock order is pool -> campaign for all control calls. Receive never takes pool.
    lock(conn,'simeon-worker-pool')
    run=get_run_by_key_in_transaction(conn,'simeon-worker-pool')
    if run is None: run=create_run_in_transaction(conn,idempotency_key='simeon-worker-pool')
    row=read(conn,run.run_id,'workers')
    slots=deepcopy(row.payload['slots']) if row else {}
    slots={key:value for key,value in slots.items() if instant(value['expires_at'])>now}
    return run,row,slots


def control(conn,*,package_entry,action,request_id,expected_revision,principal,actor='simeon',lease_id=None,text=None,now=None,worker_id=None):
    if not isinstance(request_id,str) or not 1<=len(request_id)<=100: raise WorkflowError('bounded request_id required')
    if actor!='simeon': raise WorkflowError('Only Simeon owns inbound conversations; Priscilla and Uriel cannot respond')
    if action in ('takeover','return_to_simeon','mark_simeon_unavailable'): raise WorkflowError('backup takeover is retired; delegate to a Simeon worker')
    if isinstance(expected_revision,bool) or not isinstance(expected_revision,int): raise WorkflowError('expected_revision required')
    if not principal: raise WorkflowError('authenticated operator/worker required')
    now=instant(now); pool_run,pool_row,slots=worker_pool(conn,now)
    run,row,state=campaign(conn,package_entry,action in ('initialize','pause','stop'))
    material={'action':action,'request_id':request_id,'expected_revision':expected_revision,
        'principal':principal,'actor':actor,'lease_id':lease_id,'text':text,'worker_id':worker_id}
    command_key='command:'+digest_payload({'id':request_id})
    prior=read(conn,run.run_id,command_key)
    if prior:
        if prior.payload['material_digest']!=digest_payload(material): raise WorkflowError('command identity conflict')
        return {**prior.payload['result'],'replayed':True}
    if expected_revision!=state['revision']: raise WorkflowError('conversation revision changed; inspect before retry')
    import os
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1' and action not in ('stop','pause','release'):
        raise WorkflowError('Simeon worker dispatch emergency-stopped')
    if state['owner']!='simeon' or (state.get('lease') and not state['lease'].get('worker_id')):
        state.update(owner='simeon',lease=None,epoch=state['epoch']+1,legacy_backup_retired=True)
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
    elif action=='claim':
        if live: raise WorkflowError('conversation already has an active response lease')
        if worker_id is not None and worker_id not in tuple('simeon:'+str(i) for i in range(1,MAX_SIMEON_WORKERS+1)):
            raise WorkflowError('unknown bounded Simeon worker')
        available=[key for key in ('simeon:'+str(i) for i in range(1,MAX_SIMEON_WORKERS+1)) if key not in slots]
        chosen=worker_id or (available[0] if available else None)
        if chosen not in available: raise WorkflowError('Simeon worker capacity occupied; inbound remains queued')
        from uuid import uuid4
        state['epoch']+=1
        pending=state.get('pending_message_ids',([state['last_message_id']] if state['last_message_id'] else []))
        if pending and state.get('latest_draft_key'):
            retained=read(conn,run.run_id,state['latest_draft_key'])
            if retained and retained.payload.get('in_reply_to')==pending[0] and retained.payload.get('status')=='draft_only':
                pending=pending[1:];state['pending_message_ids']=pending
        state['lease']={'id':uuid4().hex,'actor':'simeon','worker_id':chosen,'epoch':state['epoch'],
            'message_id':pending[0] if pending else None,'expires_at':(now+timedelta(minutes=5)).isoformat()}
        slots[chosen]={'package_entry':str(package_entry),'lease_id':state['lease']['id'],
            'expires_at':state['lease']['expires_at'],'principal':principal}
    elif action in ('draft','release'):
        if (not live or actor!=state['owner'] or lease['actor']!=actor or lease.get('id')!=lease_id
                or lease['epoch']!=state['epoch'] or not lease.get('worker_id')
                or slots.get(lease['worker_id'],{}).get('lease_id')!=lease_id
                or slots.get(lease['worker_id'],{}).get('principal')!=principal
                or (worker_id is not None and worker_id!=lease['worker_id'])): raise WorkflowError('current response lease required')
        if action=='draft':
            from app.personalities import require_channel,VERSION
            if not lease.get('message_id'): raise WorkflowError('response must reference an incoming message')
            require_channel(actor,'email','outbound',human_initiated=True)
            if not isinstance(text,str) or not text.strip() or len(text)>8000: raise WorkflowError('bounded draft text required')
            draft={'owner':actor,'worker_id':lease['worker_id'],'epoch':state['epoch'],'in_reply_to':lease['message_id'],
                'personality_version':VERSION,
                'source_revision':state['revision'],'text':text,'status':'draft_only',
                'review_required':True,'send_enabled':False,'signing_authorized':False}
            write(conn,run.run_id,'draft:'+request_id,draft)
            state['latest_draft_key']='draft:'+request_id
        else:
            slots.pop(lease['worker_id'],None)
            if state.get('latest_draft_key'):
                draft=read(conn,run.run_id,state['latest_draft_key'])
                if draft and draft.payload.get('epoch')==state['epoch']:
                    state['pending_message_ids']=[mid for mid in state.get('pending_message_ids',[]) if mid!=lease.get('message_id')]
            state['lease']=None; state['epoch']+=1
    else: raise WorkflowError('unsupported communications action')
    state['revision']+=1
    state['worker_limit']=MAX_SIMEON_WORKERS
    write(conn,pool_run.run_id,'workers',{'slots':slots,'limit':MAX_SIMEON_WORKERS},pool_row)
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
    def mutate(conn):
        try: return control(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def dispatch_pending(*,principal,package_entries,request_id):
    """At most four parallel internal draft workers; never send or call a model."""
    from concurrent.futures import ThreadPoolExecutor
    from app.properties_db import get_connection
    if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>40: raise WorkflowError('bounded dispatch request_id required')
    if not isinstance(package_entries,list) or not 1<=len(package_entries)<=MAX_SIMEON_WORKERS: raise WorkflowError('one to four existing conversation packages required')
    packages=[str(UUID(str(p))) for p in package_entries]
    if len(set(packages))!=len(packages): raise WorkflowError('duplicate dispatch package')
    if not principal: raise WorkflowError('authenticated operator required')
    def worker(package):
        try:
            with get_connection() as conn: view=inspect(conn,package)
            if not view.get('pending_message_ids',([view['last_message_id']] if view.get('last_message_id') else [])): return {'package_entry':package,'status':'no_inbound'}
            base=request_id+':'+package
            claim=audited(principal=principal,package_entry=package,action='claim',request_id=base+':claim',expected_revision=view['revision'])
            lease=claim['lease'];revision=claim['revision']
            if lease.get('message_id'):
                draft=audited(principal=principal,package_entry=package,action='draft',request_id=base+':draft',expected_revision=revision,
                    lease_id=lease['id'],worker_id=lease['worker_id'],text='DRAFT ONLY: Acknowledgment of retained inbound message '+lease['message_id']+
                    '. Your inquiry requires review; no agreement, promise, transaction or legal approval is made.')
                revision=draft['revision']
            audited(principal=principal,package_entry=package,action='release',request_id=base+':release',expected_revision=revision,
                lease_id=lease['id'],worker_id=lease['worker_id'])
            return {'package_entry':package,'status':'draft_prepared' if lease.get('message_id') else 'queue_drained','worker_id':lease['worker_id'],'external_actions':0}
        except WorkflowError as exc:
            return {'package_entry':package,'status':'queued_or_blocked','reason':str(exc),'external_actions':0}
    with ThreadPoolExecutor(max_workers=min(len(packages),MAX_SIMEON_WORKERS)) as pool:
        results=list(pool.map(worker,packages))
    return {'workers_maximum':MAX_SIMEON_WORKERS,'owner':'simeon','results':results,'provider_calls':0,'external_actions':0,'draft_only':True}
