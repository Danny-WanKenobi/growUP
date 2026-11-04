"""Synthetic-only vendor proposal/counterproposal/selection exercise. No transports."""
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from app.workflow import digest_payload
from app.workflow_db import WorkflowError
from app.handoff_demo_domain import DOMAIN

LABEL='SYNTHETIC PROCUREMENT SIMULATION - NO COMPANY CONTACT, REAL BID OR CONTRACT'
DEMO_RECIPIENT='werenotrandom@protonmail.com'
# Illustrative public profiles, not exhaustive market coverage or construction qualification.
PROFILES=(
    ('agility','Agility Robotics','Digit','https://www.agilityrobotics.com/',
     'Ask about supervised material movement, facility integration and deployment validation.'),
    ('apptronik','Apptronik','Apollo','https://apptronik.com/industries/manufacturing',
     'Ask about adaptable manufacturing workflows, integration and operator training.'),
    ('boston_dynamics','Boston Dynamics','Atlas','https://bostondynamics.com/products/atlas/',
     'Ask about industrial manipulation validation, task boundaries and qualified human supervision.'),
    ('figure','Figure','Figure platform','https://www.figure.ai/news/botq',
     'Ask about manufacturing/material-handling pilot scope and integration evidence.'),
    ('sanctuary','Sanctuary AI','Physical AI','https://sanctuary.ai/',
     'Ask about dexterous industrial tasks, hardware suitability and human intervention requirements.'),
    ('tesla','Tesla','Optimus','https://www.tesla.com/SI',
     'Ask about development-stage task validation, availability and safe operating boundaries.'),
    ('unitree','Unitree Robotics','G1','https://www.unitree.com/g1/',
     'Ask whether an exploratory humanoid platform could support a supervised pilot; industrial/jobsite suitability is unknown.'),
)


def money(value):
    if isinstance(value,bool) or len(str(value))>40: raise WorkflowError('invalid synthetic money')
    try: result=Decimal(str(value))
    except (InvalidOperation,ValueError): raise WorkflowError('invalid synthetic money') from None
    if not result.is_finite() or result<=0 or result>Decimal('1e15') or result.adjusted() < -12:
        raise WorkflowError('invalid synthetic money')
    return result


def evaluate_quotes(quotes,budget):
    budget=money(budget); rows=[]; seen=set(); allowed={p[0] for p in PROFILES}
    if not isinstance(quotes,list) or not 1<=len(quotes)<=len(PROFILES): raise WorkflowError('bounded synthetic quotes required')
    for quote in quotes:
        if not isinstance(quote,dict) or quote.get('synthetic') is not True or quote.get('vendor_id') not in allowed:
            raise WorkflowError('synthetic catalog vendor required')
        if quote.get('channel','email') not in ('email','phone','chat'): raise WorkflowError('unsupported synthetic communication channel')
        vendor=quote['vendor_id']
        if vendor in seen: raise WorkflowError('duplicate synthetic vendor response')
        seen.add(vendor); price=money(quote.get('price_usd')); days=quote.get('duration_days')
        if isinstance(days,bool) or not isinstance(days,int) or not 1<=days<=365: raise WorkflowError('explicit synthetic duration required')
        for name in ('scope_score','supervision_score'):
            value=quote.get(name)
            if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=100: raise WorkflowError('bounded synthetic rubric scores required')
        eligible=price<=budget and quote.get('safety_plan_provided') is True
        # Demonstration rubric, not real technical certification or vendor quality.
        cost=max(Decimal(0),Decimal(100)*(budget-price)/budget)
        schedule=max(Decimal(0),Decimal(100)*(Decimal(365)-days)/Decimal(365))
        score=cost*Decimal('.4')+schedule*Decimal('.2')+Decimal(quote['scope_score'])*Decimal('.2')+Decimal(quote['supervision_score'])*Decimal('.2')
        rows.append({'vendor_id':vendor,'synthetic':True,'price_usd':str(price),'duration_days':days,
            'eligible_in_scenario':eligible,'score':str(score.quantize(Decimal('.0001'))),
            'reasons':[] if eligible else ['Outside supplied demo budget or missing synthetic safety-plan response'],
            'criteria':{'cost_score':str(cost),'schedule_score':str(schedule),
                'scope_score':quote['scope_score'],'supervision_score':quote['supervision_score']}})
    rows.sort(key=lambda r:(not r['eligible_in_scenario'],-Decimal(r['score']),r['vendor_id']))
    winner=next((r['vendor_id'] for r in rows if r['eligible_in_scenario']),None)
    return {'ranking':rows,'selected_vendor_id':winner,
        'rubric':{'cost_weight':'0.4','schedule_weight':'0.2','scope_weight':'0.2','supervision_weight':'0.2',
            'cost_formula':'max(0,100*(budget-price)/budget)','schedule_formula':'max(0,100*(365-days)/365)',
            'tie_break':'vendor_id ascending after equal score','eligibility':'Within supplied scenario budget and synthetic safety-plan response present'},
        'selection_scope':'Simulation only; not a real recommendation, qualification, acceptance or purchase.'}


