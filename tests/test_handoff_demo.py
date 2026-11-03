"""Synthetic-only canonical custody proofs; never reaches an operational DB."""
import copy
import json
from pathlib import Path
import pytest
from app import handoff_demo as demo
from app.handoff_demo_domain import scope, guard_payload, DATABASE, ROLE
from app.custody import hmac_key, CustodyTamperError
from tests.test_retained_assessment import Cursor as BaseCursor, Conn as BaseConn
from tests.test_funnel_acceptance import _new_db


class Cursor(BaseCursor):
    def execute(self, sql, params=None):
        if sql.startswith("SELECT current_database()"):
            self._result=[{"db":self._db.get("database",DATABASE),"role":self._db.get("role",ROLE)}]
        else:
            super().execute(sql,params)


class Conn(BaseConn):
    def cursor(self):
        return Cursor(self._db)


KEY = "synthetic-test-key-distinct-0123456789"


def drive(conn, run_key, fail_at=None):
    result=demo.start_in_transaction(conn,run_key=run_key,fail_at=fail_at)
    for _ in range(demo.MAX_STEPS):
        if result["phase"] in ("complete_go","returned_to_gabriel"):
            return result
        result=demo.advance_in_transaction(conn,run_key=run_key,expected_sequence=result["sequence"])
    pytest.fail("bounded demonstration did not terminate")


def test_all_checkpoints_go_and_each_nogo_return_to_gabriel(monkeypatch):
    # Synthetic requests must not accidentally use a transport.
    import httpx
    monkeypatch.setattr(httpx.Client,"send",lambda *a,**k:pytest.fail("provider/network call forbidden"))
    db=_new_db(); conn=Conn(db)
    results=[]
    with scope(conn,KEY):
        success=drive(conn,"demo-handoff-all-go")
        assert success["current_holder"]=="dan"
        assert success["phase"]=="complete_go"
        assert [e["agent"] for e in success["events"]]==list(demo.STAGES)
        assert all(e["checkpoint"]=="GO" for e in success["events"])
        assert success["verified_receipts"]==len(demo.STAGES)-1
        results.append(success)
        for index,agent in enumerate(demo.STAGES):
            outcome=drive(conn,"demo-handoff-nogo-"+agent,agent)
            assert outcome["phase"]=="returned_to_gabriel"
            assert outcome["current_holder"]=="gabriel"
            failed=[e for e in outcome["events"] if e.get("checkpoint")=="NO-GO"]
            assert len(failed)==1 and failed[0]["agent"]==agent
            returns=[e for e in outcome["events"] if e["phase"]=="returning"]
            assert [e["agent"] for e in returns]==list(reversed(demo.STAGES[1:index+1]))
            assert outcome["verified_receipts"]==2*index
            assert all(e.get("reason")==failed[0]["reason"] for e in returns)
            results.append(outcome)
    assert len({r["dossier_id"] for r in results})==len(results)
    assert not db["packages"] and not db["conversions"] and not db["pilot_events"]
    for row in db["versions"]:
        assert row["payload"]["synthetic"] and row["payload"]["dossier"]["synthetic"]
    path=Path("tmp/handoff-demo-proof.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"synthetic":True,"proof_backend":"canonical DAO with offline SQL double; PostgreSQL validation pending",
        "scenarios":len(results),"results":[{k:v for k,v in r.items() if k!="dossier"} for r in results]},indent=2),encoding="utf-8")


@pytest.mark.parametrize("database,role", [("growup_dev",ROLE),("growup_prod",ROLE),(DATABASE,"growup_app")])
def test_wrong_database_or_role_refuses_before_write(database,role):
    db=_new_db(); db.update(database=database,role=role); conn=Conn(db)
    with pytest.raises(ValueError,match="operational databases are forbidden"):
        with scope(conn,KEY):
            demo.start_in_transaction(conn,run_key="demo-handoff-blocked")
    assert not db["runs"] and not db["versions"]


