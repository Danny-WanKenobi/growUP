"""Bounded read-only owner views over existing records. No dispatch or providers."""
from datetime import date,datetime,time,timedelta
from zoneinfo import ZoneInfo
from uuid import UUID
from copy import deepcopy

from app import wondrue_workspace as ws
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction
from app.linked_reassessments import effective_reports,stale_dependencies
from app.agents.registry import PIPELINE_IDS

RETAINED_TARGETS=('onesimus','michael','uriel','priscilla','nicole','theresia_keys','simeon','solomon')


def reference(row):
    return {'run_id':str(row['run_id']),'kind':row['kind'],'key':row['record_key'],
            'version':row['version'],'digest':row['digest']}


def latest_rows(conn,kinds,*,limit=100,offset=0,prefixes=None):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM (SELECT DISTINCT ON(run_id,kind,record_key) run_id,kind,record_key,version,payload,digest,created_at "
                    "FROM workflow_records WHERE kind=ANY(%(kinds)s) AND record_key LIKE ANY(%(prefixes)s) ORDER BY run_id,kind,record_key,version DESC) q "
                    "ORDER BY created_at DESC,run_id,kind,record_key LIMIT %(limit)s OFFSET %(offset)s",
                    {'kinds':list(kinds),'limit':limit+1,'offset':offset,'prefixes':prefixes or ['%']})
        rows=cur.fetchall()
    return rows[:limit],len(rows)>limit


def _valid(row):return ws.valid_record(row['kind'],row['payload'],row['digest'])


def _day(value):
    try:return date.fromisoformat(value) if value else None
    except (ValueError,TypeError):return None


