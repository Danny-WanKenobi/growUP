"""Authenticated owner cockpit. No public operational data or sandbox execution."""
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from app import wondrue_workspace as ws
from app.auth import wondrue_grant
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection
from app.workflow_db import WorkflowError

router=APIRouter()
ASSETS=Path(__file__).resolve().parents[1]/'wondrue_assets'
HEADERS={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",'X-Content-Type-Options':'nosniff'}


def authorize(request, mutate=False):
    from app.routes.public import OWNER_COOKIE, owner_session_valid, same_origin
    if owner_session_valid(request.cookies.get(OWNER_COOKIE,'')):
        grant='full'
    else:grant=wondrue_grant(request)
    if mutate:
        same_origin(request)
        if grant!='full':raise HTTPException(403,'Owner access is required to save or decide.')
    return grant


def call(action, fn, write=False):
    try:
        if write:return ws.transaction(action,fn)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                cur.execute("SET LOCAL statement_timeout='5000ms'")
            return fn(conn)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):
        raise HTTPException(503,'Workspace storage is unavailable. No action was executed.') from None
    except (ValueError,WorkflowError):
        raise


def safe_call(action, fn, write=False):
    try:return call(action,fn,write)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@router.get('/wondrue',response_class=HTMLResponse)
def page():
    # Static shell only. Data and every decision require authenticated endpoints.
    return HTMLResponse((ASSETS/'index.html').read_text(encoding='utf-8'),headers=HEADERS)


@router.get('/wondrue/assets/{name}')
def asset(name:str):
    if name not in ('shell.css','shell.js','guided-review.js','workbench.js','evidence.js','owner-tools.js'):raise HTTPException(404)
    return FileResponse(ASSETS/name,headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})


@router.get('/wondrue/api/status')
def status(request:Request):
    grant=authorize(request)
    from app.server_target import get_server_target
    import os
    from app import wondrue_sandbox as sandbox
    active=sandbox.enabled()
    from app.handoff_demo import configuration
    try:configuration();guided_available=True
    except ValueError:guided_available=False
    return {'grant':grant,'server':get_server_target().mode,'demo':os.getenv('DEMO_MODE','yes').lower()!='no',
            'sandbox_enabled':active,'sandbox_reason':'Isolated hypothetical database; no operational promotion, providers or messages.' if active else 'Execution is disabled until separate database/role isolation is proven.',
            'review_actions':ws.REVIEW_ACTIONS,'guided_demo_review_available':guided_available,'provider_calls':0}


@router.get('/wondrue/api/chats')
def chats(request:Request,q:str=Query('',max_length=100),offset:int=Query(0,ge=0,le=10000)):
    authorize(request)
    return safe_call('list',lambda c:ws.conversations(c,q,offset))


