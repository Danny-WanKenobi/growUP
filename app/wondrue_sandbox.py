"""Isolated hypothetical workspace. No operational connector or promotion path.

Only the pure Onesimus evaluator is reused. Every supplied quantity is assumed,
even when a source dossier contains verified claims. No full database cloning.
"""
from contextlib import contextmanager
import os
from uuid import UUID

from app.config import DatabaseConfig
from app.properties_db import get_connection
from app.workflow import digest_payload
from app.workflow_db import WorkflowError

DB='growup_wondrue_sandbox'
ROLE='growup_sandbox'
LABEL='HYPOTHETICAL SANDBOX — NOT VERIFIED OR AUTHORIZED FOR REAL-WORLD USE'


@contextmanager
def connection():
    if os.getenv('WONDRUE_SANDBOX_ENABLED')!='1':
        raise WorkflowError('Sandbox execution is disabled until database/role isolation is proven.')
    config=DatabaseConfig(host='127.0.0.1',port=5432,name=DB,user=ROLE,
                          password=os.getenv('WONDRUE_SANDBOX_PASSWORD',''))
    with get_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT current_database() AS db,current_user AS role')
            identity=cur.fetchone()
            if identity['db']!=DB or identity['role']!=ROLE:
                raise WorkflowError('sandbox database identity mismatch')
            cur.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            if any(cur.fetchone().values()):raise WorkflowError('sandbox role is overprivileged')
            cur.execute("SET LOCAL statement_timeout='5000ms'")
        yield conn


def enabled():
    return os.getenv('WONDRUE_SANDBOX_ENABLED')=='1'


def sanitize_source(record):
    if record.get('kind')!='dossier_version':raise WorkflowError('retained dossier source required')
    # No addresses, owners, contacts, emails, legal papers or full reports copied.
    return {'run_id':str(UUID(record['run_id'])),'dossier_key':record['key'],
            'version':record['version'],'digest':record['digest'],
            'copied_fields':['source reference only'],'source_claims_imported':False}


def evaluate(inputs):
    from app.agents.onesimus.service import ClassifiedInput,evaluate_scenario
    from app.agents.onesimus.engineering import SITE_UNITS,CROP_UNITS
    if not isinstance(inputs,dict) or set(inputs)-{'config','site','crop','tenant_consolidation'}:
        raise WorkflowError('bounded engineering inputs required')
    groups={}
    for name,units in (('site',SITE_UNITS),('crop',CROP_UNITS)):
        values=inputs.get(name,{})
        if not isinstance(values,dict) or set(values)-set(units):raise WorkflowError('unsupported engineering quantity')
        groups[name]={}
        for key,raw in values.items():
            if isinstance(raw,bool) or not isinstance(raw,(int,float)):
                raise WorkflowError('numeric assumed quantities required')
            groups[name][key]=ClassifiedInput(name=key,value=raw,unit=units[key],
                classification='assumed',provenance='Owner-entered sandbox assumption; not verified source data')
    consolidation=inputs.get('tenant_consolidation',False)
    if not isinstance(consolidation,bool):raise WorkflowError('explicit consolidation assumption required')
    result=evaluate_scenario(inputs.get('config','bench'),groups['crop'],groups['site'],
                             tenant_consolidation=consolidation,observed_occupancy='Unknown; not copied from source')
    return {'label':LABEL,'hypothetical':True,'authorization':False,'provider_calls':0,
            'operational_writes':0,'engineering':result}


def save(scenario_id,source,inputs):
    scenario_id=str(UUID(str(scenario_id)))
    value={'id':scenario_id,'source':sanitize_source(source),'inputs':inputs,'result':evaluate(inputs)}
    digest=digest_payload(value)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO sandbox_scenarios(id,payload,digest) VALUES (%s,%s::jsonb,%s) '
                        'ON CONFLICT(id) DO NOTHING',(scenario_id,__import__('json').dumps(value),digest))
            cur.execute('SELECT payload,digest FROM sandbox_scenarios WHERE id=%s',(scenario_id,))
            stored=cur.fetchone()
            if stored['digest']!=digest or digest_payload(stored['payload'])!=digest:
                raise WorkflowError('sandbox scenario identity conflict')
    return value


def list_scenarios(offset=0):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT payload,digest FROM sandbox_scenarios ORDER BY created_at DESC,id LIMIT 20 OFFSET %s',(offset,))
            rows=cur.fetchall()
    if any(digest_payload(r['payload'])!=r['digest'] for r in rows):raise WorkflowError('sandbox integrity failure')
    return {'items':[r['payload'] for r in rows],'has_more':len(rows)==20,'label':LABEL}
