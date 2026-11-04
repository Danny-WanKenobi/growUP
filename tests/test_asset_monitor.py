from datetime import datetime,timezone
from copy import deepcopy
import pytest
from app import asset_monitor as monitor
from app.workflow_db import WorkflowError
from app.handoff_demo_domain import scope
from tests.test_handoff_demo import KEY,Conn,Cursor
from tests.test_manny_review import ready

NOW=datetime(2026,11,5,18,tzinfo=timezone.utc)


def test_disabled_emergency_and_sabbath_do_not_connect(monkeypatch):
    monkeypatch.setattr(monitor,'bounded_connection',lambda:pytest.fail('must not connect'))
    monkeypatch.delenv('ASSET_MONITOR_ENABLED',raising=False)
    assert monitor.cycle(clock=lambda:NOW)['status']=='paused'
    monkeypatch.setenv('ASSET_MONITOR_ENABLED','1');monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1');monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','1')
    assert monitor.cycle(clock=lambda:NOW)['status']=='paused'
    monkeypatch.delenv('DOSSIER_COORDINATOR_EMERGENCY_STOP');monkeypatch.setenv('SABBATH_BLACKOUT','1')
    assert monitor.cycle(clock=lambda:datetime(2026,11,7,18,tzinfo=timezone.utc))['status']=='paused'


class MonitorCursor(Cursor):
    def execute(self,sql,params=None):
        if sql.startswith('INSERT INTO workflow_runs') and any(r['idempotency_key']==params['key'] for r in self._db['runs']): self._result=[]
        elif sql.startswith('SELECT pg_try_advisory_lock'): self._result=[dict(locked=self._db.get('monitor_lock',True))]
        elif sql.startswith('SELECT pg_advisory_unlock'): self._result=[dict(unlocked=True)]
        else: super().execute(sql,params)


class MonitorConn(Conn):
    def cursor(self): return MonitorCursor(self._db)
    def rollback(self): pass


def cycle_fixture(monkeypatch):
    conn,_=ready(monkeypatch);conn=MonitorConn(conn._db)
    monkeypatch.setenv('ASSET_MONITOR_ENABLED','1');monkeypatch.setenv('SABBATH_BLACKOUT','0')
    monkeypatch.setattr(monitor,'bounded_connection',lambda:conn)
    monkeypatch.setattr(monitor,'guard_database',lambda conn:None)
    return conn


def test_cycle_cap_cursor_resume_and_failure_fairness(monkeypatch):
    conn=cycle_fixture(monkeypatch);targets=[dict(run_id=__import__('uuid').uuid4(),record_key='d'+str(i)) for i in range(25)];seen=[];queries=[]
    def candidates(conn,cursor,limit):
        queries.append(cursor);assert limit==20
        start=0 if cursor==('','') else next(i+1 for i,t in enumerate(targets) if (str(t['run_id']),t['record_key'])==cursor)
        return targets[start:start+limit]
    monkeypatch.setattr(monitor,'candidates',candidates)
    def check(**kw):
        seen.append(kw['dossier_key'])
        if kw['dossier_key']=='d0': raise WorkflowError('sanitized blocker')
        return dict(status='already_checked')
    first=monitor.cycle(clock=lambda:NOW,check_fn=check);assert first['examined']==20 and first['outcomes']['blocked']==1
    second=monitor.cycle(clock=lambda:NOW,check_fn=check);assert second['examined']==5 and seen==['d'+str(i) for i in range(25)]
    assert queries[1]==(str(targets[19]['run_id']),'d19')


def test_overlap_and_deadline_stop_before_work(monkeypatch):
    conn=cycle_fixture(monkeypatch);conn._db['monitor_lock']=False
    assert monitor.cycle(clock=lambda:NOW)['status']=='overlap_skipped'
    conn._db['monitor_lock']=True
    monkeypatch.setattr(monitor,'candidates',lambda *a:[dict(run_id=__import__('uuid').uuid4(),record_key='one')])
    ticks=iter([0,61]);result=monitor.cycle(clock=lambda:NOW,monotonic=lambda:next(ticks),check_fn=lambda **kw:pytest.fail('deadline exceeded'))
    assert result['examined']==0


def test_synthetic_dossier_never_monitored(monkeypatch):
    conn,rid=ready(monkeypatch);monkeypatch.setenv('ASSET_MONITOR_ENABLED','1');monkeypatch.setenv('SABBATH_BLACKOUT','0')
    with scope(conn,KEY):
        before=deepcopy(conn._db)
        assert monitor.check_in_transaction(conn,run_id=rid,dossier_key='main',now=NOW)['status']=='synthetic_skipped'
        assert conn._db==before


def test_development_database_guard_rejects_other_targets(monkeypatch):
    conn,_=ready(monkeypatch)
    with pytest.raises(WorkflowError,match='development'): monitor.guard_database(conn)
