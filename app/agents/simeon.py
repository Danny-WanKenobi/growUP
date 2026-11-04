"""Closing coordination intake and exception routing; never execution authority."""
from copy import deepcopy
from datetime import date
from uuid import UUID
from urllib.parse import urlsplit
from app.workflow import digest_payload


def assess(property_id, supplied, reports):
    missing, exceptions = [], []
    def entry(name):
        value=supplied.get(name)
        return value if isinstance(value,dict) else {}
    def dated(value):
        try:
            date.fromisoformat(value)
            return True
        except (ValueError,TypeError):
            return False
    def url(value):
        try:
            parsed=urlsplit(value)
            return (isinstance(value,str) and len(value)<=2000 and parsed.scheme in ('http','https')
                and parsed.hostname and not parsed.username and not parsed.password)
        except (ValueError,TypeError,AttributeError):
            return False
    def bound(value):
        try:
            return isinstance(value,dict) and UUID(str(value.get('property_id')))==UUID(str(property_id))
        except (ValueError,TypeError,AttributeError):
            return False
    def sourced(name):
        refs=entry(name).get('evidence_refs')
        return isinstance(refs,list) and 0<len(refs)<=100 and all(url(ref) for ref in refs)
    parent=reports.get('theresia_keys')
    underwriting={}; terms={}; parent_digest=None
    if (not isinstance(parent,dict) or parent.get('digest')!=digest_payload({k:v for k,v in parent.items() if k!='digest'})
            or str(parent.get('property_id'))!=str(property_id)):
        missing.append('report:theresia_keys:property_or_digest')
    else:
        parent_digest=parent['digest']
        underwriting=parent.get('underwriting_assessment') or {}
        if not isinstance(underwriting,dict): underwriting={}
        preparation=underwriting.get('contract_preparation') or {}
        if not isinstance(preparation,dict): preparation={}
        terms=preparation.get('terms') or {}
        if not isinstance(terms,dict): terms={}
        if parent.get('missing') or underwriting.get('status')!='conditional_preparation_only':
            missing.append('theresia_keys:unresolved_underwriting')
    parties=terms.get('parties')
    expected={}
    if bound(terms) and isinstance(parties,list) and 2<=len(parties)<=20:
        for party in parties:
            if (not isinstance(party,dict) or any(not isinstance(party.get(k),str) or not party[k].strip() for k in ('name','role'))):
                missing.append('theresia_keys:invalid_parties')
                continue
            key=party['name'].strip().casefold()
            if key in expected: missing.append('theresia_keys:ambiguous_parties')
            expected[key]=party['role'].strip().casefold()
    else:
        missing.append('theresia_keys:property_bound_terms_and_parties')
    terms_digest=digest_payload(terms) if terms else None
    consent=entry('counterparty_consent').get('value')
    observations=[]; seen=set()
    if (not bound(consent) or not sourced('counterparty_consent') or not terms_digest
            or consent.get('terms_digest')!=terms_digest or not isinstance(consent.get('parties'),list)
            or len(consent['parties'])>100):
        missing.append('counterparty_consent:property_terms_and_sources')
    else:
        for index, party in enumerate(consent['parties']):
            if not isinstance(party,dict):
                missing.append('counterparty_consent:invalid_record:'+str(index)); continue
            item=deepcopy(party); item['evidence_status']='provided_unverified'
            observations.append(item)
            name=party.get('name'); role=party.get('role')
            key=name.strip().casefold() if isinstance(name,str) else ''
            if (key not in expected or not isinstance(role,str) or role.strip().casefold()!=expected[key]
                    or party.get('status') not in ('consented','declined','unknown')
                    or not dated(party.get('record_date')) or not url(party.get('source_url'))
                    or party['source_url'] not in entry('counterparty_consent')['evidence_refs']):
                missing.append('counterparty_consent:party_or_provenance:'+str(index)); continue
            if key in seen:
                missing.append('counterparty_consent:duplicate_or_conflicting_party:'+key)
            seen.add(key)
            if party['status']!='consented':
                missing.append('counterparty_consent:'+party['status']+':'+key)
        for key in sorted(set(expected)-seen):
            missing.append('counterparty_consent:missing_party:'+key)
    approval=entry('approved_terms').get('value')
    if (not bound(approval) or not sourced('approved_terms') or not terms_digest
            or approval.get('terms_digest')!=terms_digest or not dated(approval.get('record_date'))
            or approval.get('status') not in ('reviewed','approved_claimed')):
        missing.append('approved_terms:property_version_and_review_evidence')
    authority=entry('signing_authority').get('value')
    if (not bound(authority) or not sourced('signing_authority') or not dated(authority.get('record_date'))
            or not isinstance(authority.get('principal'),str) or not authority['principal'].strip()
            or authority.get('scope') not in ('review_only','signing_claimed')):
        missing.append('signing_authority:property_bound_scope_record')
    conditions=((underwriting.get('contract_preparation') or {}).get('conditions_precedent') or []) if isinstance(underwriting.get('contract_preparation'),dict) else []
    if not isinstance(conditions,list) or any(not isinstance(v,str) or not v.strip() for v in conditions):
        missing.append('theresia_keys:invalid_conditions'); conditions=[]
    if not conditions:
        missing.append('theresia_keys:conditional_report_conditions_missing')
    missing=list(dict.fromkeys(missing))
    for gap in missing:
        exceptions.append({'requirement':gap,'reason':'Closing coordination evidence is unresolved.',
            'route':'theresia_keys','status':'pending','authority':'human_owner','grants_permission':False})
    for index,condition in enumerate(conditions):
        exceptions.append({'requirement':'closing_condition:'+str(index),'reason':condition,
            'route':'human_review','status':'pending','authority':'human_owner','grants_permission':False})
    exceptions.append({'requirement':'closing_execution_authority',
        'reason':'Authenticate consent, final terms, ownership, financing and legally sufficient signing authority before any external closing action.',
        'route':'human_review','status':'pending','authority':'human_owner','grants_permission':False})
    return {'version':'simeon-closing-readiness-1','property_id':str(property_id),
        'source_theresia_report_digest':parent_digest,'terms_digest':terms_digest,
        'status':'needs_evidence' if missing else 'prepared_for_coordination_review',
        'missing':missing,'ready_for_coordination_review':not missing,'ready_to_close':False,
        'closing_checklist':[{'item':'Authenticate party consent and identity','status':'not_authenticated'}, {'item':'Bind final terms to the retained terms digest','status':'retained_claim_only'}, {'item':'Resolve inherited conditions before external execution','status':'unresolved'}, {'item':'Obtain verified legal signing and closing authority','status':'not_authorized'}],
        'specialist_review_routes':['deborah','yoseph','theresia_keys'],
        'consent_observations':observations,'terms_review':deepcopy(approval),'authority_record':deepcopy(authority),
        'conditions_precedent':[{'condition':v,'status':'unresolved','resolution_authenticated':False} for v in conditions],
        'exception_routes':exceptions,'owner_review_requests':deepcopy(exceptions),
        'evidence_status':'provided_unverified','consent_authenticated':False,'terms_approval_authenticated':False,
        'signing_authorized':False,'deal_closed':False,'external_actions':0,'provider_calls':0,
        'limitations':['Coordination intake only; supplied consent, approval or power-of-attorney claims confer no authority.',
            'Inherited conditions are preserved and never marked resolved by caller assertions.',
            'No signatures, negotiation, disbursements, escrow instructions, title transfer or legal determination.']}
