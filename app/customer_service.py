"""Inbound-only CSR: durable sessions/jobs, four leased workers and escalation.

No discovery, dossier access, contracts or unsolicited contact. Uses the shared
workflow store and audit transaction, not a separate customer database.
"""
from datetime import timedelta
from uuid import UUID, uuid4
from app.communications import instant, lock, read, write
from app.workflow import digest_payload
from app.workflow_db import WorkflowError, create_run_in_transaction, get_run_by_key_in_transaction, put_record_in_transaction
from app.public_faq import answer

MAX_WORKERS=4


def transaction(action, mutate):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.transactions import run_consequential_transaction
    from app.observability.policy import OperationClass
    event=AgentEvent(agent=AgentId.CSR,run_id='customer-service',event_type='csr.'+action,
        summary='Inbound customer support; no business commitment',details={},retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def session(conn, identity, create=False):
    key='csr-session:'+identity
    lock(conn,key)
    run=get_run_by_key_in_transaction(conn,key)
    if run is None and create: run=create_run_in_transaction(conn,idempotency_key=key)
    if run is None: raise WorkflowError('support session not found')
    row=read(conn,run.run_id,'session')
    if row is None and create:
        write(conn,run.run_id,'session',{'created_at':instant().isoformat(),'expires_at':(instant()+timedelta(hours=24)).isoformat(),'messages':0})
        row=read(conn,run.run_id,'session')
    if row is None or instant(row.payload['expires_at'])<=instant(): raise WorkflowError('support session expired')
    return run,row


def enqueue(conn, identity, request_id, text):
    request_id=str(UUID(request_id))
    if not isinstance(text,str) or not text.strip() or len(text)>4000: raise WorkflowError('bounded question required')
    run,state=session(conn,identity)
    key='message:'+request_id
    existing=read(conn,run.run_id,key)
    if existing:
        if existing.payload['text']!=text: raise WorkflowError('support message identity conflict')
        return str(run.run_id),request_id
    if state.payload['messages']>=40: raise WorkflowError('support session message limit reached')
    write(conn,run.run_id,key,{'text':text,'status':'queued','created_at':instant().isoformat(),'attempts':0})
    write(conn,run.run_id,'session',{**state.payload,'messages':state.payload['messages']+1},state)
    return str(run.run_id),request_id


def claim(conn, rid, request_id, now=None):
    now=instant(now)
    lock(conn,'csr-worker-pool')
    pool=get_run_by_key_in_transaction(conn,'csr-worker-pool')
    if pool is None: pool=create_run_in_transaction(conn,idempotency_key='csr-worker-pool')
    row=read(conn,pool.run_id,'workers')
    slots={k:v for k,v in (row.payload['slots'] if row else {}).items() if instant(v['expires_at'])>now}
    job=read(conn,UUID(rid),'message:'+request_id)
    if job is None: raise WorkflowError('support job missing')
    if job.payload['status']=='complete': return {'complete':job.payload['result']}
    if job.payload.get('lease') and any(v['token']==job.payload['lease'] for v in slots.values()): return {'queued':True}
    if job.payload['attempts']>=3: return {'queued':True,'review_required':True}
    worker=next((f'csr:{i}' for i in range(1,MAX_WORKERS+1) if f'csr:{i}' not in slots),None)
    if worker is None: return {'queued':True}
    token=uuid4().hex
    slots[worker]={'token':token,'run_id':rid,'request_id':request_id,'expires_at':(now+timedelta(minutes=2)).isoformat()}
    write(conn,pool.run_id,'workers',{'slots':slots,'maximum':MAX_WORKERS},row)
    write(conn,UUID(rid),'message:'+request_id,{**job.payload,'status':'processing','lease':token,
        'attempts':job.payload['attempts']+1,'worker':worker},job)
    return {'worker':worker,'token':token,'text':job.payload['text']}


def complete(conn,rid,request_id,token,result):
    lock(conn,'csr-worker-pool')
    pool=get_run_by_key_in_transaction(conn,'csr-worker-pool'); row=read(conn,pool.run_id,'workers')
    job=read(conn,UUID(rid),'message:'+request_id)
    if job.payload['status']=='complete': return job.payload['result']
    owned=next((k for k,v in row.payload['slots'].items() if v['token']==token and instant(v['expires_at'])>instant()),None)
    if not owned or job.payload.get('lease')!=token: raise WorkflowError('support worker lease expired')
    if result.get('escalate'):
        task={'requirement':'customer_support:'+request_id,'route':result['escalate'],'status':'pending',
              'authority':'human_owner','grants_permission':False,'reason':job.payload['text'],
              'conversation_run_id':rid,'message_id':request_id}
        put_record_in_transaction(conn,UUID(rid),kind='owner_review_task',record_key='csr:'+request_id,
            version=1,payload=task,digest=digest_payload(task))
    final={**result,'status':'complete','worker':owned,'commitments_made':False}
    write(conn,UUID(rid),'message:'+request_id,{**job.payload,'status':'complete','result':final},job)
    slots={k:v for k,v in row.payload['slots'].items() if k!=owned}
    write(conn,pool.run_id,'workers',{'slots':slots,'maximum':MAX_WORKERS},row)
    return final


def respond(identity,request_id,text):
    rid,mid=transaction('enqueue',lambda conn:enqueue(conn,identity,request_id,text))
    lease=transaction('claim',lambda conn:claim(conn,rid,mid))
    if 'complete' in lease:return lease['complete']
    if lease.get('queued'):return {'status':'queued','text':'Your message is saved. All support workers are busy; please check again shortly.'}
    result=answer(lease['text'])
    return transaction('complete',lambda conn:complete(conn,rid,mid,lease['token'],result))


def reserve_appointment(conn,identity,request_id,agent,when,contact):
    from zoneinfo import ZoneInfo
    from app.resend_tracking import address
    from app.sabbath import blackout
    if agent not in ('simeon','solomon'):raise WorkflowError('supported appointment agent required')
    now=instant(); start=instant(when); local=start.astimezone(ZoneInfo('America/Chicago'))
    if (start<=now or start>now+timedelta(days=90) or local.weekday()>4
            or not 9<=local.hour<17 or local.minute not in (0,30) or local.second
            or blackout(start) or blackout(start+timedelta(minutes=30)-timedelta(seconds=1))):
        raise WorkflowError('choose a future weekday half-hour between 09:00 and 17:00 America/Chicago, outside Sabbath')
    email=address(contact); run,_=session(conn,identity); request_id=str(UUID(request_id))
    lock(conn,'csr-appointment:'+agent)
    prior=read(conn,run.run_id,'appointment:'+request_id)
    material={'agent':agent,'start':start.isoformat(),'contact':email}
    if prior:
        if prior.payload['material']!=material:raise WorkflowError('appointment request conflict')
        return prior.payload
    calendar=get_run_by_key_in_transaction(conn,'csr-appointments:'+agent)
    if calendar is None:calendar=create_run_in_transaction(conn,idempotency_key='csr-appointments:'+agent)
    key='slot:'+start.isoformat()
    if read(conn,calendar.run_id,key):raise WorkflowError('that tentative slot is already reserved')
    value={'material':material,'status':'tentative_reserved','end':(start+timedelta(minutes=30)).isoformat(),
        'timezone':'America/Chicago','confirmation_required':True,'external_invitation_sent':False}
    write(conn,calendar.run_id,key,{'session':identity,'request_id':request_id})
    write(conn,run.run_id,'appointment:'+request_id,value)
    task={'requirement':'confirm_appointment:'+request_id,'route':agent,'status':'pending',
        'authority':'human_owner','grants_permission':False,'reason':'Confirm tentative customer appointment',
        'appointment':value}
    put_record_in_transaction(conn,run.run_id,kind='owner_review_task',record_key='appointment:'+request_id,
        version=1,payload=task,digest=digest_payload(task))
    return value


def drain_web(limit=20):
    """Recover bounded queued/expired jobs even if the browser has closed."""
    from app.sabbath import blackout
    if blackout():return {'paused':True}
    if __import__('os').getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1':return {'paused':True}
    from app.properties_db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id,record_key FROM (SELECT DISTINCT ON(run_id,record_key) "
                "run_id,record_key,payload FROM workflow_records WHERE kind='research_artifact' "
                "AND record_key LIKE 'message:%' AND run_id IN (SELECT run_id FROM workflow_runs "
                "WHERE idempotency_key LIKE 'csr-session:%') ORDER BY run_id,record_key,version DESC) q "
                "WHERE payload->>'status' IN ('queued','processing') AND (payload->>'attempts')::int<3 LIMIT %(limit)s",{'limit':min(limit,20)})
            jobs=cur.fetchall()
    def process(job):
        rid=str(job['run_id']); mid=job['record_key'].split(':',1)[1]
        lease=transaction('recover_claim',lambda conn:claim(conn,rid,mid))
        if lease.get('queued') or 'complete' in lease:return
        transaction('recover_complete',lambda conn:complete(conn,rid,mid,lease['token'],answer(lease['text'])))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:list(pool.map(process,jobs))
    return {'inspected':len(jobs),'maximum_workers':MAX_WORKERS}
