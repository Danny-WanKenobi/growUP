import base64
import hashlib
import hmac
import json
import os
import time
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import resend_tracking as service
from app.priscilla import PriscillaError
from tests.test_priscilla import _FakeConn

SECRET = "whsec_"+base64.b64encode(b"offline-test-key-0123456789abcdef").decode()


def signed(body, offset=0):
    timestamp = str(int(time.time())+offset)
    headers = {"svix-id": "msg_test_1", "svix-timestamp": timestamp}
    raw = headers["svix-id"].encode()+b"."+timestamp.encode()+b"."+body
    headers["svix-signature"] = "v1,"+base64.b64encode(hmac.new(base64.b64decode(SECRET[6:]), raw, hashlib.sha256).digest()).decode()
    return headers


def event():
    return {"type": "email.delivered", "created_at": "2026-11-03T23:00:00Z", "data": {
        "email_id": str(uuid4()), "from": "Uriel <uriel@securecodeusa.com>", "to": ["owner@example.com"]}}


def setup(monkeypatch):
    monkeypatch.setenv("RESEND_TRACKING_ENABLED", "1")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("RESEND_SENDER", "uriel@securecodeusa.com")
    monkeypatch.setenv("RESEND_RECEIVING_DOMAIN", "test.resend.app")


@pytest.mark.parametrize("failure", ["stale", "future", "tamper", "wrong_key", "duplicate", "oversized"])
def test_invalid_webhook_never_dispatches(monkeypatch, failure):
    setup(monkeypatch)
    monkeypatch.setattr(service, "audited", lambda *a: pytest.fail("must not dispatch"))
    body = json.dumps(event()).encode()
    if failure == "duplicate": body = b'{"type":"email.sent","type":"email.delivered"}'
    if failure == "oversized": body = b"x"*32769
    headers = signed(body, -600 if failure == "stale" else 600 if failure == "future" else 0)
    if failure == "tamper": body += b" "
    if failure == "wrong_key": monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_"+base64.b64encode(b"other-key-0123456789").decode())
    with TestClient(app) as client:
        assert client.post("/integrations/resend/webhook", content=body, headers=headers).status_code in (400,401,413)


def test_valid_signature_and_rotation_format(monkeypatch):
    setup(monkeypatch)
    calls = []
    monkeypatch.setattr(service, "audited", lambda *args: calls.append(args) or {"accepted": True})
    body = json.dumps(event()).encode(); headers = signed(body)
    headers["svix-signature"] = "v1,oldsignature "+headers["svix-signature"]
    with TestClient(app) as client:
        assert client.post("/integrations/resend/webhook", content=body, headers=headers).status_code == 200
    assert calls[0][1] == "resend-webhook"
    assert calls[0][2]["body_digest"] == hashlib.sha256(body).hexdigest()


def test_disabled_and_reader_enrollment_refuse(monkeypatch):
    monkeypatch.delenv("RESEND_TRACKING_ENABLED", raising=False)
    with TestClient(app) as client:
        assert client.post("/integrations/resend/webhook").status_code == 404
    setup(monkeypatch)
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "operator-test")
    monkeypatch.setenv("WONDRUE_READER_KEY", "reader-test")
    data = enrollment()
    monkeypatch.setattr(service, "audited", lambda *a: pytest.fail("must not dispatch"))
    with TestClient(app) as client:
        assert client.post("/integrations/resend/enroll", json=data, headers={"X-Wondrue-Key":"reader-test"}).status_code == 403
        assert client.post("/integrations/resend/enroll", json=data).status_code == 401


def enrollment():
    return dict(package_entry=str(uuid4()), email_id=str(uuid4()), campaign_id="campaign:test",
        recipient="owner@example.com", reply_to="reply-"+uuid4().hex+"@test.resend.app",
        receipt_ref="https://resend.com/emails/test")


def package(data):
    return {"simulated":False,"demo_mode":False,"package_state":"prepared","payload":{"campaign":{
        "status":"draft_not_sent","campaign_id":data["campaign_id"],"to":data["recipient"],"missing":[]}}}


