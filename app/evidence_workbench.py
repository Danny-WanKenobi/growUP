"""Linked evidence lifecycle; extraction and human notes never mint verification."""
from copy import deepcopy
from pathlib import Path
from uuid import UUID
import base64,json,os,subprocess,sys,tempfile
from app import owner_evidence as evidence,wondrue_workspace as ws
from app.communications import instant,lock
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction

TARGETS={'identity':['raphael'],'category':['raphael'],'dimensions':['raphael','onesimus'],
    'occupancy':['raphael'],'zoning':['nicole'],'structural':['raphael','onesimus'],
    'electrical':['onesimus'],'water':['onesimus'],'financial':['michael','elmer'],'other':['human_owner']}
FIELDS=('declared_issuer','source_url','document_date','obtained_on','claim_scope','note')


def state(conn,run_id,evidence_id):
    rid=UUID(str(run_id));base=evidence.inspect(conn,rid,evidence_id)
    revision=ws.read(conn,rid,'evidence-revision:'+base['id'])
    value=deepcopy(base)
    if revision:
        if revision.payload['metadata_digest']!=base['metadata_digest']:raise WorkflowError('revision source mismatch')
        value.update(revision.payload['metadata']);value['revision_digest']=revision.digest;value['revision']=revision.version;value['supersedes']=revision.payload.get('supersedes')
    else:value.update(revision=0,revision_digest=None)
    value['state_digest']=digest_payload({'metadata':base['metadata_digest'],'revision':value['revision_digest']})
    extraction=ws.read(conn,rid,'evidence-extraction:'+base['id'])
    review=ws.read(conn,rid,'evidence-admission:'+base['id'])
    if extraction:
        if extraction.payload.get('sha256')!=base['sha256']:raise WorkflowError('extraction byte binding failed')
        value['extraction']=dict(extraction.payload,digest=extraction.digest)
    if review:
        value['admission']=dict(review.payload,digest=review.digest,current=review.payload['state_digest']==value['state_digest'])
    return value


def _current(conn,rid,eid,expected):
    lock(conn,'evidence-lifecycle:'+str(rid)+str(eid));value=state(conn,rid,eid)
    if value['state_digest']!=expected:raise WorkflowError('evidence metadata changed; refresh')
    return value


def revise(conn,*,run_id,evidence_id,expected_digest,metadata,reason,operation_id,supersedes=None):
    rid=UUID(str(run_id));operation_id=str(UUID(str(operation_id)))
    # Serialize the document graph, not just an individual node: concurrent
    # A->B and B->A declarations must not both pass cycle detection.
    lock(conn,'evidence-supersedes:'+str(rid))
    lock(conn,'evidence-lifecycle:'+str(rid)+str(evidence_id))
    replay=ws.read(conn,rid,'evidence-correction-op:'+operation_id)
    request={'id':str(evidence_id),'expected':expected_digest,'metadata':metadata,'reason':reason,'supersedes':supersedes}
    if replay:
        if replay.payload['request_digest']!=digest_payload(request):raise WorkflowError('correction identity conflict')
        return replay.payload['result']
    value=_current(conn,rid,evidence_id,expected_digest)
    if not isinstance(metadata,dict) or set(metadata)!=set(FIELDS):raise WorkflowError('complete bounded declared metadata required')
    metadata=deepcopy(metadata)
    metadata['declared_issuer'],metadata['note']=evidence.validate_provenance(metadata['declared_issuer'],metadata['source_url'],metadata['document_date'],metadata['obtained_on'],metadata['claim_scope'],metadata['note'])
    reason=ws._text(reason,1000,'correction reason')
    parent=None
    supersedes=supersedes or (value.get('supersedes') or {}).get('id')
    if supersedes is not None:
        parent=evidence.inspect(conn,rid,supersedes)
        if parent['id']==value['id'] or parent['property_id']!=value['property_id'] or parent['source']['key']!=value['source']['key']:
            raise WorkflowError('superseding documents must be distinct and bind the same dossier')
    if parent:
        seen={value['id']};cursor=parent['id']
        for _ in range(20):
            if cursor in seen:raise WorkflowError('superseding document cycle refused')
            seen.add(cursor);ancestor=ws.read(conn,rid,'evidence-revision:'+cursor)
            cursor=((ancestor.payload.get('supersedes') or {}).get('id') if ancestor else None)
            if cursor is None:break
        else:raise WorkflowError('superseding document lineage exceeds limit')
    prior=ws.read(conn,rid,'evidence-revision:'+value['id'])
    if prior and prior.version>=50:raise WorkflowError('document revision limit reached')
    row=ws.write(conn,rid,'evidence-revision:'+value['id'],{'metadata_digest':value['metadata_digest'],'metadata':metadata,
        'parent_revision_digest':prior.digest if prior else None,'reason':reason,'recorded_at':instant().isoformat(),
        'supersedes':{'id':parent['id'],'metadata_digest':parent['metadata_digest']} if parent else None,'authentication_granted':False},prior)
    task=get_record_in_transaction(conn,rid,'owner_review_task',value['review_task_key'])
    payload=dict(task.payload,reason=metadata['note'],evidence_revision_digest=row.digest,status='pending')
    put_record_in_transaction(conn,rid,kind='owner_review_task',record_key=task.record_key,version=task.version+1,payload=payload,digest=digest_payload(payload))
    result=state(conn,rid,value['id'])
    ws.write(conn,rid,'evidence-correction-op:'+operation_id,{'request_digest':digest_payload(request),'result':result})
    return result


