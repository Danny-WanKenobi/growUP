from uuid import uuid4
from copy import deepcopy
import pytest
from app.workflow import digest_payload
from app.workflow_db import WorkflowError
from app.agents.onesimus.portfolio import plan_rooms,assess_room_finance
from app.handoff_demo import _site
from app.demo import _demo_financial_inputs


def material():
    prop=str(uuid4());inspection=dict(agent='raphael',outcome='authorized',property_id=prop);inspection['digest']=digest_payload(inspection)
    payload=dict(property_id=prop,dossier={'reports':{'raphael':inspection}});payload['digest']=digest_payload(payload)
    room=dict(room_id='north',property_id=prop,inspection_digest=inspection['digest'],distinct_room_claim=True,evidence_refs=['fixture:room'],site={k:v.model_dump(mode='json') for k,v in _site().items()})
    return payload,[room,{**deepcopy(room),'room_id':'south'}]


def test_distinct_rooms_select_once_and_finance_requires_explicit_allocations():
    payload,rooms=material();portfolio=plan_rooms(payload,rooms)
    assert portfolio['complete'] and all(r['selected_config']=='rack3' for r in portfolio['rooms'])
    expected=sum(r['selected_scenario']['outputs']['kg_per_year'] for r in portfolio['rooms'])
    assert float(portfolio['selected_room_totals']['kg_per_year'])==pytest.approx(expected)
    assert not portfolio['building_capacity_verified']
    assert not assess_room_finance(portfolio,None)['complete']
    from app.michael import FinancialInput
    inputs={k:v.model_dump(mode='json') for k,v in _demo_financial_inputs('bench').items()}
    for name,unit in [('occupancy_usd_per_m2_year','USD/m2/year'),('maintenance_usd_per_year','USD/year')]:
        inputs[name]=FinancialInput(name=name,value=0,unit=unit,source='fixture:explicit-allocated-zero',classification='assumed',scenario='fixture').model_dump(mode='json')
    finance=assess_room_finance(portfolio,{'north':deepcopy(inputs),'south':deepcopy(inputs)})
    assert finance['complete'] and finance['favorable_under_assumptions']


def test_rooms_refuse_duplication_foreign_context_and_impossible_layout():
    payload,rooms=material()
    with pytest.raises(WorkflowError,match='unique'): plan_rooms(payload,[rooms[0],rooms[0]])
    rooms[1]['property_id']=str(uuid4())
    with pytest.raises(WorkflowError,match='bind'): plan_rooms(payload,rooms)
    rooms=material()[1]
    payload,rooms=material();rooms[1]['site']['clear_height_m']['value']=.1
    portfolio=plan_rooms(payload,rooms)
    assert not portfolio['complete'] and portfolio['selected_room_totals']=={}
    rooms[1]['distinct_room_claim']=False
    with pytest.raises(WorkflowError,match='distinct'): plan_rooms(payload,rooms)
