"""Official-record bridge: retained discovery -> dossier -> Raphael."""
from __future__ import annotations
import json
from uuid import uuid4
import pytest
from tests.test_funnel_acceptance import _FakeConn, _FakeCursor, _new_db


class _Cursor(_FakeCursor):
    def execute(self, sql, params=None):
        p = dict(params or {})
        if sql.startswith("SELECT * FROM agent_events"):
            rows = [r for r in self._db.get("events", [])
                    if str(r.get("run_id")) == str(p.get("run", r.get("run_id", ""))) or "property_id" in p]
            if "event" in p:
                rows = [r for r in rows if str(r.get("event_id")) == str(p["event"]) and str(r.get("property_id")) == str(p["prop"])]
            elif "prop" in p:
                rows = [r for r in self._db.get("events", []) if str(r.get("property_id")) == str(p["prop"])]
            self._result = [dict(r) for r in rows]
        elif sql.startswith("SELECT * FROM properties"):
            row = self._db.get("property")
            self._result = [dict(row)] if row and str(row.get("id")) == str(p["id"]) else []
        else:
            super().execute(sql, params)


class _Conn(_FakeConn):
    def cursor(self):
        return _Cursor(self._db)


def _seed(monkeypatch, category="warehouse_industrial"):
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "official-test-key-0123456789")
    monkeypatch.delenv("DCAD_BULK_DATA_DIR", raising=False)
    from app.pilot import ensure_ledger_in_transaction
    db = _new_db(); db["events"] = []
    prop = uuid4(); run_id = uuid4()
    db["property"] = {"id": str(prop), "name": "801 Jackson St", "street_address": "801 Jackson St",
                      "city": "Dallas", "state": "TX", "zip_code": "75202"}
    details = {"outcome": "created", "mutation_status": "COMMITTED", "result": {"property_id": str(prop)},
               "observed": {"source": "dcad_bulk", "external_id": "dcad:00000100324000000",
                            "street_address": "801 Jackson St", "city": "Dallas", "state": "TX",
                            "zip_code": "75202", "category_hint": category,
                            "retrieved_at": "2026-11-02T00:00:00+00:00", "element_url": None, "name": None}}
    db["events"].append({"run_id": "disc-1", "event_id": "evt-1", "property_id": str(prop),
                         "event_type": "gabriel.discovery.decision", "agent": "gabriel",
                         "retention_class": "permanent", "details": details})
    db["runs"].append({"run_id": run_id, "idempotency_key": "off-1", "stage": "created",
                       "status": "open", "correlation_id": "", "limits": {}, "usage": {}, "errors": []})
    conn = _Conn(db); ensure_ledger_in_transaction(conn)
    return db, conn, run_id, prop


def _seed_generic(monkeypatch):
    db, conn, run_id, prop = _seed(monkeypatch)
    db["property"] = {"id": str(prop), "name": "1200 Congress Ave",
                      "street_address": "1200 Congress Ave", "city": "Austin",
                      "state": "TX", "zip_code": "78701"}
    db["events"][0]["details"]["observed"] = {
        "source": "generic_socrata", "external_id": "generic_socrata:TRAVIS-1",
        "street_address": "1200 Congress Ave", "city": "Austin", "state": "TX",
        "zip_code": "78701", "category_hint": "warehouse_industrial",
        "retrieved_at": "2026-11-02T00:00:00+00:00", "element_url": None, "name": None}
    db["events"][0]["details"]["structured"] = {
        "dataset_url": "https://data.travis.gov/resource/abc1.json",
        "platform": "socrata", "selection_digest": "sel-digest",
        "resource_digest": "res-digest",
        "platform_record": json.dumps({
            "account": "TRAVIS-1", "situs_address": "1200 Congress Ave",
            "land_use": "WAREHOUSE", "latitude": "30.27", "longitude": "-97.74"})}
    return db, conn, run_id, prop


def test_non_dallas_generic_to_raphael_needs_evidence_same_identity(monkeypatch):
    db, conn, run_id, prop = _seed_generic(monkeypatch)
    s = _eval(conn, run_id, prop)
    assert s["decision"] == "needs_evidence"
    assert s["current_holder"] == "gabriel" and s["feedback_reason"]
    assert s["dossier_id"] and s["official"]["property_id"] == str(prop)
    artifacts = [r for r in db["records"] if r["kind"] == "research_artifact"]
    assert artifacts and artifacts[0]["payload"]["collection_provenance"] == "generic_retained"
    assert artifacts[0]["payload"]["provider"] == "generic_socrata"
    dossiers = [r for r in db["records"] if r["kind"] == "dossier_version"]
    assert dossiers[0]["payload"]["candidate_verification"] is None
    assert dossiers[0]["payload"]["dossier"]["category_attribution"]["observed"] == \
        "warehouse_industrial"
    assert dossiers[0]["payload"]["dossier"]["county_attribution"]["evidence_status"] == \
        "unresolved"  # no fabricated Travis county credit
    recs_before = len(db["records"]); receipts_before = len(db["receipts"])
    s2 = _eval(conn, run_id, prop)
    assert s2["dossier_id"] == s["dossier_id"] and s2["decision"] == "needs_evidence"
    assert len(db["records"]) == recs_before and len(db["receipts"]) == receipts_before


def test_generic_conflicting_digest_refuses(monkeypatch):
    db, conn, run_id, prop = _seed_generic(monkeypatch)
    from app.agents.gabriel.sources.official_records import OfficialRecordError
    with pytest.raises(OfficialRecordError, match="digest"):
        _eval(conn, run_id, prop, digest="0" * 64)


