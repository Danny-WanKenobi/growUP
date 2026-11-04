"""Retained owner analysis: source-linked explanations, never external execution."""
from copy import deepcopy
from uuid import UUID
from app import wondrue_workspace as ws
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_run_by_key_in_transaction
from app.wondrue_insights import snapshot,latest_rows,reference
from app.linked_reassessments import effective_reports,effective_dossier,stale_dependencies


def compare_versions(conn,before,after):
    old,od,orr=snapshot(conn,before);new,nd,nrr=snapshot(conn,after)
    if old['data'].get('property_id')!=new['data'].get('property_id') or before['run_id']!=after['run_id'] or before['key']!=after['key'] or before['version']>=after['version']:
        raise WorkflowError('choose increasing versions of the same canonical dossier record')
    changes=[];truncated=False
    def diff(a,b,path):
        nonlocal truncated
        if a==b:return
        if len(changes)>=300:truncated=True;return
        if (isinstance(a,dict) and (isinstance(b,dict) or b is None)) or (a is None and isinstance(b,dict)):
            a=a or {};b=b or {}
            for key in sorted(set(a)|set(b)):
                if key in ('digest','source_dossier_digest','dossier_digest','rework_input_digests'):continue
                diff(a.get(key),b.get(key),path+'.'+key)
        elif isinstance(a,list) and isinstance(b,list):
            if max(len(a),len(b))<=20:
                for i in range(max(len(a),len(b))):diff(a[i] if i<len(a) else None,b[i] if i<len(b) else None,path+f'[{i}]')
            else:changes.append({'path':path,'before':{'items':len(a),'digest':digest_payload(a)},'after':{'items':len(b),'digest':digest_payload(b)},'scope':'Open retained sources for full list content.'})
        else:changes.append({'path':path,'before':deepcopy(a),'after':deepcopy(b)})
    diff({'assessment_inputs':effective_dossier(od).get('assessment_inputs'),'specialist_inputs':effective_dossier(od).get('specialist_inputs'),'reports':orr,'verification':old['data'].get('candidate_verification')},
         {'assessment_inputs':effective_dossier(nd).get('assessment_inputs'),'specialist_inputs':effective_dossier(nd).get('specialist_inputs'),'reports':nrr,'verification':new['data'].get('candidate_verification')},'dossier')
    return {'before':before,'after':after,'changes':changes,'truncated':truncated,'remaining_gaps':{a:r.get('missing') for a,r in nrr.items() if r.get('missing')},
        'stale_dependencies':stale_dependencies(nrr),'records_written':0,'scope':'Retained differences, not inferred causation. A numeric change is not verification or approval.'}


def campaign_readiness(conn,source):
    row,d,reports=snapshot(conn,source)
    from app.uriel import prepare_campaign,UrielError
    candidate=deepcopy(row['data']);candidate['dossier']=effective_dossier(d);candidate['digest']=digest_payload({k:v for k,v in candidate.items() if k!='digest'})
    stale=stale_dependencies(reports)
    blockers=['stale:'+n for n in ('raphael','onesimus','michael','uriel') if n in stale]
    try:campaign=prepare_campaign(candidate)
    except UrielError as exc:campaign={'status':'needs_evidence','missing':[str(exc)]}
    blockers+=campaign.get('missing') or []
    if campaign.get('status')=='suppressed' or campaign.get('suppression_preserved'):
        blockers.append('campaign_suppressed')
    old=(reports.get('uriel') or {}).get('campaign') or {}
    old_refs=old.get('source_reports') or old.get('evidence') or {}
    return {'source':source,'status':'needs_evidence' if blockers else 'prepared_for_review','blockers':sorted(set(blockers)),
        'retained_campaign_id':old.get('campaign_id'),'current_preparation':campaign,'retained_claim_references':old_refs,
        'external_delivery_authorized':False,'records_written':0,'provider_calls':0,
        'scope':'Draft/readiness assessment only. Existing contact, channel permission, suppression, rate, Sabbath and duplicate-send guards still apply.'}


def portfolio(conn,as_of):
    from datetime import date
    from app.manny_review import inspect_in_transaction
    date.fromisoformat(as_of);rows,more=latest_rows(conn,('dossier_version',),limit=20);items=[]
    for row in rows:
        ref=reference(row)
        try:
            if not ws.valid_record(row['kind'],row['payload'],row['digest']):raise WorkflowError('source integrity failure')
            brief=inspect_in_transaction(conn,run_id=row['run_id'],dossier_key=row['record_key'])['brief']
            from app.asset_tracking import project
            register=project(row['payload'].get('dossier') or {},property_id=row['payload']['property_id'],as_of=as_of)
            overdue=[o for o in register['obligations'] if o['overdue']]
            issues=brief['unresolved_task_keys'];stale=brief['overview'].get('priority_actions') or []
            reasons=(['overdue_obligation'] if overdue else [])+(['unresolved_review'] if issues else [])+(['assessment_gaps_or_staleness'] if stale else [])
            items.append({'source':ref,'property':brief['overview']['property'],'priority':'urgent' if overdue else 'review' if reasons else 'routine',
                'priority_basis':reasons,'overdue':overdue,'obligations':register['obligations'],'asset_status':register['asset_status'],
                'unresolved_task_keys':issues,'assessment_actions':stale,'protected_entries':len((row['payload'].get('dossier') or {}).get('owner_vault_entries') or []),
                'amounts':register['reported_amounts_usd'],'amount_scope':register['amounts_scope'],'ownership_verified':False})
        except ValueError as exc:items.append({'source':ref,'priority':'integrity_review','priority_basis':[str(exc)],'claims_quarantined':True})
    ranks={'integrity_review':0,'urgent':1,'review':2,'routine':3}
    items.sort(key=lambda i:(ranks[i['priority']],i['source']['run_id'],i['source']['key']))
    return {'as_of':as_of,'items':items,'has_more':more,'coverage':'Latest 20 retained records; not a complete asset register. No portfolio money total is inferred.',
        'ranking_basis':'Integrity, recorded overdue obligations, unresolved decisions, then stable record identity. No category or speculative profit advantage.',
        'records_written':0,'provider_calls':0,'external_actions':0}


