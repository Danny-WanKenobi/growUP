"""Structured insurance/legal evidence screening; no professional opinion or action."""
from copy import deepcopy
from datetime import date
from decimal import Decimal,InvalidOperation
from uuid import UUID
from app.workflow import digest_payload


def bound(value,property_id):
    try: return isinstance(value,dict) and UUID(str(value.get('property_id')))==UUID(str(property_id))
    except (ValueError,TypeError,AttributeError): return False


def refs(value):
    return isinstance(value,list) and 1<=len(value)<=20 and all(isinstance(r,str) and r.strip() and len(r)<=500 for r in value)


def amount(value):
    if isinstance(value,bool) or not isinstance(value,str) or len(value)>40: raise ValueError()
    number=Decimal(value)
    if not number.is_finite() or number<0 or number>Decimal('1e15') or number!=number.quantize(Decimal('.01')): raise ValueError()
    return number


def day(value):
    if not isinstance(value,str) or len(value)!=10: raise ValueError()
    return date.fromisoformat(value)


def rows(supplied,name,property_id,missing):
    entry=supplied.get(name) or {}
    entry=entry if isinstance(entry,dict) else {}
    value=entry.get('value')
    if not bound(value,property_id) or not refs(entry.get('evidence_refs')) or not isinstance(value.get('entries'),list) or len(value['entries'])>50:
        missing.append(name+':property_bound_records');return [],{}
    return deepcopy(value['entries']),value


def assess_insurance(property_id,supplied):
    """Triage scores and reported coverage gaps, never a coverage determination."""
    supplied={k:v for k,v in supplied.items() if isinstance(v,dict)} if isinstance(supplied,dict) else {}
    missing=[];issues=[];risks,inventory=rows(supplied,'risk_inventory',property_id,missing)
    policies,policy_meta=rows(supplied,'existing_policies',property_id,missing)
    requirements,requirement_meta=rows(supplied,'coverage_requirements',property_id,missing)
    as_of=inventory.get('as_of');observed=[];valid_policies=[];gaps=[];seen=set()
    try: today=day(as_of)
    except (ValueError,TypeError): today=None;missing.append('risk_inventory:as_of')
    for risk in risks:
        if (not isinstance(risk,dict) or set(risk)-{'id','description','severity','likelihood','evidence_refs'} or not isinstance(risk.get('id'),str) or not risk['id'].strip() or len(risk['id'])>80 or risk['id'] in seen
            or not refs(risk.get('evidence_refs')) or not isinstance(risk.get('description'),str) or not risk['description'].strip() or len(risk['description'])>1000
            or any(isinstance(risk.get(n),bool) or not isinstance(risk.get(n),int) or not 1<=risk[n]<=5 for n in ('severity','likelihood'))):
            missing.append('risk_inventory:unique_evidence_scored_risk');continue
        seen.add(risk['id']);score=risk['severity']*risk['likelihood']
        observed.append({**risk,'triage_score':score,'priority':'high' if score>=15 else 'medium' if score>=6 else 'low','scope':'Supplied ordinal planning scores; not actuarial likelihood.'})
    seen=set()
    for policy in policies:
        try:
            if not isinstance(policy,dict) or set(policy)-{'id','coverage','effective_on','expires_on','currency','limit_usd','deductible_usd','exclusions','evidence_refs'} or not isinstance(policy.get('id'),str) or not policy['id'] or len(policy['id'])>80 or policy['id'] in seen: raise ValueError()
            if not refs(policy.get('evidence_refs')) or not isinstance(policy.get('coverage'),str) or not policy['coverage'].strip(): raise ValueError()
            start,end=day(policy.get('effective_on')),day(policy.get('expires_on'))
            if start>end or policy.get('currency')!='USD': raise ValueError()
            limit,deductible=amount(policy.get('limit_usd')),amount(policy.get('deductible_usd'))
            if not isinstance(policy.get('exclusions'),list) or len(policy['exclusions'])>30 or any(not isinstance(e,str) or not e.strip() for e in policy['exclusions']): raise ValueError()
            seen.add(policy['id']);current=today is not None and start<=today<=end
            valid_policies.append({**policy,'reported_current_on_as_of':current,'coverage_verified':False})
            if not current: issues.append('policy_not_current_on_as_of:'+policy['id'])
            if policy['exclusions']: issues.append('policy_exclusions_require_review:'+policy['id'])
        except (ValueError,TypeError,InvalidOperation): missing.append('existing_policies:valid_unique_dated_USD_policy')
    seen=set()
    for required in requirements:
        try:
            if not isinstance(required,dict) or not isinstance(required.get('coverage'),str) or not required['coverage'] or required['coverage'] in seen or not refs(required.get('evidence_refs')): raise ValueError()
            floor=amount(required.get('minimum_limit_usd'));seen.add(required['coverage'])
            candidates=[p for p in valid_policies if p['coverage']==required['coverage'] and p['reported_current_on_as_of']]
            # Separate policies are never added together as if aggregation were guaranteed.
            largest=max((amount(p['limit_usd']) for p in candidates),default=Decimal(0))
            gap=max(Decimal(0),floor-largest)
            gaps.append(dict(coverage=required['coverage'],minimum_limit_usd=str(floor),largest_reported_policy_limit_usd=str(largest),reported_limit_gap_usd=str(gap),coverage_verified=False))
            if not candidates or gap: issues.append('coverage_gap:'+required['coverage'])
        except (ValueError,TypeError,InvalidOperation): missing.append('coverage_requirements:unique_evidenced_USD_minimum')
    observed.sort(key=lambda r:(-r['triage_score'],r['id']))
    return dict(version='yoseph-insurance-screen-1',property_id=str(property_id),as_of=as_of,risks=observed,reported_policies=valid_policies,reported_coverage_gaps=gaps,
        missing=sorted(set(missing)),review_issues=sorted(set(issues)),status='needs_evidence' if missing else 'advisory_hold' if issues else 'preliminary_screen_only',
        coverage_verified=False,binding_authorized=False,claims_authorized=False,external_actions=0,provider_calls=0,
        evidence_status='provided_unverified',limitations=['No policy authentication, insurer quotes, coverage opinion, binding or claim submission.','Effective dates and limits alone never prove a loss is covered. Exclusions, insured entity, location, endorsements and wording require qualified review.'])


