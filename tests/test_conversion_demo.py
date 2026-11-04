from copy import deepcopy
from uuid import uuid4

import pytest

from app.conversion_demo import DEFAULTS, build, simulate, validate
from app.workflow import digest_payload
from app.workflow_db import WorkflowError


def test_demo_funding_cases_and_agent_briefs_preserve_source():
    source={'synthetic':True,'domain':'synthetic_handoff_demo','property_id':str(uuid4())}
    source['digest']=digest_payload(source)
    before=deepcopy(source)
    result=build(source)
    assert source==before
    model=result['model']
    assert model['owner_funded']['owner_cash_needed_usd']=='240000.00'
    assert [v['project_funding_gap_usd'] for v in model['participation']['cases']]==['200000.00','125000.00','0.00']
    assert [v['hypothetical_gross_usd'] for v in model['participation']['cases']]==['0.00','125000.00','250000.00']
    assert all(not v['real_progression_authorized'] for v in model['participation']['cases'])
    assert result['elmer']['ledger_postings']==0
    assert result['theresia']['contract_executable'] is False
    assert result['simeon']['real_actions_enabled'] is False
    assert all(result[a]['source_model_digest']==model['digest'] for a in ('simeon','theresia','elmer'))


@pytest.mark.parametrize('change',[{'owner_units':3001},{'unit_price_usd':'NaN'},{'project_cost_usd':None},{'total_units':True}])
def test_invalid_or_unbalanced_inputs_refuse(change):
    with pytest.raises(WorkflowError):
        simulate(dict(DEFAULTS,**change),property_id=str(uuid4()),source_digest='a'*64)


def test_operational_or_tampered_source_refused():
    with pytest.raises(WorkflowError):build({'synthetic':False})
    model=simulate(DEFAULTS,property_id=str(uuid4()),source_digest='a'*64)
    model['participation']['offering_authorized']=True
    with pytest.raises(WorkflowError):validate(model)
