"""Resolve linked reassessments without overwriting inherited report slots."""
from copy import deepcopy
from app.workflow import digest_payload
from app.workflow_db import WorkflowError


def effective_reports(dossier):
    reports=deepcopy(dossier.get('reports') or {})
    for entry in dossier.get('agent_reassessments') or []:
        if not isinstance(entry,dict): raise WorkflowError('malformed reassessment')
        agent=entry.get('agent');report=entry.get('report');parent=reports.get(agent)
        from app.agents.registry import PIPELINE_IDS
        if agent not in PIPELINE_IDS[:-2] or not isinstance(report,dict) or report.get('agent')!=agent or report.get('source_dossier_digest')!=entry.get('source_dossier_digest'): raise WorkflowError('reassessment agent/source mismatch')
        if (not isinstance(report,dict) or entry.get('digest')!=digest_payload({k:v for k,v in entry.items() if k!='digest'})
                or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'})
                or entry.get('parent_report_digest')!=(parent or {}).get('digest')):
            raise WorkflowError('linked reassessment lineage/integrity failure')
        reports[agent]=deepcopy(report)
    from app.agents.elmer import latest_assessment
    accounting=latest_assessment(dossier)
    if accounting is not None: reports['elmer']=deepcopy(accounting)
    return reports


def effective_dossier(dossier):
    result=deepcopy(dossier);result['reports']=effective_reports(dossier)
    for entry in dossier.get('agent_reassessments') or []:
        if entry.get('specialist_inputs') is not None:
            result.setdefault('specialist_inputs',{})[entry['agent']]=deepcopy(entry['specialist_inputs'])
        if entry.get('assessment_inputs') is not None: result['assessment_inputs']=deepcopy(entry['assessment_inputs'])
    result.pop('agent_reassessments',None);result.pop('elmer_assessments',None)
    return result


def stale_dependencies(reports):
    stale={}
    for agent,report in reports.items():
        if agent=='manny': continue
        refs=report.get('rework_input_digests') or report.get('inherited_report_digests') or {}
        refs=dict(refs) if isinstance(refs,dict) else {}
        if report.get('source_inspection_digest'): refs['raphael']=report['source_inspection_digest']
        if report.get('source_engineering_digest'): refs['onesimus']=report['source_engineering_digest']
        mismatches=[name for name,digest in refs.items() if (reports.get(name) or {}).get('digest')!=digest]
        if mismatches: stale[agent]=sorted(mismatches)
    # A stale dependency transitively invalidates consumers, even if its old digest still matches.
    changed=True
    while changed:
        changed=False
        for agent,report in reports.items():
            if agent=='manny' or agent in stale: continue
            refs=report.get('rework_input_digests') or report.get('inherited_report_digests') or {}
            names=set(refs) if isinstance(refs,dict) else set()
            if report.get('source_inspection_digest'): names.add('raphael')
            if report.get('source_engineering_digest'): names.add('onesimus')
            blocked=sorted(names & set(stale))
            if blocked: stale[agent]=blocked;changed=True
    return stale
