"""Bounded Solomon-to-Elmer accounting intake using canonical custody."""
from uuid import UUID
import os
from app.agents.stages import _load,build_report
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction


def preview_in_transaction(conn, *, run_id, dossier_key):
    source,_=_load(conn,UUID(str(run_id)),dossier_key)
    from app.agents.elmer import latest_assessment
    latest=latest_assessment(source.payload.get('dossier') or {})
    return {'preview_only':True,'source_dossier_digest':source.digest,
        'report':latest if latest is not None else build_report('elmer',source.payload),'records_written':0,'external_actions':0}


def prepare_in_transaction(conn, *, principal, run_id, dossier_key, operation_id, expected_digest):
    from app.mvp import _require_enabled
    from app.custody_db import lock_dossier_in_transaction,issue_receipt_in_transaction,latest_version_in_transaction
    from app.dossier_reports import append_agent_report_in_transaction
    _require_enabled()
    if not isinstance(principal,str) or not principal.strip(): raise WorkflowError('authenticated operator required')
    if not isinstance(operation_id,str) or not operation_id.strip() or len(operation_id)>100: raise WorkflowError('bounded operation_id required')
    run_id=UUID(str(run_id))
    from app.custody import dossier_id_for_property
    identity=get_record_in_transaction(conn,run_id,'dossier_version',dossier_key)
    if identity is None: raise WorkflowError('stored dossier not found')
    did=dossier_id_for_property(identity.payload.get('property_id'))
    lock_dossier_in_transaction(conn,did)
    source,_=_load(conn,run_id,dossier_key)
    request={'principal':principal,'dossier_key':dossier_key,'operation_id':operation_id,'expected_digest':expected_digest}
    key='elmer-intake:'+digest_payload({'dossier_key':dossier_key,'operation_id':operation_id})
    prior=get_record_in_transaction(conn,run_id,'research_artifact',key)
    if prior:
        if prior.digest!=digest_payload(prior.payload) or prior.payload.get('request')!=request: raise WorkflowError('accounting intake replay conflict')
        return {**prior.payload['result'],'replayed':True}
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('accounting intake emergency-stopped')
    source,_=_load(conn,run_id,dossier_key,'solomon')
    if source.digest!=expected_digest: raise WorkflowError('source dossier changed; preview again')
    if (source.payload.get('dossier',{}).get('reports') or {}).get('elmer') is not None:
        raise WorkflowError('existing Elmer report requires explicit reconciliation')
    report=build_report('elmer',source.payload)
    latest=latest_version_in_transaction(conn,did)
    issue_receipt_in_transaction(conn,dossier_id=did,version=latest.version,kind='forward',
        from_agent='solomon',to_agent='elmer',issued_by=principal,note='Preliminary accounting intake only; no spending, payment or tax authority.')
    stored,version=append_agent_report_in_transaction(conn,run_id,dossier_key,agent='elmer',report=report)
    for issue in report['owner_review_requests']:
        task={**issue,'agent':'elmer','dossier_id':str(did),'property_id':report['property_id'],
            'source_dossier_digest':source.digest,'report_digest':report['digest'],'grants_permission':False}
        put_record_in_transaction(conn,run_id,kind='owner_review_task',record_key='owner:'+digest_payload(task),
            version=1,payload=task,digest=digest_payload(task))
    ready=not report['missing']
    complete=ready and not report['accounting_assessment']['exceptions']
    if complete:
        issue_receipt_in_transaction(conn,dossier_id=did,version=version.version,kind='forward',from_agent='elmer',to_agent='manny',
            issued_by=principal,note='Accounting preparation complete; Manny is the software owner agent, not legal signing or payment authority.')
    if not ready:
        issue_receipt_in_transaction(conn,dossier_id=did,version=version.version,kind='return',
            from_agent='elmer',to_agent='solomon',issued_by=principal,note='Accounting evidence gaps; no records or expenditure invented.')
    owner_package=None
    if complete:
        from app.manny_review import prepare_on_arrival_in_transaction
        owner_package=prepare_on_arrival_in_transaction(conn,principal=principal,run_id=run_id,dossier_key=dossier_key,
            assessment_digest=report['digest'],dossier_digest=stored.digest)
    result={'status':'delivered_to_manny' if complete else 'elmer_accounting_review' if ready else 'returned_to_solomon','current_holder':'manny' if complete else 'elmer' if ready else 'solomon',
        'report':report,'dossier_digest':stored.digest,'custody_version':version.version,'replayed':False,'external_actions':0}
    if owner_package:
        result.update(owner_brief_digest=owner_package['brief']['digest'],dossier_digest=owner_package['dossier_digest'],custody_version=owner_package['custody_version'])
    payload={'request':request,'result':result}
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=key,version=1,payload=payload,digest=digest_payload(payload))
    return result


