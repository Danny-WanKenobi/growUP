"""Disabled-by-default signed gateway envelope; not a vendor-specific webhook."""
import hashlib
import hmac
import json
import os
import re
import time
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from app.priscilla import ingest_tracking_audited, PriscillaError
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError

router=APIRouter(tags=['tracking-ingress'])
MAX_BODY=32768


class Envelope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    package_entry:str=Field(min_length=1,max_length=36)
    provider_event_id:str=Field(min_length=1,max_length=200)
    message_id:str=Field(min_length=1,max_length=200)
    status:str=Field(min_length=1,max_length=20)
    occurred_at:str=Field(min_length=1,max_length=64)
    evidence_refs:list[str]=Field(min_length=1,max_length=100)
    simulated:StrictBool=False


def configuration(source):
    if os.getenv('TRACKING_INGRESS_ENABLED','0')!='1':
        raise HTTPException(404,'tracking_ingress_disabled')
    try:
        sources=json.loads(os.getenv('TRACKING_INGRESS_SOURCES','{}'))
        item=sources[source]
        if not re.fullmatch(r'[a-z0-9-]{1,40}',source): raise ValueError()
        name=item['secret_env']
        if not re.fullmatch(r'TRACKING_INGRESS_KEY_[A-Z0-9_]{1,50}',name): raise ValueError()
        secret=os.getenv(name,'')
        if len(secret)<32: raise ValueError()
        for credential in ('CUSTODY_HMAC_KEY','WONDRUE_OPERATOR_KEY','WONDRUE_READER_KEY','META_API_KEY','BRAVE_SEARCH_API_KEY','HANDOFF_DEMO_HMAC_KEY'):
            existing=os.getenv(credential,'')
            if existing and hmac.compare_digest(secret.encode(),existing.encode()): raise ValueError()
        packages={str(UUID(p)) for p in item['allowed_packages']}
        if not packages or len(packages)>1000: raise ValueError()
        if not isinstance(item.get('allow_simulated',False),bool): raise ValueError()
        return secret,packages,item.get('allow_simulated',False)
    except (KeyError,ValueError,TypeError,AttributeError):
        raise HTTPException(403,'tracking_source_not_configured') from None


def verify_signature(source, secret, timestamp, signature, body):
    if not re.fullmatch(r'[0-9]{10}',timestamp or '') or abs(time.time()-int(timestamp))>300:
        raise HTTPException(401,'invalid_tracking_signature')
    expected=hmac.new(secret.encode(),source.encode()+b'.'+timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    if not re.fullmatch(r'[0-9a-f]{64}',signature or '') or not hmac.compare_digest(expected,signature):
        raise HTTPException(401,'invalid_tracking_signature')


def _unique_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate field')
        result[key]=value
    return result


@router.post('/integrations/tracking/{source}')
async def receive(source:str,request:Request):
    secret,packages,allow_simulated=configuration(source)
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>MAX_BODY: raise HTTPException(413,'tracking_body_too_large')
        body.extend(chunk)
    verify_signature(source,secret,request.headers.get('x-tracking-timestamp'),request.headers.get('x-tracking-signature'),bytes(body))
    try:
        value=json.loads(body,object_pairs_hook=_unique_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        event=Envelope.model_validate(value)
        package=str(UUID(event.package_entry))
    except (ValueError,ValidationError,TypeError,UnicodeDecodeError):
        raise HTTPException(400,'invalid_tracking_envelope') from None
    if package not in packages or (event.simulated and not allow_simulated):
        raise HTTPException(403,'tracking_source_scope_refused')
    inputs=event.model_dump(); inputs['package_entry']=package
    try:
        # Reuse the one append-only event store and transaction/audit boundary.
        # Fixed namespace prevents caller-supplied provider/principal forgery.
        from starlette.concurrency import run_in_threadpool
        result=await run_in_threadpool(ingest_tracking_audited,principal='tracking-gateway:'+source,
            provider='gateway:'+source,authentication='configured_gateway_hmac_sha256',
            authentication_context={'source':source,'verified_timestamp':request.headers.get('x-tracking-timestamp'),
                'body_sha256':hashlib.sha256(body).hexdigest(),'scope':'transport authentication at intake; underlying assertions not independently verified'},**inputs)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):
        raise HTTPException(503,'tracking_storage_unavailable') from None
    except PriscillaError:
        raise HTTPException(409,'tracking_event_refused') from None
    return {'accepted':True,'event_id':result['event_id'],'replayed':result.get('replayed',False)}