def inbox(conn,*,as_of,offset=0):
    today=date.fromisoformat(as_of);rows,more=latest_rows(conn,('owner_review_task',),offset=offset)
    items=[];resolutions={}
    for row in rows:
        ref=reference(row)
        if not _valid(row):
            items.append({'source':ref,'title':'Integrity review required','category':'integrity','owner':'human_owner',
                'urgency':'unknown','status':'quarantined','choices':[],'dependencies':[]});continue
        p=row['payload'];key=row['record_key']
        category=('support' if key.startswith(('csr:','csr-email','appointment:')) else
                  'correspondence' if any(v in key for v in ('mail','communication','reply','inbound')) else
                  'asset' if any(v in str(p.get('requirement','')) for v in ('obligation','asset:')) else 'dossier')
        due=_day(p.get('due_on'));urgency=p.get('urgency') or p.get('priority') or 'unknown'
        if urgency not in ('critical','high','normal','low'):urgency='unknown'
        annotations=ws.read(conn,UUID(str(row['run_id'])),'cockpit-task-note:'+digest_payload({'key':key}))
        annotation=dict(annotations.payload,note_digest=annotations.digest) if annotations and annotations.payload.get('task_digest')==row['digest'] else None
        # A note never resolves or modifies the originating agent's task.
        status=p.get('status','unknown')
        rid=str(row['run_id'])
        if rid not in resolutions:
            from app.manny_review import queue_in_transaction
            resolutions[rid]={t['record_key']:t for t in queue_in_transaction(conn,run_id=rid)['tasks']}
        status=resolutions[rid].get(key,{}).get('review_status',status)
        if annotation:status='owner_'+annotation['choice']+'; source workflow unchanged'
        items.append({'source':ref,'title':str(p.get('requirement') or p.get('reason') or key)[:240],
            'reason':p.get('reason'),'category':category,'owner':p.get('agent') or p.get('route') or 'unassigned',
            'urgency':urgency,'due_on':due.isoformat() if due else None,'overdue':due<today if due else None,
            'status':status,'annotation':annotation,
            'dependencies':p.get('dependencies',[]) if isinstance(p.get('dependencies',[]),list) else [],
            'choices':['acknowledge','defer','request_internal_followup'],
            'limits':'Administrative note only. No message, appointment confirmation, source verification or task closure.'})
    proposals=ws.review_list(conn,(offset//100)*30)
    retained,retained_more=latest_rows(conn,('research_artifact',),limit=30,offset=(offset//100)*30,prefixes=['retained-review'])
    retained_reviews=[dict(r['payload'],review_digest=r['digest']) for r in retained if _valid(r)]
    return {'as_of':as_of,'items':items,'proposals':proposals,'retained_reviews':retained_reviews,'has_more':more or retained_more,'offset':offset,
            'scope':'Bounded retained tasks, not a complete global count; source resolutions require the linked owner context.',
            'records_written':0,'provider_calls':0,'external_actions':0}


def snapshot(conn,ref):
    row=ws.record(conn,ref['run_id'],'dossier_version',ref['key'],ref['version'])
    if row['digest']!=ref['digest']:raise WorkflowError('selected source digest changed')
    d=row['data'].get('dossier') or {}
    reports=effective_reports(d)
    for name,report in reports.items():
        if not isinstance(report,dict) or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'}):
            raise WorkflowError('report integrity failure: '+name)
    return row,d,reports


def dependency_plan(dossier):
    reports=effective_reports(dossier);stale=stale_dependencies(reports)
    if any(not isinstance(r,dict) or r.get('digest')!=digest_payload({k:v for k,v in r.items() if k!='digest'}) for r in reports.values()):
        raise WorkflowError('dependency report integrity failure')
    edges={}
    for name,r in reports.items():
        refs=r.get('rework_input_digests') or r.get('inherited_report_digests') or {}
        edges[name]=set(refs) if isinstance(refs,dict) else set()
        if r.get('source_inspection_digest'):edges[name].add('raphael')
        if r.get('source_engineering_digest'):edges[name].add('onesimus')
    pending=set(stale);order=[]
    while pending:
        ready=sorted((n for n in pending if not (edges.get(n,set()) & pending)),
                     key=lambda n:PIPELINE_IDS.index(n) if n in PIPELINE_IDS else 100)
        if not ready:break
        order.extend(ready);pending.difference_update(ready)
    return {'stale':stale,'steps':[{'agent':n,'depends_on':sorted(edges.get(n,set()) & set(stale)),
        'can_preview_now':not bool(edges.get(n,set()) & set(stale)),
        'retained_review_supported':n in RETAINED_TARGETS,
        'action':'Reassess retained inputs only; a new exact preview/approval is required per changed dossier.'} for n in order],
        'blocked_cycle':sorted(pending),'automatic_execution':False,
        'scope':'Minimum stale-report set from retained dependency references; missing or unsupported upstream evidence is not invented.'}


def plan(conn,ref):
    row,d,_=snapshot(conn,ref)
    return {'source':{k:row[k] for k in ('run_id','kind','key','version','digest')},**dependency_plan(d)}


def compare(conn,refs):
    if not 2<=len(refs)<=4:raise WorkflowError('choose two to four dossiers')
    results=[];properties=set()
    for ref in refs:
        row,d,reports=snapshot(conn,ref)
        prop=row['data'].get('property_id')
        if not prop or prop in properties:raise WorkflowError('choose distinct property identities; alternatives are not separate properties')
        properties.add(prop)
        result={'source':{k:row[k] for k in ('run_id','kind','key','version','digest')},
            'property_id':prop,'name':d.get('name') or d.get('street_address') or row['key'],
            'category_attribution':deepcopy(d.get('category_attribution')),'county_attribution':deepcopy(d.get('county_attribution')),
            'stale_dependencies':stale_dependencies(reports),
            'missing':{n:deepcopy(r.get('missing') or []) for n,r in reports.items() if r.get('missing')},
            'engineering':deepcopy((reports.get('onesimus') or {}).get('alternatives')),
            'room_engineering':deepcopy((reports.get('onesimus') or {}).get('room_portfolio')),
            'room_finances':deepcopy((reports.get('michael') or {}).get('room_finance')),
            'finances':deepcopy((reports.get('michael') or {}).get('assessments')),
            'financial_assumptions':deepcopy((reports.get('michael') or {}).get('planning_cases')),
            'confidence':'Only retained evidence classifications; no invented combined confidence score.'}
        results.append(result)
    return {'properties':results,'ranking':None,'totals':None,'records_written':0,'provider_calls':0,
            'limits':['No ranking or category advantage is inferred.','Unknown inputs remain unknown.',
                      'Scenarios and properties are not added together; units, assumptions and source dates must be considered.']}


def briefing(conn,*,as_of):
    date.fromisoformat(as_of)
    zone=ZoneInfo('America/Chicago');start=datetime.combine(date.fromisoformat(as_of),time.min,zone)
    end=start+timedelta(days=1)
    queue=inbox(conn,as_of=as_of)
    rows,more=latest_rows(conn,('dossier_version',),limit=50)
    obligations=[];blocked=[]
    from app.asset_tracking import project
    for row in rows:
        if not _valid(row):
            blocked.append({'source':reference(row),'reason':'integrity failure'});continue
        d=row['payload'].get('dossier') or {}
        if d.get('owner_asset_events'):
            view=project(d,property_id=row['payload']['property_id'],as_of=as_of)
            for item in view['obligations']:
                event=item['event'];due=_day(event.get('due_on'))
                if event.get('status') in ('open','disputed') and (due is None or due<=date.fromisoformat(as_of)+timedelta(days=7)):
                    obligations.append({'source':reference(row),**item})
        stale=stale_dependencies(effective_reports(d))
        if stale:blocked.append({'source':reference(row),'reason':'stale downstream reports','agents':list(stale)})
    with conn.cursor() as cur:
        cur.execute("SELECT run_id,kind,record_key,version,digest,created_at FROM workflow_records "
                    "WHERE kind=ANY(%(kinds)s) AND created_at>=%(start)s AND created_at<%(end)s "
                    "ORDER BY created_at DESC,record_id LIMIT 51",{'kinds':['dossier_version','owner_review_task'],'start':start,'end':end})
        changes=cur.fetchall()
    return {'as_of':as_of,'timezone':'America/Chicago','changes':[dict(reference(r),recorded_at=r['created_at'].isoformat()) for r in changes[:50]],
        'decisions':queue,'upcoming_obligations':obligations,'blocked':blocked,
        'coverage_limited':more or len(changes)>50 or queue['has_more'],'dossiers_inspected':len(rows),
        'scope':'On-demand read-only briefing of recorded changes and obligations; no completeness, payment or scheduling assertion.',
        'records_written':0,'provider_calls':0,'external_actions':0}


def execution(conn):
    rows,more=latest_rows(conn,('research_artifact',),limit=200,
        prefixes=['coordinator:%','owner-rework:%','message:%','csr-email-job','workers','review'])
    jobs=[]
    for row in rows:
        key=row['record_key']
        if not (key.startswith(('coordinator:','owner-rework:','message:')) or key in ('csr-email-job','workers','review')):continue
        if not _valid(row):jobs.append({'source':reference(row),'state':'quarantined','controls':[]});continue
        p=row['payload'];controls=[]
        if key.startswith('coordinator:') and p.get('principal')=='wondrue-operator':
            controls=['resume'] if p.get('status')=='stopped' else ['stop'] if p.get('status')=='running' else []
        jobs.append({'source':reference(row),'agent':p.get('agent') or p.get('target_agent') or ('csr' if key.startswith('message:') or key in ('workers','csr-email-job') else 'wondrue'),
            'state':p.get('status','recorded_worker_pool' if key=='workers' else 'unknown'),
            'sequence':p.get('sequence'),'reason':p.get('reason') or p.get('reasons'),
            'lease':deepcopy(p.get('lease')),'workers':deepcopy(p.get('slots')) if key=='workers' else None,
            'controls':controls,'principal':p.get('principal'),
            'limits':'Recorded state, not proof of a live process. Expired/uncertain work is never automatically retried.'})
    return {'jobs':jobs,'has_more':more,'scan_limit':200,'records_written':0,'provider_calls':0,
            'scope':'Existing persisted jobs only; no fabricated heartbeat or unsupported worker starts.'}


def task_note(conn,ref,choice,note,expected_note_digest=None):
    if choice not in ('acknowledge','defer','request_internal_followup'):raise WorkflowError('unsupported administrative choice')
    note=ws._text(note,1000,'owner note')
    from app.communications import lock,instant
    key='cockpit-task-note:'+digest_payload({'key':ref['key']})
    lock(conn,str(ref['run_id'])+key)
    source=ws.record(conn,ref['run_id'],'owner_review_task',ref['key'])
    if source['version']!=ref['version'] or source['digest']!=ref['digest']:raise WorkflowError('task changed; refresh')
    prior=ws.read(conn,UUID(ref['run_id']),key)
    if (prior.digest if prior else None)!=expected_note_digest:raise WorkflowError('owner note changed; refresh')
    value={'task_digest':source['digest'],'choice':choice,'note':note,'recorded_at':instant().isoformat(),
        'source_workflow_changed':False,'external_actions':0,'principal':'wondrue-operator'}
    row=ws.write(conn,UUID(ref['run_id']),key,value,prior)
    return dict(value,note_digest=row.digest)
