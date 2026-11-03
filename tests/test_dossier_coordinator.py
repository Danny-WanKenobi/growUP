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

@pytest.mark.skipif(__import__('os').getenv('GROWUP_PG_INTEGRATION') != '1', reason='PostgreSQL integration explicitly disabled')
def test_live_coordinator_interruption_and_concurrent_replay(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.properties_db import create_property, get_connection
    from app.workflow import digest_payload
    from app.workflow_db import get_record_in_transaction, put_record_in_transaction
    from app.custody_db import create_version_in_transaction, snapshot_in_transaction, issue_receipt_in_transaction, verify_chain_in_transaction, latest_version_in_transaction
    from tests.test_dcad_admission import _require_gate_database, _pg_env, _pg_seed_parent, _pg_cleanup
    _require_gate_database(); _pg_env(monkeypatch)
    prop = create_property({'name':'Coordinator disposable PG recovery proof', 'street_address':'2323 Chalk Hill Rd',
        'city':'Dallas','state':'TX','zip_code':'75212','category':'commercial_property'})
    parent = _pg_seed_parent(str(prop['id']))
    from uuid import UUID
    rid = UUID(parent['run_id']); key = parent['dossier_key']; did = None
    try:
        with get_connection() as conn:
            record = get_record_in_transaction(conn, rid, 'dossier_version', key)
            version = create_version_in_transaction(conn, property_id=prop['id'], created_by='gabriel',
                payload={'dossier':record.payload['dossier'],'dossier_digest':record.digest})
            did = version.dossier_id
            snapshot_in_transaction(conn, dossier_id=did, version=1)
            issue_receipt_in_transaction(conn, dossier_id=did, version=1, kind='forward', from_agent='gabriel',
                to_agent='raphael', issued_by='pg-test', note='disposable coordinator proof')
            link = {'dossier_id':str(did),'custody_version':1,'property_id':str(prop['id']),'dossier_key':key,'receipts':{}}
            put_record_in_transaction(conn,rid,kind='custody_link',record_key=key,version=1,payload=link,digest=digest_payload(link))
            conn.commit()
        original = coordinator.append_agent_report_in_transaction
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt('controlled interruption after report and snapshot')
        monkeypatch.setattr(coordinator,'append_agent_report_in_transaction',interrupted)
        with pytest.raises(KeyboardInterrupt):
            coordinator.process(principal='pg-test',run_id=rid,dossier_key=key,expected_sequence=0)
        monkeypatch.setattr(coordinator,'append_agent_report_in_transaction',original)
        with get_connection() as conn:
            assert latest_version_in_transaction(conn,did).version == 1
            assert verify_chain_in_transaction(conn,did) == 1
            assert get_record_in_transaction(conn,rid,'research_artifact','coordinator:'+key) is None
        barrier = Barrier(2)
        def attempt():
            barrier.wait(timeout=10)
            return coordinator.process(principal='pg-test',run_id=rid,dossier_key=key,expected_sequence=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:attempt(),range(2)))
        assert sum(bool(r.get('replayed')) for r in results) == 1
        assert all(r['status']=='returned' and r['sequence']==1 for r in results)
        with get_connection() as conn:
            assert latest_version_in_transaction(conn,did).version == 2
            assert verify_chain_in_transaction(conn,did) == 2
    finally:
        _pg_cleanup([str(rid)],[str(did)] if did else [],[str(prop['id'])])
