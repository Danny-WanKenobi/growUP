"""Preliminary deterministic underwriting and unsigned preparation brief."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID
from app.michael import money_str
from app.workflow import digest_payload


def conversion_demo_brief(model):
    from app.conversion_demo import validate
    validate(model)
    return {'agent':'theresia_keys','source_model_digest':model['digest'],'mode':'demo_only',
        'owner_funded_proposal':{'document_type':'illustrative_unsigned_consulting_brief',
            'project_budget_usd':model['owner_funded']['project_budget_usd'],
            'proposed_consulting_fee_usd':model['owner_funded']['consulting_fee_proposed_usd'],
            'scope':['Concept and feasibility review','Conversion support planning','Ongoing support only under separately agreed scope and fees'],
            'conditions':['Agree fee milestones and deliverables','Confirm funding, property access and required professional reviews']},
        'participation_proposal':{'document_type':'illustrative_nonbinding_project_structure',
            'rights':model['participation']['rights_assumption'],'allocations':model['participation']['allocations'],
            'conditions':['Define issuer and legally enforceable rights','Agree contribution values, control, losses and exit terms','Determine a lawful offering and transfer route','Evidence actual funding before proceeding']},
        'contract_executable':False,'signing_authority':False,'ownership_rights_established':False}


def assess(property_id, supplied, reports):
    missing, conditions, scenarios = [], [], {}
    def entry(name):
        value = supplied.get(name)
        return value if isinstance(value, dict) else {}
    def bound(value):
        try:
            return isinstance(value, dict) and UUID(str(value.get("property_id"))) == UUID(str(property_id))
        except (ValueError, TypeError, AttributeError):
            return False
    def sourced(name):
        refs = entry(name).get("evidence_refs")
        if not isinstance(refs, list) or not refs or len(refs)>100:
            return False
        try:
            return all(isinstance(r,str) and len(r)<=2000 and urlsplit(r).scheme in ("http","https")
                and urlsplit(r).hostname and not urlsplit(r).username and not urlsplit(r).password for r in refs)
        except (ValueError, TypeError):
            return False
    def number(value, *, signed=False):
        if isinstance(value,bool) or len(str(value))>50:
            raise ValueError()
        try:
            result=Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError() from None
        if not result.is_finite() or abs(result)>Decimal('1e15') or (result and result.adjusted() < -12) or (not signed and result<0):
            raise ValueError()
        return result
    ownership=entry("ownership_and_liens").get("value")
    finance=entry("financing_terms").get("value")
    terms=entry("contract_terms").get("value")
    if (not bound(ownership) or not sourced("ownership_and_liens")
            or not isinstance(ownership.get("owner_name"),str) or not ownership["owner_name"].strip()
            or ownership.get("lien_status") not in ("none_reported","reported","unknown")):
        missing.append("ownership_and_liens:property_bound_source_record")
    else:
        try:
            date.fromisoformat(ownership.get("record_date",""))
        except (ValueError,TypeError):
            missing.append("ownership_and_liens:record_date")
        if ownership["lien_status"]=="unknown":
            missing.append("ownership_and_liens:unresolved_liens")
    names=("additional_project_costs_usd","contingency_usd","proposed_funding_usd",
           "annual_debt_service_usd","minimum_coverage_ratio")
    values={}
    if not bound(finance) or not sourced("financing_terms") or finance.get("currency")!="USD":
        missing.append("financing_terms:property_bound_USD_inputs")
    else:
        for name in names:
            try:
                values[name]=number(finance.get(name))
                if name=="minimum_coverage_ratio" and values[name]<=0:
                    raise ValueError()
            except ValueError:
                values.pop(name,None)
                missing.append("financing_terms:"+name)
    parties=(terms or {}).get("parties") if isinstance(terms,dict) else None
    if (not bound(terms) or not sourced("contract_terms") or not isinstance(parties,list) or not 2<=len(parties)<=20
            or any(not isinstance(p,dict) or any(not isinstance(p.get(k),str) or not p[k].strip() for k in ("name","role")) for p in parties)
            or len({p["name"].strip().casefold() for p in parties})!=len(parties)
            or not isinstance(terms.get("scope"),str) or not terms["scope"].strip()
            or isinstance(terms.get("term_months"),bool) or not isinstance(terms.get("term_months"),int) or terms["term_months"]<=0
            or not isinstance(terms.get("conditions"),list) or not terms["conditions"]
            or any(not isinstance(v,str) or not v.strip() for v in terms["conditions"])):
        missing.append("contract_terms:property_bound_parties_scope_duration_conditions")
    refs={}
    for agent in ("michael","nicole"):
        report=reports.get(agent)
        if (not isinstance(report,dict) or report.get("digest")!=digest_payload({k:v for k,v in report.items() if k!="digest"})
                or (report.get("property_id") is not None and str(report["property_id"])!=str(property_id))):
            missing.append("report:"+agent)
        else:
            refs[agent]=report["digest"]
    if "nicole" in refs:
        regulatory=reports["nicole"].get("regulatory_assessment") or {}
        if regulatory.get("ready_for_preliminary_underwriting") is not True or reports["nicole"].get("missing"):
            missing.append("nicole:unresolved_regulatory_intake")
    if "michael" in refs and len(values)==len(names):
        assessments=reports["michael"].get("assessments") or {}
        if not isinstance(assessments,dict):
            assessments={}
        assessments=deepcopy(assessments)
        room_finance=reports['michael'].get('room_finance') or {}
        if room_finance.get('complete'): assessments['room_portfolio']={'complete':True,'outputs':deepcopy(room_finance.get('totals') or {})}
        for name,item in assessments.items():
            if not isinstance(item,dict) or item.get("complete") is not True:
                continue
            try:
                outputs=item.get("outputs") or {}
                capex=number(outputs.get("capex_usd"))
                net=number(outputs.get("operating_net_usd_per_year"),signed=True)
            except (ValueError,AttributeError):
                missing.append("michael:"+str(name)+":invalid_financial_outputs")
                continue
            total=capex+values["additional_project_costs_usd"]+values["contingency_usd"]
            debt=values["annual_debt_service_usd"]
            gap=max(Decimal(0),total-values["proposed_funding_usd"])
            coverage=net/debt if debt>0 else None
            eligible=gap==0 and net-debt>0 and (coverage is None or coverage>=values["minimum_coverage_ratio"])
            scenarios[str(name)]={"michael_report_digest":refs["michael"],"capital_needed_usd":money_str(total),
                "funding_gap_usd":money_str(gap),"proposed_funding_surplus_usd":money_str(max(Decimal(0),values["proposed_funding_usd"]-total)),
                "annual_after_debt_proxy_usd":money_str(net-debt),"operating_surplus_debt_coverage_proxy":str(coverage) if coverage is not None else None,
                "coverage_basis":"Michael operating surplus / supplied annual debt service; not lender-defined DSCR. No ratio for zero debt service.",
                "meets_supplied_planning_limits":eligible,
                "downside_operating_surplus_80pct_after_debt_usd":money_str(net*Decimal('.8')-debt),
                "downside_stress_scope":"Illustrative 20% operating-surplus haircut, not a revenue/expense forecast or lender underwriting rule.",
                "break_even_debt_service_usd_per_year":money_str(max(Decimal(0),net)),
                "inputs":{"michael_capex_usd":money_str(capex),"michael_operating_net_usd_per_year":money_str(net),
                    **{key:str(value) for key,value in values.items()}},
                "formulas":{"capital_needed":"Michael capex + additional_project_costs + contingency",
                    "funding_gap":"max(0, capital_needed - proposed_funding)","annual_after_debt":"Michael operating_net - annual_debt_service"}}
        if not scenarios:
            missing.append("michael:complete_financial_scenario")
        elif not any(s["meets_supplied_planning_limits"] for s in scenarios.values()):
            missing.append("underwriting:no_scenario_meets_supplied_planning_limits")
    conditions.extend(["Authenticate ownership, liens and counterparty authority before commitment.",
        "Resolve Nicole regulatory conditions; preliminary readiness is not regulatory clearance.",
        "Verify financing availability and all project costs; proposed funding is not committed funding.",
        "Owner and qualified legal review of final terms and signing authority is required before execution."])
    if isinstance(terms,dict) and isinstance(terms.get("conditions"),list):
        conditions.extend(v for v in terms["conditions"] if isinstance(v,str) and v.strip())
    conditions=list(dict.fromkeys(conditions))
    missing=list(dict.fromkeys(missing))
    return {"version":"theresia-underwriting-1","property_id":str(property_id),"source_report_digests":refs,
        "status":"needs_evidence" if missing else "conditional_preparation_only", "missing":missing,
        "scenarios":scenarios,"evidence_status":"provided_unverified", "ownership_verified":False,
        "financing_approved":False,"contract_executable":False,"signing_authority":False,
        "contract_preparation":{"document_type":"unsigned_nonbinding_preparation_brief",
            "terms":deepcopy(terms),"conditions_precedent":list(dict.fromkeys(conditions)),
            "signature_blocks":[],"ownership_rights_established":False},
        "owner_review_requests":[{"requirement":"underwriting_condition:"+str(i),"reason":condition,
            "status":"pending","authority":"human_owner","grants_permission":False} for i,condition in enumerate(conditions)],
        "limitations":["Planning calculations only; neither a lender decision nor legal advice or a final contract.",
            "Additional costs and contingency must be explicitly supplied; absent amounts are never assumed zero.",
            "No debt amortization, tax, closing-cost discovery or authentication is performed. Currency USD only.",
            "Money inputs must be finite, absolute values <= 1e15, with nonzero magnitudes >= 1e-12 for arithmetic safety."],
        "external_actions":0,"provider_calls":0}
