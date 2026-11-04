"""Public landing/support routes; operator authentication remains separate."""
import base64
from collections import defaultdict,deque
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import threading
import time

from fastapi import APIRouter,HTTPException,Request,Response
from fastapi.responses import HTMLResponse,FileResponse,JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from starlette.concurrency import run_in_threadpool
from app import customer_service as csr
from app.db import DatabaseNotConfiguredError,DatabaseUnavailableError
from app.workflow_db import WorkflowError
from app.priscilla import PriscillaError

router=APIRouter()
ASSETS=Path(__file__).resolve().parents[1]/'public_assets'
COOKIE='growup_csr'
OWNER_COOKIE='growup_owner'
_rates=defaultdict(deque)
_guard=threading.Lock()


def same_origin(request):
    from app.server_target import get_server_target
    allowed={get_server_target().origin}
    if get_server_target().mode=='LIVE_SERVER':allowed.add('https://growup.securecodeusa.com')
    if os.getenv('CSR_LOCAL_PREVIEW')=='1':allowed.update({'http://127.0.0.1:8765','http://localhost:8765'})
    if request.headers.get('origin') not in allowed:raise HTTPException(403,'same_origin_required')


def limit(request,category,maximum):
    # Nginx additionally rate-limits at the edge; no visitor-supplied XFF trust here.
    ip=request.client.host if request.client else 'unknown'
    now=time.monotonic()
    with _guard:
        key=(ip,category)
        if len(_rates)>10000:
            for k in list(_rates):
                if not _rates[k] or now-_rates[k][-1]>3600:del _rates[k]
            if len(_rates)>10000:raise HTTPException(429,'support_busy')
        queue=_rates[key]
        while queue and now-queue[0]>3600:queue.popleft()
        if len(queue)>=maximum:raise HTTPException(429,'please_try_later')
        queue.append(now)


def identity(request):
    token=request.cookies.get(COOKIE,'')
    if len(token)!=43 or not all(c.isalnum() or c in '_-' for c in token):raise HTTPException(401,'start_support_session')
    return hashlib.sha256(token.encode()).hexdigest()


async def call(action,mutate):
    try:return await run_in_threadpool(csr.transaction,action,mutate)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'Support storage is temporarily unavailable. Please try again later.') from None
    except (WorkflowError,PriscillaError,ValueError) as exc:raise HTTPException(409,str(exc)) from None


@router.get('/',response_class=HTMLResponse)
def landing():
    from app.public_faq import FAQS
    from html import escape
    cards=''.join(f'<details><summary><span class="faq-number">{i:02}</span>{escape(q)}<span class="plus">+</span></summary><p>{escape(a)}</p></details>' for i,(q,a) in enumerate(FAQS,1))
    page=(ASSETS/'index.html').read_text(encoding='utf-8').replace('<!--FAQS-->',cards)
    return HTMLResponse(page,headers={'Cache-Control':'no-store','Content-Security-Policy':
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        'X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin'})


