"""Bounded Solomon-to-Elmer accounting intake using canonical custody."""
from uuid import UUID
import os
from app.agents.stages import _load,build_report
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction


def preview_in_transaction(conn, *, run_id, dossier_key):
    source,_=_load(conn,UUID(str(run_id)),dossier_key)
    return {'preview_only':True,'source_dossier_digest':source.digest,
        'report':build_report('elmer',source.payload),'records_written':0,'external_actions':0}


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
    if not ready:
        issue_receipt_in_transaction(conn,dossier_id=did,version=version.version,kind='return',
            from_agent='elmer',to_agent='solomon',issued_by=principal,note='Accounting evidence gaps; no records or expenditure invented.')
    result={'status':'elmer_accounting_review' if ready else 'returned_to_solomon','current_holder':'elmer' if ready else 'solomon',
        'report':report,'dossier_digest':stored.digest,'custody_version':version.version,'replayed':False,'external_actions':0}
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
        try: return prepare_in_transaction(conn,principal=principal,**inputs)
        except BaseException:
            try: conn.rollback()
            finally: conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)
