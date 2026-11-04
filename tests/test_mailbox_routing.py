from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import mailbox_routing as mailbox, resend_tracking
from app.main import app
from app.workflow_db import WorkflowError
from tests.test_retained_assessment import Conn
from tests.test_funnel_acceptance import _new_db


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("RESEND_RECEIVING_DOMAIN", "growup.securecodeusa.com")
    monkeypatch.setenv("RESEND_MAILBOX_ROUTING_ENABLED", "1")
    return Conn(_new_db())


def event(name="simeon", **changes):
    return {"type": "email.received", "data": {"email_id": str(uuid4()),
        "from": "person@example.com", "to": [name + "@growup.securecodeusa.com"],
        "subject": "Question", **changes}}


def deliver(conn, item):
    return resend_tracking.consume(conn, item, "msg_test", "a" * 64)


@pytest.mark.parametrize("name", ["simeon", "solomon"])
def test_named_mail_is_durable_and_only_inbound_bound_drafts_are_possible(setup, name):
    item = event(name)
    result = deliver(setup, item)
    assert result["mailbox"] == name
    before = deepcopy(setup._db)
    assert deliver(setup, item)["replayed"]
    assert setup._db == before
    draft = mailbox.control(setup, conversation_id=result["conversation_id"], action="draft",
        principal="owner", text="Reply draft", expected_revision=1)
    assert draft["from"] == name + "@growup.securecodeusa.com"
    assert draft["to"] == "person@example.com" and not draft["send_enabled"]
    with pytest.raises(WorkflowError, match="revision"):
        mailbox.control(setup, conversation_id=result["conversation_id"], action="draft",
            principal="owner", text="stale", expected_revision=1)
    with pytest.raises(WorkflowError, match="cannot initiate"):
        mailbox.control(setup, conversation_id=str(uuid4()), action="draft", principal="owner",
            text="unsolicited", expected_revision=1)


def test_uriel_is_system_notice_only_and_replays_never_duplicate_notice(setup):
    item = event("uriel")
    result = deliver(setup, item)
    view = mailbox.control(setup, conversation_id=result["conversation_id"], action="inspect", principal="owner")
    assert view["notice"]["owner"] == "system"
    assert view["notice"]["auto_submitted"] == "auto-replied"
    assert not view["notice"]["send_enabled"]
    assert "suppress automatic/list/bounce senders" in view["notice"]["required_before_send"]
    before = deepcopy(setup._db)
    assert deliver(setup, item)["replayed"] and setup._db == before
    with pytest.raises(WorkflowError, match="only Simeon or Solomon"):
        mailbox.control(setup, conversation_id=result["conversation_id"], action="draft",
            principal="owner", text="Uriel answer", expected_revision=1)


@pytest.mark.parametrize("recipients", [["unknown@growup.securecodeusa.com"],
    ["simeon@growup.securecodeusa.com", "solomon@growup.securecodeusa.com"],
    ["simeon@other.example"]])
def test_unknown_and_ambiguous_addresses_never_guess_owner(setup, recipients):
    assert deliver(setup, event(to=recipients))["mailbox"] == "human_review"


def test_conflicting_retry_refuses_and_cold_lead_is_linked_not_auto_sent(setup):
    item = event()
    result = deliver(setup, item)
    changed = deepcopy(item)
    changed["data"]["from"] = "other@example.com"
    with pytest.raises(WorkflowError, match="identity conflict"):
        deliver(setup, changed)
    rework = mailbox.control(setup, conversation_id=result["conversation_id"], action="request_uriel_followup",
        principal="owner", expected_revision=1, text="Lead requested pricing, then went silent. Preserve notes.")
    assert rework["owner"] == "uriel" and not rework["marketing_authorized"]
    assert not rework["custody_transferred"] and rework["inbound_record_digest"]


def test_unverified_intake_emergency_stop_and_legacy_alias(setup, monkeypatch):
    with pytest.raises(WorkflowError, match="verified"):
        mailbox.receive(setup, metadata=event()["data"], event_id="id", body_digest="a", authentication="claim")
    assert mailbox.receive(setup, metadata=event("reply-" + uuid4().hex)["data"],
        event_id="id", body_digest="a", authentication="resend_svix_hmac_sha256") is None
    result = deliver(setup, event())
    monkeypatch.setenv("DOSSIER_COORDINATOR_EMERGENCY_STOP", "1")
    with pytest.raises(WorkflowError, match="emergency"):
        mailbox.control(setup, conversation_id=result["conversation_id"], action="draft",
            principal="owner", text="reply", expected_revision=1)


def test_private_mailbox_endpoint_requires_operator(setup, monkeypatch):
    monkeypatch.setenv("RESEND_TRACKING_ENABLED", "1")
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "operator-test")
    monkeypatch.setenv("WONDRUE_READER_KEY", "reader-test")
    body = {"conversation_id": str(uuid4()), "action": "inspect"}
    with TestClient(app) as client:
        assert client.post("/integrations/resend/mailboxes", json=body).status_code == 401
        assert client.post("/integrations/resend/mailboxes", json=body,
            headers={"X-Wondrue-Key": "reader-test"}).status_code == 403


@pytest.mark.parametrize("sender", ["simeon@growup.securecodeusa.com", "mailer-daemon@example.com"])
def test_notice_suppresses_self_mail_and_bounces(setup, sender):
    result = deliver(setup, event("uriel", **{"from": sender}))
    view = mailbox.control(setup, conversation_id=result["conversation_id"], action="inspect", principal="owner")
    assert view["notice"]["suppressed"] and not view["notice"]["send_enabled"]


