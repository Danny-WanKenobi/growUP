import json
import pytest
from app.resend_readiness import inspect, callback_configured, PLACEHOLDER_URL


@pytest.mark.parametrize("url", [PLACEHOLDER_URL, "https://securecodeusa.local/integrations/resend/webhook",
    "http://hooks.example.org/integrations/resend/webhook", "https://127.0.0.1/integrations/resend/webhook",
    "https://hooks.example.org/wrong", "https://user:secret@hooks.example.org/integrations/resend/webhook",
    "https://hooks.example.org/integrations/resend/webhook?secret=value"])
def test_placeholder_and_nonpublic_or_wrong_callback_refuse(url):
    assert not callback_configured(url)


def test_no_host_needed_for_secret_free_local_preview():
    result = inspect({"RESEND_WEBHOOK_SECRET":"private-value-not-to-echo"})
    assert not result["configuration_complete"]
    assert result["provider_calls"] == result["database_writes"] == 0
    assert "private-value" not in json.dumps(result)
    assert not result["public_reachability_verified"]


def test_callback_is_configuration_only_never_reachability_proof():
    assert callback_configured("https://hooks.securecodeusa.com/integrations/resend/webhook")
    result = inspect({"RESEND_PUBLIC_CALLBACK_URL":"https://hooks.securecodeusa.com/integrations/resend/webhook"})
    assert result["callback_url_configured"] and not result["public_reachability_verified"]
    assert not result["configuration_complete"]


def test_receive_only_poll_configuration_needs_no_public_callback_or_webhook_secret():
    from datetime import timedelta
    from app.communications import instant
    env={'COMMUNICATIONS_ROUTER_ENABLED':'1','RESEND_POLL_ENABLED':'1','RESEND_POLL_ACCESS_APPROVED':'1',
        'RESEND_API_KEY':'re_offline_fake_never_live','RESEND_POLL_AUTHORIZATION_ID':'owner-bounded-test',
        'RESEND_POLL_REQUEST_ALLOWANCE':'1','RESEND_POLL_APPROVAL_EXPIRES_AT':(instant()+timedelta(minutes=10)).isoformat(),
        'RESEND_RECEIVING_DOMAIN':'test.resend.app'}
    result=inspect(env)
    assert result['polling_configuration_complete'] and not result['webhook_configuration_complete']
    assert not result['poll_mode_requires_public_callback']
    assert 're_offline_fake_never_live' not in json.dumps(result)
    assert not result['live_provider_compatibility_verified']
