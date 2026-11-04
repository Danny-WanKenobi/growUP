"""Append-only fictional preference assessments; never changes canonical custody."""
from copy import deepcopy
from app.conversion_demo import build
from app.workflow import digest_payload
from app.workflow_db import WorkflowError
from app.communications import lock, read, write
from app.handoff_demo_domain import assert_database, current_key


def assessment(source, pathway, concerns, previous=None):
    if pathway not in ('owner_funded', 'participation'):
        raise WorkflowError('Choose one supported fictional pathway')
    if not isinstance(concerns, str) or not concerns.strip() or len(concerns)>1000:
        raise WorkflowError('Provide a bounded fictional owner concern')
    briefs=build(source)
    if previous is not None:
        if (previous.get('digest')!=digest_payload({k:v for k,v in previous.items() if k!='digest'})
                or previous.get('source_dossier_digest')!=source['digest']
                or previous.get('property_id')!=source['property_id']):
            raise WorkflowError('Prior owner package does not match this retained source')
    selected=deepcopy(briefs['theresia'][
        'owner_funded_proposal' if pathway=='owner_funded' else 'participation_proposal'])
    value={'schema':'conversion-owner-demo-1','synthetic':True,'domain':'synthetic_handoff_demo',
        'property_id':source['property_id'],'source_dossier_digest':source['digest'],
        'revision':previous['revision']+1 if previous else 1,
        'previous_assessment_digest':previous['digest'] if previous else None,
        'pathway':pathway,'fictional_owner_concerns':concerns.strip(),
        'model_digest':briefs['model']['digest'],'simeon':briefs['simeon'],
        'theresia':selected,'elmer':briefs['elmer'],
        'manny':{'status':'prepared_for_demo_owner_review',
            'selected_pathway':pathway,'unresolved_conditions':selected['conditions'],
            'comparison':deepcopy(briefs['model']),
            'decision':'Review the fictional preference and conditions; no execution approval.',
            'business_approval':False,'funding_committed':False},
        'canonical_reports_changed':False,'custody_transition':False,
        'external_actions':0,'ledger_postings':0,'real_actions_enabled':False}
    value['digest']=digest_payload(value)
    return value


def history(conn, run_id, source):
    assert_database(conn)
    head=read(conn,run_id,'conversion-owner-head:'+source['digest'])
    if head is None:return []
    chain=[];cursor=head.payload['digest']
    while cursor:
        if len(chain)>=100:raise WorkflowError('Owner demo history exceeds bounded review limit')
        row=read(conn,run_id,'conversion-owner:'+cursor)
        if row is None:raise WorkflowError('Owner demo predecessor is missing')
        item=row.payload
        if (item.get('digest')!=cursor or digest_payload({k:v for k,v in item.items() if k!='digest'})!=cursor
                or item.get('source_dossier_digest')!=source['digest'] or item.get('property_id')!=source['property_id']):
            raise WorkflowError('Owner demo history integrity failure')
        chain.append(item);cursor=item['previous_assessment_digest']
    chain.reverse()
    if any(v['revision']!=i+1 for i,v in enumerate(chain)):
        raise WorkflowError('Owner demo revision order is invalid')
    for index,item in enumerate(chain):
        if item!=assessment(source,item['pathway'],item['fictional_owner_concerns'],chain[index-1] if index else None):
            raise WorkflowError('Owner demo assessment no longer matches its deterministic inputs')
    return chain


def append(conn,run_id,source,*,pathway,concerns,expected_previous):
    assert_database(conn)
    if current_key() is None:raise WorkflowError('Explicit isolated demo scope required')
    lock(conn,'conversion-owner:'+str(run_id)+':'+source['digest'])
    retained=history(conn,run_id,source)
    prior=retained[-1] if retained else None
    actual=prior['digest'] if prior else None
    # Exact retries return their existing assessment; changed stale requests refuse.
    if prior and prior['previous_assessment_digest']==expected_previous:
        candidate=assessment(source,pathway,concerns,retained[-2] if len(retained)>1 else None)
        if candidate==prior:return {'assessment':prior,'replayed':True}
    if actual!=expected_previous:raise WorkflowError('Preference changed; reload before reassessing')
    if len(retained)>=100:raise WorkflowError('Owner demo revision limit reached')
    item=assessment(source,pathway,concerns,prior)
    write(conn,run_id,'conversion-owner:'+item['digest'],item)
    key='conversion-owner-head:'+source['digest']
    write(conn,run_id,key,{'digest':item['digest']},read(conn,run_id,key))
    return {'assessment':item,'replayed':False}


def service(pathway,concerns,expected_previous):
    from app import handoff_demo as demo
    from app.demo_story import run_key
    from app.properties_db import get_connection
    from app.workflow_db import get_record_in_transaction
    from app.handoff_demo_domain import scope
    cfg,key=demo.configuration()
    with get_connection(cfg) as conn:
        with scope(conn,key):
            run=demo._get(conn,run_key('hudson-sky'))
            row=get_record_in_transaction(conn,run.run_id,'dossier_version','main')
            if row is None:raise WorkflowError('Retained Hudson source required')
            result=append(conn,run.run_id,row.payload,pathway=pathway,concerns=concerns,
                          expected_previous=expected_previous)
            conn.commit();return result


def prove():
    """One bounded two-preference story, followed by exact replay; no discovery."""
    from app import handoff_demo as demo
    from app.demo_story import run_key
    from app.properties_db import get_connection
    from app.workflow_db import get_record_in_transaction
    from app.handoff_demo_domain import scope
    from app.custody import dossier_id_for_property
    from app.custody_db import verify_chain_in_transaction,current_custody_in_transaction
    cfg,key=demo.configuration()
    with get_connection(cfg) as conn:
        with scope(conn,key):
            run=demo._get(conn,run_key('hudson-sky'))
            row=get_record_in_transaction(conn,run.run_id,'dossier_version','main')
            if row is None:raise WorkflowError('Retained Hudson source required')
            before=deepcopy(row.payload);did=dossier_id_for_property(before['property_id'])
            receipts=verify_chain_in_transaction(conn,did);custody=current_custody_in_transaction(conn,did)
            retained=history(conn,run.run_id,before)
            if not retained:
                first=append(conn,run.run_id,before,pathway='owner_funded',
                    concerns='Fictional owner initially prefers consulting but wants to preserve cash.',expected_previous=None)
                second=append(conn,run.run_id,before,pathway='participation',
                    concerns='Fictional owner now explores shared project funding while retaining building ownership.',
                    expected_previous=first['assessment']['digest'])
                replay=append(conn,run.run_id,before,pathway='participation',
                    concerns=second['assessment']['fictional_owner_concerns'],expected_previous=first['assessment']['digest'])
                if not replay['replayed']:raise WorkflowError('Preference replay produced a duplicate')
            retained=history(conn,run.run_id,before)
            if len(retained)<2:raise WorkflowError('Two linked preference assessments required')
            after=get_record_in_transaction(conn,run.run_id,'dossier_version','main')
            if (after.payload!=before or verify_chain_in_transaction(conn,did)!=receipts
                    or current_custody_in_transaction(conn,did)!=custody):
                raise WorkflowError('Canonical source or custody changed during demonstration')
            conn.commit()
            return {'result':'PASS','database':'growup_handoff_demo','synthetic':True,
                'retained_assessments':len(retained),'latest_digest':retained[-1]['digest'],
                'verified_receipts_unchanged':receipts,'canonical_source_unchanged':True,
                'provider_calls':0,'external_actions':0,'operational_writes':0}


if __name__=='__main__':
    import json
    print(json.dumps(prove(),indent=2))