def _eval(conn, run_id, prop, digest=None, **kwargs):
    from app import mvp
    ref = {"discovery_run_id": "disc-1", "event_id": "evt-1", "property_id": str(prop)}
    if digest: ref["evidence_digest"] = digest
    return mvp.evaluate_real(conn, run_id, "runner:x", "", property_id=str(prop),
                             official_ref=ref, stop_after_raphael=True, **kwargs)


def test_retained_official_to_raphael_needs_evidence_same_identity(monkeypatch):
    db, conn, run_id, prop = _seed(monkeypatch)
    s = _eval(conn, run_id, prop)
    assert s["decision"] == "needs_evidence"
    assert s["current_holder"] == "gabriel" and s["feedback_reason"]
    assert s["dossier_id"]
    assert s["official"]["discovery_run_id"] == "disc-1"
    recs_before = len(db["records"]); receipts_before = len(db["receipts"])
    s2 = _eval(conn, run_id, prop)
    assert s2["dossier_id"] == s["dossier_id"]
    assert s2["decision"] == "needs_evidence"
    assert s2["qualification"] == s["qualification"]
    assert len(db["records"]) == recs_before and len(db["receipts"]) == receipts_before


@pytest.mark.parametrize("cat", ["high_rise", "commercial_property", "warehouse_industrial"])
def test_all_three_categories_retained(monkeypatch, cat):
    db, conn, run_id, prop = _seed(monkeypatch, cat)
    s = _eval(conn, run_id, prop)
    assert s["decision"] == "needs_evidence"
    dossiers = [r for r in db["records"] if r["kind"] == "dossier_version"]
    assert dossiers and dossiers[0]["payload"]["candidate_verification"] is None
    assert dossiers[0]["payload"]["dossier"]["category_attribution"]["observed"] == cat


def test_conflicting_digest_refuses(monkeypatch):
    db, conn, run_id, prop = _seed(monkeypatch)
    from app.agents.gabriel.sources.official_records import OfficialRecordError
    with pytest.raises(OfficialRecordError, match="digest"):
        _eval(conn, run_id, prop, digest="0" * 64)


def test_conflicting_property_refuses(monkeypatch):
    db, conn, run_id, prop = _seed(monkeypatch)
    from app import mvp
    from app.workflow_db import WorkflowError
    with pytest.raises(WorkflowError, match="property"):
        mvp.evaluate_real(conn, run_id, "a", "", property_id=str(uuid4()),
                          official_ref={"discovery_run_id": "disc-1", "event_id": "evt-1", "property_id": str(prop)},
                          stop_after_raphael=True)


def test_readiness_names_unsupported_and_missing_dcad(monkeypatch):
    monkeypatch.delenv("DCAD_BULK_DATA_DIR", raising=False)
    from app.pilot import pilot_readiness
    r = pilot_readiness()
    assert r["unsupported_cells"] == 33 and r["supported_cells"] == 3
    assert r["missing_dcad_dir"] is True
    assert r["target_qualified"] == 144 and r["goal_claimed_met"] is False
    assert len(r["missing_adapters"]) == 11


def test_mixed_improvement_claims_stay_contradictory(monkeypatch):
    db, conn, run_id, prop = _seed(monkeypatch)
    db["events"][0]["details"]["structured"] = {"improvements": json.dumps([
        {"BLDG_CLASS_DESC": "STORAGE WAREHOUSE", "NUM_STORIES": "1"},
        {"BLDG_CLASS_DESC": "OFFICE BUILDING", "NUM_STORIES": "2"}])}
    summary = _eval(conn, run_id, prop)
    assert summary["decision"] == "rejected"
    dossier = next(r for r in db["records"] if r["kind"] == "dossier_version")["payload"]["dossier"]
    assert dossier["category_attribution"]["status"] == "contradictory"
    assert summary["current_holder"] == "gabriel"


def test_completed_official_replay_rejects_new_binding(monkeypatch):
    db, conn, run_id, prop = _seed(monkeypatch)
    _eval(conn, run_id, prop)
    from app.workflow_db import WorkflowError
    with pytest.raises(WorkflowError, match="digest"):
        _eval(conn, run_id, prop, digest="0" * 64)


def test_official_county_admission_uses_shared_policy_and_original_identity(monkeypatch):
    from app import mvp
    db, conn, run_id, prop = _seed(monkeypatch)
    binding = {"collection_run_id": str(uuid4())}
    calls = []
    def shared_policy(c, rid, dossiers, association, target, census):
        assert c is conn and rid == run_id
        assert association == {"main": str(prop)} and target == str(prop)
        assert census == binding and len(dossiers.dossiers) == 1
        calls.append(1)
    monkeypatch.setattr(mvp, "_apply_census_in_stage", shared_policy)
    _eval(conn, run_id, prop, census=binding)
    assert calls == [1]


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_official_lookup_cannot_fall_back_to_brave(monkeypatch, lookup_fails):
    from types import SimpleNamespace
    from app.discovery_runner import evaluate_property
    from app.agents.gabriel.sources import official_records
    prop = uuid4()
    monkeypatch.setattr("app.mvp.workflow_enabled", lambda: True)
    monkeypatch.setattr("app.properties_db.get_property", lambda *a: {"id": str(prop),
                        "street_address": "1 Test St", "city": "Dallas", "state": "TX"})
    monkeypatch.setattr("app.properties_db.get_connection", lambda: SimpleNamespace(close=lambda: None))
    def lookup(*a, **k):
        if lookup_fails: raise RuntimeError("test database unavailable")
        return None
    monkeypatch.setattr(official_records, "find_retained_official_in_transaction", lookup)
    monkeypatch.setattr("app.mvp.start_evaluation_audited", lambda *a, **k: pytest.fail("no fallback acquisition"))
    result = evaluate_property("test", str(prop), approved=True, timeout_seconds=12, official_ref="auto")
    assert result["outcome"] == "blocked" and result["brave_attempts"] == 0
