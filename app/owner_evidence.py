"""Immutable owner-supplied documents. Retention is never source authentication."""
import base64
import hashlib
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID
from app import wondrue_workspace as ws
from app.communications import instant,lock
from app.custody import dossier_id_for_property
from app.custody_db import lock_dossier_in_transaction
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,put_record_in_transaction
from app.wondrue_retained_review import guard

MAX_BYTES=512*1024
MAX_FILES=20
SCOPES=('identity','category','dimensions','occupancy','zoning','structural','electrical','water','financial','other')
STATUS='owner_supplied_unverified'
LIMITS='Retained untrusted attachment. Optional extraction and review are separately recorded and remain unverified. No malware-scan, source-authentication or canonical admission is implied.'


def _source(conn,ref,current=False):
    source=ws.record(conn,ref['run_id'],'dossier_version',ref['key'],None if current else ref['version'])
    guard(conn,SimpleNamespace(payload=source['data']))
    if source['version']!=ref['version'] or source['digest']!=ref['digest']:
        raise WorkflowError('selected dossier changed; inspect and select its current version')
    UUID(str(source['data'].get('property_id')))
    return source


def _index_key(key):return 'owner-evidence-index:'+digest_payload({'key':key})


def _index(conn,rid,key):
    row=ws.read(conn,rid,_index_key(key))
    entries=row.payload['entries'] if row else []
    if not isinstance(entries,list) or len(entries)>MAX_FILES:raise WorkflowError('attachment index invalid')
    return row,entries


