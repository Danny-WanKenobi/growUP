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
        for name, value in (('occupancy_usd_per_m2_year','10'), ('maintenance_usd_per_year','100')):
            payload['dossier']['assessment_inputs']['financial'][name] = {
                'value':value,'classification':'assumed','source':'fictional test input'}
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
        assert result['status'] == 'running'
        assert len(db['receipts']) == 6
        assert all(row['snapshot_id'] for row in db['receipts'])
        result = coordinator.step_in_transaction(conn, run_id=rid, dossier_key='main', principal='owner', expected_sequence=5)
        assert result['current_holder'] == 'theresia_keys'
        assert result['status'] == 'running'
        assert len(db['receipts']) == 7
        before = copy.deepcopy(db)
        replay = coordinator.step_in_transaction(conn, run_id=rid, dossier_key='main', principal='owner', expected_sequence=5)
        assert replay['replayed'] and db == before
        run = demo._get(conn,'demo-handoff-coordinator')
        from app.workflow_db import get_record_in_transaction
        dossier = get_record_in_transaction(conn,run.run_id,'dossier_version','main').payload['dossier']
        assert dossier['reports']['nicole']['regulatory_assessment']['regulatory_clearance'] is False
        result = coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=6)
        assert result['status']=='running' and result['current_holder']=='simeon'
        assert len(db['receipts'])==8
        dossier = get_record_in_transaction(conn,run.run_id,'dossier_version','main').payload['dossier']
        assessment=dossier['reports']['theresia_keys']['underwriting_assessment']
        assert assessment['status']=='conditional_preparation_only' and not assessment['contract_executable']
        assert assessment['contract_preparation']['conditions_precedent']
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=7)
        assert result['current_holder']=='solomon' and result['status']=='running'
        assert len(db['receipts'])==9
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=8)
        assert result['current_holder']=='solomon' and result['status']=='solomon_planning_review'
        assert len(db['receipts'])==9
        report=get_record_in_transaction(conn,rid,'dossier_version','main').payload['dossier']['reports']['solomon']
        assert not report['preconstruction_assessment']['construction_authorized']
        assert report['vendor_simulation']['external_deliveries']==0





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
@pytest.mark.parametrize("holder,gap", [("raphael",True),("nicole",False),("theresia_keys",False),("simeon",False),("solomon",True),("solomon",False)])
def test_live_coordinator_interruption_and_concurrent_replay(monkeypatch, holder, gap):
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
            if holder == 'nicole':
                from tests.test_nicole import inputs
                _, supplied = inputs()
                for name in ('jurisdiction','proposed_use'):
                    supplied[name]['value']['property_id'] = str(prop['id'])
                for row in supplied['official_requirements']['value']:
                    row['property_id'] = str(prop['id'])
                payload = copy.deepcopy(record.payload)
                payload['dossier']['specialist_inputs'] = {'nicole':supplied}
                reports = {}
                for name in ('raphael','onesimus'):
                    report = {'agent':name,'property_id':str(prop['id']),'scope':'disposable PG fixture only'}
                    report['digest'] = digest_payload(report)
                    reports[name] = report
                payload['dossier']['reports'] = reports
                payload = {k:v for k,v in payload.items() if k!='digest'}
                payload['digest'] = digest_payload(payload)
                record = put_record_in_transaction(conn,rid,kind='dossier_version',record_key=key,
                    version=record.version+1,payload=payload,digest=payload['digest'])
            if holder == 'theresia_keys':
                from tests.test_theresia import inputs
                _, supplied, reports = inputs()
                for entry in supplied.values():
                    entry['value']['property_id']=str(prop['id'])
                for report in reports.values():
                    report['property_id']=str(prop['id'])
                    report.pop('digest')
                    report['digest']=digest_payload(report)
                payload=copy.deepcopy(record.payload)
                payload['dossier']['specialist_inputs']={'theresia_keys':supplied}
                payload['dossier']['reports']=reports
                payload={k:v for k,v in payload.items() if k!='digest'}
                payload['digest']=digest_payload(payload)
                record=put_record_in_transaction(conn,rid,kind='dossier_version',record_key=key,
                    version=record.version+1,payload=payload,digest=payload['digest'])
            if holder in ('simeon','solomon'):
                if holder=='simeon':
                    from tests.test_simeon import inputs
                else:
                    from tests.test_solomon import inputs
                fixture_property,supplied,reports=inputs()
                def rebind(value):
                    if isinstance(value,dict): return {k:rebind(v) for k,v in value.items()}
                    if isinstance(value,list): return [rebind(v) for v in value]
                    return str(prop['id']) if value==fixture_property else value
                supplied,reports=rebind(supplied),rebind(reports)
                if holder=='simeon':
                    terms=reports['theresia_keys']['underwriting_assessment']['contract_preparation']['terms']
                    for name in ('counterparty_consent','approved_terms'):
                        supplied[name]['value']['terms_digest']=digest_payload(terms)
                for report in reports.values():
                    report.pop('digest'); report['digest']=digest_payload(report)
                if holder=='solomon':
                    supplied['approved_design']['value']['engineering_report_digest']=reports['onesimus']['digest']
                    if gap: supplied.pop('approved_design')
                payload=copy.deepcopy(record.payload)
                payload['dossier']['specialist_inputs']={holder:supplied}
                payload['dossier']['reports']=reports
                payload={k:v for k,v in payload.items() if k!='digest'}
                payload['digest']=digest_payload(payload)
                record=put_record_in_transaction(conn,rid,kind='dossier_version',record_key=key,
                    version=record.version+1,payload=payload,digest=payload['digest'])
            version = create_version_in_transaction(conn, property_id=prop['id'], created_by='gabriel',
                payload={'dossier':record.payload['dossier'],'dossier_digest':record.digest})
            did = version.dossier_id
            snapshot_in_transaction(conn, dossier_id=did, version=1)
            route = coordinator.ROUTE[:coordinator.ROUTE.index(holder)+1]
            initial_receipts = len(route)-1
            for sender, receiver in zip(route,route[1:]):
                issue_receipt_in_transaction(conn, dossier_id=did, version=1, kind='forward', from_agent=sender,
                    to_agent=receiver, issued_by='pg-test', note='disposable coordinator proof')
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
            assert verify_chain_in_transaction(conn,did) == initial_receipts
            assert get_record_in_transaction(conn,rid,'research_artifact','coordinator:'+key) is None
        barrier = Barrier(2)
        def attempt():
            barrier.wait(timeout=10)
            return coordinator.process(principal='pg-test',run_id=rid,dossier_key=key,expected_sequence=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:attempt(),range(2)))
        assert sum(bool(r.get('replayed')) for r in results) == 1
        expected='returned' if holder=='raphael' or (holder=='solomon' and gap) else 'solomon_planning_review' if holder=='solomon' else 'running'
        assert all(r['status']==expected and r['sequence']==1 for r in results)
        with get_connection() as conn:
            assert latest_version_in_transaction(conn,did).version == 2
            assert verify_chain_in_transaction(conn,did) == initial_receipts+(0 if holder=='solomon' and not gap else 1)
    finally:
        _pg_cleanup([str(rid)],[str(did)] if did else [],[str(prop['id'])])


