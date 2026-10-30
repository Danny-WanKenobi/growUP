"""Gabriel finalize: artifacts, dossier versions, qualifications (no PG)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.agents.gabriel.persist import (
    persist_dossier_versions,
    persist_qualifications,
    persist_research_artifacts,
    qualify_dossier,
)
from app.workflow_db import WorkflowError


class _FakeCursor:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self._script:
            return None
        item = self._script.pop(0)
        if item == "ECHO_INSERT":
            import json as _json
            from uuid import uuid4 as _uuid4

            params = self.executed[-1][1]
            payload = params.get("payload")
            if isinstance(payload, str):
                payload = _json.loads(payload)
            return {
                "record_id": _uuid4(),
                "run_id": params.get("run"),
                "kind": params.get("kind"),
                "record_key": params.get("key"),
                "version": params.get("version", 1),
                "payload": payload,
                "digest": params.get("digest"),
            }
        return item

    def fetchall(self) -> Any:
        return self._script.pop(0) if self._script else []


class _FakeConn:
    def __init__(self, script: list[Any]) -> None:
        self._cursor = _FakeCursor(script)

    def cursor(self) -> _FakeCursor:
        return self._cursor


def _dossier(**overrides: Any) -> dict[str, Any]:
    dossier = {
        "identity_disputes": {},
        "conflicts": [],
        "category_attribution": {"status": "observed"},
        "county_attribution": {"evidence_status": "observed"},
        "sources": ["https://broker-a.example/x"],
    }
    dossier.update(overrides)
    return dossier


def test_qualify_clean_dossier() -> None:
    result = qualify_dossier(_dossier())
    assert result["outcome"] == "qualified"
    assert result["county_credit"] == ""  # unknown county: no cell credit
    assert result["evidence_refs"] == ["https://broker-a.example/x"]


def test_qualify_verified_county_earns_credit() -> None:
    result = qualify_dossier(
        _dossier(county_attribution={"evidence_status": "verified", "value": "Dallas County"})
    )
    assert result["outcome"] == "qualified"
    assert result["county_credit"] == "Dallas County"


def test_qualify_disputes_and_conflicts_need_research() -> None:
    result = qualify_dossier(
        _dossier(identity_disputes={"name": ["A", "B"]}, conflicts=[{"topic": "price"}])
    )
    assert result["outcome"] == "needs_research"
    assert len(result["reasons"]) == 2


def test_qualify_unknown_category_needs_research() -> None:
    result = qualify_dossier(_dossier(category_attribution={"status": "unknown"}))
    assert result["outcome"] == "needs_research"


def test_qualify_contradictory_category_not_qualified() -> None:
    result = qualify_dossier(_dossier(category_attribution={"status": "contradictory"}))
    assert result["outcome"] == "not_qualified"


def _research_result() -> SimpleNamespace:
    return SimpleNamespace(
        executed=[
            SimpleNamespace(assignment_id="a1", query="tower Dallas"),
            SimpleNamespace(assignment_id="a2", query="warehouse Dallas"),
        ],
        results=[
            SimpleNamespace(
                assignment_id="a1",
                model_dump=lambda mode=None: {
                    "url": "https://broker-a.example/x", "title": "T",
                    "snippet": "S", "domain": "broker-a.example",
                    "published_at": None,
                    "retrieved_at": "2026-10-30T00:00:00+00:00", "rank": 1,
                },
            )
        ],
        errors=[SimpleNamespace(assignment_id="a3", error="budget_exhausted")],
        retrieved_at="2026-10-30T00:00:00+00:00",
    )


def test_persist_artifacts_covers_executed_and_errors() -> None:
    conn = _FakeConn([None, "ECHO_INSERT", None, "ECHO_INSERT", "ECHO_INSERT"])
    keys = persist_research_artifacts(conn, uuid4(), _research_result())
    assert keys == ["a1", "a2", "a3"]
    payloads = [
        __import__("json").loads(params["payload"])
        for sql, params in conn._cursor.executed
        if "INSERT INTO workflow_records" in sql
    ]
    assert payloads[0]["collection_provenance"] == "adapter_collected"
    assert payloads[0]["attempt"] == "completed"
    assert len(payloads[0]["results"]) == 1
    assert payloads[1]["results"] == []
    assert payloads[2]["attempt"] == "failed"
    assert payloads[2]["error"] == "budget_exhausted"


def _stub_dossier(key: str = "k1") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        model_dump=lambda mode=None: _dossier(),
    )


def _stub_ranking() -> SimpleNamespace:
    verification = SimpleNamespace(
        model_dump=lambda mode=None: {"outcome": "NOT_VERIFIED", "reasons": ["x"]}
    )
    return SimpleNamespace(
        candidates=[SimpleNamespace(key="k1", category_verification=verification)]
    )


def test_persist_dossier_versions_binds_verification() -> None:
    dossier_set = SimpleNamespace(dossiers=[_stub_dossier()], dossier_version="004B")
    conn = _FakeConn(["ECHO_INSERT"])
    digests = persist_dossier_versions(conn, uuid4(), dossier_set, _stub_ranking(), ["a1"])
    assert len(digests) == 1 and len(digests[0]) == 64
    payload = __import__("json").loads(conn._cursor.executed[0][1]["payload"])
    assert payload["candidate_verification"] == {"outcome": "NOT_VERIFIED", "reasons": ["x"]}
    assert payload["artifact_refs"] == ["a1"]
    assert payload["builder_version"] == "004B"
    assert payload["digest"] == digests[0]


def test_persist_dossier_versions_missing_verification_stays_empty() -> None:
    dossier_set = SimpleNamespace(dossiers=[_stub_dossier()], dossier_version="004B")
    ranking = SimpleNamespace(candidates=[SimpleNamespace(key="k1", category_verification=None)])
    conn = _FakeConn(["ECHO_INSERT"])
    persist_dossier_versions(conn, uuid4(), dossier_set, ranking, [])
    payload = __import__("json").loads(conn._cursor.executed[0][1]["payload"])
    assert payload["candidate_verification"] is None


def test_persist_qualifications_writes_per_dossier() -> None:
    record = {
        "record_id": uuid4(),
        "run_id": uuid4(),
        "kind": "dossier_version",
        "record_key": "k1",
        "version": 1,
        "payload": {"dossier": _dossier()},
        "digest": "d",
    }
    conn = _FakeConn([record, "ECHO_INSERT"])
    outcomes = persist_qualifications(conn, uuid4(), ["k1"])
    assert outcomes == ["qualified"]


def test_persist_qualifications_requires_dossier() -> None:
    conn = _FakeConn([None])
    with pytest.raises(WorkflowError, match="missing dossier version"):
        persist_qualifications(conn, uuid4(), ["k1"])


def _cell_dossier() -> dict[str, Any]:
    return _dossier(
        county_attribution={"evidence_status": "verified", "value": "Dallas County"},
        category_attribution={"status": "observed", "observed": "high_rise"},
    )


def test_persist_dossier_records_discovered_for_known_cell() -> None:
    dossier_set = SimpleNamespace(
        dossiers=[SimpleNamespace(key="k1", model_dump=lambda mode=None: _cell_dossier())],
        dossier_version="004B",
    )
    conn = _FakeConn(["ECHO_INSERT", {"county": "dallas county"}, {"event_id": uuid4()}])
    persist_dossier_versions(
        conn, uuid4(), dossier_set, _stub_ranking(), ["a1"],
        property_ids={"k1": str(uuid4())},
    )
    assert any("INSERT INTO pilot_events" in sql for sql, _ in conn._cursor.executed)
    transition = next(
        params["transition"] for sql, params in conn._cursor.executed
        if "INSERT INTO pilot_events" in sql
    )
    assert transition == "discovered"


def test_persist_qualifications_records_qualified_for_known_cell() -> None:
    record = {
        "record_id": uuid4(),
        "run_id": uuid4(),
        "kind": "dossier_version",
        "record_key": "k1",
        "version": 1,
        "payload": {"dossier": _cell_dossier(), "property_id": str(uuid4())},
        "digest": "d",
    }
    conn = _FakeConn([record, "ECHO_INSERT", {"county": "dallas county"}, {"event_id": uuid4()}])
    outcomes = persist_qualifications(conn, uuid4(), ["k1"])
    assert outcomes == ["qualified"]
    transition = next(
        params["transition"] for sql, params in conn._cursor.executed
        if "INSERT INTO pilot_events" in sql
    )
    assert transition == "qualified"