def prepare_audited(*,principal,**inputs):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event=AgentEvent(agent=AgentId.ELMER,run_id=str(inputs['run_id']),event_type='elmer.accounting_intake',
        summary='Preliminary internal accounting intake; no payment or tax action',
        details={'principal':principal,'operation_id':inputs['operation_id']},retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try:
            options=dict(inputs);action=options.pop('action','prepare')
            if action=='prepare': return prepare_in_transaction(conn,principal=principal,**options)
            return reconcile_in_transaction(conn,principal=principal,action=action,**options)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def reconcile_in_transaction(conn,*,principal,run_id,dossier_key,operation_id,expected_digest,action='reconcile',correction=None):
    from copy import deepcopy
    from app.mvp import _require_enabled
    from app.custody import dossier_id_for_property
    from app.custody_db import lock_dossier_in_transaction,current_custody_in_transaction,latest_version_in_transaction,issue_receipt_in_transaction
    from app.dossier_reports import append_elmer_correction_in_transaction
    _require_enabled()
    if not isinstance(principal,str) or not principal.strip(): raise WorkflowError('authenticated operator required')
    if not isinstance(operation_id,str) or not operation_id.strip() or len(operation_id)>100: raise WorkflowError('bounded operation_id required')
    if action not in ('reconcile','complete'): raise WorkflowError('unknown accounting operation')
    run_id=UUID(str(run_id)); identity=get_record_in_transaction(conn,run_id,'dossier_version',dossier_key)
    if identity is None: raise WorkflowError('stored dossier not found')
    did=dossier_id_for_property(identity.payload.get('property_id'));lock_dossier_in_transaction(conn,did)
    source,_=_load(conn,run_id,dossier_key)
    request={'principal':principal,'dossier_key':dossier_key,'operation_id':operation_id,'expected_digest':expected_digest,'action':action,'correction':correction}
    key='elmer-assessment:'+digest_payload({'dossier_key':dossier_key,'operation_id':operation_id})
    prior=get_record_in_transaction(conn,run_id,'research_artifact',key)
    if prior:
        if prior.digest!=digest_payload(prior.payload) or prior.payload.get('request')!=request: raise WorkflowError('accounting correction replay conflict')
        return {**prior.payload['result'],'replayed':True}
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1': raise WorkflowError('accounting intake emergency-stopped')
    if source.digest!=expected_digest: raise WorkflowError('source dossier changed; preview again')
    holder=current_custody_in_transaction(conn,did)
    if not holder or holder['holder'] not in ('elmer','manny','solomon'): raise WorkflowError('accounting correction requires accounting/owner/rework custody')
    dossier=source.payload.get('dossier') or {}; history=dossier.get('elmer_assessments') or []
    from app.agents.elmer import latest_assessment
    try: previous=latest_assessment(dossier)
    except ValueError as exc: raise WorkflowError(str(exc)) from None
    if not isinstance(previous,dict) or previous.get('digest')!=digest_payload({k:v for k,v in previous.items() if k!='digest'}):
        raise WorkflowError('intact prior Elmer assessment required')
    latest=latest_version_in_transaction(conn,did)
    if action=='complete' and holder['holder']!='elmer': raise WorkflowError('completion requires held Elmer assessment; Manny already holds completed work')
    if holder['holder']!='elmer':
        issue_receipt_in_transaction(conn,dossier_id=did,version=latest.version,
            kind='return' if holder['holder']=='manny' else 'forward',from_agent=holder['holder'],to_agent='elmer',
            issued_by=principal,note='Explicit accounting reassessment; original reports and receipts preserved.')
    if action=='reconcile':
        if (not isinstance(correction,dict) or set(correction)-{'reason','inputs'} or not isinstance(correction.get('reason'),str)
                or not correction['reason'].strip() or len(correction['reason'])>500 or not isinstance(correction.get('inputs'),dict)
                or not correction['inputs'] or set(correction['inputs'])-{'accounting_period','transactions','entity_and_tax_jurisdiction'}):
            raise WorkflowError('reason and bounded accounting input replacements required')
        candidate=deepcopy(source.payload)
        effective=deepcopy(previous.get('corrected_inputs') or dossier.get('specialist_inputs',{}).get('elmer') or {})
        effective.update(deepcopy(correction['inputs']))
        candidate['dossier'].setdefault('specialist_inputs',{})['elmer']=effective
        candidate['digest']=digest_payload({k:v for k,v in candidate.items() if k!='digest'})
        report=build_report('elmer',candidate)
        report.update(source_dossier_digest=source.digest,parent_assessment_digest=previous['digest'],
            assessment_number=len(history)+2,correction_reason=correction['reason'],corrected_inputs=effective)
        report['digest']=digest_payload({k:v for k,v in report.items() if k!='digest'})
        stored,latest=append_elmer_correction_in_transaction(conn,run_id,dossier_key,report)
    else:
        if correction is not None: raise WorkflowError('completion does not replace accounting evidence')
        stored=source;report=previous
    complete=not report.get('missing') and (report.get('accounting_assessment') or {}).get('intake_ready') is True and not report['accounting_assessment']['exceptions']
    if action=='complete' and not complete: raise WorkflowError('accounting gaps or exceptions require correction before completion')
    for issue in report.get('owner_review_requests') or []:
        task={**issue,'agent':'elmer','property_id':source.payload['property_id'],'dossier_id':str(did),'assessment_digest':report['digest'],'grants_permission':False}
        put_record_in_transaction(conn,run_id,kind='owner_review_task',record_key='owner:'+digest_payload(task),version=1,payload=task,digest=digest_payload(task))
    if complete:
        issue_receipt_in_transaction(conn,dossier_id=did,version=latest.version,kind='forward',from_agent='elmer',to_agent='manny',issued_by=principal,
            note='Accounting preparation complete; no legal, payment or tax authority granted.')
    owner_package=None
    if complete:
        from app.manny_review import prepare_on_arrival_in_transaction
        owner_package=prepare_on_arrival_in_transaction(conn,principal=principal,run_id=run_id,dossier_key=dossier_key,
            assessment_digest=report['digest'],dossier_digest=stored.digest)
    result={'status':'delivered_to_manny' if complete else 'elmer_accounting_review','current_holder':'manny' if complete else 'elmer',
        'assessment':report,'dossier_digest':stored.digest,'custody_version':latest.version,'replayed':False,'external_actions':0}
    if owner_package:
        result.update(owner_brief_digest=owner_package['brief']['digest'],dossier_digest=owner_package['dossier_digest'],custody_version=owner_package['custody_version'])
    payload={'request':request,'result':result}
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=key,version=1,payload=payload,digest=digest_payload(payload))
    return result
