"""Bounded owner-only real correspondence through shared mailbox services.

No general marketing permission. Ledger and synthetic dossier links stay in the
isolated demo DB; actual provider receipts are labeled actual transport evidence.
"""
import hashlib
import json
import os
import re
import time
from uuid import UUID

import httpx

from app.communications import instant, lock, read, write
from app.workflow import digest_payload
from app.workflow_db import WorkflowError, create_run_in_transaction, get_run_by_key_in_transaction
from app.uriel_demo_send import transaction
from app.uriel_campaign import DEMO_RECIPIENT
from app.resend_tracking import address

HTTP_CLIENT = httpx.Client
AUTHORIZATION = 'owner-correspondence-20261104-v1'
DOMAIN = 'growup.securecodeusa.com'
ACTORS = ('simeon', 'solomon')
RETURN_RUN_KEY = 'demo-handoff-conversation-return-20261104'


def config(actor='simeon', receiving=False):
    if actor not in ACTORS or os.getenv('CORRESPONDENCE_PROOF_APPROVED') != '1':
        raise WorkflowError('owner correspondence proof disabled')
    if os.getenv('DEMO_MODE', 'yes') != 'yes' or os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0') == '1':
        raise WorkflowError('demo mode required and emergency stop must be clear')
    from app.sabbath import blackout
    if blackout(): raise WorkflowError('correspondence paused during Sabbath')
    key = (os.getenv('RESEND_RECEIVING_API_KEY') if receiving else None) or os.getenv('RESEND_API_KEY','')
    if not key.startswith('re_') or any(c.isspace() for c in key):
        raise WorkflowError('private provider credential required')
    return {'authorization':AUTHORIZATION, 'key':key, 'agent':actor}


def reserve(conn, cfg, operation, method, path, body):
    lock(conn, AUTHORIZATION)
    run = get_run_by_key_in_transaction(conn, AUTHORIZATION)
    if run is None: run = create_run_in_transaction(conn, idempotency_key=AUTHORIZATION)
    key = 'request:' + operation
    prior = read(conn, run.run_id, key)
    material = {'method':method, 'path':path, 'body_digest':digest_payload(body)}
    if prior:
        if prior.payload['material'] != material: raise WorkflowError('request identity conflict')
        return str(run.run_id), False, prior.payload
    usage_row = read(conn, run.run_id, 'usage')
    usage = dict(usage_row.payload) if usage_row else {'reads':0,'sends':0}
    counter, ceiling = ('sends',3) if method == 'POST' else ('reads',16)
    if usage[counter] >= ceiling: raise WorkflowError('proof request allowance consumed')
    usage[counter] += 1
    state = {'material':material, 'status':'claimed_before_transmission', 'created_at':instant().isoformat()}
    write(conn, run.run_id, key, state)
    write(conn, run.run_id, 'usage', usage, usage_row)
    return str(run.run_id), True, state


def request(cfg, operation, method, path, body=None):
    if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',operation):
        raise WorkflowError('bounded request identity required')
    if method not in ('GET','POST'): raise WorkflowError('unsupported method')
    if method == 'POST':
        if path != '/emails' or set(body) - {'from','to','reply_to','subject','text','headers'}:
            raise WorkflowError('unsupported send request')
        if body['to'] != [DEMO_RECIPIENT] or body['from'] != cfg['agent']+'@'+DOMAIN or body.get('reply_to') != body['from']:
            raise WorkflowError('owner-only recipient and actor envelope required')
        if cfg['agent']=='simeon' and not (body.get('headers') or {}).get('In-Reply-To'):
            raise WorkflowError('Simeon cannot initiate contact')
    elif path not in ('/emails/receiving',) and not (
            path.startswith('/emails/receiving/') and str(UUID(path.rsplit('/',1)[1])) == path.rsplit('/',1)[1]) and not (
            path.startswith('/emails/') and str(UUID(path.rsplit('/',1)[1])) == path.rsplit('/',1)[1]):
        raise WorkflowError('unsupported read request')
    rid, won, prior = transaction(cfg, 'reserve', lambda conn:reserve(conn,cfg,operation,method,path,body))
    if not won:
        if 'response' not in prior: raise WorkflowError('uncertain prior request; no retransmission')
        return prior['response']
    status, data = 'uncertain_or_refused', None
    try:
        config(cfg['agent'], receiving=path.startswith('/emails/receiving'))
        deadline = time.monotonic()+15
        with HTTP_CLIENT(timeout=12,follow_redirects=False,trust_env=False) as client:
            with client.stream(method,'https://api.resend.com'+path,json=body if method=='POST' else None,
                    headers={'Authorization':'Bearer '+cfg['key'],'Idempotency-Key':AUTHORIZATION+'/'+operation,
                             'Accept-Encoding':'identity'}) as response:
                code=response.status_code
                if code not in (200,201): raise WorkflowError('provider refused request (HTTP '+str(code)+')')
                raw=bytearray()
                for chunk in response.iter_bytes():
                    if len(raw)+len(chunk)>262144 or time.monotonic()>deadline: raise ValueError()
                    raw.extend(chunk)
        data=json.loads(raw)
        if not isinstance(data,dict): raise ValueError()
        if method=='POST': data={'id':str(UUID(data['id']))}
        status='provider_accepted' if method=='POST' else 'retrieved'
    except (httpx.HTTPError,ValueError,KeyError,TypeError,WorkflowError) as exc:
        def failed(conn):
            row=read(conn,UUID(rid),'request:'+operation)
            write(conn,UUID(rid),'request:'+operation,{**row.payload,'status':status},row)
        transaction(cfg,'failed',failed)
        # Never expose provider response bodies, headers or credentials.
        if isinstance(exc,WorkflowError): raise
        raise WorkflowError('request uncertain; no retry') from None
    def retain(conn):
        row=read(conn,UUID(rid),'request:'+operation)
        write(conn,UUID(rid),'request:'+operation,{**row.payload,'status':status,'response':data,
            'authentication':'resend_authenticated_api','response_sha256':hashlib.sha256(raw).hexdigest()},row)
    transaction(cfg,'retain',retain)
    return data


