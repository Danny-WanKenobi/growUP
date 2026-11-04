"""Native Resend callbacks and operator-only enrollment; disabled by default."""
import hashlib
import json
import os
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from app.auth import wondrue_grant
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.priscilla import PriscillaError
from app import resend_tracking as service
from app.routes.tracking_ingress import _unique_pairs

router = APIRouter(tags=["resend-tracking"])
MAX_BODY = 32768


class Enrollment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    package_entry: str = Field(min_length=36, max_length=36)
    campaign_id: str = Field(min_length=1, max_length=200)
    email_id: str = Field(min_length=36, max_length=36)
    reply_to: str = Field(min_length=1, max_length=254)
    recipient: str = Field(min_length=1, max_length=254)
    receipt_ref: str = Field(min_length=1, max_length=2000)


def enabled():
    if os.getenv("RESEND_TRACKING_ENABLED", "0") != "1":
        raise HTTPException(404, "resend_tracking_disabled")


async def dispatch(action, principal, inputs):
    try:
        return await run_in_threadpool(service.audited, action, principal, inputs)
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise HTTPException(503, "tracking_storage_unavailable") from None
    except (PriscillaError, ValueError, TypeError, AttributeError):
        raise HTTPException(409, "resend_correlation_refused") from None


@router.post("/integrations/resend/enroll")
async def enroll(body: Enrollment, request: Request):
    enabled()
    if wondrue_grant(request) != "full":
        raise HTTPException(403, "operator_required")
    return await dispatch("enroll", "wondrue-operator", body.model_dump())


@router.post("/integrations/resend/webhook")
async def webhook(request: Request):
    enabled()
    from app.server_target import get_server_target
    try: target=get_server_target()
    except ValueError: raise HTTPException(503,'invalid_server_target') from None
    if not target.callback_available:
        raise HTTPException(503,'resend_callback_unavailable_on_dev_server')
    secret = os.getenv("RESEND_WEBHOOK_SECRET", "")
    if not secret.startswith("whsec_"):
        raise HTTPException(503, "resend_signing_not_configured")
    body = bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk) > MAX_BODY:
            raise HTTPException(413, "resend_body_too_large")
        body.extend(chunk)
    try:
        event_id = service.verify(bytes(body), request.headers, secret)
    except PriscillaError:
        raise HTTPException(401, "invalid_resend_signature") from None
    try:
        event = json.loads(body, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(event, dict):
            raise ValueError()
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "invalid_resend_event") from None
    return await dispatch("receive", "resend-webhook", {
        "event": event, "event_id": event_id, "body_digest": hashlib.sha256(body).hexdigest()})
