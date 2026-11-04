"""Bounded independent-room Kratky alternatives; never certifies building capacity."""
from copy import deepcopy
from decimal import Decimal
from app.workflow import digest_payload
from app.workflow_db import WorkflowError
from app.agents.onesimus.service import ClassifiedInput,evaluate_scenario
from app.agents.onesimus.engineering import SITE_UNITS
from app.agents.onesimus.crops import crop_profile


def plan_rooms(payload,rooms,*,crop=None):
    if payload.get('digest')!=digest_payload({k:v for k,v in payload.items() if k!='digest'}): raise WorkflowError('room portfolio source integrity failure')
    from app.linked_reassessments import effective_reports
    inspection=effective_reports(payload.get('dossier') or {}).get('raphael') or {}
    if inspection.get('property_id') is not None and str(inspection['property_id'])!=str(payload['property_id']): raise WorkflowError('inspection property identity mismatch')
    if inspection.get('outcome')!='authorized' or inspection.get('digest')!=digest_payload({k:v for k,v in inspection.items() if k!='digest'}): raise WorkflowError('room planning requires retained preliminary Raphael authorization')
    if not isinstance(rooms,list) or not 1<=len(rooms)<=12: raise WorkflowError('1-12 explicitly identified rooms required')
    seen=set();results=[];missing=[]
    for room in rooms:
        if not isinstance(room,dict) or set(room)-{'room_id','property_id','inspection_digest','evidence_refs','site','distinct_room_claim'}: raise WorkflowError('unsupported room fields')
        name=room.get('room_id')
        if not isinstance(name,str) or not name.strip() or len(name)>80 or name in seen: raise WorkflowError('unique bounded room IDs required')
        if str(room.get('property_id'))!=str(payload['property_id']) or room.get('inspection_digest')!=inspection['digest']: raise WorkflowError('room must bind the property and current inspection report')
        if room.get('distinct_room_claim') is not True: raise WorkflowError('explicit distinct-room assumption required before summing areas')
        refs=room.get('evidence_refs')
        if not isinstance(refs,list) or not 1<=len(refs)<=10 or any(not isinstance(r,str) or not r.strip() or len(r)>500 for r in refs): raise WorkflowError('room evidence references required')
        raw=room.get('site')
        if not isinstance(raw,dict) or set(raw)-set(SITE_UNITS): raise WorkflowError('supported classified room site inputs required')
        site={}
        for key,value in raw.items():
            if not isinstance(value,dict): raise WorkflowError('room quantities require classification, unit and provenance')
            item=ClassifiedInput.model_validate({**value,'name':key})
            if item.classification=='verified': item=item.model_copy(update={'classification':'assumed'})
            site[key]=item
        alternatives={config:evaluate_scenario(config,crop or crop_profile(),site) for config in ('bench','rack3')}
        feasible=[(key,result) for key,result in alternatives.items() if result.get('complete') and result.get('layout_status')=='plausible_under_assumptions']
        feasible.sort(key=lambda pair:(-Decimal(str(pair[1]['outputs']['kg_per_year'])),Decimal(str(pair[1]['outputs'].get('lighting_connected_kw',0))),pair[0]))
        chosen=feasible[0] if feasible else None
        if chosen is None: missing.append('room:'+name+':no_plausible_layout')
        results.append(dict(room_id=name,evidence_refs=deepcopy(refs),alternatives=alternatives,selected_config=chosen[0] if chosen else None,
            selected_scenario=chosen[1] if chosen else None,selection_scope='Highest estimated annual kg, then lower lighting kW, then config ID; not a profitability or safety optimum.'))
        seen.add(name)
    quantities=('growing_m2','kg_per_year','water_l_per_year','lighting_kwh_per_year','hvac_kwh_per_year','labor_hours_per_year','water_plus_structure_kg')
    totals={}
    if not missing:
        for name in quantities:
            values=[r['selected_scenario']['outputs'].get(name) for r in results]
            totals[name]=str(sum((Decimal(str(v)) for v in values),Decimal(0))) if all(v is not None for v in values) else None
    return dict(version='onesimus-room-portfolio-1',property_id=payload['property_id'],inspection_digest=inspection['digest'],rooms=results,
        complete=not missing,missing=missing,selected_room_totals=totals,alternatives_not_additive=True,
        aggregation_basis='Only one selected alternative per caller-declared distinct room. Room boundaries and capacity are not independently verified.',
        building_capacity_verified=False,construction_authorized=False)


def assess_room_finance(portfolio,raw_inputs):
    """Costs are room-specific; no silent repetition of property-wide allowances."""
    from app.michael import financial_inputs_from_raw,money_str,FINANCIAL_NAMES
    expected={r['room_id'] for r in portfolio.get('rooms') or []}
    if not portfolio.get('complete'): return dict(complete=False,missing=['plausible_room_portfolio'],rooms={},totals={})
    if not isinstance(raw_inputs,dict) or set(raw_inputs)!=expected: return dict(complete=False,missing=['explicit_financial_inputs_for_each_selected_room'],rooms={},totals={})
    assessments={};missing=[]
    for room in portfolio['rooms']:
        if not isinstance(raw_inputs[room['room_id']],dict): raise WorkflowError('structured room financial inputs required')
        if set(raw_inputs[room['room_id']])-set(FINANCIAL_NAMES)-{'occupancy_usd_per_m2_year','maintenance_usd_per_year'}: raise WorkflowError('unsupported room financial input')
        inputs=financial_inputs_from_raw(raw_inputs[room['room_id']],scenario=room['room_id'])
        # Existing planning cases enforce explicit occupancy/maintenance allowances.
        from app.michael import planning_cases
        cases=planning_cases(room['selected_config'],room['selected_scenario'],inputs)
        assessment=cases.get('cases',{}).get('base')
        if assessment is None or not assessment.get('complete'): missing.append('room:'+room['room_id']+':financial_inputs')
        assessments[room['room_id']]=cases
    totals={}
    if not missing:
        for name in ('capex_usd','revenue_usd_per_year','opex_usd_per_year','operating_net_usd_per_year'):
            totals[name]=money_str(sum((Decimal(a['cases']['base']['outputs'][name]) for a in assessments.values()),Decimal(0)))
    return dict(complete=not missing,missing=missing,rooms=assessments,totals=totals,
        favorable_under_assumptions=not missing and Decimal(totals['operating_net_usd_per_year'])>0,
        scope='Sum room-specific planning allowances only. Shared overhead must be allocated once in supplied inputs; no capacity, occupancy, profit or financing verification.',
        shared_costs_allocation_verified=False)