def discover(operation='inbound-list-1'):
    cfg=config(receiving=True)
    result=request(cfg,operation,'GET','/emails/receiving')
    rows=result.get('data')
    if not isinstance(rows,list) or len(rows)>100: raise WorkflowError('bounded mailbox list required')
    matched=[]
    for row in rows:
        if address(row.get('from')) != DEMO_RECIPIENT: continue
        recipients=[address(v) for v in row.get('to',[])]
        if len(recipients)!=1 or recipients[0] not in [a+'@'+DOMAIN for a in ACTORS]: continue
        matched.append({k:row.get(k) for k in ('id','to','subject','created_at')})
    return {'candidates':matched,'has_more':bool(result.get('has_more')),'complete_coverage':False}


def fetch(mid,actor):
    mid=str(UUID(mid)); cfg=config(actor,receiving=True)
    detail=request(cfg,'detail-'+mid,'GET','/emails/receiving/'+mid)
    if (str(detail.get('id'))!=mid or address(detail.get('from'))!=DEMO_RECIPIENT
            or [address(v) for v in detail.get('to',[])] != [actor+'@'+DOMAIN]):
        raise WorkflowError('owner and actor envelope mismatch')
    from app.mailbox_routing import receive,retain_content
    metadata={**detail,'email_id':mid}
    def retain(conn):
        result=receive(conn,metadata=metadata,event_id='api:'+mid,body_digest=digest_payload(detail),
            authentication='resend_authenticated_api')
        retain_content(conn,conversation_id=mid,detail=detail,response_digest=digest_payload(detail))
        return result
    return transaction(cfg,'inbound',retain)


def reply(mid,actor,text):
    mid=str(UUID(mid)); cfg=config(actor)
    if not isinstance(text,str) or not text.strip() or len(text)>8000: raise WorkflowError('bounded reply required')
    def prepare(conn):
        from app.mailbox_routing import control
        view=control(conn,conversation_id=mid,action='inspect',principal='owner-proof')
        if view['state']['owner']!=actor or view['inbound']['identity']['from']!=DEMO_RECIPIENT:
            raise WorkflowError('incoming owner message bound to actor required')
        run=get_run_by_key_in_transaction(conn,'direct-mail:'+mid)
        content=read(conn,run.run_id,'content')
        if not content: raise WorkflowError('authenticated message body required')
        original=view['inbound']['identity']['subject']
        body={'from':actor+'@'+DOMAIN,'to':[DEMO_RECIPIENT],'reply_to':actor+'@'+DOMAIN,
            'subject':original if original.lower().startswith('re:') else 'Re: '+original,
            'text':text,'headers':{'In-Reply-To':content.payload['message_id'],
                                    'References':content.payload['message_id']}}
        prior=read(conn,run.run_id,'reply-plan')
        if prior and prior.payload!=body: raise WorkflowError('reply identity conflict')
        if not prior: write(conn,run.run_id,'reply-plan',body)
        return body
    body=transaction(cfg,'prepare_reply',prepare)
    response=request(cfg,actor+'-reply-'+mid,'POST','/emails',body)
    def receipt(conn):
        run=get_run_by_key_in_transaction(conn,'direct-mail:'+mid)
        prior=read(conn,run.run_id,'reply-receipt')
        value={'provider_id':response['id'],'request_digest':digest_payload(body),
               'status':'provider_accepted','delivered':False,'authorization':AUTHORIZATION}
        if prior and prior.payload!=value: raise WorkflowError('reply receipt conflict')
        if not prior: write(conn,run.run_id,'reply-receipt',value)
    transaction(cfg,'reply_receipt',receipt)
    return response


