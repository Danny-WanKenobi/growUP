"""Resend event intake and operator-controlled, immutable message enrollment.

No HTTP client or sender: verified webhook metadata is the only provider input.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from email import policy
from email.parser import HeaderParser
from uuid import UUID

from app.priscilla import PriscillaError, ingest_tracking_in_transaction, record_event_in_transaction
from app.uriel import get_package_in_transaction
from app.workflow import digest_payload

STATUSES = {"email.sent": "sent", "email.delivered": "delivered", "email.bounced": "bounced"}


def address(value):
    if not isinstance(value, str) or len(value) > 320 or any(c in value for c in "\r\n"):
        raise PriscillaError("invalid email address")
    try:
        header = HeaderParser(policy=policy.default).parsestr("From: "+value+"\n\n")["From"]
        if header is None or header.defects or len(header.addresses) != 1:
            raise ValueError()
        mailbox = header.addresses[0]
        local, domain = mailbox.username, mailbox.domain
        if (not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local
                or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain)
                or "." not in domain or ".." in domain
                or any(part.startswith("-") or part.endswith("-") or len(part) > 63 for part in domain.split("."))):
            raise ValueError()
        email = mailbox.addr_spec
        if len(email) > 254:
            raise ValueError()
        return email.casefold()
    except (ValueError, TypeError, AttributeError, IndexError):
        raise PriscillaError("one unambiguous email mailbox required") from None


def verify(body, headers, secret, now=None):
    """Svix documented HMAC-SHA256 over id.timestamp.RAW body."""
    try:
        event_id = headers.get("svix-id", "")
        timestamp = headers.get("svix-timestamp", "")
        signatures = headers.get("svix-signature", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", event_id):
            raise ValueError()
        if not re.fullmatch(r"[0-9]{10}", timestamp) or abs((time.time() if now is None else now)-int(timestamp)) > 300:
            raise ValueError()
        if not secret.startswith("whsec_") or len(signatures) > 2048:
            raise ValueError()
        key = base64.b64decode(secret[6:], validate=True)
        if len(key) < 16:
            raise ValueError()
        expected = base64.b64encode(hmac.new(key, event_id.encode()+b"."+timestamp.encode()+b"."+body, hashlib.sha256).digest()).decode()
        if not any(version == "v1" and hmac.compare_digest(signature, expected)
                   for version, signature in (part.split(",", 1) for part in signatures.split() if "," in part)):
            raise ValueError()
        return event_id
    except (ValueError, TypeError, AttributeError):
        raise PriscillaError("invalid Resend signature") from None


def _lock(conn, keys):
    with conn.cursor() as cur:
        for key in sorted(keys):
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%(key)s))", {"key": "resend:"+key})


def _enrollment(conn, field, value):
    # Field names are internal constants, never input SQL identifiers.
    if field not in ("email_id", "reply_to"):
        raise PriscillaError("invalid enrollment lookup")
    with conn.cursor() as cur:
        cur.execute("SELECT package_entry, payload FROM conversion_events WHERE "
                    "payload->'detail'->'resend_enrollment'->>"+"'"+field+"' = %(value)s", {"value": value})
        rows = cur.fetchall() or []
    if len(rows) > 1:
        raise PriscillaError("ambiguous message enrollment")
    if not rows:
        return None
    row = dict(rows[0])
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return str(row["package_entry"]), payload["detail"]["resend_enrollment"]


def enroll(conn, *, package_entry, campaign_id, email_id, reply_to, recipient, receipt_ref):
    """Operator attests a provider send receipt; enrollment itself proves no send.

    Unique reply alias must have been set as Reply-To in the outbound request.
    No automatic enrollment from untrusted tags, subjects or callback package IDs.
    """
    package_entry, email_id = str(UUID(package_entry)), str(UUID(email_id))
    recipient, reply_to = address(recipient), address(reply_to)
    domain = os.getenv("RESEND_RECEIVING_DOMAIN", "").strip().casefold()
    if not re.fullmatch(r"reply-[0-9a-f]{32}@"+re.escape(domain), reply_to) or not domain:
        raise PriscillaError("unique reply alias on configured receiving domain required")
    if not isinstance(receipt_ref, str) or not receipt_ref.startswith("https://resend.com/") or len(receipt_ref) > 2000:
        raise PriscillaError("Resend send receipt reference required")
    _lock(conn, ["email:"+email_id, "reply:"+reply_to])
    material = dict(email_id=email_id, reply_to=reply_to, recipient=recipient,
                    campaign_id=campaign_id, receipt_ref=receipt_ref,
                    sender=address(os.getenv("RESEND_SENDER", "")))
    existing = _enrollment(conn, "email_id", email_id)
    if existing:
        if existing != (package_entry, material):
            raise PriscillaError("message enrollment is immutable")
        return {"enrolled": True, "replayed": True, "email_id": email_id}
    if _enrollment(conn, "reply_to", reply_to):
        raise PriscillaError("reply alias already enrolled")
    package = get_package_in_transaction(conn, package_entry)
    campaign = ((package or {}).get("payload") or {}).get("campaign") or {}
    if (not package or package.get("simulated") or package.get("demo_mode")
            or package.get("package_state") != "prepared" or campaign.get("missing")
            or campaign.get("status") != "draft_not_sent" or campaign.get("campaign_id") != campaign_id
            or address(campaign.get("to")) != recipient):
        raise PriscillaError("eligible actual campaign and exact recipient binding required")
    record_event_in_transaction(conn, package_entry=package_entry, kind="delivery", simulated=False,
        label="Operator-enrolled Resend message; no delivery assertion", evidence_refs=[receipt_ref],
        detail={"lifecycle_status": "prepared", "resend_enrollment": material,
                "authentication": "operator_attested_send_receipt; not independently retrieved"})
    return {"enrolled": True, "replayed": False, "email_id": email_id}


def consume(conn, event, event_id, body_digest, *, authentication="resend_svix_hmac_sha256", route_conversation=False, poll_authorization_id=None):
    if authentication not in ("resend_svix_hmac_sha256", "resend_authenticated_api"):
        raise PriscillaError("unsupported Resend authentication")
    kind, data = event.get("type"), event.get("data")
    if not isinstance(data, dict):
        raise PriscillaError("invalid Resend event")
    original_kind = kind
    inbox_id = None
    if kind in ("inbox.email.received", "inbox.email.sent"):
        expected_inbox = os.getenv("RESEND_INBOX_ID", "")
        inbox_id = str(UUID(data.get("inbox_id", "")))
        email = data.get("email")
        if (not expected_inbox or inbox_id != str(UUID(expected_inbox)) or not isinstance(email, dict)
                or email.get("id") != data.get("email_id")
                or email.get("direction") != ("inbound" if kind.endswith("received") else "outbound")):
            raise PriscillaError("Inbox event scope or metadata refused")
        data = {**email, "email_id": data["email_id"]}
        kind = "email.received" if kind.endswith("received") else "email.sent"
    if kind not in STATUSES and kind != "email.received":
        return {"accepted": True, "ignored": True, "reason": "unsupported_event_type"}
    email_id = str(UUID(data.get("email_id", "")))
    if kind == "email.received":
        if os.getenv("RESEND_MAILBOX_ROUTING_ENABLED", "0") == "1":
            from app.mailbox_routing import receive as receive_mailbox
            direct = receive_mailbox(conn, metadata=data, event_id=event_id,
                body_digest=body_digest, authentication=authentication)
            if direct is not None:
                if direct.get('mailbox')=='csr':
                    from app.csr_email import enqueue
                    enqueue(conn, data)
                return direct
        recipients = data.get("to")
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 50:
            raise PriscillaError("bounded received recipient list required")
        matches = [_enrollment(conn, "reply_to", address(value)) for value in recipients]
        matches = [item for item in matches if item is not None]
        if len(matches) != 1:
            raise PriscillaError("unmatched or ambiguous reply; no guessed correlation")
        package, enrollment = matches[0]
        if address(data.get("from")) != enrollment["recipient"]:
            raise PriscillaError("reply sender differs from enrolled recipient")
        status = "replied"
    else:
        match = _enrollment(conn, "email_id", email_id)
        if not match:
            raise PriscillaError("message not enrolled; retain provider event for retry")
        package, enrollment = match
        recipients = data.get("to")
        if (not isinstance(recipients, list) or len(recipients) != 1
                or address(recipients[0]) != enrollment["recipient"]
                or address(data.get("from")) != enrollment["sender"]):
            raise PriscillaError("event envelope differs from enrolled sender/recipient")
        status = STATUSES[kind]
    # Repeated delivery callbacks share an outbound message ID. Different actual
    # inbound replies retain their provider email IDs in audit context, not counts.
    result = ingest_tracking_in_transaction(conn, package_entry=package, provider="resend",
        provider_event_id=event_id, message_id=enrollment["email_id"], status=status,
        occurred_at=event.get("created_at"), evidence_refs=["https://resend.com/emails/"+email_id],
        authentication=authentication, label="Resend observed "+status,
        authentication_context={"body_sha256": body_digest, "provider_email_id": email_id,
            "event_type": original_kind, "inbox_id": inbox_id, "enrollment_digest": digest_payload(enrollment),
            "poll_authorization_id":poll_authorization_id,
            "body_digest_scope":"raw signed webhook" if authentication=="resend_svix_hmac_sha256" else "API event projection; raw response digest retained in polling request ledger",
            "scope": "Provider event authenticated; reply sender field is not proof of owner identity or interest."})
    if kind == "email.received" and (route_conversation or os.getenv("COMMUNICATIONS_ROUTER_ENABLED", "0") == "1"):
        from app.communications import receive
        result['conversation'] = receive(conn,package_entry=package,provider_message_id=email_id,
            enrollment=enrollment,metadata=data,authentication=authentication)
    return result



def audited(action, principal, inputs):
    from app.observability.events import AgentEvent, AgentId, RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event = AgentEvent(agent=AgentId.PRISCILLA, run_id=str(inputs.get("package_entry") or inputs.get("event_id")),
        event_type="tracking.resend_"+action, summary="Resend tracking; no outbound action",
        details={"principal": principal}, retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event, mutate=lambda conn: (enroll(conn, **inputs) if action == "enroll" else consume(conn, **inputs)))