def correspondence_proposal(conn,*,source,conversation_id,operation_id,start,end,claim_scope,note):
    from app import owner_evidence as evidence
    from app.communications import lock,instant
    import base64
    rid=UUID(source['run_id']);mid=str(UUID(str(conversation_id)));op=str(UUID(str(operation_id)))
    row=evidence._source(conn,source);lock(conn,'correspondence-evidence:'+op)
    run=get_run_by_key_in_transaction(conn,'direct-mail:'+mid)
    if run is None:raise WorkflowError('retained direct conversation not found; no provider fetch is performed')
    inbound=ws.read(conn,run.run_id,'inbound');content=ws.read(conn,run.run_id,'content');state=ws.read(conn,run.run_id,'state')
    if not inbound or not content or not state or state.payload.get('owner') not in ('simeon','solomon') or inbound.payload.get('authentication') not in ('resend_svix_hmac_sha256','resend_authenticated_api') or content.payload.get('authentication')!='resend_authenticated_api':
        raise WorkflowError('authenticated retained Simeon/Solomon content required; transport is not sender authority')
    text=content.payload.get('text','')
    if any(isinstance(v,bool) or not isinstance(v,int) for v in (start,end)) or not 0<=start<end<=len(text) or end-start>8000:raise WorkflowError('select an exact bounded retained message excerpt')
    request={'source':source,'message':mid,'start':start,'end':end,'content_digest':content.digest,'claim_scope':claim_scope,'note':note}
    key='correspondence-evidence:'+op;prior=ws.read(conn,rid,key)
    if prior:
        if prior.payload['request_digest']!=digest_payload(request):raise WorkflowError('correspondence link identity conflict')
        return prior.payload
    result=evidence.intake(conn,evidence_id=op,source=source,filename='correspondence-'+mid+'.txt',content_base64=base64.b64encode(text[start:end].encode()).decode(),
        issuer='Untrusted '+state.payload['owner']+' correspondent; authenticated transport only',source_url=None,document_date=None,obtained_on=instant().date().isoformat(),
        claim_scope=claim_scope,note=note+' [Explicit owner dossier correlation; not verified sender identity, consent or ownership.]')
    value={'request_digest':digest_payload(request),'source':source,'conversation_id':mid,'message_record_digest':content.digest,'inbound_digest':inbound.digest,
        'excerpt_offsets':[start,end],'evidence_id':result['id'],'authentication_status':'sender_claim_unverified','external_actions':0,'provider_calls':0}
    ws.write(conn,rid,key,value);return value


def dossier_analysis(conn,source):
    row,d,reports=snapshot(conn,source)
    from app.agents.onesimus.portfolio import engineering_sensitivities
    from app.michael import financial_inputs_from_raw,operating_stress_cases
    effective=effective_dossier(d);raw=(effective.get('assessment_inputs') or {}).get('financial') or {};engineering=reports.get('onesimus') or {}
    return {'source':source,'engineering_sensitivities':engineering_sensitivities(engineering),
        'financial_stress':{name:operating_stress_cases(name,item.get('scenario') or item,financial_inputs_from_raw(raw,scenario=name)) for name,item in (engineering.get('alternatives') or {}).items()},
        'campaign':campaign_readiness(conn,source),'stale_dependencies':stale_dependencies(reports),'records_written':0,'provider_calls':0,
        'scope':'On-demand deterministic analysis of retained inputs; original reports are not rewritten. All stresses are explicit assumptions.'}


def correspondence_listing(conn):
    rows,more=latest_rows(conn,('research_artifact',),limit=50,prefixes=['inbound']);items=[]
    for row in rows:
        if not ws.valid_record(row['kind'],row['payload'],row['digest']):continue
        p=row['payload'];mid=(p.get('identity') or {}).get('email_id')
        if not mid:continue
        run=get_run_by_key_in_transaction(conn,'direct-mail:'+str(mid))
        if not run or str(run.run_id)!=str(row['run_id']):continue
        state=ws.read(conn,run.run_id,'state');content=ws.read(conn,run.run_id,'content')
        if not state or state.payload.get('owner') not in ('simeon','solomon') or not content:continue
        items.append({'conversation_id':str(mid),'owner':state.payload['owner'],'subject':p['identity']['subject'],'received_at':p.get('received_at'),
            'content_digest':content.digest,'text':content.payload['text'],'classification':'untrusted_sender_claim','source_record_digest':row['digest']})
    return {'items':items,'has_more':more,'scope':'Retained authenticated transport only. Manual owner dossier correlation is required; no ownership or agreement is inferred.','provider_calls':0,'records_written':0}
