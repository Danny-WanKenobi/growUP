"""Invented US demonstration properties. Geography is real; no property is real."""
from copy import deepcopy


def case(id,name,city,state,region,category,story,*,fail_at=None,fault=None,levels=None,site=None):
    return dict(id=id,name=name,city=city,state=state,region=region,property_type=category,story=story,
        expected_failure=fail_at,fault=fault,levels=levels,site_overrides=site or {},
        address='FICTIONAL '+id.upper()+' — not a deliverable street address',synthetic=True)


CASES=(
 case('hudson-sky','Hudson Lantern Tower','New York','NY','Northeast','high_rise','A handsome 62-story tower: model a small lower-floor farm while tenant relocation remains hypothetical.',levels=62,site={'usable_area_m2':600,'room_length_m':30,'room_width_m':25}),
 case('cascadia-spire','Cascadia Glass Spire','Seattle','WA','Pacific Northwest','high_rise','A 44-story glass tower with daylight potential; supplemental lighting remains quantified, never assumed free.',levels=44,site={'usable_area_m2':300,'room_length_m':25,'room_width_m':15}),
 case('loop-atrium','Loop Atrium Tower','Chicago','IL','Midwest','high_rise','Beautiful 53-story tower, but no measured usable pilot-floor area: height alone cannot establish a layout.',fail_at='onesimus',fault='missing_area',levels=53),
 case('tidal-crown','Tidal Crown Tower','Miami','FL','Southeast','high_rise','A 38-story waterfront tower whose annex and main-tower identities conflict; return for identity evidence.',fail_at='raphael',fault='identity',levels=38),
 case('plainview-plaza','Plainview Neighborhood Plaza','Cleveland','OH','Midwest','commercial_property','An ordinary two-room commercial unit: a deliberately plain, repeatable concept.'),
 case('archive-arcade','Archive Arcade','Boston','MA','Northeast','commercial_property','A former archive retail unit: an attractive concept cannot replace a missing regulatory jurisdiction.',fail_at='nicole',fault='jurisdiction'),
 case('station-annex','Station Annex Offices','Los Angeles','CA','Pacific','commercial_property','A narrow transit-adjacent office annex: access aisles reduce growing footprint before crop output is calculated.',site={'usable_area_m2':72,'room_length_m':18,'room_width_m':4}),
 case('breeze-court','Breeze Court Shops','Honolulu','HI','Hawaii','commercial_property','A modest shop courtyard: rain capture is an optional upper-bound concept, never guaranteed water supply.'),
 case('rivet-hall','Rivet Hall Distribution','Detroit','MI','Midwest','warehouse_industrial','A conventional warehouse bay with explicit passive-Kratky container, lighting, water and labor estimates.'),
 case('elm-fork','Elm Fork Sample Depot','Dallas','TX','South Central','warehouse_industrial','A bland warehouse used to show due-today, overdue, evidence correction, protected filing and later reopening.'),
 case('mesa-coolhouse','Mesa Coolhouse','Phoenix','AZ','Southwest','warehouse_industrial','A hot-climate warehouse: HVAC assumptions are explicit, but an absent selling price blocks a financial conclusion.',fail_at='michael',fault='market_price',site={'hvac_w_per_m2':180}),
 case('boreal-bay','Boreal Bay Logistics','Anchorage','AK','Alaska','warehouse_industrial','A cold-climate logistics bay: a repeated invoice ID is a reconciliation problem, not a second legitimate expense.',fail_at='elmer',fault='duplicate_invoice'),
 case('rowhouse-grove','Rowhouse Grove Lot','Baltimore','MD','Mid-Atlantic','vacant_lot','A former paved corner lot: outdoor canopy layers are explored, while soil contamination remains unknown.',fail_at='raphael',fault='outdoor_scope'),
 case('peach-canopy','Peach Canopy Lot','Atlanta','GA','Southeast','vacant_lot','An empty neighborhood lot: layered perennial planting context is supplied; no indoor lettuce yields are invented.',fail_at='raphael',fault='outdoor_scope'),
 case('high-plains','High Plains Pocket Lot','Denver','CO','Mountain','vacant_lot','A dry vacant lot: water rights and reliable supply are unknown, so a food forest is not declared executable.',fail_at='raphael',fault='outdoor_scope'),
 case('rain-garden','Rain Garden Commons Lot','Portland','OR','Pacific Northwest','vacant_lot','A wet-season lot: drainage and summer water availability remain different questions.',fail_at='raphael',fault='outdoor_scope'),
)


def get_case(id):
    if id is None: return None
    from app.workflow_db import WorkflowError
    item=next((c for c in CASES if c['id']==id),None)
    if item is None: raise WorkflowError('unknown synthetic property scenario')
    return deepcopy(item)


def catalogue():
    return dict(synthetic=True,properties=deepcopy(list(CASES)),coverage='Illustrative US geographic diversity, not every state or real source coverage.',
        outdoor_boundary='Vacant lots exercise honest Raphael returns and conceptual food-forest context; no real outdoor admission policy is added.')


def apply(payload,profile,verification):
    dossier=payload['dossier'];dossier.update(name='FICTIONAL '+profile['name'],street_address=profile['address'],city=profile['city'],state=profile['state'],demo_scenario=deepcopy(profile))
    dossier['category_attribution']={'status':'observed','observed':profile['property_type']}
    payload['candidate_verification']=verification
    inputs=dossier['specialist_inputs']
    inputs['nicole']['jurisdiction']['value'].update(city=profile['city'],state=profile['state'])
    inputs['elmer']['entity_and_tax_jurisdiction']['value']['jurisdiction']='FICTIONAL '+profile['state']+' example; not a tax determination'
    if profile['id']=='hudson-sky':
        dossier['demo_room_plan']=[{'room_id':'north-pilot','site_overrides':{'usable_area_m2':100,'room_length_m':10,'room_width_m':10}}, {'room_id':'south-pilot','site_overrides':{'usable_area_m2':72,'room_length_m':18,'room_width_m':4}}]
    if profile['id']=='elm-fork':
        from app.agents.solomon import PHASES
        inputs['solomon']['construction_budget']['value'].update(planning_start='2026-11-03',phase_duration_days={name:days for (name,_),days in zip(PHASES,(2,3,5,4,3,2))})
    fault=profile['fault']
    if fault=='identity': dossier['identity_disputes']={'annex':'FICTIONAL main tower and annex identity conflict'}
    if fault=='jurisdiction': inputs['nicole'].pop('jurisdiction')
    if fault=='duplicate_invoice':
        row=dict(id='fiction-invoice-1',kind='expense',amount_usd='25.00',currency='USD',occurred_on='2026-11-03',evidence_ref='https://records.fiction.example/invoice')
        inputs['elmer']['transactions']['value'].update(coverage='reported_records',entries=[row,deepcopy(row)])
    if profile['property_type']=='vacant_lot':
        from app.agents.onesimus.report import build_food_forest_concept
        def assumed(value,unit=None):
            result=dict(value=value,classification='assumed',provenance='FICTIONAL site context only')
            if unit: result['unit']=unit
            return result
        site=dict(area_m2=assumed(1200,'m2'),climate=assumed('FICTIONAL '+profile['city']+' seasonal planning context'),
            soil=dict(value=None,classification='unknown',provenance='No fictional soil result supplied'),
            water_supply=dict(value=None,classification='unknown',provenance='No reliable supply or water rights asserted'))
        dossier['food_forest_preview']=build_food_forest_concept(site)
    return payload
