import copy
import pytest
from app import dossier_coordinator as coordinator
from app import handoff_demo as demo
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError
from tests.test_handoff_demo import Conn, KEY
from tests.test_funnel_acceptance import _new_db


def seeded(monkeypatch, fail_at=None):
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED', '1')
    db = _new_db(); conn = Conn(db)
    demo.start_in_transaction(conn, run_key='demo-handoff-coordinator', fail_at=fail_at)
    demo.advance_in_transaction(conn, run_key='demo-handoff-coordinator', expected_sequence=0)
    run = demo._get(conn, 'demo-handoff-coordinator')
    return db, conn, run.run_id


def test_real_policy_returns_to_gabriel_and_replay_is_stable(monkeypatch):
    conn = Conn(_new_db())
    with scope(conn, KEY):
        db, conn, rid = seeded(monkeypatch, fail_at="raphael")
    with scope(conn, KEY):
        result = coordinator.step_in_transaction(conn, run_id=rid, dossier_key='main', principal='owner', expected_sequence=0)
        assert result['current_holder'] == 'gabriel'
        assert result['status'] == 'returned'
        before = copy.deepcopy(db)
        replay = coordinator.step_in_transaction(conn, run_id=rid, dossier_key='main', principal='owner', expected_sequence=0)
        assert replay['replayed'] and db == before
        with pytest.raises(WorkflowError, match='identity/configuration'):
            coordinator.step_in_transaction(conn, run_id=rid, dossier_key='main', principal='foreign', expected_sequence=1)


def test_stop_resume_and_emergency_stop_do_not_release_custody(monkeypatch):
    conn = Conn(_new_db())
    with scope(conn, KEY):
        db, conn, rid = seeded(monkeypatch)
    with scope(conn, KEY):
        def call(seq, action='step'):
            return coordinator.step_in_transaction(conn, run_id=rid, dossier_key='main', principal='owner', expected_sequence=seq, action=action)
        receipts = len(db['receipts'])
        assert call(0, 'stop')['status'] == 'stopped'
        assert call(1)['replayed']
        assert call(1, 'resume')['status'] == 'running'
        monkeypatch.setenv('DOSSIER_COORDINATOR_EMERGENCY_STOP', '1')
        assert call(2)['status'] == 'stopped'
        assert len(db['receipts']) == receipts


def test_whole_route_and_upstream_gap(monkeypatch):
    original = demo._fixture
    def fixture(key, fail_at):
        payload = original(key, fail_at)
        payload['dossier']['assessment_inputs'] = {
            'site': {k: v.model_dump(mode='json') for k, v in demo._site().items()},
            'financial': {k: v.model_dump(mode='json') for k, v in demo._demo_financial_inputs('bench').items()}}
        return demo.sealed({k:v for k,v in payload.items() if k!='digest'})
    monkeypatch.setattr(demo, '_fixture', fixture)
    conn = Conn(_new_db())
    with scope(conn, KEY):
        db, conn, rid = seeded(monkeypatch)
    with scope(conn, KEY):
        holders = []
        for sequence in range(5):
            result = coordinator.step_in_transaction(conn, run_id=rid, dossier_key='main', principal='owner', expected_sequence=sequence)
            holders.append(result['current_holder'])
        assert holders == ['onesimus', 'michael', 'uriel', 'priscilla', 'nicole']
        assert result['status'] == 'delivered_to_nicole'
        assert len(db['receipts']) == 6
        assert all(row['snapshot_id'] for row in db['receipts'])


def test_wondrue_refuses_read_only_before_dispatch(monkeypatch):
    from app.wondrue import handle_command, WondrueError
    monkeypatch.setattr(coordinator, 'process', lambda **kw: pytest.fail('must not dispatch'))
    with pytest.raises(WondrueError, match='read-only'):
        handle_command(mode='read-only', max_mode='full', target='real', text='',
            intent_override={'intent':'coordinate_dossier','args':{'run_id':'unused','expected_sequence':0}})


def test_audit_failure_rolls_back_report_receipt_and_checkpoint(monkeypatch):
    from app import properties_db
    from tests.test_handoff_demo import Cursor
    db = _new_db(); initial_conn = Conn(db)
    with scope(initial_conn, KEY):
        db, initial_conn, rid = seeded(monkeypatch, fail_at='raphael')
    before = copy.deepcopy(db)
    class FailingCursor(Cursor):
        def execute(self, sql, params=None):
            if sql.startswith('INSERT INTO agent_events'):
                raise RuntimeError('simulated audit interruption')
            return super().execute(sql, params)
    class TransactionConn(Conn):
        def cursor(self): return FailingCursor(self._db)
        def rollback(self):
            self._db.clear(); self._db.update(copy.deepcopy(before))
        def commit(self): pass
        def close(self): pass
    conn = TransactionConn(db)
    monkeypatch.setattr(properties_db, 'get_connection', lambda *a, **k: conn)
    # The transaction module imports its connection factory directly.
    from app.observability import transactions
    monkeypatch.setattr(transactions, 'get_connection', lambda *a, **k: conn)
    with scope(conn, KEY):
        with pytest.raises(Exception, match='event_insert_failed'):
            coordinator.process(principal='owner', run_id=rid, dossier_key='main', expected_sequence=0)
    assert db == before


def test_coordinator_approval_is_bound_to_operator(monkeypatch):
    from app.wondrue import handle_command, WondrueAuthError
    intent = {'intent':'coordinate_dossier','args':{'run_id':'unused','expected_sequence':0}}
    preview = handle_command(mode='ask', max_mode='full', target='real', text='', principal='owner', intent_override=intent)
    with pytest.raises(WondrueAuthError, match='different operator'):
        handle_command(mode='ask', max_mode='full', target='real', text='', principal='foreign',
            intent_override=intent, approval_token=preview['approval_token'])
