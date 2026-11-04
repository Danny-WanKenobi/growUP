"""Append-only asset observations and obligation monitoring; no external execution."""
from copy import deepcopy
from datetime import date
from decimal import Decimal,InvalidOperation
from uuid import UUID
import os
import re

from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction
from app import manny_review
from app.agents.stages import _load
from app.dossier_reports import append_owner_material_in_transaction

SECTION='owner_asset_events'


def _date(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value): raise WorkflowError('ISO calendar date required')
    try: return date.fromisoformat(value)
    except ValueError: raise WorkflowError('invalid calendar date') from None


def _text(value,name,maximum=500):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum: raise WorkflowError('bounded '+name+' required')
    return value


def _seal(value):
    value=deepcopy(value);value['digest']=digest_payload(value);return value


def _validated_event(raw,property_id,as_of):
    if not isinstance(raw,dict): raise WorkflowError('structured asset event required')
    common={'kind','entity_id','property_id','observed_on','note','evidence_refs','previous_entity_digest'}
    kind=raw.get('kind');allowed=common|({'status'} if kind=='asset_status' else {'status','category','description','due_on','amount_usd','currency'})
    if kind not in ('asset_status','obligation') or set(raw)-allowed: raise WorkflowError('unsupported asset event fields')
    try:
        if UUID(str(raw.get('property_id')))!=UUID(str(property_id)): raise ValueError()
    except (ValueError,TypeError,AttributeError): raise WorkflowError('asset event must bind this property') from None
    entity=_text(raw.get('entity_id'),'entity_id',80)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',entity): raise WorkflowError('simple stable entity_id required')
    if kind=='asset_status' and entity!='asset': raise WorkflowError('asset status uses entity_id=asset')
    observed=_date(raw.get('observed_on'))
    if observed>_date(as_of): raise WorkflowError('future observation cannot establish current status')
    _text(raw.get('note'),'evidence/correction note',1000)
    refs=raw.get('evidence_refs')
    if not isinstance(refs,list) or not 1<=len(refs)<=10: raise WorkflowError('evidence references required')
    for ref in refs: _text(ref,'evidence reference')
    if 'previous_entity_digest' not in raw: raise WorkflowError('explicit previous_entity_digest required (null for new entity)')
    result=deepcopy(raw)
    if kind=='asset_status':
        if raw.get('status') not in ('unknown','under_review','planning','on_hold','reported_operating','reported_inactive'): raise WorkflowError('unsupported reported asset status')
    else:
        if raw.get('status') not in ('open','disputed','reported_satisfied','cancelled'): raise WorkflowError('unsupported obligation status')
        if raw.get('category') not in ('maintenance','inspection','lease','tax','insurance','regulatory','financial','other'): raise WorkflowError('unsupported obligation category')
        _text(raw.get('description'),'obligation description')
        if raw.get('due_on') is not None: _date(raw['due_on'])
        if raw.get('amount_usd') is not None:
            if raw.get('currency')!='USD' or not isinstance(raw['amount_usd'],str): raise WorkflowError('USD amount must be a decimal string')
            try:
                amount=Decimal(raw['amount_usd'])
                if not amount.is_finite() or amount<0 or amount>Decimal('1000000000000') or amount!=amount.quantize(Decimal('0.01')): raise ValueError()
            except (InvalidOperation,ValueError): raise WorkflowError('finite nonnegative cents required') from None
            result['amount_usd']=str(amount.quantize(Decimal('0.01')))
        elif raw.get('currency') not in (None,'USD'): raise WorkflowError('unsupported currency')
    return result


def project(dossier,*,property_id,as_of):
    """Read-only projection for an explicit date; missing coverage stays unknown."""
    _date(as_of);latest={};parent=None
    history=dossier.get(SECTION) or []
    if not isinstance(history,list) or len(history)>100: raise WorkflowError('asset history must be bounded')
    for index,entry in enumerate(history):
        if (not isinstance(entry,dict) or entry.get('digest')!=digest_payload({k:v for k,v in entry.items() if k!='digest'})
                or entry.get('sequence')!=index+1 or entry.get('parent_event_digest')!=parent): raise WorkflowError('asset history integrity/sequence failure')
        event=entry.get('event');validated=_validated_event(event,property_id,entry['as_of'])
        if validated!=event: raise WorkflowError('asset event is not canonical')
        key=(event['kind'],event['entity_id']);previous=latest.get(key)
        if event['previous_entity_digest']!=(previous or {}).get('digest'): raise WorkflowError('asset entity correction lineage failure')
        if previous and event['observed_on']<previous['event']['observed_on']: raise WorkflowError('out-of-order observation; append a current correction')
        latest[key]=entry;parent=entry['digest']
    current={}
    for entry in history:
        if entry['event']['observed_on']<=as_of: current[(entry['event']['kind'],entry['event']['entity_id'])]=entry
    asset=current.get(('asset_status','asset'));obligations=[];exceptions=[];totals={'open':'0.00','disputed':'0.00'};sums={k:Decimal(0) for k in totals};unknown=0
    if asset and asset['event']['status'] in ('on_hold','reported_operating'):
        exceptions.append(dict(entity_digest=asset['digest'],entity_id='asset',reason='asset:'+asset['event']['status']))
    for (kind,entity),entry in sorted(current.items()):
        if kind!='obligation': continue
        event=entry['event'];overdue=event['status']=='open' and event.get('due_on') is not None and event['due_on']<as_of
        item=dict(event=deepcopy(event),entity_digest=entry['digest'],overdue=overdue,classification='reported_unverified',payment_verified=False)
        obligations.append(item)
        if event['status'] in sums:
            if event.get('amount_usd') is None: unknown+=1
            else: sums[event['status']]+=Decimal(event['amount_usd'])
        if overdue or event['status']=='disputed': exceptions.append(dict(entity_digest=entry['digest'],entity_id=entity,reason='obligation:overdue' if overdue else 'obligation:disputed'))
    return dict(schema='manny-asset-register-1',as_of=as_of,sequence=len(history),latest_event_digest=parent,
        asset_status=deepcopy(asset['event']) if asset else {'status':'unknown'},asset_entity_digest=asset['digest'] if asset else None,
        obligations=obligations,exceptions=exceptions,reported_amounts_usd={k:str(v.quantize(Decimal('0.01'))) for k,v in sums.items()},
        active_obligations_with_unknown_amount=unknown,obligations_with_unknown_due_date=sum(r['event'].get('due_on') is None for r in obligations),amounts_scope='Known reported subtotals only; unknown amounts excluded, not zero.',coverage='recorded_observations_only; completeness unknown',
        ownership_verified=False,payments_verified=False,external_actions=0,records_written=0)


