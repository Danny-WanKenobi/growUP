"""Explicit non-real contract fixtures, accepted ONLY by the dedicated gate DB.

No fake authenticated property is inserted in development or production.
"""
from copy import deepcopy
from uuid import uuid4
from app.workflow import digest_payload
from app.workflow_db import create_run_in_transaction,put_record_in_transaction


def retained_fixture(conn):
    from tests.test_handoff_demo import Conn,KEY,drive
    from tests.test_funnel_acceptance import _new_db
    from app.handoff_demo_domain import scope
    from app.agents.stages import _load
    from app.custody import dossier_id_for_property
    from app.custody_db import create_version_in_transaction,snapshot_in_transaction,issue_receipt_in_transaction
    from app.agents.registry import PIPELINE_IDS
    memory=Conn(_new_db())
    with scope(memory,KEY):
        from app import handoff_demo
        key='demo-handoff-gate-contract-'+uuid4().hex
        drive(memory,key)
        original=_load(memory,handoff_demo._get(memory,key).run_id,'main')[0].payload
    def strip(value):
        if isinstance(value,list):return [strip(v) for v in value]
        if isinstance(value,dict):return {k:strip(v) for k,v in value.items() if k not in ('synthetic','domain','demo_label','demo_scenario','owner_briefs','owner_decisions','owner_rework','owner_vault_entries')}
        return value
    payload=strip(original);d=payload['dossier'];d['name']='TEST FIXTURE — NOT A REAL PROPERTY';payload['test_material']=True
    replacements={}
    def replace(value):
        if isinstance(value,str):return replacements.get(value,value)
        if isinstance(value,list):return [replace(v) for v in value]
        if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
        return value
    for agent in PIPELINE_IDS:
        report=d['reports'].get(agent)
        if not report:continue
        old=original['dossier']['reports'][agent]['digest']
        report=replace({k:v for k,v in report.items() if k!='digest'})
        report['digest']=digest_payload(report);d['reports'][agent]=report;replacements[old]=report['digest']
    payload['version']=1;payload['digest']=digest_payload({k:v for k,v in payload.items() if k!='digest'})
    run=create_run_in_transaction(conn,idempotency_key='pg-workbench-contract-'+uuid4().hex)
    row=put_record_in_transaction(conn,run.run_id,kind='dossier_version',record_key='main',version=1,payload=payload,digest=payload['digest'])
    did=dossier_id_for_property(payload['property_id'])
    version=create_version_in_transaction(conn,property_id=payload['property_id'],created_by='gabriel',payload={'dossier':d,'dossier_digest':row.digest,'run_id':str(run.run_id),'test_material':True})
    snapshot_in_transaction(conn,dossier_id=did,version=version.version)
    issue_receipt_in_transaction(conn,dossier_id=did,version=1,kind='forward',from_agent='gabriel',to_agent='manny',issued_by='pg-test-owner',note='TEST contract fixture; no property verification claim')
    link={'dossier_id':str(did),'custody_version':1,'property_id':payload['property_id'],'dossier_key':'main','receipts':{}}
    put_record_in_transaction(conn,run.run_id,kind='custody_link',record_key='main',version=1,payload=link,digest=digest_payload(link))
    task={'agent':'nicole','property_id':payload['property_id'],'requirement':'TEST retained contract refresh','status':'pending','authority':'human_owner','grants_permission':False,'test_material':True}
    put_record_in_transaction(conn,run.run_id,kind='owner_review_task',record_key='test-retained-task',version=1,payload=task,digest=digest_payload(task))
    return run,row
