from copy import deepcopy
import pytest
from app.agents.simeon import assess
from app.agents.stages import build_report
from app.workflow import digest_payload
from tests.test_theresia import inputs as underwriting_inputs


def inputs():
    prop,supplied,reports=underwriting_inputs()
    payload={'property_id':prop,'dossier':{'reports':reports,'specialist_inputs':{'theresia_keys':supplied}}}
    payload['digest']=digest_payload(payload)
    parent=build_report('theresia_keys',payload)
    terms=supplied['contract_terms']['value']; td=digest_payload(terms)
    source='https://records.example/consent'
    wrap=lambda value:{'value':value,'evidence_refs':[source]}
    supplied={'counterparty_consent':wrap({'property_id':prop,'terms_digest':td,'parties':[
        {**party,'status':'consented','record_date':'2026-11-03','source_url':source} for party in terms['parties']]}),
        'approved_terms':wrap({'property_id':prop,'terms_digest':td,'status':'reviewed','record_date':'2026-11-03'}),
        'signing_authority':wrap({'property_id':prop,'principal':'Owner','scope':'review_only','record_date':'2026-11-03'})}
    return prop,supplied,{'theresia_keys':parent}


def test_coordination_review_never_means_authority_or_closed_deal():
    prop,supplied,reports=inputs(); before=deepcopy((supplied,reports))
    result=assess(prop,supplied,reports)
    assert result['ready_for_coordination_review'] and not result['ready_to_close']
    assert not any(result[k] for k in ('consent_authenticated','terms_approval_authenticated','signing_authorized','deal_closed'))
    assert result['conditions_precedent'] and all(not c['resolution_authenticated'] for c in result['conditions_precedent'])
    assert {r['route'] for r in result['exception_routes']}=={'human_review'}
    assert (supplied,reports)==before


@pytest.mark.parametrize('change',['property','terms','declined','unknown','missing_party','duplicate','role','source','date','parent','authority'])
def test_unresolved_evidence_routes_upstream(change):
    prop,supplied,reports=inputs(); parties=supplied['counterparty_consent']['value']['parties']
    if change=='property': supplied['counterparty_consent']['value']['property_id']='00000000-0000-0000-0000-000000000001'
    if change=='terms': supplied['approved_terms']['value']['terms_digest']='outdated-version'
    if change in ('declined','unknown'): parties[0]['status']=change
    if change=='missing_party': parties.pop()
    if change=='duplicate': parties.append(deepcopy(parties[0]))
    if change=='role': parties[0]['role']='unrelated'
    if change=='source': parties[0]['source_url']='https://elsewhere.example/unretained'
    if change=='date': parties[0]['record_date']='unknown'
    if change=='parent': reports['theresia_keys']['underwriting_assessment']['status']='changed'
    if change=='authority': supplied['signing_authority']['value']['scope']='unbounded_full_access'
    result=assess(prop,supplied,reports)
    assert result['status']=='needs_evidence'
    assert any(r['route']=='theresia_keys' for r in result['exception_routes'])
    assert not result['deal_closed']


def test_asserted_approval_and_power_of_attorney_never_grant_execution():
    prop,supplied,reports=inputs()
    supplied['approved_terms']['value'].update(status='approved_claimed',authenticated=True)
    supplied['signing_authority']['value'].update(scope='signing_claimed',poa_valid=True)
    result=assess(prop,supplied,reports)
    assert not result['signing_authorized'] and not result['terms_approval_authenticated']
    assert all(not r['grants_permission'] for r in result['owner_review_requests'])
    assert any(r['requirement']=='closing_execution_authority' for r in result['owner_review_requests'])


def test_shared_report_is_sealed_with_exception_routes():
    prop,supplied,reports=inputs()
    payload={'property_id':prop,'dossier':{'reports':reports,'specialist_inputs':{'simeon':supplied}}}
    payload['digest']=digest_payload(payload)
    result=build_report('simeon',payload)
    assert result['status']=='prepared_not_approved' and result['owner_review_requests']
    assert result['closing_assessment']['terms_digest']==supplied['approved_terms']['value']['terms_digest']
    assert result['digest']==digest_payload({k:v for k,v in result.items() if k!='digest'})


def test_exception_routes_persist_in_existing_owner_queue_and_replay(monkeypatch):
    from tests.test_dossier_coordinator import specialist_seed
    from tests.test_handoff_demo import KEY
    from app.handoff_demo_domain import scope
    from app.agents.stages import prepare_in_transaction,owner_queue_in_transaction
    conn,rid=specialist_seed(monkeypatch,fail=True,agent='simeon')
    with scope(conn,KEY):
        result=prepare_in_transaction(conn,run_id=rid,dossier_key='main',agent='simeon')
        assert result['report']['closing_assessment']['status']=='needs_evidence'
        tasks=owner_queue_in_transaction(conn,run_id=rid)['tasks']
        assert {t.get('route') for t in tasks} >= {'theresia_keys','human_review'}
        assert all(not t['grants_permission'] for t in tasks)
        before=deepcopy(conn._db)
        assert prepare_in_transaction(conn,run_id=rid,dossier_key='main',agent='simeon')['replayed']
        assert conn._db==before
