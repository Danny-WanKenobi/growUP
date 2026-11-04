"""Real retained-property gate proof. Never substitutes assumptions for evidence."""
from uuid import UUID
from app.agents.stages import _load
from app.agents.raphael.service import evaluate_dossier
from app.wondrue_retained_review import guard
from app.custody_db import current_custody_in_transaction,verify_chain_in_transaction
from app.linked_reassessments import effective_reports,stale_dependencies
from app.workflow_db import WorkflowError


def inspect(conn,source):
    row,did=_load(conn,UUID(source['run_id']),source['key']);guard(conn,row)
    if row.payload.get('test_material') or row.payload['dossier'].get('test_material'):raise WorkflowError('real operating proof refuses test fixtures')
    if row.digest!=source['digest'] or row.version!=source['version']:raise WorkflowError('real proof source is not the current canonical version')
    verdict=evaluate_dossier(row.payload);reports=effective_reports(row.payload['dossier'])
    holder=current_custody_in_transaction(conn,did);chain=verify_chain_in_transaction(conn,did)
    from app.agents.registry import PIPELINE_IDS
    manifest=[{'agent':a,'report_digest':(reports.get(a) or {}).get('digest'),'status':(reports.get(a) or {}).get('status',(reports.get(a) or {}).get('outcome','missing')),
        'missing':(reports.get(a) or {}).get('missing',['retained_report'])} for a in PIPELINE_IDS if a!='gabriel']
    blockers=list(verdict['basis']) if verdict['outcome']!='authorized' else []
    blockers+=['missing_report:'+m['agent'] for m in manifest if not m['report_digest']]
    blockers+=['stale_report:'+a for a in stale_dependencies(reports)]
    return {'source':source,'property_id':row.payload['property_id'],'address':row.payload['dossier'].get('street_address'),
        'status':'blocked_needs_evidence' if verdict['outcome']!='authorized' else 'retained_downstream_complete' if not blockers else 'authorized_with_downstream_work_pending',
        'raphael':verdict,'custody':holder,'verified_receipts':chain,'reports':manifest,'blockers':blockers,
        'records_written':0,'provider_calls':0,'external_actions':0,'synthetic':False,
        'scope':'Read-only proof of actual retained gates and custody. No fresh assessment, handoff, recovery run or positive end-to-end execution is claimed.'}
