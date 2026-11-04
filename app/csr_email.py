"""Inbound-only CSR email worker: signed intake, API body read, draft/send ledger.

No unsolicited recipient, no attachment downloads, no automatic retry after an
ambiguous send. DEMO_MODE retains drafts; actual replies require explicit live
delivery configuration. Shared CSR workers answer and escalate the same FAQs.
"""
from datetime import timedelta
from html.parser import HTMLParser
import hashlib
import json
import os
import time
from uuid import UUID

import httpx
from app.communications import instant,lock,read,write
from app import customer_service as csr
from app.workflow_db import WorkflowError,get_run_by_key_in_transaction,create_run_in_transaction
from app.workflow import digest_payload
from app.resend_tracking import address

HTTP_CLIENT=httpx.Client


def enqueue(conn,metadata):
    mid=str(UUID(metadata['email_id']))
    run=get_run_by_key_in_transaction(conn,'direct-mail:'+mid)
    if run is None:raise WorkflowError('authenticated mailbox intake required')
    if read(conn,run.run_id,'csr-email-job'):return
    write(conn,run.run_id,'csr-email-job',{'message_id':mid,'status':'queued','attempts':0,'created_at':instant().isoformat()})


def endpoint():
    from app.mailbox_routing import domain
    return 'csr@'+domain()


def eligible(detail):
    """Conservative auto-response/loop suppression using provider-returned headers."""
    headers=detail.get('headers')
    if not isinstance(headers,dict):return False
    headers={str(k).lower():str(v).lower() for k,v in headers.items()}
    sender=address(detail.get('from')); local,host=sender.rsplit('@',1)
    if host==endpoint().split('@')[1] or local in ('mailer-daemon','postmaster','noreply','no-reply'):return False
    if headers.get('auto-submitted','no')!='no' or headers.get('precedence') in ('bulk','list','junk'):return False
    if any(name in headers for name in ('list-id','list-unsubscribe','x-auto-response-suppress')):return False
    auth=detail.get('authentication')
    if not isinstance(auth,dict) or auth.get('dmarc')!='pass':return False
    return [address(v) for v in detail.get('to',[])]==[endpoint()]