def run_extractor(raw,mime):
    env={k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP') if k in os.environ}
    request=json.dumps({'mime':mime,'content_base64':base64.b64encode(raw).decode()}).encode()
    try:
        with tempfile.TemporaryDirectory(prefix='growup-document-') as cwd:
            result=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).with_name('document_worker.py'))],input=request,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,cwd=cwd,env=env,timeout=8,creationflags=0x08000000 if os.name=='nt' else 0)
        if result.returncode or len(result.stdout)>200000:raise ValueError()
        value=json.loads(result.stdout)
        if value.get('status')=='extracted' and value.get('resource_limits_enforced') is not True:raise ValueError()
        return value
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return {'status':'parser_refused','segments':[],'measurements':[],'reason':'Parser unavailable, interrupted or exceeded its resource envelope; original retained.'}


def extract(conn,*,run_id,evidence_id,expected_digest):
    rid=UUID(str(run_id));value=_current(conn,rid,evidence_id,expected_digest)
    old=ws.read(conn,rid,'evidence-extraction:'+value['id'])
    if old:return state(conn,rid,value['id'])
    metadata,raw=evidence.download(conn,rid,value['id'])
    result=run_extractor(raw,metadata['detected_type'])
    result.update(evidence_id=value['id'],sha256=value['sha256'],metadata_digest=value['metadata_digest'],
        extracted_at=instant().isoformat(),classification='unverified_extraction',authentication_granted=False,
        parser_version='bounded-text-1')
    ws.write(conn,rid,'evidence-extraction:'+value['id'],result)
    return state(conn,rid,value['id'])


def admit(conn,*,run_id,evidence_id,expected_digest,choice,note,collection_run_id=None,expected_admission_digest=None):
    rid=UUID(str(run_id));value=_current(conn,rid,evidence_id,expected_digest)
    if choice not in ('hold','reject','check_official_match'):raise WorkflowError('explicit evidence disposition required')
    prior=ws.read(conn,rid,'evidence-admission:'+value['id'])
    request_digest=digest_payload({'choice':choice,'state':value['state_digest'],'note':note,'collection':str(collection_run_id) if collection_run_id else None,'parent':expected_admission_digest})
    if prior and prior.payload.get('request_digest')==request_digest:return value
    if (prior.digest if prior else None)!=expected_admission_digest:raise WorkflowError('admission decision changed; refresh')
    if prior and prior.version>=100:raise WorkflowError('admission history limit reached')
    result={'request_digest':request_digest,'choice':choice,'state_digest':value['state_digest'],'sha256':value['sha256'],'note':ws._text(note,1000,'review reason'),
        'status':'rejected_claim' if choice=='reject' else 'needs_evidence','authentication_granted':False,'recorded_at':instant().isoformat(),
        'basis':['Owner-supplied documents cannot authenticate themselves; no shared-policy override.']}
    if choice=='check_official_match':
        if collection_run_id is None:raise WorkflowError('an exact retained official collection run is required')
        from app.mvp import _load_verified_successor_inputs,preview_successor_in_transaction
        source=ws.record(conn,rid,'dossier_version',value['source']['key'])
        bundle,blockers=_load_verified_successor_inputs(conn,property_id=UUID(value['property_id']),parent_run_id=rid,
            dossier_key=value['source']['key'],collection_run_id=UUID(str(collection_run_id)))
        collected=bundle.get('collection') or {}
        match=collected.get('body_sha256')==value['sha256']
        if not match:blockers=list(blockers)+['Uploaded bytes do not exactly match the retained authenticated collection.']
        if blockers:result['basis']=blockers
        else:
            preview=preview_successor_in_transaction(conn,property_id=value['property_id'],parent_run_id=str(rid),dossier_key=source['key'],collection_run_id=str(collection_run_id))
            result.update(status='shared_policy_preview' if preview['ready'] else 'needs_evidence',shared_preview=preview,
                basis=preview.get('blockers') or preview.get('intended',{}).get('decision_basis',[]),collection_run_id=str(collection_run_id))
        # The original upload remains unverified. Only the existing separately
        # authorized successor commit may admit collector evidence canonically.
    prior=ws.read(conn,rid,'evidence-admission:'+value['id'])
    result['parent_decision_digest']=prior.digest if prior else None
    ws.write(conn,rid,'evidence-admission:'+value['id'],result,prior)
    return state(conn,rid,value['id'])


