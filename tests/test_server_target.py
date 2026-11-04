import pytest
from fastapi.testclient import TestClient
from app.server_target import get_server_target,inspect
from app.main import app


@pytest.mark.parametrize('mode,origin,database',[('DEV_SERVER','https://growup.local:8443','growup_dev'),('LIVE_SERVER','https://growup.securecodeusa.com:8443','growup_prod')])
@pytest.mark.parametrize('demo',['yes','no'])
def test_server_and_action_modes_are_independent(mode,origin,database,demo):
    result=inspect({'SERVER_MODE':mode,'DEMO_MODE':demo,'DATABASE_NAME':database})
    assert result['origin']==origin and result['configuration_matches_server']
    assert all(url.startswith(origin+'/') for url in result['endpoints'].values())
    assert result['external_action_mode']==('simulated' if demo=='yes' else 'live_requires_separate_permissions')
    assert not result['provider_access_granted'] and not result['public_routing_verified']


def test_unknown_target_and_cross_server_database_do_not_fall_back(monkeypatch):
    with pytest.raises(ValueError): get_server_target({'SERVER_MODE':'VPS_MAYBE'})
    assert not inspect({'SERVER_MODE':'DEV_SERVER','DATABASE_NAME':'growup_prod'})['configuration_matches_server']
    monkeypatch.setenv('SERVER_MODE','LIVE_SERVER');monkeypatch.setenv('DATABASE_NAME','growup_dev')
    with TestClient(app) as client:
        response=client.get('/ready')
        assert response.status_code==503 and response.json()['server_target']=='database_mismatch'


def test_registered_callback_is_independent_of_public_ui_ports():
    target=get_server_target({'SERVER_MODE':'LIVE_SERVER'})
    assert target.url('/wondrue')=='https://growup.securecodeusa.com:8443/wondrue'
    assert target.url('/integrations/resend/webhook')=='https://growup.securecodeusa.com/integrations/resend/webhook'
    assert inspect({'SERVER_MODE':'DEV_SERVER'})['http_origin']=='http://growup.local:8080'


def test_development_webhook_is_explicitly_unavailable_without_dispatch(monkeypatch):
    from app import resend_tracking
    monkeypatch.setenv('SERVER_MODE','DEV_SERVER');monkeypatch.setenv('RESEND_TRACKING_ENABLED','1')
    monkeypatch.setattr(resend_tracking,'audited',lambda *args:pytest.fail('must not dispatch'))
    with TestClient(app) as client:
        response=client.post('/integrations/resend/webhook',json={})
        assert response.status_code==503 and response.json()['detail']=='resend_callback_unavailable_on_dev_server'
        assert client.get('/health').status_code==200
