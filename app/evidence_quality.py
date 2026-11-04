"""Declared evidence validity and conflict review; never source authentication."""
from copy import deepcopy
from datetime import date,timedelta
from uuid import UUID
from app import owner_evidence as evidence,wondrue_workspace as ws
from app.communications import lock,instant
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction


def freshness(metadata,policy,as_of):
    """Receipt time never substitutes for the document's effective date."""
    day=date.fromisoformat(as_of)
    if not policy:return {'status':'not_configured','as_of':as_of,'blocks_planning':False,'authentication_granted':False}
    deadline=date.fromisoformat(policy['expires_on']) if policy.get('expires_on') else None
    basis=metadata.get(policy['date_basis'])
    if deadline and day>deadline:status='expired'
    elif basis and date.fromisoformat(basis)>day:status='future_dated'
    elif policy.get('reconfirm_after_days') is not None:
        if not basis:status='date_unknown'
        else:
            start=date.fromisoformat(basis)
            status='future_dated' if start>day else 'reconfirmation_due' if day>=start+timedelta(days=policy['reconfirm_after_days']) else 'within_declared_window'
    else:status='within_declared_window'
    return {'status':status,'as_of':as_of,'declared_date':basis,'date_basis':policy['date_basis'],'expires_on':policy.get('expires_on'),
        'blocks_planning':status!='within_declared_window','authentication_granted':False,
        'scope':'Owner-declared review window only; freshness does not authenticate claims. Expiry date is inclusive; reconfirmation is due on its calculated date.'}


def inspect(conn,run_id,evidence_id,as_of=None):
    from app.evidence_workbench import state
    rid=UUID(str(run_id));value=state(conn,rid,evidence_id);day=as_of or instant().date().isoformat()
    policy=ws.read(conn,rid,'evidence-validity:'+value['id'])
    index=ws.read(conn,rid,'evidence-conflict-index:'+value['id']);conflicts=[]
    for key in (index.payload['keys'] if index else []):
        row=ws.read(conn,rid,key)
        if row is None:raise WorkflowError('conflict history reference missing')
        p=deepcopy(row.payload);p['digest']=row.digest;p['version']=row.version
        if value['id'] not in [r['id'] for r in p['evidence']]:raise WorkflowError('conflict identity binding failed')
        p['current']=True
        for ref in p['evidence']:
            if state(conn,rid,ref['id'])['state_digest']!=ref['state_digest']:p['current']=False
        conflicts.append(p)
    policy_current=not policy or policy.payload['state_digest']==value['state_digest']
    result=freshness(value,policy.payload if policy else None,day)
    if not policy_current:result.update(status='metadata_changed_requires_review',blocks_planning=True)
    blockers=([result['status']] if result['blocks_planning'] else [])
    blockers+=['conflict:'+p['id'] for p in conflicts if not p['current'] or p['disposition']=='unresolved']
    return {'evidence_id':value['id'],'source':value['source'],'state_digest':value['state_digest'],
        'validity_policy':dict(policy.payload,digest=policy.digest) if policy else None,'freshness':result,'conflicts':conflicts,
        'blockers':blockers,'review_digest':digest_payload({'policy':policy.digest if policy else None,'conflicts':[p['digest'] for p in conflicts]}),
        'provider_calls':0,'authentication_granted':False,'records_written':0}


def save_validity(conn,*,run_id,evidence_id,expected_state_digest,expected_policy_digest,expires_on,reconfirm_after_days,date_basis,note):
    from app.evidence_workbench import _current
    rid=UUID(str(run_id));value=_current(conn,rid,evidence_id,expected_state_digest)
    if date_basis not in ('document_date','obtained_on'):raise WorkflowError('explicit declared document/obtained date basis required')
    if expires_on is not None:date.fromisoformat(expires_on)
    if reconfirm_after_days is not None and (isinstance(reconfirm_after_days,bool) or not isinstance(reconfirm_after_days,int) or not 1<=reconfirm_after_days<=3650):raise WorkflowError('reconfirmation interval must be 1-3650 days')
    if expires_on is None and reconfirm_after_days is None:raise WorkflowError('explicit expiration or reconfirmation rule required')
    key='evidence-validity:'+value['id'];prior=ws.read(conn,rid,key)
    request={'state_digest':value['state_digest'],'expires_on':expires_on,'reconfirm_after_days':reconfirm_after_days,'date_basis':date_basis,'note':ws._text(note,1000,'validity reason'),'parent_digest':expected_policy_digest}
    if prior and prior.payload['request_digest']==digest_payload(request):return inspect(conn,rid,value['id'])
    if (prior.digest if prior else None)!=expected_policy_digest:raise WorkflowError('validity policy changed; refresh')
    if prior and prior.version>=50:raise WorkflowError('validity history limit reached')
    ws.write(conn,rid,key,{**request,'request_digest':digest_payload(request),'recorded_at':instant().isoformat(),'authentication_granted':False},prior)
    return inspect(conn,rid,value['id'])


