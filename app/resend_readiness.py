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
    from app.server_target import get_server_target,inspect as inspect_server
    try:
        target=get_server_target(env); server=inspect_server(env)
    except ValueError:
        return {'configuration_complete':False,'webhook_configuration_complete':False,'polling_configuration_complete':False,
            'blockers':['Invalid SERVER_MODE or DEMO_MODE'],'provider_calls':0,'database_writes':0}
    url = env.get("RESEND_PUBLIC_CALLBACK_URL") or (target.url(CALLBACK_PATH) if target.callback_available else PLACEHOLDER_URL)
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
    blockers.extend(server['blockers'])
    if not target.callback_available: blockers.append('DEV_SERVER has no public callback; use offline verification or separately bounded polling')
    elif url!=target.url(CALLBACK_PATH): blockers.append('Callback must match the selected LIVE_SERVER endpoint')
    if not valid_url: blockers.append("Replace RESEND_PUBLIC_CALLBACK_URL after selecting a public HTTPS host")
    if not secret_ready: blockers.append("Install the private Resend webhook signing secret")
    if not receiving_ready: blockers.append("Set the actual Resend receiving domain for unique reply aliases")
    if not sender_ready: blockers.append("Set RESEND_SENDER to the verified sending address")
    if inbox and not inbox_ready: blockers.append("Correct the optional RESEND_INBOX_ID UUID")
    if not enabled: blockers.append("Tracking remains disabled")
    poll_blockers=list(server['blockers'])
    for variable in ('COMMUNICATIONS_ROUTER_ENABLED','RESEND_POLL_ENABLED','RESEND_POLL_ACCESS_APPROVED'):
        if env.get(variable,'0')!='1': poll_blockers.append(variable+' remains disabled')
    if not env.get('RESEND_API_KEY','').startswith('re_'): poll_blockers.append('Install the private Resend API credential')
    if not env.get('RESEND_POLL_AUTHORIZATION_ID'): poll_blockers.append('Set the separately approved authorization ID')
    try:
        if not 1<=int(env.get('RESEND_POLL_REQUEST_ALLOWANCE','0'))<=4: raise ValueError()
    except ValueError: poll_blockers.append('Set a separately approved request allowance between 1 and 4')
    from app.communications import instant
    try:
        expiry=env.get('RESEND_POLL_APPROVAL_EXPIRES_AT','')
        if not expiry or not 0<(instant(expiry)-instant()).total_seconds()<=3600: raise ValueError()
    except (ValueError,TypeError): poll_blockers.append('Set an explicit approval expiry within one hour')
    if not receiving_ready: poll_blockers.append('Set the actual receiving domain')
    if not poll_blockers:
        from app.resend_poll import configuration
        from app.workflow_db import WorkflowError
        try: configuration(env)
        except (WorkflowError,ValueError,TypeError): poll_blockers.append('Polling configuration fails runtime validation')

    return {"mode":"configuration preview only", "server_target":server,
        "effective_callback_url":server['resend_callback_url'], "callback_url_configured":valid_url,
        "callback_path":CALLBACK_PATH, "signing_secret_configured":secret_ready,
        "receiving_domain_configured":receiving_ready, "sender_configured":sender_ready, "inbox_id_configured":inbox_ready,
        "tracking_enabled":enabled, "configuration_complete":not blockers, "blockers":blockers,
        "direct_mailbox_routing_enabled":env.get("RESEND_MAILBOX_ROUTING_ENABLED", "0")=="1",
        "direct_mailbox_delivery_implemented":False,
        "receiving_read_api_key_installed":bool(env.get("RESEND_RECEIVING_API_KEY","")),
        "webhook_configuration_complete":not blockers, "polling_configuration_complete":not poll_blockers, "polling_blockers":poll_blockers,
        "public_reachability_verified":False, "live_provider_compatibility_verified":False,
        "provider_calls":0, "database_writes":0,
        "communications_router_enabled":env.get("COMMUNICATIONS_ROUTER_ENABLED","0")=="1",
        "polling_enabled":env.get("RESEND_POLL_ENABLED","0")=="1",
        "polling_access_approved":env.get("RESEND_POLL_ACCESS_APPROVED","0")=="1",
        "poll_api_key_installed":bool(env.get("RESEND_API_KEY","")),
        "poll_request_allowance_configured":env.get("RESEND_POLL_REQUEST_ALLOWANCE","0")!="0",
        "poll_mode_requires_public_callback":False,
        "note":("Private DEV_SERVER: callbacks are intentionally unavailable; offline tests and separately authorized polling remain possible."
            if not target.callback_available else "LIVE_SERVER callback selected; this preview does not verify routing or enable provider access. Resolve the listed prerequisites before activation.")}


if __name__ == "__main__":
    print(json.dumps(inspect(), indent=2))
