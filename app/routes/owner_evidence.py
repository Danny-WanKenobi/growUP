"""Private bounded upload/download. No parsing, source fetching or promotion."""
from datetime import date
from uuid import UUID
from urllib.parse import quote
from fastapi import APIRouter,Request,HTTPException,Query
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict,Field,ValidationError
from starlette.concurrency import run_in_threadpool
from app import owner_evidence as evidence
from app.routes.wondrue_cockpit import authorize,safe_call
from app.routes.wondrue_workbench import Reference

router=APIRouter()
BODY_LIMIT=750_000


class Intake(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:UUID
    source:Reference
    filename:str=Field(min_length=1,max_length=160)
    content_base64:str=Field(min_length=1,max_length=4*((evidence.MAX_BYTES+2)//3))
    issuer:str=Field(min_length=1,max_length=240)
    source_url:str|None=Field(None,max_length=2048)
    document_date:date|None=None
    obtained_on:date|None=None
    claim_scope:list[str]=Field(min_length=1,max_length=8)
    note:str=Field(min_length=1,max_length=1000)


@router.post('/wondrue/api/evidence')
async def upload(request:Request):
    authorize(request,True)
    if request.headers.get('content-type','').split(';')[0]!='application/json':raise HTTPException(415,'JSON document envelope required.')
    length=request.headers.get('content-length')
    if length is not None:
        try:valid=0<=int(length)<=BODY_LIMIT
        except ValueError:valid=False
        if not valid:raise HTTPException(413,'Upload envelope exceeds the limit.')
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>BODY_LIMIT:raise HTTPException(413,'Upload envelope exceeds the limit.')
        data.extend(chunk)
    try:body=Intake.model_validate_json(bytes(data))
    except ValidationError:raise HTTPException(422,'Invalid document envelope. Check file size, dates and metadata; authentication claims are not accepted.') from None
    values=body.model_dump(mode='json');values['evidence_id']=values.pop('id')
    return await run_in_threadpool(safe_call,'evidence_intake',lambda c:evidence.intake(c,**values),True)


@router.get('/wondrue/api/evidence')
def listing(request:Request,run_id:UUID,key:str=Query(...,min_length=1,max_length=500),version:int=Query(...,ge=1),digest:str=Query(...,pattern='^[0-9a-f]{64}$')):
    authorize(request)
    source={'run_id':str(run_id),'key':key,'version':version,'digest':digest}
    return safe_call('evidence_list',lambda c:evidence.listing(c,source))


@router.get('/wondrue/api/evidence/{run_id}/{evidence_id}/download')
def download(run_id:UUID,evidence_id:UUID,request:Request):
    authorize(request)
    metadata,raw=safe_call('evidence_download',lambda c:evidence.download(c,run_id,evidence_id))
    return Response(raw,media_type='application/octet-stream',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'none'; sandbox",'Content-Disposition':"attachment; filename=retained-document.bin; filename*=UTF-8''"+quote(metadata['filename'],safe=''),
        'X-Content-SHA256':metadata['sha256']})


@router.get('/wondrue/api/evidence/{run_id}/{evidence_id}')
def inspect(run_id:UUID,evidence_id:UUID,request:Request):
    authorize(request)
    return safe_call('evidence_metadata',lambda c:evidence.inspect(c,run_id,evidence_id))