def validate_document(name,encoded):
    if not isinstance(name,str) or not 1<=len(name)<=160 or any(ord(c)<32 or ord(c)==127 for c in name) or '/' in name or '\\' in name:
        raise WorkflowError('a plain filename of at most 160 characters is required')
    if not isinstance(encoded,str) or len(encoded)>4*((MAX_BYTES+2)//3):raise WorkflowError('document exceeds 512 KiB')
    try:raw=base64.b64decode(encoded,validate=True)
    except (ValueError,UnicodeError):raise WorkflowError('invalid file encoding') from None
    if not raw or len(raw)>MAX_BYTES:raise WorkflowError('document must contain 1 byte to 512 KiB')
    suffix=name.rsplit('.',1)[-1].lower() if '.' in name else ''
    mime=None
    if suffix=='pdf' and raw.startswith(b'%PDF-'):mime='application/pdf'
    elif suffix=='png' and raw.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
    elif suffix in ('jpg','jpeg') and raw.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
    elif suffix in ('txt','csv'):
        try:raw.decode('utf-8-sig')
        except UnicodeError:raise WorkflowError('text documents must be UTF-8') from None
        if b'\x00' not in raw:mime='text/plain' if suffix=='txt' else 'text/csv'
    if mime is None:raise WorkflowError('supported documents: PDF, PNG, JPEG, UTF-8 TXT/CSV with matching headers; archives, HTML and executable formats are refused')
    return raw,mime


def _metadata(conn,rid,evidence_id):
    row=ws.read(conn,rid,'owner-evidence:'+str(UUID(str(evidence_id))))
    if row is None:raise WorkflowError('retained attachment not found')
    p=row.payload
    if p.get('authentication_status')!=STATUS or p.get('source_authentication_granted') is not False:
        raise WorkflowError('attachment authentication marker invalid')
    _source(conn,p['source'])
    return row


def validate_provenance(issuer,source_url,document_date,obtained_on,claim_scope,note):
    issuer=ws._text(issuer,240,'declared issuer/source')
    note=ws._text(note,1000,'claim description')
    if not isinstance(claim_scope,list) or not 1<=len(claim_scope)<=8 or len(set(claim_scope))!=len(claim_scope) or any(s not in SCOPES for s in claim_scope):
        raise WorkflowError('select one to eight explicit supported claim scopes')
    for value in (document_date,obtained_on):
        if value is not None:
            try:date.fromisoformat(value)
            except (ValueError,TypeError):raise WorkflowError('dates must be ISO dates or unknown') from None
    if source_url is not None:
        if not isinstance(source_url,str) or len(source_url)>2048 or any(ord(c)<=32 for c in source_url):raise WorkflowError('bounded public source URL required')
        try:
            url=urlsplit(source_url)
            if url.scheme not in ('https','http') or not url.hostname or url.username or url.password or url.fragment:raise ValueError()
            url.port
        except ValueError:raise WorkflowError('source URL must use HTTP(S), without credentials or fragment; it is retained, never fetched') from None
    return issuer,note


def intake(conn,*,evidence_id,source,filename,content_base64,issuer,source_url,document_date,obtained_on,claim_scope,note):
    evidence_id=str(UUID(str(evidence_id)));rid=UUID(source['run_id'])
    raw,mime=validate_document(filename,content_base64)
    issuer,note=validate_provenance(issuer,source_url,document_date,obtained_on,claim_scope,note)
    sha=hashlib.sha256(raw).hexdigest()
    request={'source':source,'filename':filename,'declared_issuer':issuer,'source_url':source_url,
        'document_date':document_date,'obtained_on':obtained_on,'claim_scope':claim_scope,'note':note,'sha256':sha}
    # Same canonical lock as report writers, plus a bounded index lock.
    selected=_source(conn,source)
    did=dossier_id_for_property(selected['data']['property_id'])
    lock_dossier_in_transaction(conn,did);lock(conn,'owner-evidence:'+str(rid)+source['key'])
    previous=ws.read(conn,rid,'owner-evidence:'+evidence_id)
    if previous:
        row=_metadata(conn,rid,evidence_id)
        if row.payload['request_digest']!=digest_payload(request):raise WorkflowError('attachment identity conflict')
        return dict(row.payload,metadata_digest=row.digest,replayed=True)
    _source(conn,source,current=True)
    index,entries=_index(conn,rid,source['key'])
    if len(entries)>=MAX_FILES:raise WorkflowError('this dossier already has 20 retained attachments')
    if any(e['sha256']==sha for e in entries):raise WorkflowError('these bytes are already attached; use the existing retained document')
    blob=ws.write(conn,rid,'owner-evidence-bytes:'+evidence_id,{'id':evidence_id,'content_base64':base64.b64encode(raw).decode('ascii'),'sha256':sha})
    task_key='owner-evidence-review:'+evidence_id
    value={'schema':'owner-evidence-1','id':evidence_id,**request,'request_digest':digest_payload(request),
        'property_id':selected['data']['property_id'],'received_at':instant().isoformat(),'received_by':'wondrue-operator',
        'authentication_status':STATUS,'source_authentication_granted':False,'bytes':len(raw),'detected_type':mime,
        'blob_key':blob.record_key,'blob_digest':blob.digest,'review_task_key':task_key,'limits':LIMITS,
        'provider_calls':0,'external_actions':0}
    row=ws.write(conn,rid,'owner-evidence:'+evidence_id,value)
    task={'agent':'human_owner','property_id':value['property_id'],'dossier_id':str(did),
        'requirement':'Review owner-supplied evidence: '+filename,'reason':note,'status':'pending','authority':'human_owner',
        'grants_permission':False,'authentication_status':STATUS,'evidence_id':evidence_id,'evidence_metadata_digest':row.digest,
        'source_dossier_digest':source['digest'],'source_version':source['version'],'dossier_key':source['key']}
    put_record_in_transaction(conn,rid,kind='owner_review_task',record_key=task_key,version=1,payload=task,digest=digest_payload(task))
    ws.write(conn,rid,_index_key(source['key']),{'entries':entries+[{'id':evidence_id,'metadata_digest':row.digest,'sha256':sha}]},index)
    return dict(value,metadata_digest=row.digest,replayed=False)


def listing(conn,source):
    _source(conn,source);rid=UUID(source['run_id']);_,entries=_index(conn,rid,source['key']);items=[]
    for entry in entries:
        row=_metadata(conn,rid,entry['id'])
        if row.digest!=entry['metadata_digest'] or row.payload['sha256']!=entry['sha256'] or row.payload['source']['key']!=source['key']:
            raise WorkflowError('attachment index binding failed')
        items.append(dict(row.payload,metadata_digest=row.digest,bound_to_selected_version=row.payload['source']==source))
    return {'source':source,'items':items,'limit':MAX_FILES,'max_bytes':MAX_BYTES,'records_written':0,'provider_calls':0,'limits':LIMITS}


def inspect(conn,run_id,evidence_id):
    rid=UUID(str(run_id));row=_metadata(conn,rid,evidence_id);p=row.payload
    _,entries=_index(conn,rid,p['source']['key'])
    if not any(e['id']==p['id'] and e['metadata_digest']==row.digest for e in entries):raise WorkflowError('attachment is not in its dossier index')
    return dict(p,metadata_digest=row.digest)


def download(conn,run_id,evidence_id):
    rid=UUID(str(run_id));p=inspect(conn,rid,evidence_id)
    blob=ws.read(conn,rid,p['blob_key'])
    if blob is None or blob.digest!=p['blob_digest'] or blob.payload.get('id')!=p['id']:raise WorkflowError('attachment bytes missing or corrupt')
    raw,mime=validate_document(p['filename'],blob.payload['content_base64'])
    if len(raw)!=p['bytes'] or hashlib.sha256(raw).hexdigest()!=p['sha256'] or blob.payload['sha256']!=p['sha256'] or mime!=p['detected_type']:
        raise WorkflowError('attachment checksum or type mismatch')
    return p,raw