def test_controlled_enrollment_replay_and_reassignment(monkeypatch):
    setup(monkeypatch); data = enrollment(); stored = []
    monkeypatch.setattr(service, "get_package_in_transaction", lambda *a: package(data))
    monkeypatch.setattr(service, "record_event_in_transaction", lambda *a, **kw: stored.append(kw))
    monkeypatch.setattr(service, "_enrollment", lambda *a: None)
    assert service.enroll(_FakeConn([]), **data)["enrolled"]
    material = stored[0]["detail"]["resend_enrollment"]
    monkeypatch.setattr(service, "_enrollment", lambda *a: (data["package_entry"], material))
    assert service.enroll(_FakeConn([]), **data)["replayed"]
    with pytest.raises(PriscillaError, match="immutable"):
        service.enroll(_FakeConn([]), **{**data,"package_entry":str(uuid4())})


@pytest.mark.parametrize("change", ["simulated", "demo", "suppressed", "recipient", "missing", "alias"])
def test_enrollment_gates(monkeypatch, change):
    setup(monkeypatch); data = enrollment(); row = package(data)
    if change == "simulated": row["simulated"] = True
    if change == "demo": row["demo_mode"] = True
    if change == "suppressed": row["payload"]["campaign"]["status"] = "suppressed"
    if change == "recipient": row["payload"]["campaign"]["to"] = "other@example.com"
    if change == "missing": row["payload"]["campaign"]["missing"] = ["financials"]
    if change == "alias": data["reply_to"] = "uriel@test.resend.app"
    monkeypatch.setattr(service, "get_package_in_transaction", lambda *a: row)
    monkeypatch.setattr(service, "_enrollment", lambda *a: None)
    monkeypatch.setattr(service, "record_event_in_transaction", lambda *a, **kw: pytest.fail("no insert"))
    with pytest.raises(PriscillaError): service.enroll(_FakeConn([]), **data)


def test_delivery_reply_same_correlation_without_interest(monkeypatch):
    data = enrollment(); material = {**data,"sender":"uriel@securecodeusa.com"}
    monkeypatch.setattr(service, "_enrollment", lambda *a: (data["package_entry"], material))
    calls=[]
    monkeypatch.setattr(service, "ingest_tracking_in_transaction", lambda *a, **kw: calls.append(kw) or {"event_id":"stored"})
    delivered = event(); delivered["data"]["email_id"] = data["email_id"]
    service.consume(None, delivered, "evt_delivery", "a"*64)
    reply = {"type":"email.received","created_at":delivered["created_at"],"data":{
        "email_id":str(uuid4()),"from":data["recipient"],"to":[data["reply_to"]]}}
    service.consume(None, reply, "evt_reply", "b"*64)
    assert [c["status"] for c in calls] == ["delivered","replied"]
    assert calls[0]["message_id"] == calls[1]["message_id"] == data["email_id"]
    assert calls[1]["authentication"] == "resend_svix_hmac_sha256"
    reply["data"]["from"] = "stranger@example.com"
    with pytest.raises(PriscillaError): service.consume(None,reply,"evt_other","c"*64)
    monkeypatch.setattr(service, "_enrollment", lambda *a: None)
    with pytest.raises(PriscillaError): service.consume(None,reply,"evt_unknown","d"*64)


def test_reserved_provider_cannot_be_operator_spoofed():
    from tests.test_campaign_tracking import inputs
    from app.priscilla import ingest_tracking_in_transaction
    with pytest.raises(PriscillaError, match="namespace"):
        ingest_tracking_in_transaction(_FakeConn([]), **{**inputs(str(uuid4())),"provider":"resend"})