class TextOnly(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag in ('p','br','div','li'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.hidden:self.hidden-=1
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def plain(detail):
    text=detail.get('text')
    if text is None:
        html=detail.get('html')
        if not isinstance(html,str) or len(html)>64000:raise WorkflowError('bounded readable message required')
        parser=TextOnly();parser.feed(html);text=''.join(parser.parts)
    if not isinstance(text,str) or not text.strip() or len(text)>16000:raise WorkflowError('bounded readable message required')
    return text


def reserve(conn,rid,phase,material):
    lock(conn,'csr-email:'+str(rid))
    prior=read(conn,rid,'csr-request:'+phase)
    if prior:
        if prior.payload['material_digest']!=digest_payload(material):raise WorkflowError('CSR request conflict')
        return False,prior.payload
    if phase in ('send','fetch'):
        lock(conn,'csr-email-daily')
        usage=get_run_by_key_in_transaction(conn,'csr-email-daily')
        if usage is None:usage=create_run_in_transaction(conn,idempotency_key='csr-email-daily')
        key=instant().date().isoformat(); row=read(conn,usage.run_id,key)
        usage_value=dict(row.payload) if row else {'count':0,'fetches':0}
        counter='count' if phase=='send' else 'fetches'
        count=usage_value.get(counter,0)
        maximum=int(os.getenv('CSR_EMAIL_DAILY_MAX','20') if phase=='send' else os.getenv('CSR_EMAIL_FETCH_DAILY_MAX','100'))
        if not 1<=maximum<=(100 if phase=='send' else 200) or count>=maximum:raise WorkflowError('CSR daily request allowance consumed')
        usage_value[counter]=count+1
        write(conn,usage.run_id,key,usage_value,row)
        # One automated reply per sender per rolling hour, in addition to per-message dedup.
        if phase=='send':
            sender_key='sender:'+hashlib.sha256(material['to'][0].encode()).hexdigest()
            last=read(conn,usage.run_id,sender_key)
            if last and instant(last.payload['at'])+timedelta(hours=1)>instant():raise WorkflowError('CSR sender cooldown')
            write(conn,usage.run_id,sender_key,{'at':instant().isoformat()},last)
    value={'material_digest':digest_payload(material),'status':'claimed_before_transmission'}
    write(conn,rid,'csr-request:'+phase,value)
    return True,value


def http(rid,mid,phase,body=None):
    sending=phase=='send'
    if phase not in ('send','fetch') or os.getenv('CSR_EMAIL_ENABLED','0')!='1':raise WorkflowError('CSR email worker disabled')
    from app.sabbath import blackout
    if blackout() or os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1':raise WorkflowError('CSR email paused')
    method='POST' if sending else 'GET'
    path='/emails' if sending else '/emails/receiving/'+str(UUID(mid))
    key=os.getenv('RESEND_API_KEY' if sending else 'RESEND_RECEIVING_API_KEY','')
    if not key.startswith('re_'):raise WorkflowError('CSR private API credential missing')
    if sending:
        from app.personalities import require_channel
        require_channel('csr','email','outbound',human_initiated=bool((body.get('headers') or {}).get('In-Reply-To')))
        if os.getenv('DEMO_MODE','yes')!='no' or os.getenv('CSR_EMAIL_DELIVERY_APPROVED','0')!='1':
            raise WorkflowError('CSR real delivery is not enabled')
        if (set(body)!={'from','to','reply_to','subject','text','headers'} or body['from']!=endpoint()
                or body['reply_to']!=endpoint() or len(body['to'])!=1 or not body['headers'].get('In-Reply-To')):
            raise WorkflowError('inbound-only CSR reply required')
        def validate(conn):
            inbound=read(conn,rid,'inbound');content=read(conn,rid,'content');draft=read(conn,rid,'csr-reply-draft')
            if (not inbound or not content or not draft or draft.payload!=body
                    or inbound.payload['identity']['email_id']!=mid
                    or body['to']!=[inbound.payload['identity']['from']]
                    or inbound.payload['identity']['to']!=[endpoint()]
                    or body['headers']['In-Reply-To']!=content.payload['message_id']):
                raise WorkflowError('CSR delivery must bind an actual incoming message and retained reply')
        csr.transaction('email_send_validate',validate)
    material=body if sending else {'id':mid}
    won,prior=csr.transaction('email_reserve',lambda conn:reserve(conn,rid,phase,material))
    if not won:
        if prior.get('status')!='complete':raise WorkflowError('uncertain CSR attempt; owner review required')
        return prior['response']
    deadline=time.monotonic()+15
    try:
        with HTTP_CLIENT(timeout=12,follow_redirects=False,trust_env=False) as client:
            with client.stream(method,'https://api.resend.com'+path,json=body,
                headers={'Authorization':'Bearer '+key,'Idempotency-Key':'growup-csr/'+mid,'Accept-Encoding':'identity'}) as response:
                if response.status_code not in (200,201):raise WorkflowError('CSR provider request refused')
                raw=bytearray()
                for chunk in response.iter_bytes():
                    if len(raw)+len(chunk)>262144 or time.monotonic()>deadline:raise WorkflowError('bounded CSR response required')
                    raw.extend(chunk)
        data=json.loads(raw)
        if sending:data={'id':str(UUID(data['id'])),'status':'provider_accepted','delivered':False}
        else:
            if str(data.get('id'))!=mid:raise WorkflowError('CSR response identity mismatch')
            data={k:data.get(k) for k in ('id','from','to','subject','text','html','headers','authentication','message_id')}
    except (httpx.HTTPError,ValueError,KeyError,TypeError):raise WorkflowError('CSR provider result uncertain; no automatic retry') from None
    def retain(conn):
        row=read(conn,rid,'csr-request:'+phase)
        write(conn,rid,'csr-request:'+phase,{**row.payload,'status':'complete','response':data,
            'response_sha256':hashlib.sha256(raw).hexdigest()},row)
    csr.transaction('email_response',retain)
    return data


def process(rid):
    rid=UUID(str(rid))
    def begin(conn):
        lock(conn,'csr-email:'+str(rid)); row=read(conn,rid,'csr-email-job')
        if row is None or row.payload['status'] not in ('queued','processing'):return None
        if row.payload.get('until') and instant(row.payload['until'])>instant():return None
        if row.payload['attempts']>=3:return None
        value={**row.payload,'status':'processing','until':(instant()+timedelta(minutes=3)).isoformat(),
            'attempts':row.payload['attempts']+1}
        write(conn,rid,'csr-email-job',value,row);return value
    job=csr.transaction('email_claim',begin)
    if job is None:return {'status':'already_complete_or_leased'}
    status='needs_review'
    try:
        detail=http(rid,job['message_id'],'fetch')
        # Retained direct-mailbox envelope remains the authority, not arbitrary fetched recipients.
        def validate(conn):
            inbound=read(conn,rid,'inbound').payload['identity']
            if (address(detail['from'])!=inbound['from'] or [address(v) for v in detail['to']]!=inbound['to']
                    or detail['subject']!=inbound['subject']):raise WorkflowError('CSR fetched envelope mismatch')
        csr.transaction('email_validate',validate)
        if not eligible(detail):status='suppressed_auto_reply'
        else:
            from app.mailbox_routing import retain_content
            text=plain(detail)
            csr.transaction('email_content',lambda conn:retain_content(conn,conversation_id=job['message_id'],
                detail={**detail,'text':text},response_digest=digest_payload(detail)))
            identity=hashlib.sha256(('csr-email:'+job['message_id']).encode()).hexdigest()
            csr.transaction('email_session',lambda conn:csr.session(conn,identity,True))
            response=csr.respond(identity,job['message_id'],text[:4000],channel='email')
            if response['status']=='queued':status='queued'
            else:
                body={'from':endpoint(),'to':[address(detail['from'])],'reply_to':endpoint(),
                    'subject':detail['subject'] if detail['subject'].lower().startswith('re:') else 'Re: '+detail['subject'],
                    'text':'Grow Up Care\n\n'+response['text']+'\n\nThis is general support guidance, not a business commitment.',
                    'headers':{'In-Reply-To':detail['message_id'],'References':detail['message_id'],'Auto-Submitted':'auto-replied'}}
                csr.transaction('email_draft',lambda conn:write(conn,rid,'csr-reply-draft',body) if not read(conn,rid,'csr-reply-draft') else None)
                if os.getenv('DEMO_MODE','yes')=='no' and os.getenv('CSR_EMAIL_DELIVERY_APPROVED','0')=='1':
                    http(rid,job['message_id'],'send',body);status='provider_accepted'
                else:status='draft_only'
    except (WorkflowError,ValueError,TypeError):status='needs_review'
    def finish(conn):
        row=read(conn,rid,'csr-email-job');write(conn,rid,'csr-email-job',{**row.payload,'status':status,'until':None},row)
        if status=='needs_review':
            from app.workflow_db import put_record_in_transaction
            task={'requirement':'csr_email_review:'+job['message_id'],'route':'human_owner','status':'pending',
                'authority':'human_owner','grants_permission':False,'reason':'CSR email needs review; no automatic retry',
                'message_id':job['message_id']}
            put_record_in_transaction(conn,rid,kind='owner_review_task',record_key='csr-email-review',version=1,
                payload=task,digest=digest_payload(task))
    csr.transaction('email_finish',finish)
    return {'status':status}


def drain(limit=10):
    if os.getenv('CSR_EMAIL_ENABLED','0')!='1':return {'enabled':False}
    from app.sabbath import blackout
    if blackout() or os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1':return {'paused':True}
    if not isinstance(limit,int) or not 1<=limit<=20:raise WorkflowError('bounded worker limit required')
    from app.properties_db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id FROM (SELECT DISTINCT ON(run_id) run_id,payload FROM workflow_records "
                "WHERE kind='research_artifact' AND record_key='csr-email-job' ORDER BY run_id,version DESC) q "
                "WHERE payload->>'status' IN ('queued','processing') LIMIT %(limit)s",{'limit':limit})
            rows=cur.fetchall()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=csr.MAX_WORKERS) as pool:
        results=list(pool.map(lambda row:process(row['run_id']),rows))
    return {'processed':len(results),'results':results,'maximum_workers':csr.MAX_WORKERS}


if __name__=='__main__':print(json.dumps(drain()))
