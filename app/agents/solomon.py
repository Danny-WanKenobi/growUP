"""Preconstruction planning only; no orders, construction or communications."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import UUID
from app.workflow import digest_payload
from app.michael import money_str

PHASES=(('survey','Site and constraints review'),('design','Qualified design review'),
        ('permits','Permits and safety review'),('procurement','Conditional procurement planning'),
        ('installation','Installation planning'),('inspection','Commissioning and inspection planning'))


def assess(property_id,supplied,reports):
    missing=[]; refs={}; values={}
    def bound(value):
        try: return isinstance(value,dict) and UUID(str(value.get('property_id')))==UUID(str(property_id))
        except (ValueError,TypeError,AttributeError): return False
    for agent in ('onesimus','nicole','simeon'):
        report=reports.get(agent)
        if (not isinstance(report,dict) or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'})
                or (report.get('property_id') is not None and str(report['property_id'])!=str(property_id))):
            missing.append('report:'+agent)
        else: refs[agent]=report['digest']
    for name in ('approved_design','permits','contractor_and_safety_plan','construction_budget'):
        entry=supplied.get(name)
        if not isinstance(entry,dict) or not bound(entry.get('value')):
            missing.append(name+':property_bound_record'); continue
        provenance=entry.get('evidence_refs')
        if not isinstance(provenance,list) or not provenance or any(not isinstance(r,str) or not r.strip() for r in provenance):
            missing.append(name+':source_references'); continue
        values[name]=deepcopy(entry['value'])
    design=values.get('approved_design') or {}
    if design.get('scope')!='preconstruction_planning' or not refs.get('onesimus') or not (design.get('engineering_report_digest')==refs['onesimus'] or design.get('engineering_reference')=='retained_onesimus_report'):
        missing.append('approved_design:concept_scope_and_engineering_binding')
    permits=values.get('permits') or {}
    if permits.get('status') not in ('pending','claimed_issued','unknown'):
        missing.append('permits:status')
    safety=values.get('contractor_and_safety_plan') or {}
    if (not isinstance(safety.get('coordinator'),str) or not safety['coordinator'].strip()
            or not isinstance(safety.get('site_access_plan'),str) or not safety['site_access_plan'].strip()
            or safety.get('review_status') not in ('pending','claimed_reviewed')):
        missing.append('contractor_and_safety_plan:coordination_and_review')
    budget=values.get('construction_budget') or {}; total=Decimal(0); line_items=[]
    try:
        def amount(value):
            if isinstance(value,bool) or len(str(value))>40: raise ValueError()
            result=Decimal(str(value))
            if not result.is_finite() or result<0 or result>Decimal('1e15') or (result and result.adjusted() < -12): raise ValueError()
            return result
        ceiling=amount(budget.get('limit_usd'))
        rows=budget.get('line_items')
        if budget.get('currency')!='USD' or ceiling<=0 or not isinstance(rows,list) or not 1<=len(rows)<=30: raise ValueError()
        seen=set()
        for row in rows:
            if not isinstance(row,dict) or row.get('phase') not in dict(PHASES) or row['phase'] in seen: raise ValueError()
            seen.add(row['phase']); cost=amount(row.get('estimated_cost_usd')); total+=cost
            line_items.append({'phase':row['phase'],'estimated_cost_usd':money_str(cost),'classification':'provided_estimate'})
        if total>ceiling: missing.append('construction_budget:estimate_exceeds_supplied_limit')
    except (ValueError,InvalidOperation,TypeError):
        ceiling=None; missing.append('construction_budget:explicit_finite_USD_allowances')
    if 'simeon' in refs:
        closing=reports['simeon'].get('closing_assessment') or {}
        if not isinstance(closing,dict): closing={}
        if closing.get('ready_for_coordination_review') is not True or reports['simeon'].get('missing'):
            missing.append('simeon:coordination_intake_unresolved')
    if 'nicole' in refs:
        regulatory=reports['nicole'].get('regulatory_assessment') or {}
        if not isinstance(regulatory,dict): regulatory={}
        if regulatory.get('ready_for_preliminary_underwriting') is not True: missing.append('nicole:regulatory_intake_unresolved')
    conditions=['Resolve actual closing, ownership and site-access authority before any mobilization.',
        'Obtain authenticated permits and qualified structural/electrical/fire-safety review.',
        'Validate passive Kratky design, loads, water, lighting, HVAC and worker access before installation.',
        'Verify subcontractor qualifications, insurance, scope, costs and safe human/robot interaction.',
        'Authorize procurement, binding contracts and construction separately; a planning handoff grants none.']
    missing=list(dict.fromkeys(missing))
    return {'version':'solomon-preconstruction-1','property_id':str(property_id),'source_report_digests':refs,
        'status':'needs_evidence' if missing else 'preconstruction_planning_only','missing':missing,
        'engineering_report_digest':refs.get('onesimus'),'design_reference':deepcopy(design),
        'planning_ready':not missing,'construction_authorized':False,'site_safety_verified':False,
        'permits_authenticated':False,'deal_closed':False,'scope':'preconstruction_planning_only',
        'work_breakdown':[{'phase':key,'description':description,'depends_on':[] if i==0 else [PHASES[i-1][0]],
            'execution_status':'not_authorized','duration_days':None} for i,(key,description) in enumerate(PHASES)],
        'budget':{'currency':'USD','line_items':line_items,'estimated_total_usd':money_str(total) if ceiling is not None else None,
            'supplied_limit_usd':money_str(ceiling) if ceiling is not None else None,'binding_quote':False},
        'communications':{'direction':'two_way','planned_channels':['email','phone','chat'],
            'active_transports':[],'mode':'draft_or_simulation_only','external_deliveries':0},
        'owner_review_requests':[{'requirement':'preconstruction:'+str(i),'reason':condition,'route':'human_review',
            'status':'pending','authority':'human_owner','grants_permission':False} for i,condition in enumerate(conditions)],
        'limitations':['Planning and supplied estimates only, not construction-ready drawings or verified capacities.',
            'Supplied permits/review claims are unverified; no source research, engineering certification or physical supervision.',
            'Human communication channels are deferred and not installed or enabled.'],
        'provider_calls':0,'external_actions':0,'signing_authorized':False}
