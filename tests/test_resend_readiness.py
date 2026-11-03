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
