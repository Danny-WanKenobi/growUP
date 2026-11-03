"""No-network, no-database Resend configuration preview; safe with placeholders."""
import base64
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit
from uuid import UUID

CALLBACK_PATH = "/integrations/resend/webhook"
PLACEHOLDER_URL = "https://REPLACE_ME_CALLBACK_HOST.example.invalid"+CALLBACK_PATH


def callback_configured(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold()
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != CALLBACK_PATH
                or parsed.port not in (None, 443) or "replace_me" in value.casefold()
                or host == "localhost" or host.endswith((".local", ".localhost", ".invalid", ".test", ".example"))
                or host in ("example.com", "example.org", "example.net") or "." not in host):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass  # Host syntax only: no DNS lookup or reachability claim.
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def inspect(environ=None):
    env = os.environ if environ is None else environ
    url = env.get("RESEND_PUBLIC_CALLBACK_URL", PLACEHOLDER_URL)
    valid_url = callback_configured(url)
    secret = env.get("RESEND_WEBHOOK_SECRET", "")
    try:
        secret_ready = secret.startswith("whsec_") and len(base64.b64decode(secret[6:], validate=True)) >= 16
    except (ValueError, TypeError):
        secret_ready = False
    domain = env.get("RESEND_RECEIVING_DOMAIN", "")
    receiving_ready = bool(re.fullmatch(r"[A-Za-z0-9-]+(?:[.][A-Za-z0-9-]+)+", domain) and "replace_me" not in domain.casefold() and not domain.endswith((".invalid", ".local")))
    sender = env.get("RESEND_SENDER", "")
    sender_ready = bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>]+[.][^\s@<>]+", sender))
    inbox = env.get("RESEND_INBOX_ID", "")
    try:
        inbox_ready = bool(inbox and UUID(inbox))
    except ValueError:
        inbox_ready = False
    enabled = env.get("RESEND_TRACKING_ENABLED", "0") == "1"
    blockers = []
    if not valid_url: blockers.append("Replace RESEND_PUBLIC_CALLBACK_URL after selecting a public HTTPS host")
    if not secret_ready: blockers.append("Install the private Resend webhook signing secret")
    if not receiving_ready: blockers.append("Set the actual Resend receiving domain for unique reply aliases")
    if not sender_ready: blockers.append("Set RESEND_SENDER to the verified sending address")
    if inbox and not inbox_ready: blockers.append("Correct the optional RESEND_INBOX_ID UUID")
    if not enabled: blockers.append("Tracking remains disabled")
    return {"mode":"configuration preview only", "callback_url_configured":valid_url,
        "callback_path":CALLBACK_PATH, "signing_secret_configured":secret_ready,
        "receiving_domain_configured":receiving_ready, "sender_configured":sender_ready, "inbox_id_configured":inbox_ready,
        "tracking_enabled":enabled, "configuration_complete":not blockers, "blockers":blockers,
        "public_reachability_verified":False, "live_provider_compatibility_verified":False,
        "provider_calls":0, "database_writes":0,
        "note":"Placeholder configuration permits local signed-fixture testing; public callbacks remain unavailable."}


if __name__ == "__main__":
    print(json.dumps(inspect(), indent=2))