def test_demo_scope_restores_operational_signing_key(monkeypatch):
    monkeypatch.setenv("CUSTODY_HMAC_KEY","operational-key-not-used-by-demo")
    conn=Conn(_new_db())
    original=hmac_key()
    with pytest.raises(RuntimeError):
        with scope(conn,KEY):
            assert hmac_key()!=original
            raise RuntimeError("interruption")
    assert hmac_key()==original
    with pytest.raises(ValueError,match="explicit isolated demo scope"):
        guard_payload(conn,{"synthetic":True})


def test_replay_sequence_and_tampering(monkeypatch):
    db=_new_db(); conn=Conn(db)
    with scope(conn,KEY):
        demo.start_in_transaction(conn,run_key="demo-handoff-replay")
        first=demo.advance_in_transaction(conn,run_key="demo-handoff-replay",expected_sequence=0)
        before=copy.deepcopy(db)
        assert demo.advance_in_transaction(conn,run_key="demo-handoff-replay",expected_sequence=0)==first
        assert demo.start_in_transaction(conn,run_key="demo-handoff-replay")==first
        assert db==before
        with pytest.raises(demo.WorkflowError,match="ahead"):
            demo.advance_in_transaction(conn,run_key="demo-handoff-replay",expected_sequence=99)
        db["receipts"][0]["mac"]="0"*64
        before=copy.deepcopy(db)
        with pytest.raises(CustodyTamperError):
            demo.advance_in_transaction(conn,run_key="demo-handoff-replay",expected_sequence=1)
        assert db==before


def test_no_operational_configuration_fallback(monkeypatch):
    monkeypatch.setenv("HANDOFF_DEMO_ENABLED","1")
    monkeypatch.setenv("DATABASE_NAME",DATABASE)
    monkeypatch.delenv("HANDOFF_DEMO_DATABASE_NAME",raising=False)
    with pytest.raises(demo.WorkflowError,match="no operational credential fallback"):
        demo.configuration()


def test_operational_mutators_reject_synthetic_before_write():
    from app.custody_db import create_version_in_transaction
    from uuid import uuid4
    db=_new_db(); db["database"]="growup_dev"; conn=Conn(db)
    with pytest.raises(ValueError,match="operational databases are forbidden"):
        create_version_in_transaction(conn,property_id=uuid4(),payload={"dossier":{"synthetic":True}},created_by="gabriel")
    assert not db["versions"]


def test_display_refuses_workflow_dossier_tampering():
    from app.workflow import digest_payload
    db=_new_db(); conn=Conn(db)
    with scope(conn,KEY):
        demo.start_in_transaction(conn,run_key="demo-handoff-display")
        row=next(r for r in db["records"] if r["kind"]=="dossier_version")
        row["payload"]["dossier"]["name"]="tampered"
        row["payload"]["digest"]=digest_payload({k:v for k,v in row["payload"].items() if k!="digest"})
        row["digest"]=row["payload"]["digest"]
        with pytest.raises(demo.WorkflowError,match="current canonical dossier"):
            demo.inspect_in_transaction(conn,run_key="demo-handoff-display")


def test_wondrue_demo_bounds_and_actor_approval(monkeypatch):
    from app.wondrue import handle_command, WondrueError, WondrueAuthError
    calls=[]
    monkeypatch.setattr(demo,"service",lambda *a,**kw:calls.append((a,kw)) or {"synthetic":True})
    intent={"intent":"start_handoff_demo","args":{"run_key":"demo-handoff-ui"}}
    with pytest.raises(WondrueError,match="read-only"):
        handle_command(mode="read-only",max_mode="full",text="",target="demo",intent_override=intent)
    with pytest.raises(WondrueError,match="synthetic target"):
        handle_command(mode="full",max_mode="full",text="",target="real",intent_override=intent)
    with pytest.raises(WondrueError,match="live inference disabled"):
        handle_command(mode="full",max_mode="full",text="",target="demo",use_live=True,intent_override=intent)
    preview=handle_command(mode="ask",max_mode="full",text="",target="demo",principal="owner",intent_override=intent)
    assert preview["outcome"]=="preview" and not calls
    with pytest.raises(WondrueAuthError,match="different operator"):
        handle_command(mode="ask",max_mode="full",text="",target="demo",principal="other",intent_override=intent,
                       approval_token=preview["approval_token"])
    assert not calls
    result=handle_command(mode="full",max_mode="full",text="",target="demo",principal="owner",intent_override=intent)
    assert result["data"]["synthetic"] is True and calls[0][1]["principal"]=="owner"


