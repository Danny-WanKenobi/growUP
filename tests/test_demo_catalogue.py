import pytest
from app import handoff_demo as demo
from app.demo_catalogue import CASES
from app.handoff_demo_domain import scope
from tests.test_handoff_demo import Conn,KEY
from tests.test_funnel_acceptance import _new_db


@pytest.mark.parametrize('profile',CASES,ids=[p['id'] for p in CASES])
def test_catalogue_routes_and_distinct_geography(monkeypatch,profile):
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    conn=Conn(_new_db())
    with scope(conn,KEY):
        result=demo.start_in_transaction(conn,run_key='demo-handoff-catalogue-'+profile['id'],scenario_id=profile['id'])
        for _ in range(demo.MAX_STEPS):
            if result['phase'] in ('complete_go','returned_to_gabriel'): break
            result=demo.advance_in_transaction(conn,run_key=result['run_key'],expected_sequence=result['sequence'])
        assert result['phase']==('returned_to_gabriel' if profile['expected_failure'] else 'complete_go')
        assert [e['agent'] for e in result['events'] if e.get('checkpoint')=='NO-GO']==([profile['expected_failure']] if profile['expected_failure'] else [])
        assert result['dossier']['dossier']['city']==profile['city']
        if profile['id']=='elm-fork':
            from app.demo_owner_story import exercise
            run=demo._get(conn,result['run_key'])
            story=exercise(conn,run,full=True)
            assert story['result']=='PASS' and story['vault_preserved_after_new_evidence']
            assert exercise(conn,run,full=True)==story
        if profile['property_type']=='vacant_lot':
            concept=result['dossier']['dossier']['food_forest_preview']
            assert concept['conceptual_only'] and concept['yield_estimate'] is None and 'soil' in concept['missing']


def test_catalogue_selection_binding_and_owner_story_gate(monkeypatch):
    from app.workflow_db import WorkflowError
    from app.demo_owner_story import exercise
    conn=Conn(_new_db())
    with scope(conn,KEY):
        demo.start_in_transaction(conn,run_key='demo-handoff-profile-binding',scenario_id='hudson-sky')
        with pytest.raises(WorkflowError,match='conflicting'): demo.start_in_transaction(conn,run_key='demo-handoff-profile-binding',scenario_id='cascadia-spire')
        with pytest.raises(WorkflowError,match='overridden'): demo.start_in_transaction(conn,run_key='demo-handoff-profile-invalid',scenario_id='hudson-sky',fail_at='raphael')
        with pytest.raises(WorkflowError,match='completed'): exercise(conn,demo._get(conn,'demo-handoff-profile-binding'),full=True)
    assert len({p['state'] for p in CASES})==16
    assert {p['property_type'] for p in CASES}=={'high_rise','commercial_property','warehouse_industrial','vacant_lot'}
    assert all(sum(p['property_type']==kind for p in CASES)==4 for kind in {p['property_type'] for p in CASES})
