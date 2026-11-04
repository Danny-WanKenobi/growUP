"""Private evidence, sensitivity and owner-workflow endpoints; no transports."""
from datetime import date
from typing import Literal,Any
from uuid import UUID
from fastapi import APIRouter,Request
from pydantic import BaseModel,ConfigDict,Field
from app.routes.wondrue_cockpit import authorize,safe_call
from app.routes.wondrue_workbench import Reference,SourceRequest,read_post,Decision
from app import evidence_workbench as evidence,owner_analysis as analysis,refresh_workflow as refresh

router=APIRouter()


@router.get('/wondrue/api/analysis/input-fields')
def input_fields(request:Request):
    authorize(request)
    from app.agents.onesimus.engineering import CROP_UNITS
    from app.michael import FINANCIAL_UNITS
    return {'onesimus':CROP_UNITS,'michael':FINANCIAL_UNITS}


class EvidenceAction(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_digest:str=Field(pattern='^[0-9a-f]{64}$')


@router.get('/wondrue/api/evidence/{run_id}/{evidence_id}/lifecycle')
def state(run_id:UUID,evidence_id:UUID,request:Request):
    authorize(request);return safe_call('evidence_state',lambda c:evidence.state(c,run_id,evidence_id))


@router.post('/wondrue/api/evidence/{run_id}/{evidence_id}/extract')
def extract(run_id:UUID,evidence_id:UUID,body:EvidenceAction,request:Request):
    authorize(request,True);return safe_call('extract_document',lambda c:evidence.extract(c,run_id=run_id,evidence_id=evidence_id,expected_digest=body.expected_digest),True)


class Revision(EvidenceAction):
    operation_id:UUID
    metadata:dict[str,Any]
    reason:str=Field(min_length=1,max_length=1000)
    supersedes:UUID|None=None


@router.post('/wondrue/api/evidence/{run_id}/{evidence_id}/revise')
def revise(run_id:UUID,evidence_id:UUID,body:Revision,request:Request):
    authorize(request,True);return safe_call('revise_document',lambda c:evidence.revise(c,run_id=run_id,evidence_id=evidence_id,**body.model_dump(mode='json')),True)


class Admission(EvidenceAction):
    expected_admission_digest:str|None=Field(None,pattern='^[0-9a-f]{64}$')
    choice:Literal['hold','reject','check_official_match']
    note:str=Field(min_length=1,max_length=1000)
    collection_run_id:UUID|None=None


@router.post('/wondrue/api/evidence/{run_id}/{evidence_id}/admission')
def admit(run_id:UUID,evidence_id:UUID,body:Admission,request:Request):
    authorize(request,True);return safe_call('review_document_admission',lambda c:evidence.admit(c,run_id=run_id,evidence_id=evidence_id,**body.model_dump(mode='json')),True)


@router.get('/wondrue/api/evidence/{run_id}/{evidence_id}/plan')
def route_plan(run_id:UUID,evidence_id:UUID,request:Request):
    authorize(request);return safe_call('evidence_plan',lambda c:evidence.route_plan(c,run_id=run_id,evidence_id=evidence_id))


class VersionComparison(BaseModel):
    model_config=ConfigDict(extra='forbid')
    before:Reference
    after:Reference


@router.post('/wondrue/api/analysis/version-diff')
def compare(body:VersionComparison,request:Request):
    read_post(request);return safe_call('version_diff',lambda c:analysis.compare_versions(c,body.before.model_dump(mode='json'),body.after.model_dump(mode='json')))


@router.post('/wondrue/api/analysis/dossier')
def dossier(body:SourceRequest,request:Request):
    read_post(request);return safe_call('dossier_analysis',lambda c:analysis.dossier_analysis(c,body.source.model_dump(mode='json')))


@router.post('/wondrue/api/analysis/real-proof')
def real_proof(body:SourceRequest,request:Request):
    read_post(request)
    from app.real_operating_proof import inspect
    return safe_call('real_operating_proof',lambda c:inspect(c,body.source.model_dump(mode='json')))


@router.get('/wondrue/api/analysis/portfolio')
def portfolio(request:Request,as_of:date):
    authorize(request);return safe_call('owner_portfolio',lambda c:analysis.portfolio(c,as_of.isoformat()))


@router.get('/wondrue/api/analysis/correspondence')
def messages(request:Request):
    authorize(request);return safe_call('correspondence_inventory',analysis.correspondence_listing)


class Correspondence(SourceRequest):
    conversation_id:UUID
    operation_id:UUID
    start:int=Field(ge=0,le=16000)
    end:int=Field(ge=1,le=16000)
    claim_scope:list[str]=Field(min_length=1,max_length=8)
    note:str=Field(min_length=1,max_length=800)


@router.post('/wondrue/api/analysis/correspondence')
def propose_message(body:Correspondence,request:Request):
    authorize(request,True);return safe_call('correspondence_evidence_proposal',lambda c:analysis.correspondence_proposal(c,**body.model_dump(mode='json')),True)


class Refresh(SourceRequest):
    id:UUID
    note:str=Field(min_length=1,max_length=1000)


@router.post('/wondrue/api/refresh-workflow')
def prepare_refresh(body:Refresh,request:Request):
    authorize(request,True);return safe_call('prepare_refresh',lambda c:refresh.prepare(c,**body.model_dump(mode='json')),True)


@router.get('/wondrue/api/refresh-workflow/{id}')
def inspect_refresh(id:UUID,request:Request):
    authorize(request);return safe_call('inspect_refresh',lambda c:refresh.inspect(c,id))


@router.post('/wondrue/api/refresh-workflow/{id}/decision')
def decide_refresh(id:UUID,body:Decision,request:Request):
    authorize(request,True);return safe_call('decide_refresh',lambda c:refresh.decide(c,id=id,**body.model_dump(mode='json')),True)


@router.post('/wondrue/api/refresh-workflow/{id}/advance')
def advance_refresh(id:UUID,request:Request):
    authorize(request,True);return safe_call('advance_refresh',lambda c:refresh.advance(c,id=id),True)