class NewChat(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID


@router.post('/wondrue/api/chats')
def new_chat(body:NewChat,request:Request):
    authorize(request,True)
    return safe_call('create_chat',lambda c:ws.chat(c,body.id,True)[1].payload,True)


@router.get('/wondrue/api/chats/{chat_id}')
def conversation(chat_id:UUID,request:Request):
    authorize(request)
    return safe_call('history',lambda c:ws.turns(c,chat_id))


class Message(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:UUID
    text:str=Field(min_length=1,max_length=4000)
    intent:dict[str,Any]|None=None
    expected_context_revision:int|None=Field(None,ge=0)


class DossierSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:UUID
    expected_revision:int=Field(ge=0)
    run_id:UUID|None=None
    key:str|None=Field(None,min_length=1,max_length=500)
    version:int|None=Field(None,ge=1)
    digest:str|None=Field(None,min_length=64,max_length=64)


@router.post('/wondrue/api/chats/{chat_id}/selection')
def select_dossier(chat_id:UUID,body:DossierSelection,request:Request):
    authorize(request,True)
    fields=(body.run_id,body.key,body.version,body.digest)
    if any(v is not None for v in fields) and not all(v is not None for v in fields):
        raise HTTPException(422,'A complete dossier reference or an explicit empty selection is required.')
    reference=None if body.run_id is None else {'run_id':str(body.run_id),'key':body.key,'version':body.version,'digest':body.digest}
    from app.wondrue_dossier_chat import select
    return safe_call('select_dossier',lambda c:select(c,chat_id,body.request_id,body.expected_revision,reference),True)


def answer(conn,text,intent=None):
    if intent:
        from app.model_adapter import validate_intent
        from app.wondrue import READ_ONLY_INTENTS,handle_command
        intent=validate_intent(intent)
        if intent['intent'] not in READ_ONLY_INTENTS:
            raise WorkflowError('Operational changes must be proposed in Hold for Human Review.')
        result=handle_command(mode='read-only',max_mode='read-only',text='',target='real',
                              intent_override=intent,use_live=False,conn=conn,principal='wondrue-reader')
        return {'text':'Here is the retained result. Unknowns remain unknown; no operational changes were made.','data':result}
    words=text.lower()
    if any(w in words for w in ('sandbox','hypothetical')):
        from app.wondrue_sandbox import enabled
        return {'text':'The isolated Sandbox is available from Library dossiers. It copies only a source reference and uses explicitly assumed engineering inputs; it cannot promote results to the pipeline.' if enabled() else 'Sandbox execution is disabled. No production data will be copied or changed until separate database and role isolation have been proven.'}
    if any(w in words for w in ('review','approval','attention','exception')):
        return {'text':'Hold for Human Review contains explicit operational proposals and retained agent tasks. An administrative decision does not authenticate property facts or authorize external actions.',
                'view':'reviews','reviews':ws.review_list(conn),'tasks':ws.catalogue(conn,tasks=True)}
    if any(w in words for w in ('agents','capabilities','team')):
        from app.agents.registry import describe_agents
        return {'text':'These are the registered agent responsibilities and boundaries. Registration alone is not evidence that an agent has completed a real dossier.','data':describe_agents()}
    if any(w in words for w in ('dossier','library','evidence','financial','property','properties','custody')):
        return {'text':'Here are retained records in this environment. Open one to inspect its evidence, reports and provenance. The synthetic catalogue remains in its separate demonstration viewer.',
                'view':'library','records':ws.catalogue(conn)}
    return {'text':'I can inspect retained dossiers, explain agent capabilities and present items awaiting your review. Try “Show the Library”, “What needs my review?” or “Explain the agent team”. This cockpit uses deterministic retained-record tools; no model or provider has been called.'}


@router.post('/wondrue/api/chats/{chat_id}/messages')
def message(chat_id:UUID,body:Message,request:Request):
    authorize(request,True)
    def mutate(conn):
        fingerprint=ws.digest_payload({'text':body.text.strip(),'intent':body.intent,'context_revision':body.expected_context_revision})
        run,meta=ws.chat(conn,chat_id)
        replay=ws.read(conn,run.run_id,'request:'+str(body.request_id))
        if replay:
            if replay.payload['text']!=body.text.strip() or replay.payload.get('input_digest')!=fingerprint:raise WorkflowError('message identity conflict')
            return replay.payload
        context=meta.payload.get('context')
        if meta.payload.get('context_revision',0)>0 and body.expected_context_revision!=meta.payload.get('context_revision'):
            raise WorkflowError('conversation dossier selection changed; reload before asking')
        if context and body.intent:
            args=body.intent.get('args') or {}
            if (args.get('run_id') and args['run_id']!=context['run_id']) or (args.get('dossier_key') and args['dossier_key']!=context['key']):
                raise WorkflowError('Select that dossier explicitly before discussing it in this conversation.')
        if context and body.intent is None:
            from app.wondrue_dossier_chat import explain
            result=explain(conn,context,body.text)
        else:result=answer(conn,body.text,body.intent)
        return ws.append_turn(conn,chat_id,body.request_id,body.text,result,fingerprint)
    return safe_call('message',mutate,True)


@router.get('/wondrue/api/library')
def library(request:Request,q:str=Query('',max_length=100),offset:int=Query(0,ge=0,le=10000)):
    authorize(request)
    return safe_call('library',lambda c:ws.catalogue(c,q,offset))


@router.get('/wondrue/api/record')
def record(request:Request,run_id:UUID,kind:str,key:str=Query(...,max_length=500),version:int|None=Query(None,ge=1)):
    authorize(request)
    return safe_call('record',lambda c:ws.record(c,run_id,kind,key,version))


@router.get('/wondrue/api/owner-context')
def owner_context(request:Request,run_id:UUID,key:str=Query(...,max_length=500)):
    authorize(request)
    from app.manny_review import inspect_in_transaction
    return safe_call('owner_context',lambda c:inspect_in_transaction(c,run_id=run_id,dossier_key=key))


@router.get('/wondrue/api/reviews')
def reviews(request:Request,offset:int=Query(0,ge=0,le=10000)):
    authorize(request)
    return safe_call('reviews',lambda c:{'proposals':ws.review_list(c,offset),'tasks':ws.catalogue(c,offset=offset,tasks=True)})


class Proposal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID
    intent:dict[str,Any]
    reason:str=Field(min_length=1,max_length=1000)


@router.post('/wondrue/api/reviews')
def propose(body:Proposal,request:Request):
    authorize(request,True)
    return safe_call('propose',lambda c:ws.propose(c,body.id,body.intent,body.reason),True)


class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_digest:str=Field(min_length=64,max_length=64)
    choice:str
    note:str=Field(min_length=1,max_length=1000)


@router.post('/wondrue/api/reviews/{review_id}/decision')
def decide(review_id:UUID,body:Decision,request:Request):
    authorize(request,True)
    state=safe_call('decision',lambda c:ws.decide(c,review_id,body.expected_digest,body.choice,body.note),True)
    if state['status']!='dispatch_claimed':return state
    # A crash after this durable claim is visible and never automatically retried.
    from app.wondrue import handle_command
    try:
        result=handle_command(mode='full',max_mode='full',text='',target='real',
                              intent_override=state['intent'],use_live=False,principal='wondrue-operator')
    except Exception:
        return safe_call('dispatch_uncertain',lambda c:ws.finish(c,str(review_id)),True)
    return safe_call('dispatch_complete',lambda c:ws.finish(c,str(review_id),result),True)


class SandboxRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID
    run_id:UUID
    key:str=Field(min_length=1,max_length=500)
    version:int=Field(ge=1)
    inputs:dict[str,Any]
    name:str|None=Field(None,min_length=1,max_length=80)
    parent_id:UUID|None=None
    expected_parent_digest:str|None=Field(None,pattern='^[0-9a-f]{64}$')


@router.post('/wondrue/api/sandbox')
def sandbox(body:SandboxRequest,request:Request):
    authorize(request,True)
    from app import wondrue_sandbox as isolated
    if not isolated.enabled():raise HTTPException(409,'Sandbox execution is disabled until database/role isolation is proven. No copy or execution occurred.')
    source=safe_call('sandbox_source',lambda c:ws.record(c,body.run_id,'dossier_version',body.key,body.version))
    try:return isolated.save(body.id,source,body.inputs,body.name,body.parent_id,body.expected_parent_digest)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'Sandbox storage unavailable; no operational fallback is allowed.') from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@router.get('/wondrue/api/sandbox')
def sandbox_history(request:Request,offset:int=Query(0,ge=0,le=10000)):
    authorize(request)
    from app import wondrue_sandbox as isolated
    try:return isolated.list_scenarios(offset)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'Sandbox storage unavailable; no operational fallback is allowed.') from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from None
