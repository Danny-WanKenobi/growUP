"""Durable, evidence-referenced project observations; no construction execution."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import UUID
import os

from app.agents.solomon import PHASES
from app.agents.stages import _load
from app.workflow import digest_payload
from app.workflow_db import WorkflowError, get_record_in_transaction, put_record_in_transaction, list_records_in_transaction

KIND = 'solomon_project_event'


def _text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value)>limit:
        raise WorkflowError('invalid '+name)
    return value.strip()


def validate_event(raw):
    if not isinstance(raw, dict): raise WorkflowError('structured project event required')
    allowed={'kind','phase','status','evidence_refs','note','cost_delta_usd','schedule_delta_days'}
    if set(raw)-allowed: raise WorkflowError('unknown project event fields')
    event=deepcopy(raw)
    event['note']=_text(event.get('note'),'note')
    if not isinstance(event.get('phase'),str) or event['phase'] not in dict(PHASES): raise WorkflowError('unknown project phase')
    refs=event.get('evidence_refs')
    if not isinstance(refs,list) or not 1<=len(refs)<=10: raise WorkflowError('1-10 evidence references required')
    event['evidence_refs']=[_text(r,'evidence reference',500) for r in refs]
    kind=event.get('kind'); status=event.get('status')
    statuses={'progress':('planned','blocked','reported_complete'),
              'inspection':('pending','passed_claim','failed'), 'change_order':('proposed',)}
    if not isinstance(kind,str) or kind not in statuses or status not in statuses[kind]: raise WorkflowError('invalid project event kind/status')
    if kind=='change_order':
        try:
            raw_cost=event.get('cost_delta_usd')
            if isinstance(raw_cost,bool) or len(str(raw_cost))>40: raise ValueError()
            cost=Decimal(str(raw_cost))
            if not cost.is_finite() or abs(cost)>Decimal('1e12') or cost.as_tuple().exponent < -2: raise ValueError()
            days=event.get('schedule_delta_days')
            if isinstance(days,bool) or not isinstance(days,int) or abs(days)>3650: raise ValueError()
        except (InvalidOperation,ValueError,TypeError): raise WorkflowError('bounded USD delta and integer day delta required') from None
        event['cost_delta_usd']=format(cost,'.2f')
    elif 'cost_delta_usd' in event or 'schedule_delta_days' in event:
        raise WorkflowError('only proposed changes carry cost/schedule deltas')
    return event


def inspect_in_transaction(conn, *, run_id, dossier_key):
    source,_=_load(conn,UUID(str(run_id)),dossier_key)
    return {'project':deepcopy(source.payload.get('dossier',{}).get('solomon_project')),
        'source_dossier_digest':source.digest,'records_written':0,'external_actions':0}


def record_in_transaction(conn, *, principal, run_id, dossier_key, event_id, expected_sequence, event):
    from app.mvp import _require_enabled
    from app.dossier_reports import append_solomon_tracking_in_transaction
    _require_enabled()
    principal=_text(principal,'principal',200); event_id=_text(event_id,'event_id',100)
    if isinstance(expected_sequence,bool) or not isinstance(expected_sequence,int) or expected_sequence<0:
        raise WorkflowError('nonnegative expected_sequence required')
    event=validate_event(event); run_id=UUID(str(run_id))
    # _load acquires the shared dossier lock, preventing simultaneous sequence winners.
    source,did=_load(conn,run_id,dossier_key,'solomon')
    request={'principal':principal,'dossier_key':dossier_key,'event_id':event_id,
             'expected_sequence':expected_sequence,'event':event}
    key='solomon:'+digest_payload({'dossier_key':dossier_key,'event_id':event_id})
    prior=get_record_in_transaction(conn,run_id,KIND,key)
    if prior:
        if prior.digest!=digest_payload(prior.payload) or prior.payload.get('request')!=request:
            raise WorkflowError('project event replay conflict')
        return {**deepcopy(prior.payload['result']),'replayed':True}
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('project updates emergency-stopped')
    dossier=source.payload.get('dossier') or {}
    report=(dossier.get('reports') or {}).get('solomon') or {}
    if (report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'})
            or not (report.get('preconstruction_assessment') or {}).get('planning_ready')):
        raise WorkflowError('held planning-ready Solomon report required')
    current=deepcopy(dossier.get('solomon_project') or {'sequence':0,'phases':{},'inspections':{},'proposed_changes':[]})
    if current['sequence']!=expected_sequence: raise WorkflowError('project sequence mismatch; inspect before retry')
    phase=event['phase']; phase_names=[p[0] for p in PHASES]
    if event['kind']=='progress':
        if event['status']=='reported_complete':
            predecessors=phase_names[:phase_names.index(phase)]
            if any(current['phases'].get(p)!='reported_complete' for p in predecessors):
                raise WorkflowError('preceding phase reports are incomplete')
            if current['inspections'].get(phase)=='failed': raise WorkflowError('failed inspection remains unresolved')
        current['phases'][phase]=event['status']
    elif event['kind']=='inspection': current['inspections'][phase]=event['status']
    else:
        if len(current['proposed_changes'])>=100: raise WorkflowError('proposed-change limit reached')
        current['proposed_changes'].append({'event_id':event_id,**event,'approval_status':'pending','applied':False})
    if (event['kind']=='progress' and event['status']!='reported_complete') or (event['kind']=='inspection' and event['status']=='failed'):
        for dependent in phase_names[phase_names.index(phase)+1:]:
            if dependent in current['phases']: current['phases'][dependent]='blocked'
        if event['kind']=='inspection': current['phases'][phase]='blocked'
    base=Decimal(str(report['preconstruction_assessment']['budget']['estimated_total_usd']))
    delta=sum((Decimal(c['cost_delta_usd']) for c in current['proposed_changes']),Decimal(0))
    if base+delta<0: raise WorkflowError('proposed total cannot be negative')
    baseline_days=(report['preconstruction_assessment'].get('schedule_plan') or {}).get('total_days')
    proposed_days=baseline_days+sum(c['schedule_delta_days'] for c in current['proposed_changes']) if baseline_days is not None else None
    if proposed_days is not None and proposed_days<0: raise WorkflowError('proposed schedule cannot be negative')
    current.update(sequence=expected_sequence+1,property_id=source.payload['property_id'],
        source_report_digest=report['digest'],last_event_id=event_id,
        proposed_total_usd=format(base+delta,'.2f'),baseline_estimate_usd=format(base,'.2f'),
        proposed_schedule_delta_days=sum(c['schedule_delta_days'] for c in current['proposed_changes']),
        baseline_schedule_days=baseline_days,proposed_schedule_days=proposed_days,
        schedule_scope='sum of proposed deltas only; no baseline duration or completion date verified',
        classification='synthetic' if source.payload.get('synthetic') is True else 'operator_reported_unverified',
        construction_authorized=False,procurement_authorized=False,external_actions=0,
        status='planning_observations_only',
        limitations=['Progress and passed inspections are supplied claims, not authenticated completion or safety certification.',
                    'Change proposals are never applied to the approved budget or scope.'])
    stored,version=append_solomon_tracking_in_transaction(conn,run_id,dossier_key,current)
    result={'project':current,'dossier_digest':stored.digest,'custody_version':version.version,'replayed':False,'external_actions':0}
    payload={'request':request,'source_dossier_digest':source.digest,'result':result}
    put_record_in_transaction(conn,run_id,kind=KIND,record_key=key,version=1,payload=payload,digest=digest_payload(payload))
    if event['kind']=='change_order' or (event['kind']=='inspection' and event['status']=='failed'):
        task={'agent':'solomon','property_id':source.payload['property_id'],'dossier_id':str(did),
              'requirement':'project:'+event_id,'reason':event['note'],'route':'human_review',
              'status':'pending','authority':'human_owner','grants_permission':False,'project_event_key':key}
        put_record_in_transaction(conn,run_id,kind='owner_review_task',record_key='owner:'+digest_payload(task),
            version=1,payload=task,digest=digest_payload(task))
    return result


def record_audited(*, principal, **inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId.SOLOMON,run_id=str(inputs['run_id']),event_type='solomon.project_observation',
        summary='Internal project observation; no work or expenditure authorized',
        details={'principal':principal,'event_id':inputs['event_id']},retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try: return record_in_transaction(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)