def specialist_seed(monkeypatch, fail=False, agent="nicole"):
    """Use the existing isolated synthetic pipeline to hand custody to the requested specialist."""
    conn=Conn(_new_db())
    with scope(conn,KEY):
        monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
        demo.start_in_transaction(conn,run_key='demo-handoff-nicole-coordinator',fail_at=agent if fail else None)
        for sequence in range(demo.STAGES.index(agent)):
            demo.advance_in_transaction(conn,run_key='demo-handoff-nicole-coordinator',expected_sequence=sequence)
        run=demo._get(conn,'demo-handoff-nicole-coordinator')
    return conn,run.run_id


def test_nicole_gap_returns_to_priscilla_with_snapshot_and_owner_tasks(monkeypatch):
    from app.workflow_db import get_record_in_transaction,list_records_in_transaction
    conn,rid=specialist_seed(monkeypatch,fail=True)
    with scope(conn,KEY):
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=0)
        assert result['status']=='returned' and result['current_holder']=='priscilla'
        source=get_record_in_transaction(conn,rid,'dossier_version','main')
        assert source.payload['dossier']['reports']['nicole']['status']=='needs_evidence'
        tasks=list_records_in_transaction(conn,rid,'owner_review_task')
        assert tasks and all(not r.payload['grants_permission'] for r in tasks)
        assert conn._db['receipts'][-1]['snapshot_id']


def test_legacy_nicole_checkpoint_needs_explicit_resume_and_preserves_replay(monkeypatch):
    from app.workflow import digest_payload
    from app.workflow_db import get_record_in_transaction,put_record_in_transaction
    from app.custody import dossier_id_for_property
    conn,rid=specialist_seed(monkeypatch)
    with scope(conn,KEY):
        source=get_record_in_transaction(conn,rid,'dossier_version','main')
        legacy={'version':coordinator.LEGACY_VERSION,'sequence':5,'status':'delivered_to_nicole',
            'principal':'owner','dossier_id':str(dossier_id_for_property(source.payload['property_id'])),
            'dossier_key':'main','dossier_digest':source.digest}
        put_record_in_transaction(conn,rid,kind='research_artifact',record_key='coordinator:main',
            version=1,payload=legacy,digest=digest_payload(legacy))
        before=copy.deepcopy(conn._db)
        assert coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=4)['replayed']
        assert conn._db==before
        with pytest.raises(WorkflowError,match='explicit resume'):
            coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=5)
        resumed=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=5,action='resume')
        assert resumed['version']==coordinator.VERSION and resumed['sequence']==6
        assert len(conn._db['receipts'])==len(before['receipts'])
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=6)
        assert result['current_holder']=='theresia_keys'


