"""Two conversion choices for an isolated demonstration; no financial execution."""
from copy import deepcopy
from decimal import Decimal,InvalidOperation
from uuid import UUID
import re

from app.workflow import digest_payload
from app.workflow_db import WorkflowError

VERSION='conversion-options-demo-1'
DEFAULTS={'currency':'USD','project_cost_usd':'200000.00','consulting_fee_usd':'15000.00',
    'reserve_usd':'25000.00','offering_cost_usd':'25000.00','unit_price_usd':'50.00',
    'total_units':10000,'owner_units':3000,'growup_units':2000,'public_units':5000,'treasury_units':0}


def _money(value):
    if isinstance(value,bool) or len(str(value))>40:raise WorkflowError('bounded explicit demo amount required')
    try:number=Decimal(str(value))
    except (InvalidOperation,ValueError):raise WorkflowError('explicit finite demo amount required') from None
    if not number.is_finite() or number<0 or number>Decimal('1e12') or number.as_tuple().exponent < -2:
        raise WorkflowError('nonnegative finite cents required; missing is not zero')
    return number


def _usd(value):return format(value,'.2f')


def simulate(inputs, *, property_id, source_digest):
    if not isinstance(inputs,dict) or set(inputs)!=set(DEFAULTS) or inputs.get('currency')!='USD':
        raise WorkflowError('complete explicit USD demo inputs required')
    try:prop=str(UUID(str(property_id)))
    except ValueError:raise WorkflowError('explicit demo property identity required') from None
    if not isinstance(source_digest,str) or not re.fullmatch('[0-9a-f]{64}',source_digest):
        raise WorkflowError('retained source digest required')
    amounts={k:_money(inputs[k]) for k in DEFAULTS if k.endswith('_usd')}
    for name in ('total_units','owner_units','growup_units','public_units','treasury_units'):
        if type(inputs[name]) is not int or not 0<=inputs[name]<=1_000_000:
            raise WorkflowError('bounded whole demo allocation units required')
    if not inputs['total_units'] or not inputs['public_units'] or amounts['unit_price_usd']<=0 or amounts['project_cost_usd']<=0:
        raise WorkflowError('positive project budget, units and illustrative price required')
    if sum(inputs[k] for k in ('owner_units','growup_units','public_units','treasury_units'))!=inputs['total_units']:
        raise WorkflowError('proposed allocations must exactly equal total units')
    allocations=[{'party':label,'units':inputs[key],
        'percent':_usd(Decimal(inputs[key])*100/inputs['total_units'])}
        for label,key in (('Property owner','owner_units'),('Grow Up','growup_units'),('Potential outside participants','public_units'),('Unallocated treasury','treasury_units'))]
    cases=[]
    for name,sold in (('No subscriptions',0),('Partial subscriptions',inputs['public_units']//2),('Full target scenario',inputs['public_units'])):
        gross=Decimal(sold)*amounts['unit_price_usd']
        available=max(Decimal(0),gross-amounts['offering_cost_usd']-amounts['reserve_usd'])
        gap=max(Decimal(0),amounts['project_cost_usd']-available)
        cases.append({'name':name,'hypothetical_units_subscribed':sold,
            'subscription_percent':_usd(Decimal(sold)*100/inputs['public_units']),
            'hypothetical_gross_usd':_usd(gross),'planning_available_for_project_usd':_usd(available),
            'project_funding_gap_usd':_usd(gap),'project_budget_covered_in_simulation':gap==0,
            'real_progression_authorized':False,'actual_funds_received_usd':None,
            'next_step':'Illustration covers the assumed budget; actual rights, funding and approvals remain unestablished.' if gap==0 else 'Do not proceed on this funding scenario; the modeled project remains underfunded.'})
    result={'version':VERSION,'synthetic':True,'domain':'synthetic_handoff_demo',
        'demo_label':'FICTIONAL FINANCING ILLUSTRATION — NOT AN OFFER, TOKEN ISSUANCE OR VERIFIED RESCUE',
        'property_id':prop,'source_dossier_digest':source_digest,'inputs':deepcopy(inputs),
        'assumptions_status':'Invented additional demo inputs, not the retained Michael underwriting or real owner finances.',
        'owner_funded':{'title':'Owner-funded consulting','owner_cash_needed_usd':_usd(amounts['project_cost_usd']+amounts['consulting_fee_usd']+amounts['reserve_usd']),
            'project_budget_usd':_usd(amounts['project_cost_usd']),'consulting_fee_proposed_usd':_usd(amounts['consulting_fee_usd']),
            'reserve_usd':_usd(amounts['reserve_usd']),'building_equity_transfer_proposed':False,'fee_agreed':False,
            'scope':'Owner funds the conversion; Grow Up provides agreed consulting/support services. No continuing fee or scope is invented.'},
        'participation':{'title':'Tokenized project participation — simulation only',
            'rights_assumption':'Proposed project-entity participation, not direct ownership of the building.',
            'owner_contribution_assumption':'A proposed right to use identified space; consent, value and enforceability are unverified.',
            'growup_contribution_assumption':'Proposed services; not a cash contribution or settled invoice.',
            'allocations':allocations,'cases':cases,'liquidity_promised':False,
            'actual_tokens_issued':0,'actual_holdings_verified':False,'legal_entity_established':False,
            'offering_authorized':False,'funds_received_verified':False},
        'formulas':{'owner_cash_needed':'project_cost + consulting_fee + reserve',
            'hypothetical_gross':'hypothetical units subscribed * illustrative USD price',
            'available_for_project':'max(0, hypothetical gross - offering cost allowance - reserve)',
            'project_funding_gap':'max(0, project cost - available for project)',
            'allocation_percent':'100 * proposed allocation units / total illustrative units'},
        'provider_calls':0,'external_actions':0,'ledger_postings':0,'signing_enabled':False,
        'scope':'Read-only demo calculation. No wallet, exchange, issuance, fundraising, ownership transfer or autonomous closing exists.'}
    result['digest']=digest_payload(result)
    return result


def validate(model):
    if (not isinstance(model,dict) or model.get('synthetic') is not True or model.get('version')!=VERSION
            or model.get('digest')!=digest_payload({k:v for k,v in model.items() if k!='digest'})):
        raise WorkflowError('intact synthetic conversion model required')
    return model


def build(payload):
    if (payload.get('synthetic') is not True or payload.get('domain')!='synthetic_handoff_demo'
            or payload.get('digest')!=digest_payload({k:v for k,v in payload.items() if k!='digest'})):
        raise WorkflowError('isolated synthetic dossier required for conversion illustration')
    from app.agents.simeon import conversion_demo_brief as sales
    from app.agents.theresia import conversion_demo_brief as proposal
    from app.agents.elmer import conversion_demo_brief as audit
    model=simulate(DEFAULTS,property_id=payload['property_id'],source_digest=payload['digest'])
    return {'model':model,'simeon':sales(model),'theresia':proposal(model),'elmer':audit(model),
            'synthetic':True,'source_dossier_digest':payload['digest'],'reports_mutated':False}