def inspect_in_transaction(conn,*,run_id,dossier_key,as_of):
    source,_=_load(conn,UUID(str(run_id)),dossier_key)
    return {**project(source.payload['dossier'],property_id=source.payload['property_id'],as_of=as_of),'source_dossier_digest':source.digest}


def mutate_in_transaction(conn,*,principal,run_id,dossier_key,operation_id,expected_digest,as_of,event=None,operator_authorized=False):
    from app.mvp import _require_enabled
    _require_enabled()
    if not isinstance(principal,str) or not principal.strip() or operator_authorized is not True: raise WorkflowError('explicit operator grant required')
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('asset tracking emergency-stopped')
    _date(as_of);run_id=UUID(str(run_id));source,did=manny_review._locked(conn,run_id,dossier_key)
    request=dict(principal=principal,dossier_key=dossier_key,expected_digest=expected_digest,as_of=as_of,event=event)
    key,replay=manny_review._operation(conn,run_id,dossier_key,operation_id,request)
    if replay: return replay
    source,_=_load(conn,run_id,dossier_key,'manny')
    if source.digest!=expected_digest: raise WorkflowError('source dossier changed; inspect again')
    state=project(source.payload['dossier'],property_id=source.payload['property_id'],as_of=as_of)
    if event is not None:
        event=_validated_event(event,source.payload['property_id'],as_of)
        prior=next((r for r in reversed(source.payload['dossier'].get(SECTION) or []) if (r['event']['kind'],r['event']['entity_id'])==(event['kind'],event['entity_id'])),None)
        if event['previous_entity_digest']!=(prior or {}).get('digest'): raise WorkflowError('entity changed; inspect again')
        if prior and event['observed_on']<prior['event']['observed_on']: raise WorkflowError('out-of-order observation; append a current correction')
        material=_seal(dict(schema='manny-asset-event-1',sequence=state['sequence']+1,parent_event_digest=state['latest_event_digest'],
            source_dossier_digest=source.digest,principal=principal,as_of=as_of,event=event,grants_permission=False))
        source,_=append_owner_material_in_transaction(conn,run_id,dossier_key,section=SECTION,material=material)
        state=project(source.payload['dossier'],property_id=source.payload['property_id'],as_of=as_of)
    created=[]
    for issue in state['exceptions']:
        task_key='owner-asset:'+digest_payload({'dossier_id':str(did),**issue})
        if get_record_in_transaction(conn,run_id,'owner_review_task',task_key) is None:
            task=dict(agent='manny',property_id=source.payload['property_id'],dossier_id=str(did),requirement=issue['reason'],
                entity_id=issue['entity_id'],entity_digest=issue['entity_digest'],detected_as_of=as_of,status='pending',authority='human_owner',grants_permission=False)
            put_record_in_transaction(conn,run_id,kind='owner_review_task',record_key=task_key,version=1,payload=task,digest=digest_payload(task));created.append(task_key)
    brief=manny_review.inspect_in_transaction(conn,run_id=run_id,dossier_key=dossier_key)['brief']
    brief.pop('digest');brief['asset_tracking']=state;brief=_seal(brief)
    source,version=append_owner_material_in_transaction(conn,run_id,dossier_key,section='owner_briefs',material=brief)
    result=dict(asset_tracking=state,created_task_keys=created,owner_brief_digest=brief['digest'],dossier_digest=source.digest,custody_version=version.version,external_actions=0,replayed=False)
    record=dict(request=request,result=result)
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=key,version=1,payload=record,digest=digest_payload(record))
    return result


def audited(*,principal,**inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId.MANNY,run_id=str(inputs['run_id']),event_type='manny.asset_tracking',summary='Append reported asset/obligation observation or monitor due dates',details={'operation_id':inputs['operation_id']},retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try: return mutate_in_transaction(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def retained_view(dossier,property_id):
    """Latest explicit monitoring date; never pretend an old check is current."""
    dates=[entry['as_of'] for entry in dossier.get(SECTION) or []]
    dates.extend(b['asset_tracking']['as_of'] for b in dossier.get('owner_briefs') or [] if isinstance(b.get('asset_tracking'),dict))
    return project(dossier,property_id=property_id,as_of=max(dates)) if dates else None