def test_theresia_evidence_gap_returns_to_nicole_and_keeps_conditional_report(monkeypatch):
    from app.workflow_db import get_record_in_transaction,list_records_in_transaction
    conn,rid=specialist_seed(monkeypatch,fail=True,agent='theresia_keys')
    with scope(conn,KEY):
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=0)
        assert result['current_holder']=='nicole' and result['status']=='returned'
        report=get_record_in_transaction(conn,rid,'dossier_version','main').payload['dossier']['reports']['theresia_keys']
        assert report['status']=='needs_evidence' and not report['underwriting_assessment']['contract_executable']
        assert report['underwriting_assessment']['contract_preparation']['conditions_precedent']
        assert list_records_in_transaction(conn,rid,'owner_review_task')
        assert conn._db['receipts'][-1]['snapshot_id']


def test_version_two_theresia_delivery_needs_resume_without_moving_custody(monkeypatch):
    from app.workflow import digest_payload
    from app.workflow_db import get_record_in_transaction,put_record_in_transaction
    from app.custody import dossier_id_for_property
    conn,rid=specialist_seed(monkeypatch,agent='theresia_keys')
    with scope(conn,KEY):
        source=get_record_in_transaction(conn,rid,'dossier_version','main')
        legacy={'version':coordinator.PREVIOUS_VERSION,'sequence':6,'status':'delivered_to_theresia_keys',
            'principal':'owner','dossier_id':str(dossier_id_for_property(source.payload['property_id'])),
            'dossier_key':'main','dossier_digest':source.digest}
        put_record_in_transaction(conn,rid,kind='research_artifact',record_key='coordinator:main',version=1,payload=legacy,digest=digest_payload(legacy))
        before=copy.deepcopy(conn._db)
        with pytest.raises(WorkflowError,match='explicit resume'):
            coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=6)
        assert conn._db==before
        resumed=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=6,action='resume')
        assert resumed['version']==coordinator.VERSION and resumed['upgraded_from']==coordinator.PREVIOUS_VERSION
        assert len(conn._db['receipts'])==len(before['receipts'])
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=7)
        assert result['current_holder']=='simeon'
        before=copy.deepcopy(conn._db)
        assert coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=7)['replayed']
        assert conn._db==before


def test_solomon_evidence_gap_returns_to_simeon_with_snapshot(monkeypatch):
    from app.workflow_db import get_record_in_transaction,list_records_in_transaction
    conn,rid=specialist_seed(monkeypatch,fail=True,agent='solomon')
    with scope(conn,KEY):
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=0)
        assert result['status']=='returned' and result['current_holder']=='simeon'
        report=get_record_in_transaction(conn,rid,'dossier_version','main').payload['dossier']['reports']['solomon']
        assert report['status']=='needs_evidence' and not report['preconstruction_assessment']['construction_authorized']
        assert conn._db['receipts'][-1]['snapshot_id']
        assert list_records_in_transaction(conn,rid,'owner_review_task')


def test_version_three_simeon_requires_explicit_resume_before_planning(monkeypatch):
    from app.workflow import digest_payload
    from app.workflow_db import get_record_in_transaction,put_record_in_transaction
    from app.custody import dossier_id_for_property
    conn,rid=specialist_seed(monkeypatch,agent='simeon')
    with scope(conn,KEY):
        source=get_record_in_transaction(conn,rid,'dossier_version','main')
        legacy={'version':coordinator.THIRD_VERSION,'sequence':7,'status':'delivered_to_simeon',
            'principal':'owner','dossier_id':str(dossier_id_for_property(source.payload['property_id'])),
            'dossier_key':'main','dossier_digest':source.digest}
        put_record_in_transaction(conn,rid,kind='research_artifact',record_key='coordinator:main',version=1,payload=legacy,digest=digest_payload(legacy))
        with pytest.raises(WorkflowError,match='explicit resume'):
            coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=7)
        count=len(conn._db['receipts'])
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=7,action='resume')
        assert len(conn._db['receipts'])==count and result['version']==coordinator.VERSION
        result=coordinator.step_in_transaction(conn,run_id=rid,dossier_key='main',principal='owner',expected_sequence=8)
        assert result['current_holder']=='solomon' and result['status']=='running'