def assess_legal(property_id,supplied):
    """Document/issue readiness only; never determines governing law or enforceability."""
    supplied={k:v for k,v in supplied.items() if isinstance(v,dict)} if isinstance(supplied,dict) else {}
    missing=[];issues=[];documents,meta=rows(supplied,'governing_documents',property_id,missing)
    jurisdiction=(supplied.get('jurisdiction') or {}).get('value');question=(supplied.get('legal_question') or {}).get('value')
    if not bound(jurisdiction,property_id) or not refs((supplied.get('jurisdiction') or {}).get('evidence_refs')) or not isinstance(jurisdiction.get('name'),str) or not jurisdiction['name'].strip(): missing.append('jurisdiction:property_bound_context')
    if not bound(question,property_id) or not refs((supplied.get('legal_question') or {}).get('evidence_refs')) or not isinstance(question.get('text'),str) or not question['text'].strip(): missing.append('legal_question:property_bound_question')
    seen={};review=[]
    for document in documents:
        if (not isinstance(document,dict) or set(document)-{'id','version','status','issues','evidence_refs'} or not isinstance(document.get('id'),str) or not document['id'].strip() or len(document['id'])>100
            or not isinstance(document.get('version'),str) or not document['version'].strip() or not refs(document.get('evidence_refs'))
            or document.get('status') not in ('draft','signed_claimed','withdrawn','unknown') or not isinstance(document.get('issues'),list) or len(document['issues'])>30):
            missing.append('governing_documents:identified_versioned_evidence');continue
        if document['id'] in seen and seen[document['id']]!=document['version']: issues.append('conflicting_document_versions:'+document['id'])
        elif document['id'] in seen: missing.append('governing_documents:duplicate_document')
        seen[document['id']]=document['version']
        if document['status']!='signed_claimed': issues.append('document_not_reported_signed:'+document['id'])
        for issue in document['issues']:
            if not isinstance(issue,str) or not issue.strip() or len(issue)>500: missing.append('governing_documents:bounded_issue');continue
            issues.append('document_issue:'+document['id']+':'+issue)
        review.append({**deepcopy(document),'signature_authenticated':False,'enforceability_determined':False})
    return dict(version='deborah-legal-issue-screen-1',property_id=str(property_id),jurisdiction_context=deepcopy(jurisdiction),question=deepcopy(question),documents=review,
        missing=sorted(set(missing)),review_issues=sorted(set(issues)),status='needs_evidence' if missing else 'advisory_hold' if issues else 'preliminary_screen_only',
        legal_clearance=False,signing_authorized=False,external_actions=0,provider_calls=0,evidence_status='provided_unverified',
        qualified_review_questions=['Which executed document and version controls this property and proposed scope?','Who has authenticated authority to consent, sign or delegate?','Which unresolved ownership, lease, permitting, liability or insurance questions need qualified review?'],
        limitations=['No legal research, legal opinion, enforceability determination, filing or representation.','Reported signatures and document versions remain unverified source claims.'])
