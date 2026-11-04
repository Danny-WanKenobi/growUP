"""Bounded authenticated GET-only Resend polling. Disabled until separately approved."""
import hashlib
import json
import os
import time
from uuid import UUID
import httpx
from app.communications import instant,lock,read,write
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,create_run_in_transaction
from app.observability.events import AgentEvent,AgentId,RetentionClass
from app.observability.policy import OperationClass
from app.observability.transactions import run_consequential_transaction
from app.resend_tracking import address,_enrollment,consume

MAX_BODY=262144
HTTP_CLIENT=httpx.Client


def configuration(environ=None):
    env=os.environ if environ is None else environ
    from app.server_target import inspect as inspect_server
    try: server=inspect_server(env)
    except ValueError: raise WorkflowError('invalid server/action mode configuration') from None
    if server['blockers']: raise WorkflowError('server/database target mismatch; polling refused')
    if env.get('RESEND_POLL_ENABLED','0')!='1' or env.get('RESEND_POLL_ACCESS_APPROVED','0')!='1':
        raise WorkflowError('Resend polling/provider access is disabled')
    if env.get('COMMUNICATIONS_ROUTER_ENABLED','0')!='1':
        raise WorkflowError('communications router is disabled')
    key=env.get('RESEND_API_KEY','')
    approval=env.get('RESEND_POLL_AUTHORIZATION_ID','')
    domain=env.get('RESEND_RECEIVING_DOMAIN','').strip().casefold()
    try:
        import re
        allowance=int(env.get('RESEND_POLL_REQUEST_ALLOWANCE','0'))
        expires=instant(env.get('RESEND_POLL_APPROVAL_EXPIRES_AT',''))
        if (not key.startswith('re_') or any(c.isspace() for c in key) or not 1<=allowance<=4
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',approval) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:[.][a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+',domain) or len(domain)>253 or 'replace_me' in domain
                or domain.endswith(('.invalid','.local')) or '.' not in domain or expires<=instant() or (expires-instant()).total_seconds()>3600):
            raise ValueError()
    except (ValueError,TypeError):
        raise WorkflowError('bounded Resend approval/configuration incomplete') from None
    return {'key':key,'approval':approval,'allowance':allowance,'expires':expires.isoformat(),'domain':domain}


