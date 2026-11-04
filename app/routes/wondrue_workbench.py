"""Private, bounded operational views and explicitly reviewed retained rework."""
from datetime import date
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Request,Query,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app.routes.wondrue_cockpit import authorize,safe_call
from app import wondrue_insights as views,wondrue_retained_review as retained
from app.db import DatabaseNotConfiguredError,DatabaseUnavailableError

router=APIRouter()


class Reference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    run_id:UUID
    key:str=Field(min_length=1,max_length=500)
    version:int=Field(ge=1)
    digest:str=Field(pattern='^[0-9a-f]{64}$')


class SourceRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source:Reference


@router.get('/wondrue/api/workbench/inbox')
def inbox(request:Request,as_of:date,offset:int=Query(0,ge=0,le=10000)):
    authorize(request)
    return safe_call('inbox',lambda c:views.inbox(c,as_of=as_of.isoformat(),offset=offset))


@router.get('/wondrue/api/workbench/briefing')
def briefing(request:Request,as_of:date):
    authorize(request)
    return safe_call('briefing',lambda c:views.briefing(c,as_of=as_of.isoformat()))


@router.get('/wondrue/api/workbench/execution')
def execution(request:Request):
    authorize(request)
    return safe_call('execution',views.execution)


def read_post(request):
    authorize(request)
    from app.routes.public import same_origin
    same_origin(request)


@router.post('/wondrue/api/workbench/plan')
def plan(body:SourceRequest,request:Request):
    read_post(request)
    return safe_call('dependency_plan',lambda c:views.plan(c,body.source.model_dump(mode='json')))


class Comparison(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sources:list[Reference]=Field(min_length=2,max_length=4)


@router.post('/wondrue/api/workbench/compare')
def compare(body:Comparison,request:Request):
    read_post(request)
    return safe_call('compare',lambda c:views.compare(c,[r.model_dump(mode='json') for r in body.sources]))


class TaskNote(SourceRequest):
    choice:Literal['acknowledge','defer','request_internal_followup']
    note:str=Field(min_length=1,max_length=1000)
    expected_note_digest:str|None=Field(None,pattern='^[0-9a-f]{64}$')
    confirm:Literal[True]


@router.post('/wondrue/api/workbench/task-note')
def task_note(body:TaskNote,request:Request):
    authorize(request,True)
    return safe_call('task_note',lambda c:views.task_note(c,body.source.model_dump(mode='json'),body.choice,body.note,body.expected_note_digest),True)


class RetainedRequest(SourceRequest):
    id:UUID
    task_key:str=Field(min_length=1,max_length=500)
    target:Literal['onesimus','michael','uriel','priscilla','nicole','theresia_keys','simeon','solomon']
    note:str=Field(min_length=1,max_length=1000)
    evidence_links:list[dict]=Field(default_factory=list,max_length=8)
    changes:list[dict]=Field(default_factory=list,max_length=8)


def retained_call(action,**kwargs):
    try:return retained.service(action,**kwargs)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'Retained review storage unavailable; no fallback or external action.') from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@router.post('/wondrue/api/retained-review')
def prepare(body:RetainedRequest,request:Request):
    authorize(request,True)
    return retained_call('prepare',review_id=body.id,ref=body.source.model_dump(mode='json'),task_key=body.task_key,target=body.target,note=body.note,evidence_links=body.evidence_links,changes=body.changes)


@router.get('/wondrue/api/retained-review/{review_id}')
def inspect(review_id:UUID,request:Request):
    authorize(request)
    return retained_call('inspect',review_id=review_id)


class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_digest:str=Field(pattern='^[0-9a-f]{64}$')
    choice:Literal['approve','reject','defer']
    note:str=Field(min_length=1,max_length=1000)


def advance(review_id,state):
    for _ in range(3):
        if state['review']['status']=='complete':break
        state=retained_call('advance',review_id=review_id)
    return state


@router.post('/wondrue/api/retained-review/{review_id}/decision')
def decide(review_id:UUID,body:Decision,request:Request):
    authorize(request,True)
    state=retained_call('decide',review_id=review_id,expected_digest=body.expected_digest,choice=body.choice,note=body.note)
    return advance(review_id,state) if body.choice=='approve' else state


@router.post('/wondrue/api/retained-review/{review_id}/resume')
def resume(review_id:UUID,request:Request):
    authorize(request,True)
    return advance(review_id,retained_call('inspect',review_id=review_id))


class ScenarioRef(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID
    digest:str=Field(pattern='^[0-9a-f]{64}$')


class ScenarioComparison(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sources:list[ScenarioRef]=Field(min_length=2,max_length=4)


@router.post('/wondrue/api/sandbox/compare')
def scenario_compare(body:ScenarioComparison,request:Request):
    read_post(request)
    from app.wondrue_sandbox import compare_scenarios
    try:return compare_scenarios([r.model_dump(mode='json') for r in body.sources])
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'Isolated scenario storage unavailable.') from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from None
