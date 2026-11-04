"""Authenticated direct-mailbox intake and internal drafts; never sends mail.

Uses the existing workflow record/transaction services. Provider authentication
establishes transport provenance, not a sender's identity or business authority.
"""
import os
from copy import deepcopy
from uuid import UUID

from app.communications import instant, lock, read, write
from app.workflow_db import WorkflowError, create_run_in_transaction, get_run_by_key_in_transaction


def domain():
    from app.resend_tracking import address
    value = os.getenv("RESEND_RECEIVING_DOMAIN", "").strip().casefold()
    if not value or address("simeon@" + value) != "simeon@" + value:
        raise WorkflowError("valid receiving domain required")
    return value


def receive(conn, *, metadata, event_id, body_digest, authentication):
    """Return None for legacy campaign aliases; retain named/unknown direct mail.

    Message UUID is the durable retry identity, not the webhook delivery ID.
    No inferred dossier correlation, identity assertion, or outbound action.
    """
    from app.resend_tracking import address
    if authentication not in ("resend_svix_hmac_sha256", "resend_authenticated_api"):
        raise WorkflowError("verified Resend transport required")
    recipients = metadata.get("to")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 50:
        raise WorkflowError("bounded recipient list required")
    recipients = sorted(set(address(v) for v in recipients))
    local = [v for v in recipients if v.rsplit("@", 1)[1] == domain()]
    # Existing unique campaign aliases continue through exact enrollment checks.
    if len(local) == 1 and local[0].startswith("reply-"):
        return None
    mid = str(UUID(metadata.get("email_id", "")))
    sender = address(metadata.get("from"))
    subject = metadata.get("subject") or ""
    if not isinstance(subject, str) or len(subject) > 1000:
        raise WorkflowError("bounded subject required")
    identity = {"email_id": mid, "from": sender, "to": recipients, "subject": subject}
    lock(conn, "direct-mail:" + mid)
    key = "direct-mail:" + mid
    run = get_run_by_key_in_transaction(conn, key)
    if run is None:
        run = create_run_in_transaction(conn, idempotency_key=key)
    prior = read(conn, run.run_id, "inbound")
    if prior:
        if prior.payload["identity"] != identity:
            raise WorkflowError("mailbox message identity conflict")
        state = read(conn, run.run_id, "state")
        return {"accepted": True, "replayed": True, "mailbox": state.payload["owner"],
                "conversation_id": mid, "external_actions": 0}
    names = {"uriel", "simeon", "solomon"}
    name = local[0].split("@", 1)[0] if len(local) == 1 else None
    owner = name if name in names else "human_review"
    state = {"owner": owner, "revision": 1, "status": "metadata_requires_review",
             "dossier_id": None, "delivery_enabled": False, "last_inbound_id": mid}
    write(conn, run.run_id, "inbound", {"identity": identity, "received_at": instant().isoformat(),
          "event_id": event_id, "body_digest": body_digest, "authentication": authentication,
          "content_status": "metadata_only", "scope": "Untrusted sender claims; no ownership or interest assertion."})
    if owner == "uriel":
        # Never dispatch Uriel as a responder. Notice is a system-owned draft.
        # No auto-send from webhook metadata: authenticated full headers must be
        # inspected for auto-submitted/list/bounce signals before future delivery.
        sender_local, sender_domain = sender.rsplit("@", 1)
        suppress = sender_domain == domain() or sender_local in (
            "mailer-daemon", "postmaster", "noreply", "no-reply", "do-not-reply")
        state["status"] = "notice_suppressed" if suppress else "unattended_notice_draft"
        write(conn, run.run_id, "notice", {"owner": "system", "status": "draft_only",
              "suppressed": suppress,
              "to": sender, "from": "uriel@" + domain(), "reply_to": "simeon@" + domain(),
              "text": "This mailbox is unattended and Uriel will not reply. Please contact Simeon at simeon@" + domain() + ".",
              "auto_submitted": "auto-replied", "send_enabled": False,
              "required_before_send": ["authenticated full headers", "suppress automatic/list/bounce senders",
                                       "recipient suppression and rate limits", "durable send deduplication"]})
    write(conn, run.run_id, "state", state)
    return {"accepted": True, "replayed": False, "mailbox": owner,
            "conversation_id": mid, "external_actions": 0}


def control(conn, *, conversation_id, action, principal, text=None, expected_revision=None):
    """Inspect, draft a response, or prepare a linked cold-lead rework request.

    A request for Uriel does not transfer canonical custody or restart marketing.
    It remains reviewable until an evidence-linked dossier is explicitly bound.
    """
    if not principal:
        raise WorkflowError("authenticated operator required")
    mid = str(UUID(conversation_id))
    lock(conn, "direct-mail:" + mid)
    run = get_run_by_key_in_transaction(conn, "direct-mail:" + mid)
    if run is None:
        raise WorkflowError("retained inbound conversation required; cannot initiate contact")
    row = read(conn, run.run_id, "state")
    state = deepcopy(row.payload)
    inbound = read(conn, run.run_id, "inbound")
    if action == "inspect":
        return {"state": state, "inbound": inbound.payload,
                "notice": getattr(read(conn, run.run_id, "notice"), "payload", None)}
    if os.getenv("DOSSIER_COORDINATOR_EMERGENCY_STOP", "0") == "1":
        raise WorkflowError("communications emergency-stopped")
    if isinstance(expected_revision, bool) or expected_revision != state["revision"]:
        raise WorkflowError("conversation revision changed")
    if state["owner"] not in ("simeon", "solomon"):
        raise WorkflowError("only Simeon or Solomon can prepare conversation responses")
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise WorkflowError("bounded response or rework notes required")
    if action == "draft":
        result = {"owner": state["owner"], "from": state["owner"] + "@" + domain(),
                  "to": inbound.payload["identity"]["from"], "in_reply_to": mid,
                  "text": text, "status": "draft_only", "send_enabled": False,
                  "principal": principal}
    elif action == "request_uriel_followup" and state["owner"] == "simeon":
        result = {"owner": "uriel", "conversation_id": mid, "inbound_record_digest": inbound.digest,
                  "notes": text, "status": "requires_dossier_link_and_campaign_review",
                  "custody_transferred": False, "marketing_authorized": False, "principal": principal}
    else:
        raise WorkflowError("unsupported mailbox action")
    state["revision"] += 1
    write(conn, run.run_id, "action:" + str(state["revision"]), result)
    write(conn, run.run_id, "state", state, row)
    return {**result, "revision": state["revision"], "external_actions": 0}


def audited(principal, inputs):
    from app.observability.events import AgentEvent, AgentId, RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event = AgentEvent(agent=AgentId.WONDRUE, run_id=inputs["conversation_id"],
        event_type="communications.mailbox_control", summary="Direct-mailbox internal operation; no delivery",
        details={"action": inputs["action"], "principal": principal}, retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event, mutate=lambda conn: control(conn, principal=principal, **inputs))
