"""Evidence-linked owner overview; software coordination, never business authority."""
from copy import deepcopy
from uuid import UUID
from app.workflow import digest_payload
from app.agents.elmer import latest_assessment
from app.agents.registry import PIPELINE_IDS


def assess(property_id,dossier):
    missing=[]; values={}; supplied=(dossier.get('specialist_inputs') or {}).get('manny') or {}
    for name in ('asset_ownership','operating_mandate','asset_obligations'):
        entry=supplied.get(name) if isinstance(supplied,dict) else None
        try:
            value=entry['value'];refs=entry['evidence_refs']
            if not isinstance(value,dict) or UUID(str(value.get('property_id')))!=UUID(str(property_id)): raise ValueError()
            if not isinstance(refs,list) or not 1<=len(refs)<=10 or any(not isinstance(r,str) or not r.strip() or len(r)>500 for r in refs): raise ValueError()
            values[name]=deepcopy(value)
        except (KeyError,ValueError,TypeError,AttributeError): missing.append(name+':property_bound_evidence')
    if values.get('asset_ownership',{}).get('status') not in ('claimed','unknown'): missing.append('asset_ownership:claim_status')
    if values.get('operating_mandate',{}).get('scope')!='review_only': missing.append('operating_mandate:review_only')
    obligations=values.get('asset_obligations',{})
    if (not isinstance(obligations.get('entries'),list) or len(obligations.get('entries',[]))>100
            or obligations.get('coverage') not in ('reported','none_reported')
            or (obligations.get('coverage')=='none_reported' and obligations.get('entries'))): missing.append('asset_obligations:explicit_reported_records')
    reports=dossier.get('reports') or {};manifest=[];valid={}
    for agent in PIPELINE_IDS[:-1]:
        report=reports.get(agent)
        if agent=='elmer':
            try: report=latest_assessment(dossier)
            except ValueError: report=None
        intact=isinstance(report,dict) and report.get('digest')==digest_payload({k:v for k,v in report.items() if k!='digest'})
        if intact: valid[agent]=report
        manifest.append({'agent':agent,'retained':intact,'report_digest':report['digest'] if intact else None,
            'status':report.get('status',report.get('outcome','not_declared')) if intact else 'missing_or_invalid',
            'missing':deepcopy(report.get('missing',[])) if intact else ['retained_report']})
    for name in ('michael','elmer'):
        if name not in valid: missing.append('report:'+name)
    raphael=valid.get('raphael') or {}; engineering=valid.get('onesimus') or {}; finance=valid.get('michael') or {}
    alternatives={}
    engineering_options=engineering.get('alternatives') or {}
    if not isinstance(engineering_options,dict): engineering_options={}
    for name,value in engineering_options.items():
        if not isinstance(value,dict): continue
        scenario=value.get('scenario') or {}
        if not isinstance(scenario,dict): scenario={}
        alternatives[name]={'concept_ready':value.get('concept_ready'),'outputs':deepcopy(scenario.get('outputs') or {}),
            'units':deepcopy(scenario.get('output_units') or {}),'classification':'engineering_estimate',
            'source_report_digest':engineering.get('digest')}
    financial={}
    financial_options=finance.get('assessments') or {}
    if not isinstance(financial_options,dict): financial_options={}
    for name,value in financial_options.items():
        if not isinstance(value,dict): continue
        financial[name]={'complete':value.get('complete'),'verdict':value.get('verdict','unknown'),
            'outputs':deepcopy(value.get('outputs') or {}),'missing':deepcopy(value.get('missing') or []),
            'classification':'scenario_estimate','source_report_digest':finance.get('digest')}
    accounting=valid.get('elmer',{}).get('accounting_assessment') or {}
    facts=deepcopy(raphael.get('facts') or {})
    unknowns=[name for name,value in facts.items() if not isinstance(value,dict) or value.get('classification')!='verified']
    if not facts: unknowns=['retained_property_assessment_facts']
    return {'version':'manny-owner-overview-1','property_id':str(property_id),
        'status':'needs_evidence' if missing else 'prepared_for_owner_review','missing':sorted(set(missing)),
        'property':{k:deepcopy(dossier.get(k)) for k in ('name','street_address','city','state','zip_code')},
        'report_manifest':manifest,'property_assessment':{'outcome':raphael.get('outcome','unknown'),
            'facts':facts,'unknown_or_unverified':unknowns,'source_report_digest':raphael.get('digest')},
        'engineering_scenarios':alternatives,'financial_scenarios':financial,
        'latest_accounting':deepcopy(accounting),'latest_accounting_digest':valid.get('elmer',{}).get('digest'),
        'solomon_project_digest':digest_payload(dossier['solomon_project']) if isinstance(dossier.get('solomon_project'),dict) else None,
        'asset_inputs':values,'recommended_action':'Human review of evidence, assumptions and exceptions; no external execution.',
        'external_actions':0,'business_approval':False,'ownership_verified':False,'signing_authorized':False,
        'limitations':['Manny is a software owner agent, not the human owner or legal signatory.',
            'Forecasts and reported expenses retain their original classifications; no profitability or payment is verified here.',
            'Closing an exception changes review status only, never underlying verification or permissions.']}