def build_vendor_round(payload,counters=None):
    if payload.get('synthetic') is not True or payload.get('domain')!=DOMAIN:
        raise WorkflowError('vendor exercise requires explicit isolated synthetic dossier')
    if payload.get('digest')!=digest_payload({k:v for k,v in payload.items() if k!='digest'}):
        raise WorkflowError('synthetic dossier digest mismatch')
    entry=((payload.get('dossier') or {}).get('specialist_inputs') or {}).get('solomon') or {}
    budget_entry=entry.get('construction_budget') or {}
    budget_value=budget_entry.get('value') or {}
    try: budget=money(budget_value.get('limit_usd'))
    except (WorkflowError,AttributeError):
        return {'synthetic':True,'domain':DOMAIN,'label':LABEL,'status':'needs_scenario_budget','external_deliveries':0}
    packets=[]
    reports=(payload.get('dossier') or {}).get('reports') or {}
    source_refs={name:(reports.get(name) or {}).get('digest') for name in ('raphael','onesimus','nicole','simeon')}
    for vendor,name,platform,source,focus in PROFILES:
        packet={'vendor_id':vendor,'company':name,'platform_reference':platform,'source_url':source,
            'profile_checked_on':'2026-11-03','profile_scope':'Public company/product reference; availability and construction suitability unverified.',
            'subject':LABEL+' - '+name+' supervised indoor-farm pilot inquiry',
            'body':LABEL+'\n'+focus+'\nRequest a conditional proposal for supervised logistics and setup planning around a passive Kratky concept. '
                'Do not assume structural work, electrical work, elevated access or unsupervised operation is permitted. '
                'Provide scope exclusions, training, integration, safety evidence, pricing assumptions and schedule. '
                'All project inputs and commercial exchanges in this exercise are fictional.',
            'email':{'to':DEMO_RECIPIENT,'delivery':'simulated_only','sent':False},
            'attachments':{'conditional_scope':{'project_id':payload['property_id'],'method':'passive_noncirculating_kratky',
                'purpose':'Supervised pilot planning only','excluded':['structural certification','electrical installation','unsupervised jobsite work']},
                'engineering_assumptions':{'report_digest':source_refs['onesimus'],'status':'preliminary; validate all loads, access and operating constraints'},
                'site_constraints':{'inspection_report_digest':source_refs['raphael'],'regulatory_report_digest':source_refs['nicole'],
                    'closing_report_digest':source_refs['simeon'],'construction_authorized':False},
                'response_rubric':{'cost_weight':'0.4','schedule_weight':'0.2','scope_weight':'0.2','supervision_weight':'0.2',
                    'safety_evidence':'Required before any real deployment; scenario flags are fictional'},
                'demo_budget':{'currency':'USD','supplied_limit_usd':str(budget),'synthetic':True}},
            'source_dossier_digest':payload['digest'],'synthetic':True,'domain':DOMAIN}
        packet['proposal_id']='synthetic-proposal:'+digest_payload(packet)
        packets.append(packet)
    if counters is None:
        counters=[{'vendor_id':profile[0],'synthetic':True,'price_usd':str(20000+2000*i),
            'duration_days':30+3*i,'scope_score':80-i,'supervision_score':75+i,
            'safety_plan_provided':True,'domain':DOMAIN,'channel':('email','phone','chat')[i%3],
            'label':'FICTIONAL counter-proposal; not received from '+profile[1]}
            for i,profile in enumerate(PROFILES)]
    evaluation=evaluate_quotes(counters,budget)
    selected=evaluation['selected_vendor_id']
    chosen=next((p for p in packets if p['vendor_id']==selected),None)
    contract=None if chosen is None else {'label':'SIMULATED FINAL CONTRACT PACKAGE - UNSIGNED, NONBINDING, NO LEGAL EFFECT',
        'vendor_id':selected,'for_company':chosen['company'],'to':DEMO_RECIPIENT,'delivery':'simulated_only','sent':False,
        'source_proposal_id':chosen['proposal_id'],'terms_source':'Fictional ranked counter-proposal only',
        'commercial_terms':next(deepcopy(q) for q in counters if q['vendor_id']==selected),
        'scope':'Conditional supervised pilot planning; no physical work is commissioned.',
        'conditions':['Actual scope, costs, availability, insurance and safe task suitability must be verified.',
            'Owner/legal approval, closing authority and authenticated permits are unresolved.',
            'No procurement, signature, site access or construction is authorized.'],
        'signature_blocks':[],'signing_authorized':False,'synthetic':True,'domain':DOMAIN}
    result={'version':'solomon-vendor-simulation-1','synthetic':True,'domain':DOMAIN,'label':LABEL,
        'proposals':packets,'counterproposals':deepcopy(counters),'evaluation':evaluation,'final_contract_package':contract,
        'events':[{'event':'proposal_simulated','vendor_id':p['vendor_id'],'channel':'email','synthetic':True} for p in packets]+
            [{'event':'counterproposal_simulated','vendor_id':q['vendor_id'],'channel':q.get('channel','email'),'synthetic':True} for q in counters]+
            ([] if selected is None else [{'event':'candidate_selected_in_simulation','vendor_id':selected,'synthetic':True},
             {'event':'contract_dispatch_simulated','vendor_id':selected,'synthetic':True}]),
        'catalog_scope':'Seven illustrative public companies; not exhaustive, not a market-leadership ranking or verified construction shortlist.',
        'real_vendor_responses':0,'provider_calls':0,'external_deliveries':0,'actual_contracts':0}
    result['digest']=digest_payload(result)
    return result
