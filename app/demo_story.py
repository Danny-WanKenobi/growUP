"""Explicitly fictional expansion stories; static snapshots, never live DB reads."""
from copy import deepcopy
from pathlib import Path
import json
from app.workflow import digest_payload
from app.workflow_db import WorkflowError
ROOT=Path(__file__).resolve().parents[1]/'config'/'demo_story'


def load(name):
    return json.loads((ROOT/name).read_text(encoding='utf-8'))


def seed_cases():
    from app.demo_catalogue import case
    result=[]
    for index,row in enumerate(load('property_seeds.json')['seeds'],1):
        item=case('reference-'+str(index),'SIMULATED conversion of '+row['street_address'],row['city'],row['state'],'Retained discovery reference',row['category'],
            'Real address reference only. Vacancy, distress, tenant relocation, dimensions, engineering, finances, consent and all approvals below are fictional demonstration assumptions.',
            levels=50 if row['category']=='high_rise' else None,site={'usable_area_m2':100,'room_length_m':10,'room_width_m':10})
        item['address']=row['street_address'];item['real_reference']=deepcopy(row)
        item['synthetic_augmentation']=['category treated as assumed for simulation','invented dimensions and engineering inputs','invented financial rates and market demand','simulated verification and consent','fictional specialist/contract/accounting inputs']
        result.append(item)
    return result


STATES='AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split()
ROBOTS=(('Agility Robotics','Digit','Material staging and tote logistics'),('Apptronik','Apollo','Modular rack component staging'),('Tesla','Optimus','Repetitive assembly concept'),('Figure AI','Figure 03','Supervised material-handling concept'),('Boston Dynamics','Atlas','Heavy-part sequencing concept'),('Unitree Robotics','G1','Compact inspection-assistance concept'),('UBTECH Robotics','Walker S2','Maintenance-support concept'))


def program():
    texas=load('texas_places.json')
    return {'synthetic':True,'status':'fictional_expansion_plan_not_executed','texas_places':texas,
        'other_states':[{'state':state,'scenario':'Hypothetical flagship tall-building conversion','largest_real_building_verified':False} for state in STATES if state!='TX'],
        'texas_story':'One hypothetical flagship conversion per listed incorporated place. Not a claim that a skyscraper exists there or that its tallest building was identified.',
        'tenant_story':'Fictional incentives and voluntary relocation to other owner properties. Lease-expiry/nonrenewal outcomes require jurisdiction, consent and legal review; no real tenant action.',
        'distress_story':'Distress and vacancy are invented for this scenario, not facts about the referenced properties.',
        'robotics':[{'company':company,'robot':robot,'proposed_scope':task,'proposal':'SIMULATED tailored proposal: '+task+' for passive Kratky retrofit logistics; request safety, sanitation, payload and integration evidence.',
            'counterproposal':'SIMULATED conditional response; no real company contact or quotation.',
            'contract_status':'SIMULATED awarded in expansion story; no real contract, endorsement, availability or construction capability established.',
            'human_supervision_required':True,'external_actions':0} for company,robot,task in ROBOTS],
        'robot_reference':'https://www.originofbots.com/articles/top-7-industrial-humanoid-robots-transforming-workforces-2026',
        'operational_writes':0,'external_actions':0}


def package_id():
    return digest_payload({'seeds':load('property_seeds.json'),'program':program(),'version':'demo-tour-1'})[:12]


def run_key(scenario):return 'demo-handoff-tour-'+package_id()+'-'+scenario


def build():
    """Explicit preparation only; every service call asserts the isolated DB."""
    from app import handoff_demo as demo
    from app.demo_catalogue import CASES
    results=[];snapshots=[]
    for profile in [*seed_cases(),*CASES]:
        result=demo.run_bounded_scenario(run_key(profile['id']),scenario_id=profile['id'])
        expected='returned_to_gabriel' if profile['expected_failure'] else 'complete_go'
        if result['phase']!=expected:raise WorkflowError('story stopped at an unexpected checkpoint')
        if profile['id']=='elm-fork':result=demo.service('owner_story',run_key=result['run_key'])
        from app.demo_tour import snapshot
        snapshots.append(snapshot(result))
        results.append({k:result[k] for k in ('run_key','phase','verified_receipts','current_holder','scenario_id')})
    cfg,key=demo.configuration()
    from app.properties_db import get_connection
    from app.handoff_demo_domain import scope
    from app.communications import read,write
    with get_connection(cfg) as conn:
        with scope(conn,key):
            run=demo._get(conn,results[0]['run_key'],lock=True)
            old=read(conn,run.run_id,'demonstration-expansion-program')
            if not old:write(conn,run.run_id,'demonstration-expansion-program',demo.marked(program()))
            elif old.payload!=demo.marked(program()):raise WorkflowError('prepared program changed; preserve and create a new package')
            manifest=read(conn,run.run_id,'showcase-tour-manifest')
            value=demo.marked({'cases':snapshots})
            if manifest is None:write(conn,run.run_id,'showcase-tour-manifest',value)
            elif manifest.payload!=value:raise WorkflowError('Tour source changed; original manifest retained for review')
            conn.commit()
    return {'result':'PASS','database':'growup_handoff_demo','results':results,'executed_scenarios':len(results),'texas_planned_slots':len(program()['texas_places']['places']),
        'other_state_planned_slots':49,'simulated_robotics_contracts':7,'provider_calls':0,'operational_writes':0,'external_actions':0,'synthetic':True}
