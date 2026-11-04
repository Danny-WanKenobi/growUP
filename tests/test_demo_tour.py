"""No provider calls; reuse real custody services only in explicit demo scope."""
from copy import deepcopy
import pytest
from app import handoff_demo as demo
from app.demo_story import seed_cases,program,run_key
from app.handoff_demo_domain import scope
from app.demo_tour import snapshot,inspect_in_transaction,CHAPTERS
from tests.test_handoff_demo import Conn,KEY
from tests.test_funnel_acceptance import _new_db


@pytest.mark.parametrize('profile',seed_cases(),ids=lambda p:p['id'])
def test_source_reference_gets_new_synthetic_identity_and_completes(monkeypatch,profile):
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1');conn=Conn(_new_db())
    with scope(conn,KEY):
        result=demo.start_in_transaction(conn,run_key=run_key(profile['id']),scenario_id=profile['id'])
        for _ in range(demo.MAX_STEPS):
            if result['phase'] in ('complete_go','returned_to_gabriel'):break
            result=demo.advance_in_transaction(conn,run_key=result['run_key'],expected_sequence=result['sequence'])
        assert result['phase']=='complete_go' and result['current_holder']=='manny'
        d=result['dossier']['dossier'];assert result['property_id']!=profile['real_reference']['id']
        assert d['synthetic'] and d['reference_copy']['source_database']=='growup_dev'
        assert d['synthetic_augmentation'] and len(d['reports']['solomon']['simulated_robotics_program'])==7
        assert all(c['external_actions']==0 for c in d['reports']['solomon']['simulated_robotics_program'])
        assert d['reports']['onesimus']['reference_context']['library_digest']
        assert snapshot(result)['digest']==result['dossier']['digest']


def test_program_is_planned_not_executed_and_geography_is_not_building_evidence():
    p=program();assert p['status']=='fictional_expansion_plan_not_executed'
    assert len(p['texas_places']['places'])>1000 and len(p['other_states'])==49
    assert len({r['company'] for r in p['robotics']})==7
    assert not any(r['largest_real_building_verified'] for r in p['other_states'])
    assert 480<=sum(len(t.split()) for _,t in CHAPTERS)<=580


def test_tour_refuses_operational_database_and_missing_demo_configuration(monkeypatch):
    with pytest.raises(ValueError,match='operational databases are forbidden'):
        inspect_in_transaction(Conn(dict(_new_db(),database='growup_dev',role='growup_app')))
    monkeypatch.setenv('HANDOFF_DEMO_ENABLED','1');monkeypatch.delenv('HANDOFF_DEMO_DATABASE_NAME',raising=False)
    from app.demo_tour import service
    with pytest.raises(ValueError,match='no operational credential fallback'):service()


def test_tour_auth_and_static_page_do_not_call_operational_connector(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import properties_db
    monkeypatch.setenv('WONDRUE_OPERATOR_KEY','TEST-tour-only')
    monkeypatch.setattr(properties_db,'get_connection',lambda *a,**k:pytest.fail('operational connector forbidden for static tour'))
    with TestClient(app) as client:
        assert client.get('/wondrue/demo-tour').status_code==200
        assert client.get('/wondrue/api/demo-tour').status_code==401
        assert client.get('/wondrue/api/demo-tour/reference/us').status_code==401
        pdf=client.get('/wondrue/api/demo-tour/reference/us',headers={'X-Wondrue-Key':'TEST-tour-only'})
        assert pdf.status_code==200 and pdf.content.startswith(b'%PDF-') and pdf.headers['cache-control']=='no-store'
        html=client.get('/wondrue/demo-tour').text
        assert 'shell.js' not in html and 'demo-tour.js' in html
        js=client.get('/wondrue/assets/demo-tour.js').text
        assert "fetch('/wondrue/api/demo-tour'" in js and '/chats/' not in js