def test_signed_webhook_reaches_direct_router_and_bad_signature_never_writes(setup, monkeypatch):
    import json
    from tests.test_resend_tracking import SECRET, signed
    monkeypatch.setenv("SERVER_MODE", "LIVE_SERVER")
    monkeypatch.setenv("RESEND_TRACKING_ENABLED", "1")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(resend_tracking, "audited", lambda action, principal, inputs:
        resend_tracking.consume(setup, **inputs))
    body = json.dumps(event("solomon")).encode()
    before = deepcopy(setup._db)
    with TestClient(app) as client:
        assert client.post("/integrations/resend/webhook", content=body).status_code == 401
        assert setup._db == before
        response = client.post("/integrations/resend/webhook", content=body, headers=signed(body))
        assert response.status_code == 200 and response.json()["mailbox"] == "solomon"
        assert client.post("/integrations/resend/webhook", content=body,
            headers=signed(body)).json()["replayed"]


@pytest.mark.skipif(__import__('os').getenv('GROWUP_PG_INTEGRATION') != '1',
    reason='PostgreSQL integration explicitly disabled')
def test_pg_direct_mailbox_persistence_concurrency_and_atomic_rollback(monkeypatch):
    """Real DB, audit transaction and locks; no provider or operational DB use."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.properties_db import get_connection
    from app.workflow_db import get_run_by_key_in_transaction
    from app.db import DatabaseUnavailableError
    from app.observability import transactions
    from tests.test_dcad_admission import _require_gate_database
    _require_gate_database()
    monkeypatch.setenv('RESEND_RECEIVING_DOMAIN', 'growup.securecodeusa.com')
    monkeypatch.setenv('RESEND_MAILBOX_ROUTING_ENABLED', '1')
    monkeypatch.setenv('DEMO_MODE', 'no')
    monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP', '0')
    item = event('simeon')
    mid = item['data']['email_id']
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        return resend_tracking.audited('receive', 'pg-test-mailbox',
            {'event': item, 'event_id': 'msg_' + uuid4().hex, 'body_digest': 'a' * 64})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(not result['replayed'] for result in results) == 1
    with get_connection() as conn:
        view = mailbox.control(conn, conversation_id=mid, action='inspect', principal='pg-test')
        assert view['state']['owner'] == 'simeon' and view['state']['revision'] == 1
        run = get_run_by_key_in_transaction(conn, 'direct-mail:' + mid)
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) AS n FROM workflow_records WHERE run_id=%(rid)s', {'rid':run.run_id})
            assert cur.fetchone()['n'] == 2
    draft = mailbox.audited('pg-test', {'conversation_id': mid, 'action':'draft',
        'expected_revision':1, 'text':'PostgreSQL retained draft only.'})
    assert draft['revision'] == 2 and not draft['send_enabled']
    with pytest.raises(WorkflowError, match='revision'):
        mailbox.audited('pg-test', {'conversation_id':mid, 'action':'draft',
            'expected_revision':1, 'text':'Stale retry must not append.'})
    followup = mailbox.audited('pg-test', {'conversation_id':mid, 'action':'request_uriel_followup',
        'expected_revision':2, 'text':'Retain notes for later dossier linkage.'})
    assert followup['revision'] == 3 and not followup['custody_transferred']
    with get_connection() as conn:
        view = mailbox.control(conn, conversation_id=mid, action='inspect', principal='pg-test')
        assert view['state']['revision'] == 3
        run = get_run_by_key_in_transaction(conn, 'direct-mail:' + mid)
        assert mailbox.read(conn, run.run_id, 'action:2').payload['text'] == draft['text']
        assert mailbox.read(conn, run.run_id, 'action:3').payload['inbound_record_digest']

    for name in ('uriel', 'solomon', 'unknown'):
        other = event(name)
        result = resend_tracking.audited('receive', 'pg-test-mailbox',
            {'event':other, 'event_id':'msg_'+uuid4().hex, 'body_digest':'b'*64})
        with get_connection() as conn:
            view = mailbox.control(conn, conversation_id=result['conversation_id'], action='inspect', principal='pg-test')
            assert view['state']['owner'] == (name if name != 'unknown' else 'human_review')
            assert not view['state']['delivery_enabled']
            if name == 'uriel':
                assert view['notice']['owner'] == 'system' and not view['notice']['send_enabled']

    # Inject failure AFTER business writes but BEFORE audit/commit. Neither run,
    # records nor authority may survive; a later normal retry must succeed fresh.
    interrupted = event('solomon')
    interrupted_mid = interrupted['data']['email_id']
    original = transactions.insert_event_in_transaction
    def fail_audit(*args, **kwargs):
        raise RuntimeError('intentional test-only audit failure')
    monkeypatch.setattr(transactions, 'insert_event_in_transaction', fail_audit)
    inputs = {'event':interrupted, 'event_id':'msg_'+uuid4().hex, 'body_digest':'c'*64}
    with pytest.raises(DatabaseUnavailableError):
        resend_tracking.audited('receive', 'pg-test-mailbox', inputs)
    monkeypatch.setattr(transactions, 'insert_event_in_transaction', original)
    with get_connection() as conn:
        assert get_run_by_key_in_transaction(conn, 'direct-mail:' + interrupted_mid) is None
    assert not resend_tracking.audited('receive', 'pg-test-mailbox', inputs)['replayed']