def test_viewer_shell_contains_no_credentials():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response=client.get("/wondrue/handoff-demo")
    assert response.status_code==200
    assert "SYNTHETIC DATA" in response.text
    assert "localStorage" not in response.text
    assert 'type="password"' in response.text
    assert all('NO-GO at '+name in response.text for name in demo.STAGES)


def test_postgresql_suite_orchestration_is_bounded(monkeypatch):
    db=_new_db(); conn=Conn(db)
    def local_service(action,**kwargs):
        with scope(conn,KEY):
            if action=="start": return demo.start_in_transaction(conn,run_key=kwargs["run_key"],fail_at=kwargs.get("fail_at"))
            return demo.advance_in_transaction(conn,run_key=kwargs["run_key"],expected_sequence=kwargs["expected_sequence"])
    monkeypatch.setattr(demo,"service",local_service)
    result=demo.run_suite("demo-handoff-suite")
    assert result["result"]=="PASS" and result["scenarios"]==15
    assert sum(r["verified_receipts"] for r in result["results"])==195


def test_initializer_uses_explicit_demo_migration_configuration(monkeypatch):
    from app.config import DatabaseConfig
    from app import migrate, properties_db
    cfg=DatabaseConfig(host="127.0.0.1",port=5432,name=DATABASE,user=ROLE,password="fictional-test-only")
    calls=[]
    class MigrationCursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,*args): pass
        def fetchall(self): return []
    class MigrationConnection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def cursor(self): return MigrationCursor()
        def commit(self): pass
        def close(self): pass
    def explicit_connection(config=None):
        assert config is cfg
        calls.append(config)
        return MigrationConnection()
    monkeypatch.setattr(demo,"configuration",lambda:(cfg,KEY))
    monkeypatch.setattr(demo,"assert_database",lambda conn:None)
    monkeypatch.setattr(properties_db,"get_connection",explicit_connection)
    monkeypatch.setattr(migrate,"get_connection",explicit_connection)
    monkeypatch.setattr(migrate,"get_database_config",lambda:pytest.fail("operational configuration fallback"))
    monkeypatch.setattr(migrate,"_pending_migrations",lambda applied:[])
    assert demo.initialize()["applied"]==[]
    assert len(calls)==2


def test_setup_failure_diagnostics_redact_before_truncation(monkeypatch,tmp_path,capsys):
    import importlib.util
    import subprocess
    spec=importlib.util.spec_from_file_location("demo_setup",Path("deployment/setup-handoff-demo.py"))
    setup=importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)
    env=tmp_path/"demo.env"; env.write_text("test-only",encoding="utf-8")
    monkeypatch.setattr(setup,"ENV",env)
    password="fictional-password-to-redact-0123456789"
    signing="fictional-signing-key-to-redact-9876543210"
    monkeypatch.setattr(setup,"read_private",lambda:{"HANDOFF_DEMO_DATABASE_PASSWORD":password,"HANDOFF_DEMO_HMAC_KEY":signing})
    def fail(*args,**kwargs):
        raise subprocess.CalledProcessError(1,["systemd-run"],output=password+"x"*5980+signing,stderr="synthetic failure")
    monkeypatch.setattr(setup.subprocess,"run",fail)
    with pytest.raises(subprocess.CalledProcessError): setup.command(["systemd-run"])
    output=capsys.readouterr().out
    assert password not in output and signing not in output
    assert "Failed setup component" in output and "[REDACTED]" in output