def route_plan(conn,*,run_id,evidence_id):
    value=state(conn,run_id,evidence_id)
    source=ws.record(conn,run_id,'dossier_version',value['source']['key'])
    from app.wondrue_insights import dependency_plan
    from app.manny_review import queue_in_transaction
    targets=list(dict.fromkeys(a for s in value['claim_scope'] for a in TARGETS[s]))
    tasks=queue_in_transaction(conn,run_id=run_id,dossier_key=source['key'])['tasks']
    return {'evidence':{'id':value['id'],'state_digest':value['state_digest'],'sha256':value['sha256']},
        'source':{k:source[k] for k in ('run_id','key','version','digest')},'suggested_agents':targets,
        'matching_review_tasks':[{'key':t['record_key'],'requirement':t.get('requirement'),'status':t['review_status']} for t in tasks if t['review_status'] not in ('closed_by_owner','closed_as_duplicate')],
        'downstream':dependency_plan(source['data']['dossier']),'admission':value.get('admission'),
        'automatic_execution':False,'limits':'Scope suggests responsibility, not evidence truth. Select an exact unresolved task and approve a preview; no source fact changes are inferred.'}


def resolve_links(conn,run_id,dossier_key,links):
    if not isinstance(links,list) or len(links)>8:raise WorkflowError('at most eight explicit evidence links')
    resolved=[]
    for ref in links:
        if not isinstance(ref,dict) or set(ref)!={'id','state_digest'}:raise WorkflowError('exact evidence link required')
        lock(conn,'evidence-lifecycle:'+str(UUID(str(run_id)))+str(UUID(str(ref['id']))))
        value=state(conn,run_id,ref['id'])
        if value['source']['key']!=dossier_key or value['state_digest']!=ref['state_digest']:raise WorkflowError('linked evidence changed or belongs to another dossier')
        admission=value.get('admission') or {}
        if admission.get('current') and admission.get('status')=='rejected_claim':raise WorkflowError('rejected evidence cannot support reassessment')
        resolved.append({'id':value['id'],'state_digest':value['state_digest'],'sha256':value['sha256'],
            'authentication_status':value['authentication_status'],'extraction_digest':(value.get('extraction') or {}).get('digest'),
            'admission_digest':admission.get('digest') if admission.get('current') else None})
    return resolved


def assumption_changes(conn,source,target,changes,supporting):
    """Only explicitly selected extracted quantities; never building verification."""
    from decimal import Decimal
    from app.linked_reassessments import effective_dossier
    from app.agents.onesimus.engineering import CROP_UNITS
    from app.michael import FINANCIAL_UNITS,FINANCIAL_PERIODS
    if not changes:return {}
    if target not in ('onesimus','michael') or not isinstance(changes,list) or not 1<=len(changes)<=8:
        raise WorkflowError('bounded Onesimus crop or Michael rate assumptions only')
    group='crop' if target=='onesimus' else 'financial';units=CROP_UNITS if group=='crop' else FINANCIAL_UNITS
    values=deepcopy((effective_dossier(source.payload['dossier']).get('assessment_inputs') or {}).get(group) or {})
    seen=set()
    for change in changes:
        if not isinstance(change,dict) or set(change)!={'evidence_id','measurement_index','name','unit'}:raise WorkflowError('select an exact extracted quantity, field and unit')
        name=change['name']
        if name not in units or change['unit']!=units[name] or name in seen:raise WorkflowError('unsupported, duplicate or mismatched assumption unit')
        link=next((s for s in supporting if s['id']==change['evidence_id']),None)
        if not link:raise WorkflowError('assumption requires approved supporting evidence')
        value=state(conn,source.run_id,link['id']);measurements=(value.get('extraction') or {}).get('measurements',[]);index=change['measurement_index']
        if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(measurements):raise WorkflowError('exact retained measurement required')
        item=measurements[index]
        if item['unit_text']!=change['unit']:raise WorkflowError('no implicit unit conversion for extracted claims')
        number=Decimal(item['value_text'])
        if not number.is_finite() or number<0:raise WorkflowError('finite nonnegative assumed quantity required')
        citation='evidence:'+link['id']+'@'+value['sha256']+'#'+json.dumps(item['locator'],sort_keys=True)
        if group=='crop':values[name]={'value':float(number),'unit':change['unit'],'classification':'assumed','provenance':citation}
        else:values[name]={'value':str(number),'unit':change['unit'],'classification':'assumed','source':citation,
            'currency':'USD','period':sorted(FINANCIAL_PERIODS[name])[0],'observed_date':value.get('document_date') or '',
            'uncertainty':'Owner-selected extracted assumption; not an authenticated market rate.'}
        seen.add(name)
    return {group:values}