def solomon_introduction():
    cfg=config('solomon')
    body={'from':'solomon@'+DOMAIN,'to':[DEMO_RECIPIENT],'reply_to':'solomon@'+DOMAIN,
        'subject':'Grow Up Solomon correspondence proof 20261104',
        'text':'Owner-only construction correspondence test for fictional Hudson Lantern Tower. '
               'Please reply with a hypothetical scheduling question. This is not a proposal, procurement, '
               'contract, construction authorization or commitment. No real work is requested.'}
    return request(cfg,'solomon-introduction','POST','/emails',body)


def cold_return(mid):
    """Proof-only: genuine exchange, explicit synthetic cold-lead scenario."""
    mid=str(UUID(mid)); cfg=config()
    def existing(conn):
        conversation=get_run_by_key_in_transaction(conn,'direct-mail:'+mid)
        if not conversation: raise WorkflowError('genuine incoming owner message required')
        retained=read(conn,conversation.run_id,'followup:owner-cold-return-1')
        if retained: return {**retained.payload['result'],'replayed':True}
        ledger=get_run_by_key_in_transaction(conn,AUTHORIZATION)
        receipt=read(conn,ledger.run_id,'request:simeon-reply-'+mid) if ledger else None
        if not receipt or receipt.payload['status']!='provider_accepted':
            raise WorkflowError('established inbound plus accepted reply required')
        return None
    prior=transaction(cfg,'inspect_return',existing)
    if prior: return prior
    from app import handoff_demo as demo
    key=RETURN_RUN_KEY
    view=demo.service('start',run_key=key,scenario_id='hudson-sky')
    for _ in range(15):
        if view['current_holder']=='simeon': break
        if view['phase']!='forward': raise WorkflowError('proof dossier not available to Simeon')
        view=demo.service('advance',run_key=key,expected_sequence=view['sequence'])
    if view['current_holder']!='simeon': raise WorkflowError('bounded fixture progression did not reach Simeon')
    def mutate(conn):
        from app.simeon_followup import return_in_transaction
        run=get_run_by_key_in_transaction(conn,key)
        return return_in_transaction(conn,conversation_id=mid,run_id=run.run_id,dossier_key='main',
            expected_digest=view['dossier']['digest'],operation_id='owner-cold-return-1',principal='owner-proof',
            notes='Owner asked about the preliminary campaign. Preserve the discussion and clarify conditional financial assumptions, '
                  'lighting costs and evidence gaps in a tailored follow-up. No agreement or commitment was requested.',
            reason='Explicit simulated cold-lead scenario for fictional Hudson Lantern; no actual inactivity or consent inferred')
    return transaction(cfg,'cold_return',mutate)


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','list','fetch','reply','solomon-introduction','cold-return'])
    parser.add_argument('--operation',default='inbound-list-1')
    parser.add_argument('--message-id')
    parser.add_argument('--actor',choices=ACTORS,default='simeon')
    parser.add_argument('--text')
    args=parser.parse_args()
    try:
        if args.action=='status':
            cfg=config()
            def status(conn):
                run=get_run_by_key_in_transaction(conn,AUTHORIZATION)
                usage=read(conn,run.run_id,'usage') if run else None
                return {'authorization':AUTHORIZATION,'usage':usage.payload if usage else {'reads':0,'sends':0},
                    'receiving_key_installed':bool(os.getenv('RESEND_RECEIVING_API_KEY','').startswith('re_')),
                    'provider_calls':0}
            result=transaction(cfg,'status',status)
        elif args.action=='list': result=discover(args.operation)
        elif args.action=='fetch': result=fetch(args.message_id,args.actor)
        elif args.action=='reply': result=reply(args.message_id,args.actor,args.text)
        elif args.action=='cold-return': result=cold_return(args.message_id)
        else: result=solomon_introduction()
        print(json.dumps(result,indent=2))
        return 0
    except (WorkflowError,ValueError,TypeError,KeyError) as exc:
        # Our WorkflowError strings contain no provider bodies or credentials.
        print('STOP: '+(str(exc) if isinstance(exc,WorkflowError) else 'invalid proof input'))
        return 1


if __name__=='__main__': raise SystemExit(main())