@pytest.mark.skipif(os.getenv("GROWUP_PG_INTEGRATION") != "1", reason="PostgreSQL integration explicitly disabled")
def test_live_resend_enrollment_concurrency_delivery_reply_and_replay(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tests.test_dcad_admission import _require_gate_database
    from tests.test_priscilla_sim import _live_seed_packages
    from app.properties_db import get_connection
    from app.priscilla import timeline_in_transaction, metrics_in_transaction
    _require_gate_database()
    setup(monkeypatch)
    monkeypatch.setenv("DEMO_MODE", "no")
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "resend-pg-test-custody-0123456789")
    data = enrollment(); campaign = package(data)["payload"]["campaign"]
    with get_connection() as conn:
        data["package_entry"] = str(_live_seed_packages(conn, 1, campaign=campaign)[0])
        conn.commit()
    barrier = Barrier(2)
    def attempt():
        barrier.wait(timeout=10)
        return service.audited("enroll", "pg-test-operator", data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(r["replayed"] for r in results) == 1
    with get_connection() as conn:
        assert metrics_in_transaction(conn, package_entry=data["package_entry"])["actual"]["delivery"] == 0
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "resend-pg-test-operator")
    wrong = {**data,"package_entry":str(uuid4())}
    with pytest.raises(PriscillaError): service.audited("enroll", "pg-test-operator", wrong)
    delivered = event(); delivered["data"]["email_id"] = data["email_id"]
    reply = {"type":"email.received","created_at":delivered["created_at"],"data":{
        "email_id":str(uuid4()),"from":data["recipient"],"to":[data["reply_to"]]}}
    with TestClient(app) as client:
        for number, value in enumerate((reply, delivered)):
            body = json.dumps(value).encode(); headers = signed(body)
            headers["svix-id"] = "msg_"+uuid4().hex
            raw = headers["svix-id"].encode()+b"."+headers["svix-timestamp"].encode()+b"."+body
            headers["svix-signature"] = "v1,"+base64.b64encode(hmac.new(base64.b64decode(SECRET[6:]), raw, hashlib.sha256).digest()).decode()
            response = client.post("/integrations/resend/webhook", content=body, headers=headers)
            assert response.status_code == 200, response.text
            duplicate = client.post("/integrations/resend/webhook", content=body, headers=headers)
            assert duplicate.status_code == 200 and duplicate.json()["replayed"]
    with get_connection() as conn:
        view = timeline_in_transaction(conn, package_entry=data["package_entry"])
        assert view["actual"]["message_status_counts"]["delivered"] == 1
        assert view["actual"]["message_status_counts"]["replied"] == 1
        assert view["actual"]["message_status_counts"]["interested"] == 0
        assert view["actual"]["reply_rate_of_delivered"] == 1
        assert len(view["events"]) == 3


def test_official_svix_published_signature_vector():
    # https://docs.svix.com/receiving/verifying-payloads/how-manual
    headers = {"svix-id":"msg_loFOjxBNrRLzqYUf", "svix-timestamp":"1731705121",
        "svix-signature":"v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0="}
    assert service.verify(b'{"event_type":"ping","data":{"success":true}}', headers,
        "whsec_plJ3nmyCDGBKInavdOK15jsl", now=1731705121) == headers["svix-id"]


def test_wondrue_enrollment_permission_and_same_actor_recovery(monkeypatch):
    from app.wondrue import handle_command, WondrueError, WondrueAuthError
    setup(monkeypatch)
    calls=[]
    monkeypatch.setattr(service, "audited", lambda *a: calls.append(a) or {"enrolled":True})
    intent={"intent":"enroll_resend_message","args":enrollment()}
    kwargs=dict(max_mode="full",target="real",text="",principal="owner",intent_override=intent)
    with pytest.raises(WondrueError,match="read-only"):
        handle_command(mode="read-only",**kwargs)
    preview=handle_command(mode="ask",**kwargs)
    assert not calls
    result=handle_command(mode="ask",approval_token=preview["approval_token"],**kwargs)
    assert calls[0][1] == "owner"
    preview=handle_command(mode="ask",**kwargs)
    with pytest.raises(WondrueAuthError,match="different operator"):
        handle_command(mode="ask",approval_token=preview["approval_token"],**{**kwargs,"principal":"foreign"})
    with pytest.raises(WondrueError,match="real target"):
        handle_command(mode="full",**{**kwargs,"target":"demo"})


def test_native_inbox_events_require_exact_configured_scope(monkeypatch):
    data=enrollment(); inbox=str(uuid4()); email=str(uuid4())
    monkeypatch.setenv("RESEND_INBOX_ID",inbox)
    material={**data,"sender":"uriel@securecodeusa.com"}
    monkeypatch.setattr(service,"_enrollment",lambda *a:(data["package_entry"],material))
    calls=[]
    monkeypatch.setattr(service,"ingest_tracking_in_transaction",lambda *a,**kw:calls.append(kw) or {})
    payload={"type":"inbox.email.received","created_at":"2026-11-03T23:00:00Z","data":{
        "inbox_id":inbox,"email_id":email,"email":{
            "id":email,"direction":"inbound","from":"Owner <owner@example.com>","to":[data["reply_to"]]}}}
    service.consume(None,payload,"inbox_event","a"*64)
    assert calls[0]["status"] == "replied"
    assert calls[0]["authentication_context"]["inbox_id"] == inbox
    payload["data"]["inbox_id"]=str(uuid4())
    with pytest.raises(PriscillaError): service.consume(None,payload,"wrong_inbox","b"*64)
