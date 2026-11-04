"""One recipient-restricted demo-review delivery; ordinary outreach stays simulated.

Durable claim/audit before transmission. No automatic retry even after timeout;
Resend idempotency is supplementary and expires sooner than our retained claim.
"""
import base64
from datetime import datetime
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
import re
import time
from uuid import UUID

import httpx

from app.communications import lock, read, write, instant
from app.uriel_campaign import demo_message, DEMO_RECIPIENT, SENDER, CONTACT
from app.workflow import digest_payload
from app.workflow_db import WorkflowError, create_run_in_transaction, get_run_by_key_in_transaction

HTTP_CLIENT = httpx.Client
MAX_RESPONSE = 65536


def configuration():
    if os.getenv('DEMO_MODE', 'yes').lower() != 'yes':
        raise WorkflowError('demo sender requires DEMO_MODE=yes')
    if os.getenv('URIEL_DEMO_SEND_APPROVED') != '1':
        raise WorkflowError('recipient-restricted demo delivery is disabled')
    if os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP', '0') == '1':
        raise WorkflowError('demo delivery emergency-stopped')
    authorization = os.getenv('URIEL_DEMO_SEND_AUTHORIZATION', '')
    key = os.getenv('RESEND_API_KEY', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', authorization):
        raise WorkflowError('explicit demo delivery authorization required')
    if not key.startswith('re_') or any(c.isspace() for c in key):
        raise WorkflowError('private Resend API credential required')
    from app.sabbath import blackout
    if blackout(): raise WorkflowError('demo delivery blocked during Sabbath')
    return {'authorization':authorization, 'key':key}


def transaction(config, action, mutate):
    from app.handoff_demo import configuration as demo_configuration
    from app.handoff_demo_domain import scope
    from app.observability.events import AgentEvent, AgentId, RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    db, signing_key = demo_configuration()
    def scoped(conn):
        with scope(conn, signing_key): return mutate(conn)
    event = AgentEvent(agent=AgentId(config.get('agent', 'uriel')), run_id=config['authorization'],
        event_type=('communications.owner_proof_' if config.get('agent') else 'uriel.demo_delivery_') + action,
        summary='Owner-only demo email exception; ordinary outreach stays simulated',
        details={'recipient':DEMO_RECIPIENT, 'authorization':config['authorization']},
        retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_EXTERNAL,
        event=event, mutate=scoped, config=db)


def claim_in_transaction(conn, config, material):
    key = 'uriel-demo-send:' + config['authorization']
    lock(conn, key)
    package_key = 'uriel-demo-package:' + material['campaign_id']
    lock(conn, package_key)
    package_run = get_run_by_key_in_transaction(conn, package_key)
    if package_run is not None:
        binding = read(conn, package_run.run_id, 'binding')
        if binding is None or binding.payload['authorization'] != config['authorization']:
            raise WorkflowError('campaign already claimed under another authorization')
    run = get_run_by_key_in_transaction(conn, key)
    if run is not None:
        prior = read(conn, run.run_id, 'delivery')
        if prior is None or prior.payload['request_digest'] != digest_payload(material):
            raise WorkflowError('authorization already bound to another package')
        return str(run.run_id), False, prior.payload
    run = create_run_in_transaction(conn, idempotency_key=key)
    if package_run is None:
        package_run = create_run_in_transaction(conn, idempotency_key=package_key)
        write(conn, package_run.run_id, 'binding', {'authorization':config['authorization'],
            'delivery_run_id':str(run.run_id), 'campaign_id':material['campaign_id']})
    state = {**material, 'request_digest':digest_payload(material), 'recipient':DEMO_RECIPIENT,
        'status':'claimed_before_transmission', 'requests_consumed':1,
        'created_at':instant().isoformat(), 'automatic_retries':0,
        'scope':'Real owner review email, not synthetic delivery evidence or marketing permission.'}
    write(conn, run.run_id, 'delivery', state)
    return str(run.run_id), True, state


def outcome(config, rid, status, provider_id=None):
    def mutate(conn):
        lock(conn, 'uriel-demo-send:' + config['authorization'])
        row = read(conn, UUID(rid), 'delivery')
        state = {**row.payload, 'status':status, 'provider_id':provider_id,
                 'updated_at':instant().isoformat()}
        write(conn, UUID(rid), 'delivery', state, row)
        return state
    return transaction(config, 'outcome', mutate)


def send_once(campaign, *, start_at, timezone_name):
    from app.personalities import require_channel
    require_channel('uriel','email','outbound')
    config = configuration()
    prepared = demo_message(campaign, start_at=start_at, timezone_name=timezone_name)
    mime = BytesParser(policy=policy.default).parsebytes(prepared['message_bytes'])
    # Construct the complete transport body internally. No recipient, headers,
    # attachment path, CC/BCC, schedule or URL can be supplied by the caller.
    body = {'from':SENDER, 'to':[DEMO_RECIPIENT], 'reply_to':CONTACT,
        'subject':str(mime['Subject']), 'text':mime.get_body(preferencelist=('plain',)).get_content(),
        'attachments':[{'filename':'growup-campaign-demo.zip',
            'content':base64.b64encode(prepared['zip_bytes']).decode()}]}
    html_part = mime.get_body(preferencelist=('html',))
    if html_part:
        body['html'] = html_part.get_content()
        for part in mime.walk():
            if part.get('Content-ID') == '<growup-concept>':
                body['attachments'].append({'filename':'growup-concept.png','content_id':'growup-concept',
                    'content':base64.b64encode(part.get_payload(decode=True)).decode()})
    material = {'campaign_id':campaign['campaign_id'], 'body_digest':digest_payload(body),
        'zip_sha256':hashlib.sha256(prepared['zip_bytes']).hexdigest(),
        'itinerary':prepared['itinerary'], 'from':SENDER, 'reply_to':CONTACT}
    rid, won, prior = transaction(config, 'claim', lambda conn: claim_in_transaction(conn, config, material))
    if not won:
        return {'status':prior['status'], 'provider_id':prior.get('provider_id'),
            'replayed':True, 'new_provider_requests':0, 'delivered':False}
    # Any interruption after the committed claim consumes the one-shot approval.
    # Never release/refund the claim, including an explicit HTTP rejection.
    try:
        configuration()  # Recheck emergency stop and blackout immediately before HTTP.
        deadline = time.monotonic() + 15
        with HTTP_CLIENT(timeout=12.0, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', 'https://api.resend.com/emails', json=body,
                    headers={'Authorization':'Bearer '+config['key'],
                             'Idempotency-Key':'growup-demo/'+config['authorization'],
                             'Accept-Encoding':'identity'}) as response:
                if response.status_code not in (200, 201):
                    raise WorkflowError('provider refused demo delivery; no automatic retry')
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline: raise ValueError('response deadline exceeded')
                    if len(raw)+len(chunk)>MAX_RESPONSE: raise ValueError('bounded response required')
                    raw.extend(chunk)
        provider_id = str(UUID(json.loads(raw)['id']))
    except (httpx.HTTPError, WorkflowError, ValueError, KeyError, TypeError):
        outcome(config, rid, 'uncertain_or_refused')
        raise WorkflowError('demo attempt consumed; inspect provider history before any new authorization') from None
    outcome(config, rid, 'provider_accepted', provider_id)
    return {'status':'provider_accepted', 'provider_id':provider_id, 'replayed':False,
        'new_provider_requests':1, 'recipient':DEMO_RECIPIENT, 'delivered':False,
        'meaning':'Accepted by Resend; recipient delivery is not yet confirmed.'}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-key', required=True)
    parser.add_argument('--start-at', required=True)
    parser.add_argument('--timezone', required=True)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    from app.handoff_demo import service
    from app.uriel import prepare_campaign
    try:
        retained = service('inspect', run_key=args.run_key)
        if retained['phase'] != 'complete_go':
            raise WorkflowError('completed synthetic dossier required for owner review email')
        campaign = prepare_campaign(retained['dossier'])
        start = datetime.fromisoformat(args.start_at)
        prepared = demo_message(campaign, start_at=start, timezone_name=args.timezone)
        if args.send:
            result = send_once(campaign, start_at=start, timezone_name=args.timezone)
        else:
            result = {'status':'preview_only', 'campaign_id':campaign['campaign_id'],
                'recipient':DEMO_RECIPIENT, 'sender':SENDER, 'reply_to':CONTACT,
                'zip_bytes':len(prepared['zip_bytes']),
                'zip_sha256':hashlib.sha256(prepared['zip_bytes']).hexdigest(),
                'itinerary':prepared['itinerary'], 'provider_requests':0,
                'api_credential_installed':bool(os.getenv('RESEND_API_KEY', '').startswith('re_'))}
        print(json.dumps(result, indent=2))
        return 0
    except (WorkflowError, ValueError, TypeError, KeyError):
        print('STOP: demo preparation or delivery failed; preserve the existing authorization and ledger. No secrets displayed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
