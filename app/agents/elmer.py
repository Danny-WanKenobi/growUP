"""Preliminary project-cost intake; no bank posting, payments or tax decisions."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID
from app.workflow import digest_payload


def conversion_demo_brief(model):
    from app.conversion_demo import validate
    validate(model)
    return {'agent':'elmer','source_model_digest':model['digest'],'mode':'demo_only',
        'consulting_fee_proposed_usd':model['owner_funded']['consulting_fee_proposed_usd'],
        'actual_fee_income_verified_usd':None,'actual_capital_received_usd':None,
        'classification_notes':['Proposed consulting fees are not earned or collected income.',
            'Hypothetical investor capital is separate from operating revenue.',
            'Proposed service/space contributions are not cash receipts.',
            'Illustrative units do not establish Manny’s real property holdings.',
            'Existing accounting history is unchanged; no simulation is posted to a ledger.'],
        'allocation_balanced':sum(a['units'] for a in model['participation']['allocations'])==model['inputs']['total_units'],
        'ledger_postings':0,'payments_authorized':False,'tax_treatment_determined':False}


def _amount(value, signed=False):
    if isinstance(value,bool) or len(str(value))>40: raise ValueError('invalid amount')
    try: amount=Decimal(str(value))
    except (InvalidOperation,ValueError): raise ValueError('invalid amount') from None
    if (not amount.is_finite() or abs(amount)>Decimal('1e15') or amount.as_tuple().exponent < -2
            or (not signed and amount<0)): raise ValueError('invalid amount')
    return amount


def _bound(value, prop):
    try: return isinstance(value,dict) and UUID(str(value.get('property_id')))==UUID(str(prop))
    except (ValueError,TypeError,AttributeError): return False


def assess(property_id, dossier):
    from app.linked_reassessments import effective_reports
    missing=[]; reports=effective_reports(dossier); solomon=reports.get('solomon')
    baseline=None; ceiling=None; estimate_lines=[]; changes=[]; proposed=None; project=dossier.get('solomon_project')
    source_digest=None
    try:
        if (not isinstance(solomon,dict) or solomon.get('property_id')!=str(property_id)
                or solomon.get('digest')!=digest_payload({k:v for k,v in solomon.items() if k!='digest'})):
            raise ValueError()
        planning=solomon['preconstruction_assessment']
        if planning.get('planning_ready') is not True: raise ValueError()
        source_digest=solomon['digest']
        baseline=_amount(planning['budget']['estimated_total_usd'])
        ceiling=_amount(planning['budget']['supplied_limit_usd'])
        estimate_lines=deepcopy(planning['budget']['line_items'])
        if sum((_amount(r['estimated_cost_usd']) for r in estimate_lines),Decimal(0))!=baseline: raise ValueError()
        if project is not None:
            if (not isinstance(project,dict) or project.get('property_id')!=str(property_id)
                    or project.get('source_report_digest')!=source_digest): raise ValueError()
            rows=project.get('proposed_changes')
            if not isinstance(rows,list) or len(rows)>100: raise ValueError()
            ids=set()
            for row in rows:
                if (not isinstance(row,dict) or not isinstance(row.get('event_id'),str) or not row['event_id']
                        or row['event_id'] in ids or row.get('applied') is not False
                        or row.get('approval_status')!='pending'): raise ValueError()
                ids.add(row['event_id']); delta=_amount(row.get('cost_delta_usd'),signed=True)
                changes.append({'event_id':row['event_id'],'cost_delta_usd':format(delta,'.2f'),
                    'approval_status':'pending','applied':False,'evidence_refs':deepcopy(row.get('evidence_refs') or [])})
            proposed=baseline+sum((Decimal(c['cost_delta_usd']) for c in changes),Decimal(0))
            if proposed<0 or _amount(project.get('baseline_estimate_usd'))!=baseline or _amount(project.get('proposed_total_usd'))!=proposed:
                raise ValueError()
        else: proposed=baseline
    except (ValueError,TypeError,KeyError,AttributeError):
        missing.append('solomon:bound_reconciled_cost_records'); baseline=ceiling=proposed=None; changes=[]; estimate_lines=[]
    supplied=(dossier.get('specialist_inputs') or {}).get('elmer') or {}; values={}; refs={}
    for name in ('accounting_period','transactions','entity_and_tax_jurisdiction'):
        entry=supplied.get(name) if isinstance(supplied,dict) else None
        evidence=entry.get('evidence_refs') if isinstance(entry,dict) else None
        if (not isinstance(entry,dict) or not _bound(entry.get('value'),property_id)
                or not isinstance(evidence,list) or not 1<=len(evidence)<=10
                or any(not isinstance(r,str) or not r.strip() or len(r)>500 for r in evidence)):
            missing.append(name+':property_bound_evidence'); continue
        values[name]=entry['value']; refs[name]=deepcopy(evidence)
    start=end=None
    try:
        start=date.fromisoformat(values['accounting_period']['start']); end=date.fromisoformat(values['accounting_period']['end'])
        if start>end: raise ValueError()
    except (ValueError,KeyError,TypeError): missing.append('accounting_period:valid_dates')
    entity=values.get('entity_and_tax_jurisdiction') or {}
    if any(not isinstance(entity.get(n),str) or not entity[n].strip() or len(entity[n])>200 for n in ('entity_name','jurisdiction')):
        missing.append('entity_and_tax_jurisdiction:identified_entity_and_jurisdiction')
    commitments=[]; expenses=[]; totals_known=False; exception_reasons=[]
    try:
        if start is None or end is None: raise ValueError()
        transaction_set=values['transactions']
        if transaction_set.get('coverage') not in ('reported_records','none_reported'): raise ValueError()
        entries=transaction_set.get('entries')
        if (not isinstance(entries,list) or len(entries)>200 or (transaction_set['coverage']=='none_reported' and entries)
                or (transaction_set['coverage']=='reported_records' and not entries)): raise ValueError()
        seen=set()
        for row in entries:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or len(row['id'])>100 or row['id'] in seen:
                raise ValueError()
            seen.add(row['id'])
            if row.get('kind') not in ('commitment','expense') or row.get('currency')!='USD': raise ValueError()
            if not isinstance(row.get('evidence_ref'),str) or not row['evidence_ref'].strip() or len(row['evidence_ref'])>500: raise ValueError()
            occurred=date.fromisoformat(row['occurred_on'])
            if start is None or end is None or not start<=occurred<=end: raise ValueError()
            amount=_amount(row.get('amount_usd'))
            if amount<=0: raise ValueError()
            ref=row.get('commitment_id')
            if ref is not None and (not isinstance(ref,str) or not ref or len(ref)>100): raise ValueError()
            if row['kind']=='commitment' and ref is not None: raise ValueError()
            entry={'id':row['id'],'kind':row['kind'],'amount_usd':format(amount,'.2f'),'currency':'USD',
                'occurred_on':occurred.isoformat(),'evidence_ref':row['evidence_ref'],
                'evidence_status':'reported_unverified','payment_status':'unknown'}
            if ref is not None: entry['commitment_id']=ref
            (commitments if row['kind']=='commitment' else expenses).append(entry)
        by_id={r['id']:Decimal(r['amount_usd']) for r in commitments}; linked={k:Decimal(0) for k in by_id}
        for expense in expenses:
            ref=expense.get('commitment_id')
            if ref is not None:
                if ref not in by_id: raise ValueError()
                linked[ref]+=Decimal(expense['amount_usd'])
        for key in by_id:
            if linked[key]>by_id[key]: exception_reasons.append('Reported expenses exceed commitment '+key)
        remaining=sum((max(Decimal(0),by_id[k]-linked[k]) for k in by_id),Decimal(0))
        totals_known=True
    except (ValueError,TypeError,KeyError):
        missing.append('transactions:valid_unique_period_records_and_links'); commitments=[]; expenses=[]; remaining=None
    total_c=sum((Decimal(c['amount_usd']) for c in commitments),Decimal(0)) if totals_known else None
    total_e=sum((Decimal(c['amount_usd']) for c in expenses),Decimal(0)) if totals_known else None
    if proposed is not None and ceiling is not None and proposed>ceiling: exception_reasons.append('Pending proposals exceed supplied planning limit')
    if total_e is not None and baseline is not None and total_e>baseline: exception_reasons.append('Reported expenses exceed baseline estimate')
    exposure=total_e+remaining if total_e is not None and remaining is not None else None
    evidence_groups={}
    for row in expenses:
        evidence_groups.setdefault(row['evidence_ref'].strip(),[]).append(row['id'])
    collisions=[{'evidence_ref':ref,'expense_ids':ids,
        'scope':'Possible shared invoice or legitimate separate line items; resolve scope with evidence, never silently remove a record.'}
        for ref,ids in sorted(evidence_groups.items()) if len(ids)>1]
    for group in collisions:
        exception_reasons.append('Multiple expense IDs share evidence; review distinct-charge scope: '+', '.join(group['expense_ids']))
    return {'version':'elmer-project-cost-intake-1','property_id':str(property_id),
        'source_solomon_report_digest':source_digest,'source_project_digest':digest_payload(project) if isinstance(project,dict) else None,
        'status':'needs_evidence' if missing else 'preliminary_accounting_intake',
        'missing':list(dict.fromkeys(missing)),'intake_ready':not missing,'evidence_refs':refs,
        'accounting_period':{'start':start.isoformat() if start else None,'end':end.isoformat() if end else None},
        'entity':deepcopy(entity),'baseline_estimates':{'currency':'USD','total_usd':format(baseline,'.2f') if baseline is not None else None,'line_items':estimate_lines},
        'pending_changes':{'entries':changes,'proposed_total_usd':format(proposed,'.2f') if proposed is not None else None,'applied':False},
        'reported_commitments':{'entries':commitments,'total_usd':format(total_c,'.2f') if total_c is not None else None,
            'unexpensed_linked_balance_usd':format(remaining,'.2f') if remaining is not None else None},
        'reported_actual_expenses':{'entries':expenses,'total_usd':format(total_e,'.2f') if total_e is not None else None,'payments_verified':False},
        'same_evidence_expense_groups':collisions,'distinct_charge_total_verified':False,
        'exceptions':exception_reasons,'classification':'reported_unverified',
        'budget_variance':{'reported_expenses_plus_unexpensed_commitments_usd':format(exposure,'.2f') if exposure is not None else None,
            'baseline_less_reported_exposure_usd':format(baseline-exposure,'.2f') if baseline is not None and exposure is not None else None,
            'scope':'Reported expense plus remaining linked commitments; not cash paid or a complete liability inventory. Proposals are separate.'},
        'reconciliation_scope':'Only supplied period records; commitments and expenses overlap and must not be added as distinct spend.',
        'external_actions':0,'payments_authorized':False,'tax_liability_calculated':False,
        'limitations':['No bank reconciliation, cash-payment proof, completeness certification, tax calculation or filing.',
            'No supplied accounting records means unknown totals; explicit none_reported means zero reported records only.']}


def latest_assessment(dossier):
    """Resolve an intact linked assessment chain without rewriting the original."""
    previous=(dossier.get('reports') or {}).get('elmer')
    if not isinstance(previous,dict): return None
    for report in [previous,*(dossier.get('elmer_assessments') or [])]:
        if not isinstance(report,dict) or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'}):
            raise ValueError('accounting assessment integrity failure')
        if report is not previous and report.get('parent_assessment_digest')!=previous['digest']:
            raise ValueError('accounting assessment lineage failure')
        previous=report
    return previous


def read_vault_in_transaction(conn,*,run_id,dossier_key):
    """Elmer's internal read-only accounting access; never transfers custody."""
    from app.owner_work import read_vault_in_transaction as read
    result=read(conn,run_id=run_id,dossier_key=dossier_key)
    for item in result['entries']:
        dossier=item['payload'].get('dossier') or {}
        assessment=latest_assessment(dossier)
        item['accounting_assessment']=deepcopy(assessment)
    from app.agents.stages import _load
    from uuid import UUID
    current,_=_load(conn,UUID(str(run_id)),dossier_key)
    result['latest_working_accounting_assessment']=deepcopy(latest_assessment(current.payload.get('dossier') or {}))
    result.update(reader='elmer',access='read_only',corrections='Use linked working-dossier accounting reassessment; archived bytes are immutable.')
    return result