@router.get('/public-assets/{name}')
def asset(name:str):
    if name not in ('site.css','site.js'):raise HTTPException(404)
    return FileResponse(ASSETS/name,headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-cache'})


@router.post('/support/start')
async def start(request:Request):
    same_origin(request);limit(request,'start',12)
    if os.getenv('CSR_PUBLIC_ENABLED','0')!='1':raise HTTPException(503,'Customer care is not active yet. Please try again later.')
    if request.cookies.get(COOKIE):
        try:
            who=identity(request)
            await call('session_resume',lambda conn:csr.session(conn,who) and {'ok':True})
            return JSONResponse({'ready':True,'assistant':'Grow Up Care','resumed':True})
        except HTTPException as exc:
            if exc.status_code not in (401,409):raise
    token=secrets.token_urlsafe(32); hashed=hashlib.sha256(token.encode()).hexdigest()
    await call('session',lambda conn:csr.session(conn,hashed,True) and {'ok':True})
    result=JSONResponse({'ready':True,'assistant':'Grow Up Care','scope':'FAQ guidance and support requests; no business commitments.'})
    result.set_cookie(COOKIE,token,httponly=True,secure=request.url.scheme=='https',samesite='strict',max_age=86400,path='/support')
    return result


class Message(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    request_id:str=Field(min_length=36,max_length=36)
    text:str=Field(min_length=1,max_length=4000)


@router.post('/support/message')
async def message(body:Message,request:Request):
    same_origin(request);limit(request,'message',80)
    if os.getenv('CSR_PUBLIC_ENABLED','0')!='1':raise HTTPException(503,'Customer care is not active yet.')
    try:return await run_in_threadpool(csr.respond,identity(request),body.request_id,body.text)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'Customer care is temporarily unavailable; please try again.') from None
    except (WorkflowError,ValueError) as exc:raise HTTPException(409,str(exc)) from None


class Appointment(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    request_id:str=Field(min_length=36,max_length=36)
    agent:str=Field(pattern='^(simeon|solomon)$')
    when:str=Field(min_length=20,max_length=40)
    contact:str=Field(min_length=3,max_length=254)


@router.post('/support/appointment')
async def appointment(body:Appointment,request:Request):
    same_origin(request);limit(request,'appointment',5)
    if os.getenv('CSR_PUBLIC_ENABLED','0')!='1':raise HTTPException(503,'Customer care is not active yet.')
    who=identity(request)
    return await call('appointment',lambda conn:csr.reserve_appointment(conn,who,body.request_id,body.agent,body.when,body.contact))


def owner_session_valid(token):
    key=os.getenv('PUBLIC_SESSION_SECRET','')
    if len(key)<32 or not isinstance(token,str) or len(token)>256:return False
    try:
        payload,signature=token.rsplit('.',1)
        issued=int(payload.split('.',1)[0])
        expected=hmac.new(key.encode(),payload.encode(),hashlib.sha256).hexdigest()
        return -30<=time.time()-issued<14400 and hmac.compare_digest(signature,expected)
    except (ValueError,AttributeError,TypeError):return False


@router.get('/public/session-check')
def session_check(request:Request):
    return Response(status_code=204 if owner_session_valid(request.cookies.get(OWNER_COOKIE,'')) else 401)


@router.post('/owner-auth')
def login(request:Request):
    same_origin(request)
    expected=os.getenv('PUBLIC_LOGIN_BRIDGE_SECRET','')
    session_key=os.getenv('PUBLIC_SESSION_SECRET','')
    if len(expected)<32 or len(session_key)<32:raise HTTPException(503,'owner_login_not_configured')
    if (not request.headers.get('x-growup-authenticated-user') or not hmac.compare_digest(
            request.headers.get('x-growup-login-bridge','').encode(),expected.encode())):
        raise HTTPException(401,'existing_gateway_authentication_required')
    payload=str(int(time.time()))+'.'+secrets.token_hex(16)
    signature=hmac.new(session_key.encode(),payload.encode(),hashlib.sha256).hexdigest()
    response=JSONResponse({'authenticated':True,'destination':'/wondrue'})
    response.set_cookie(OWNER_COOKIE,payload+'.'+signature,secure=True,httponly=True,samesite='strict',max_age=14400,path='/')
    response.headers['Cache-Control']='no-store'
    return response


@router.post('/support/logout')
def logout(request:Request):
    same_origin(request)
    result=JSONResponse({'logged_out':True});result.delete_cookie(OWNER_COOKIE,path='/',secure=True,httponly=True,samesite='strict')
    return result


@router.get('/support/admin/tickets')
async def tickets(request:Request):
    from app.auth import wondrue_grant
    if wondrue_grant(request)!='full':raise HTTPException(403,'operator_required')
    from app.properties_db import get_connection
    def query():
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT run_id,record_key,payload FROM (SELECT DISTINCT ON(run_id,record_key) "
                    "run_id,record_key,payload,version FROM workflow_records WHERE kind='owner_review_task' "
                    "AND (record_key LIKE 'csr:%' OR record_key LIKE 'appointment:%' OR record_key='csr-email-review') "
                    "ORDER BY run_id,record_key,version DESC) q LIMIT 50")
                return {'tickets':[{**dict(row),'run_id':str(row['run_id'])} for row in cur.fetchall()],
                        'scope':'Support requests only; routes do not grant permission or confirm appointments.'}
    try:return await run_in_threadpool(query)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'support_storage_unavailable') from None