def save_conflict(conn,*,run_id,id,evidence_refs,claim_scope,claims,disposition,note,expected_digest=None):
    from app.evidence_workbench import _current
    rid=UUID(str(run_id));cid=str(UUID(str(id)));lock(conn,'evidence-quality:'+str(rid))
    if claim_scope not in evidence.SCOPES or disposition not in ('unresolved','reconciled_for_planning'):raise WorkflowError('supported claim scope and explicit review disposition required')
    if not isinstance(evidence_refs,list) or len(evidence_refs)!=2 or len({str(r['id']) for r in evidence_refs})!=2:raise WorkflowError('two distinct retained documents required')
    if not isinstance(claims,list) or len(claims)!=2:raise WorkflowError('record both exact competing claims')
    bound=[]
    for ref in sorted(evidence_refs,key=lambda r:str(r['id'])):
        v=_current(conn,rid,ref['id'],ref['state_digest']);bound.append(v)
    if bound[0]['source']['key']!=bound[1]['source']['key']:raise WorkflowError('conflicting documents must belong to the same dossier')
    if disposition=='reconciled_for_planning' and claim_scope not in ('financial','other'):
        raise WorkflowError('identity, category and physical/regulatory conflicts require authenticated upstream evidence; cannot reconcile here')
    request={'id':cid,'evidence':deepcopy(evidence_refs),'claim_scope':claim_scope,'claims':[ws._text(c,2000,'competing claim') for c in claims],
        'disposition':disposition,'note':ws._text(note,1000,'conflict explanation'),'parent_digest':expected_digest}
    key='evidence-conflict:'+cid;prior=ws.read(conn,rid,key)
    if prior and prior.payload['request_digest']==digest_payload(request):return dict(prior.payload,digest=prior.digest,version=prior.version)
    if (prior.digest if prior else None)!=expected_digest:raise WorkflowError('conflict decision changed; refresh')
    if prior and (prior.payload['evidence'][0]['id']!=evidence_refs[0]['id'] or prior.payload['evidence'][1]['id']!=evidence_refs[1]['id'] or prior.payload['claim_scope']!=claim_scope):raise WorkflowError('conflict identity/scope cannot change')
    if prior and prior.version>=50:raise WorkflowError('conflict history limit reached')
    for value in bound:
        index=ws.read(conn,rid,'evidence-conflict-index:'+value['id']);keys=index.payload['keys'] if index else []
        if key not in keys:
            if len(keys)>=20:raise WorkflowError('document conflict limit reached')
            ws.write(conn,rid,'evidence-conflict-index:'+value['id'],{'keys':keys+[key]},index)
    row=ws.write(conn,rid,key,{**request,'request_digest':digest_payload(request),'recorded_at':instant().isoformat(),'authentication_granted':False,
        'scope':'Administrative planning explanation only; original claims and canonical verification conflicts remain unchanged.'},prior)
    return dict(row.payload,digest=row.digest,version=row.version)


def conflict_history(conn,run_id,id):
    rid=UUID(str(run_id));key='evidence-conflict:'+str(UUID(str(id)));latest=ws.read(conn,rid,key)
    if latest is None:raise WorkflowError('conflict not found')
    for ref in latest.payload['evidence']:evidence.inspect(conn,rid,ref['id'])
    result=[];parent=None
    for version in range(1,latest.version+1):
        row=get_record_in_transaction(conn,rid,'research_artifact',key,version)
        if row is None or row.digest!=digest_payload(row.payload) or row.payload.get('parent_digest')!=parent:raise WorkflowError('conflict decision history integrity failure')
        result.append(dict(row.payload,digest=row.digest,version=row.version));parent=row.digest
    return {'decisions':result,'records_written':0,'authentication_granted':False}
