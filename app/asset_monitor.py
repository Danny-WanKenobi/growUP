"""Bounded automatic due-date checks. No discovery, provider or messaging code."""
from datetime import datetime,timezone
import json
import os
import signal
import time
from uuid import UUID
from zoneinfo import ZoneInfo
from app import asset_tracking,manny_review
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction,put_record_in_transaction,create_run_in_transaction
from app.properties_db import get_connection

CONTROL='manny-asset-monitor-1'
LOCK='growup:manny:asset-monitor'
MAX_DOSSIERS=20
MAX_SECONDS=60
_INTERRUPTED=False


def stopped(now):
    from app.discovery_runner import _sabbath_blackout
    return (_INTERRUPTED or os.getenv('ASSET_MONITOR_ENABLED','0')!='1' or os.getenv('MVP_WORKFLOW_ENABLED','0')!='1'
            or os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1' or _sabbath_blackout(now))


def guard_database(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT current_database() AS db, current_user AS role')
        row=cur.fetchone()
    if not row or row['db']!='growup_dev' or row['role']!='growup_app': raise WorkflowError('automatic monitor requires development growup_dev/growup_app')


def bounded_connection():
    conn=get_connection()
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '5000ms'")
        cur.execute("SET lock_timeout = '1000ms'")
    conn.commit()
    return conn


def check_in_transaction(conn,*,run_id,dossier_key,now):
    """One locked, daily/source-bound check. Caller owns audit and commit."""
    if now.tzinfo is None: raise WorkflowError('aware monitoring clock required')
    if stopped(now): return dict(status='paused',external_actions=0)
    source,did=manny_review._locked(conn,run_id,dossier_key)
    if source.payload.get('synthetic') is True or source.payload.get('domain')=='synthetic_handoff_demo': return dict(status='synthetic_skipped',external_actions=0)
    from app.custody_db import current_custody_in_transaction
    holder=current_custody_in_transaction(conn,did)
    if not holder or holder['holder']!='manny': return dict(status='custody_skipped',external_actions=0)
    dossier=source.payload['dossier']
    if not dossier.get(asset_tracking.SECTION): return dict(status='untracked_skipped',external_actions=0)
    as_of=now.astimezone(ZoneInfo('America/Chicago')).date().isoformat()
    state=asset_tracking.project(dossier,property_id=source.payload['property_id'],as_of=as_of)
    status_key='asset-monitor-status:'+dossier_key
    previous=get_record_in_transaction(conn,run_id,'research_artifact',status_key)
    if previous and previous.digest!=digest_payload(previous.payload): raise WorkflowError('automatic monitoring status integrity failure')
    if previous and previous.payload['as_of']>as_of: raise WorkflowError('automatic monitoring clock moved backwards')
    if previous and previous.payload['as_of']==as_of and previous.payload['asset_event_digest']==state['latest_event_digest']:
        return dict(status='already_checked',external_actions=0)
    operation='auto-asset-'+digest_payload(dict(as_of=as_of,asset_event_digest=state['latest_event_digest']))
    result=asset_tracking.mutate_in_transaction(conn,principal='asset-monitor',run_id=run_id,dossier_key=dossier_key,operation_id=operation,
        expected_digest=source.digest,as_of=as_of,operator_authorized=True,brief_policy='on_exception')
    status=dict(schema=CONTROL,as_of=as_of,checked_at=now.astimezone(timezone.utc).isoformat(),asset_event_digest=state['latest_event_digest'],
        source_dossier_digest=source.digest,checked_state=result['asset_tracking'],created_task_keys=result['created_task_keys'],external_actions=0)
    put_record_in_transaction(conn,run_id,kind='research_artifact',record_key=status_key,version=previous.version+1 if previous else 1,payload=status,digest=digest_payload(status))
    return dict(status='checked',created_tasks=len(result['created_task_keys']),external_actions=0)


def check(*,run_id,dossier_key,now):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    # The same bounded connection is used for the guard, mutation and permanent audit.
    conn=bounded_connection()
    try:
        guard_database(conn)
        result=check_in_transaction(conn,run_id=run_id,dossier_key=dossier_key,now=now)
        if result['status']=='checked':
            from app.observability.store import insert_event_in_transaction
            event=AgentEvent(agent=AgentId.MANNY,run_id=str(run_id),event_type='manny.automatic_monitor',summary='Bounded internal due-date check',
                details={'status':'checked','created_tasks':result['created_tasks']},retention_class=RetentionClass.PERMANENT)
            insert_event_in_transaction(conn,event)
        conn.commit();return result
    except BaseException:
        conn.rollback();raise
    finally: conn.close()


def candidates(conn,cursor,limit):
    with conn.cursor() as cur:
        cur.execute("""SELECT run_id,record_key FROM (
          SELECT DISTINCT ON (run_id,record_key) run_id,record_key,payload,version
          FROM workflow_records WHERE kind='dossier_version'
          ORDER BY run_id,record_key,version DESC
        ) latest WHERE CASE WHEN jsonb_typeof(payload->'dossier'->'owner_asset_events')='array' THEN jsonb_array_length(payload->'dossier'->'owner_asset_events') ELSE 0 END > 0
          AND payload->>'synthetic' IS DISTINCT FROM 'true'
          AND payload->>'domain' IS DISTINCT FROM 'synthetic_handoff_demo'
          AND (run_id::text,record_key) > (%(run)s,%(key)s)
          ORDER BY run_id::text,record_key LIMIT %(limit)s""",dict(run=cursor[0],key=cursor[1],limit=limit))
        return [dict(row) for row in cur.fetchall()]


def cycle(*,clock=None,monotonic=None,check_fn=None):
    clock=clock or (lambda:datetime.now(timezone.utc));monotonic=monotonic or time.monotonic;check_fn=check_fn or check
    started=monotonic();now=clock()
    if stopped(now): return dict(status='paused',examined=0,external_actions=0)
    conn=bounded_connection();locked=False
    try:
        guard_database(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(hashtext(%(key)s)) AS locked',dict(key=LOCK));locked=cur.fetchone()['locked']
        conn.commit()
        if not locked: return dict(status='overlap_skipped',examined=0,external_actions=0)
        control=create_run_in_transaction(conn,idempotency_key=CONTROL,limits=dict(provider_calls=0,external_actions=0));conn.commit()
        row=get_record_in_transaction(conn,control.run_id,'research_artifact','cursor')
        if row and row.digest!=digest_payload(row.payload): raise WorkflowError('monitor cursor integrity failure')
        cursor=(row.payload['run_id'],row.payload['dossier_key']) if row else ('','')
        batch=candidates(conn,cursor,MAX_DOSSIERS)
        if not batch and cursor!=('',''): batch=candidates(conn,('',''),MAX_DOSSIERS)
        conn.commit();counts={};examined=0
        for target in batch:
            if monotonic()-started>=MAX_SECONDS or stopped(clock()): break
            try: outcome=check_fn(run_id=UUID(str(target['run_id'])),dossier_key=target['record_key'],now=clock())['status']
            except (KeyboardInterrupt,SystemExit): raise
            except Exception: outcome='blocked'  # Never log credentials, record payloads or exception text.
            counts[outcome]=counts.get(outcome,0)+1;examined+=1
            payload=dict(run_id=str(target['run_id']),dossier_key=target['record_key'],last_cycle_at=clock().isoformat(),outcome=outcome)
            row=put_record_in_transaction(conn,control.run_id,kind='research_artifact',record_key='cursor',version=row.version+1 if row else 1,payload=payload,digest=digest_payload(payload));conn.commit()
        return dict(status='complete',examined=examined,outcomes=counts,external_actions=0)
    except BaseException:
        conn.rollback();raise
    finally:
        if locked:
            try:
                with conn.cursor() as cur: cur.execute('SELECT pg_advisory_unlock(hashtext(%(key)s))',dict(key=LOCK))
                conn.commit()
            except Exception: pass
        conn.close()


def main():
    def interrupt(_signum,_frame):
        global _INTERRUPTED
        _INTERRUPTED=True
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupt)
    signal.signal(signal.SIGINT,interrupt)
    try:
        result=cycle();print(json.dumps(result,sort_keys=True))
        if result.get('outcomes',{}).get('blocked'): raise SystemExit(1)
    except KeyboardInterrupt: print('{"status":"interrupted","external_actions":0}');raise SystemExit(130)
    except Exception: print('{"status":"blocked","external_actions":0}');raise SystemExit(1)


if __name__=='__main__': main()
