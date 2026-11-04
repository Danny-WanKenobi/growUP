"""Owner-approved synthetic review only; deliberately not an operational executor."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,HTTPException,Request,Query
from pydantic import BaseModel,ConfigDict,Field
from app import guided_demo_review as guided
from app.db import DatabaseNotConfiguredError,DatabaseUnavailableError
from app.routes.wondrue_cockpit import authorize

router=APIRouter()


def call(action,**inputs):
    try:return guided.service(action,**inputs)
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):
        raise HTTPException(503,'Isolated demo storage is unavailable. No operational fallback or external action is permitted.') from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


class CaseRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID


@router.get('/wondrue/api/guided-review')
def cases(request:Request):
    authorize(request)
    return call('list')


@router.post('/wondrue/api/guided-review')
def create(body:CaseRequest,request:Request):
    authorize(request,True)
    return call('create',case_id=body.id)


@router.get('/wondrue/api/guided-review/{case_id}')
def inspect(case_id:UUID,request:Request,review_id:UUID|None=None):
    authorize(request)
    return call('inspect',case_id=case_id,review_id=review_id)


@router.get('/wondrue/api/guided-review/{case_id}/record')
def record(case_id:UUID,request:Request,version:int=Query(...,ge=1)):
    authorize(request)
    return call('record',case_id=case_id,version=version)


class PreviewRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID
    choice:Literal['supplied_update','keep_unknown']
    expected_digest:str=Field(min_length=64,max_length=64)


@router.post('/wondrue/api/guided-review/{case_id}/preview')
def preview(case_id:UUID,body:PreviewRequest,request:Request):
    authorize(request,True)
    return call('preview',case_id=case_id,review_id=body.id,choice=body.choice,expected_digest=body.expected_digest)


class OwnerDecision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_digest:str=Field(min_length=64,max_length=64)
    choice:Literal['approve','defer','reject']
    note:str=Field(min_length=1,max_length=1000)


def _continue(case_id,review_id,state):
    # Three bounded transactions; every checkpoint is atomic with its mutations.
    for _ in range(3):
        if state['review']['status']=='complete':return state
        state=call('advance',case_id=case_id,review_id=review_id)
    return state


@router.post('/wondrue/api/guided-review/{case_id}/{review_id}/decision')
def decide(case_id:UUID,review_id:UUID,body:OwnerDecision,request:Request):
    authorize(request,True)
    state=call('decide',case_id=case_id,review_id=review_id,choice=body.choice,
        expected_digest=body.expected_digest,note=body.note)
    return _continue(case_id,review_id,state) if body.choice=='approve' else state


@router.post('/wondrue/api/guided-review/{case_id}/{review_id}/resume')
def resume(case_id:UUID,review_id:UUID,request:Request):
    authorize(request,True)
    state=call('inspect',case_id=case_id,review_id=review_id)
    return _continue(case_id,review_id,state)