def transaction(action,config,mutate):
    event=AgentEvent(agent=AgentId.SIMEON,run_id=config['approval'],event_type='communications.poll_'+action,
        summary='Bounded Resend receive-only polling; no delivery',details={'authorization_id':config['approval']},
        retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def begin(config):
    def mutate(conn):
        lock(conn,'poll:'+config['approval'])
        from app.workflow_db import get_run_by_key_in_transaction
        if get_run_by_key_in_transaction(conn,'communications-poll:'+config['approval']) is not None:
            raise WorkflowError('authorization cycle already claimed; no automatic retry')
        run=create_run_in_transaction(conn,idempotency_key='communications-poll:'+config['approval'])
        if read(conn,run.run_id,'approval'): raise WorkflowError('authorization cycle already claimed; no automatic retry')
        state={'allowance':config['allowance'],'used':0,'expires':config['expires'],
            'key_fingerprint':hashlib.sha256(config['key'].encode()).hexdigest(),
            'receiving_domain':config['domain'],'transport':'standard_received_email_api','status':'claimed'}
        write(conn,run.run_id,'approval',state)
        return str(run.run_id)
    return transaction('claim',config,mutate)


def reserve(config,rid,path):
    def mutate(conn):
        lock(conn,'poll:'+config['approval'])
        row=read(conn,UUID(rid),'approval'); state=dict(row.payload)
        if (state['used']>=state['allowance'] or instant(state['expires'])<=instant()
                or state['key_fingerprint']!=hashlib.sha256(config['key'].encode()).hexdigest() or state['receiving_domain']!=config['domain']):
            raise WorkflowError('poll allowance exhausted, expired or credential changed')
        state['used']+=1
        write(conn,UUID(rid),'request:'+str(state['used']),{'path':path,'status':'reserved_before_transmission'})
        write(conn,UUID(rid),'approval',state,row)
        return state['used']
    return transaction('reserve',config,mutate)


def get_json(config,rid,path):
    # Path is constructed internally from fixed paths/validated UUIDs only.
    if path!='/emails/receiving':
        if path!='/emails/receiving/'+str(UUID(path.split('/')[-1])):
            raise WorkflowError('unsupported receive-only Resend path')
    request_number=reserve(config,rid,path)  # Durable consumption before ambiguous HTTP attempt.
    deadline=time.monotonic()+12
    try:
        with HTTP_CLIENT(timeout=10.0,follow_redirects=False,trust_env=False) as client:
            with client.stream('GET','https://api.resend.com'+path,
                    headers={'Authorization':'Bearer '+config['key'],'Accept':'application/json','Accept-Encoding':'identity'}) as response:
                if response.status_code!=200: raise WorkflowError('Resend read failed; no retry')
                body=bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic()>deadline: raise WorkflowError('Resend response deadline exceeded; no retry')
                    if len(body)+len(chunk)>MAX_BODY: raise WorkflowError('Resend response exceeds limit')
                    body.extend(chunk)
        from app.routes.tracking_ingress import _unique_pairs
        data=json.loads(body,object_pairs_hook=_unique_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if not isinstance(data,dict): raise ValueError()
        def retain_response(conn):
            row=read(conn,UUID(rid),'request:'+str(request_number))
            write(conn,UUID(rid),'request:'+str(request_number),{**row.payload,'status':'received',
                'response_sha256':hashlib.sha256(body).hexdigest(),'response_bytes':len(body)},row)
        transaction('response',config,retain_response)
        return data
    except (httpx.HTTPError,ValueError,UnicodeDecodeError,RecursionError):
        raise WorkflowError('Resend read/parse failed; allowance remains consumed') from None


def poll_once():
    """One list + at most three enrolled message reads; no pagination or retry."""
    config=configuration(); rid=begin(config)
    listing=get_json(config,rid,'/emails/receiving')
    rows=listing.get('data')
    if not isinstance(rows,list) or len(rows)>100: raise WorkflowError('unexpected bounded received-email list')
    def retain_list(conn):
        candidates=[]; unlinked=[]; seen=set()
        for item in rows:
            if not isinstance(item,dict): raise WorkflowError('malformed received-email metadata')
            mid=str(UUID(item.get('id','')))
            if mid in seen: continue
            seen.add(mid)
            recipients=item.get('to')
            if not isinstance(recipients,list) or not 1<=len(recipients)<=50: raise WorkflowError('invalid received recipients')
            in_scope=[address(v) for v in recipients if address(v).split('@')[-1]==config['domain']]
            if not in_scope: continue
            matches=[_enrollment(conn,'reply_to',v) for v in in_scope]
            matches=[v for v in matches if v]
            if len(matches)==1 and address(item.get('from'))!=matches[0][1]['recipient']:
                matches=[]
            if len(matches)!=1:
                # Retain unresolved scoped metadata for Simeon triage, never guess a property.
                unlinked.append(mid)
                from app.workflow_db import put_record_in_transaction
                task={'requirement':'correlate_inbound:'+mid,'route':'simeon','status':'pending',
                    'authority':'human_owner','grants_permission':False,'provider_message_id':mid,
                    'reason':'Scoped inbound message is not uniquely enrolled; no guessed property or reply.'}
                put_record_in_transaction(conn,UUID(rid),kind='owner_review_task',record_key='inbound:'+mid,
                    version=1,payload=task,digest=digest_payload(task))
            else:
                from app.workflow_db import get_run_by_key_in_transaction
                conversation=get_run_by_key_in_transaction(conn,'communications:'+matches[0][0])
                retained=read(conn,conversation.run_id,'inbound:'+mid) if conversation else None
                if not retained or not retained.payload.get('detail_retrieved'):
                    candidates.append(mid)
            write(conn,UUID(rid),'listed:'+mid,{'id':mid,'to':in_scope,'from':item.get('from'),
                'owner':'simeon','correlation':'enrolled' if len(matches)==1 else 'owner_review_required'})
        write(conn,UUID(rid),'listing',{'candidate_ids':candidates,'unlinked_ids':unlinked,
            'has_more':bool(listing.get('has_more')),'coverage':'One provider list response only; not complete mailbox coverage.'})
        return candidates,unlinked
    candidates,unlinked=transaction('retain_list',config,retain_list)
    results=[]
    for mid in candidates[:max(0,config['allowance']-1)]:
        data=get_json(config,rid,'/emails/receiving/'+mid)
        if data.get('id')!=mid: raise WorkflowError('Resend response identity mismatch')
        event={'type':'email.received','created_at':data.get('created_at'),
            'data':{k:data.get(k) for k in ('from','to','subject','text')}}
        event['data']['email_id']=mid
        def retain(conn,event=event,mid=mid):
            return consume(conn,event,'poll-received:'+mid,digest_payload(event),
                authentication='resend_authenticated_api',route_conversation=True,poll_authorization_id=config['approval'])
        results.append(transaction('retain_message',config,retain))
    def finish(conn):
        row=read(conn,UUID(rid),'approval'); state=dict(row.payload); state['status']='completed'
        write(conn,UUID(rid),'approval',state,row)
        return {'authorization_id':config['approval'],'requests_consumed':state['used'],
            'messages_processed':len(results),'unlinked_ids':unlinked,'deferred_ids':candidates[len(results):],
            'mailbox_has_more':bool(listing.get('has_more')),'outbound_deliveries':0}
    return transaction('finish',config,finish)


def inspect_poll(conn,authorization_id):
    from app.workflow_db import get_run_by_key_in_transaction
    run=get_run_by_key_in_transaction(conn,'communications-poll:'+str(authorization_id))
    if run is None: raise WorkflowError('poll authorization not found')
    listing=read(conn,run.run_id,'listing')
    approval=read(conn,run.run_id,'approval')
    return {'authorization_id':authorization_id,'status':approval.payload['status'],
        'requests_consumed':approval.payload['used'],'request_allowance':approval.payload['allowance'],
        'receiving_domain':approval.payload['receiving_domain'],'transport':approval.payload['transport'],
        'listing':listing.payload if listing else None,
        'unlinked_metadata':[read(conn,run.run_id,'listed:'+mid).payload for mid in (listing.payload.get('unlinked_ids') or [])] if listing else [],
        'triage_owner':'simeon','delivery_enabled':False}


if __name__=='__main__':
    # No keys or provider error bodies are printed.
    try: print(json.dumps(poll_once(),indent=2))
    except Exception: raise SystemExit('STOP: polling failed or is disabled; preserve durable state, do not retry automatically.')
