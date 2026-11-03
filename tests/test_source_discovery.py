"""Worldwide-qualification discovery integration tests (offline).

The DFW catalog is candidate data only: qualification decides, the
curated registry enrolls, and only Dallas has a compatible adapter.
No candidate earns P1 authority from a domain suffix or an LLM.
"""
from __future__ import annotations

import pytest
import os


@pytest.fixture(autouse=True)
def _offline_source_dns(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])


class _StoredFinding:
    """Injectable assignment-store contract; PostgreSQL races need Ubuntu."""
    def __init__(self):
        import threading
        self.rows = {}
        self.lock = threading.Lock()

    def claim(self, key, fingerprint, mission):
        from app.agents.gabriel.sources.source_selection import SourceSelectionError
        with self.lock:
            if key not in self.rows:
                self.rows[key] = {"status": "winner", "run_id": "11111111-1111-4111-8111-111111111111",
                                  "fingerprint": fingerprint}
                return dict(self.rows[key])
            row = self.rows[key]
            if row["fingerprint"] != fingerprint:
                raise SourceSelectionError("different mission/limits")
            return {**row, "status": "completed" if "decision" in row else "uncertain"}

    def complete(self, key, *, decision, evidence):
        import copy
        self.rows[key].update(decision=copy.deepcopy(decision), evidence=copy.deepcopy(evidence))

    def load_decision(self, digest):
        import copy
        return next((copy.deepcopy(r["decision"]) for r in self.rows.values()
                     if r.get("decision", {}).get("digest") == digest), None)


def _durable_find(store, http, **kwargs):
    from app.agents.gabriel.sources.discovery import select_for_mission
    from app.agents.gabriel.sources.source_selection import SourceMission, DiscoveryBudget
    _register_travis_fake()
    return select_for_mission(SourceMission(jurisdiction="Travis County", approved=True,
                              budget=DiscoveryBudget(max_requests=1)),
                              assignment_key="test-source-1", durable_store=store,
                              http_get=http, supported_adapters={"travis_appraisal"},
                              env={"GABRIEL_CATALOG_API_URL": "https://catalog.example.org/api"}, **kwargs)


def test_durable_replay_after_restart_preserves_api_decision_without_network(monkeypatch):
    from app.agents.gabriel.schemas import SourceSelectionAttestation
    from app.agents.gabriel.sources.source_selection import clear_retained, enforce_selection
    store = _StoredFinding()
    http, calls = _fake_catalog(_travis_payload())
    decision = _durable_find(store, http)
    clear_retained()
    def forbidden(*a, **k):
        pytest.fail("replay must not resolve DNS or transmit")
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    replay = _durable_find(store, forbidden)
    assert replay == decision and len(calls) == 1
    clear_retained()
    transmitted = SourceSelectionAttestation.model_validate(decision).model_dump(exclude_unset=True)
    assert enforce_selection(source="travis_appraisal", jurisdiction="Travis County",
                             selection=transmitted, durable_store=store) == decision


def test_uncertain_assignment_cannot_transmit_again_or_change_limits():
    from app.agents.gabriel.sources.source_selection import SourceSelectionError, UncertainFindingError
    from app.agents.gabriel.sources.discovery import select_for_mission
    from app.agents.gabriel.sources.source_selection import SourceMission, DiscoveryBudget
    store = _StoredFinding()
    calls = []
    def interrupted(*a, **k):
        calls.append(1)
        raise TimeoutError("test interruption")
    with pytest.raises(UncertainFindingError):
        _durable_find(store, interrupted)
    with pytest.raises(UncertainFindingError):
        _durable_find(store, interrupted)
    assert calls == [1]
    with pytest.raises(SourceSelectionError, match="different mission"):
        select_for_mission(SourceMission(jurisdiction="Dallas County", approved=True,
                           budget=DiscoveryBudget(max_requests=2)), assignment_key="test-source-1",
                           durable_store=store, http_get=interrupted)
    assert calls == [1]


def test_missing_assignment_refuses_real_finding_before_dns(monkeypatch):
    from app.agents.gabriel.sources.discovery import select_for_mission
    from app.agents.gabriel.sources.source_selection import SourceMission, DiscoveryBudget, SourceSelectionError
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("must refuse before DNS"))
    with pytest.raises(SourceSelectionError, match="assignment_key"):
        select_for_mission(SourceMission(jurisdiction="Dallas County", approved=True,
                           budget=DiscoveryBudget(max_requests=1)))


def test_uncommitted_decision_not_cached_as_authorization():
    from app.agents.gabriel.sources.source_selection import _RETAINED, clear_retained, SourceSelectionError
    store = _StoredFinding()
    def failed(*a, **k):
        raise SourceSelectionError("test commit failed")
    store.complete = failed
    clear_retained()
    http, _ = _fake_catalog(_travis_payload())
    with pytest.raises(SourceSelectionError, match="commit failed"):
        _durable_find(store, http)
    assert not _RETAINED


def test_bootstrap_refuses_unknown_source_and_foreign_dcad_scope():
    from app.agents.gabriel.sources.source_selection import bootstrap_selection, SourceSelectionError
    for source, scope in [("arbitrary", "Dallas County"), ("dcad_bulk", "Tarrant County"),
                          ("overpass", None)]:
        with pytest.raises(SourceSelectionError):
            bootstrap_selection(source=source, jurisdiction=scope)

from app.agents.gabriel.sources.discovery import (
    discover_sources,
    pilot_source_plan,
    qualified_adapter,
)


def test_dallas_selects_registered_adapter_via_registry_enrollment():
    result = discover_sources("dallas county", "high_rise")
    assert result["selected_adapter"] == "dcad_bulk"
    top = result["ranked"][0]
    assert top["name"].startswith("Dallas Central Appraisal District")
    # Domain alone grants nothing (.org); authority is the curated
    # registry enrollment, stated explicitly.
    assert top["p1_eligible"] is False
    assert "registry" in result["authority_basis"].lower()


def test_non_dallas_counties_stay_provisional_without_adapter():
    for county in ("tarrant county", "collin county", "wise county"):
        result = discover_sources(county, "commercial_property")
        assert result["selected_adapter"] is None
        assert result["ranked"], county
        top = result["ranked"][0]
        assert top["status"] == "needs_review"
        assert top["p1_eligible"] is False
        assert top["authority_candidates"] == []
        assert result["blockers"]


def test_no_county_earns_p1_from_domain_alone():
    plan = pilot_source_plan()
    assert len(plan) == 36
    for entry in plan:
        for ranked in entry["ranked"]:
            assert ranked["p1_eligible"] is False


def test_pilot_plan_covers_canonical_counties_and_categories():
    plan = pilot_source_plan()
    counties = {entry["county"] for entry in plan}
    categories = {entry["category"] for entry in plan}
    assert len(counties) == 12
    assert categories == {
        "high_rise", "commercial_property", "warehouse_industrial"}


def test_qualified_adapter_only_for_dallas():
    assert qualified_adapter("dallas county", "warehouse_industrial") \
        == "dcad_bulk"
    assert qualified_adapter("navarro county", "high_rise") is None


def test_unknown_county_refused():
    with pytest.raises(ValueError):
        discover_sources("travis county", "high_rise")


def _fake_catalog(payload):
    calls = []
    def _get(url, params=None, timeout=None):
        calls.append((url, params))
        import json as _j
        return {"status_code": 200, "headers": {}, "content": _j.dumps(payload).encode()}
    return _get, calls


def _travis_payload():
    base = {"jurisdiction": "Travis County", "country": "US", "record_types": ["parcels", "appraisal"],
            "free": True, "requires_auth": False, "robots_restricted": False, "property_level": True}
    return {"sources": [
        {**base, "name": "Weak Portal", "publisher_name": "Weak Co", "publisher_domain": "weak.example.com",
         "claimed_official": False, "access_method": "portal_search", "license_clear": True,
         "formats": ["csv"], "update_age_days": 30, "provenance_url": "https://weak.example.com/", "adapter": "travis_appraisal"},
        {**base, "name": "Travis Appraisal API", "publisher_name": "Travis Central Appraisal District",
         "publisher_domain": "appraisal.travis.gov", "claimed_official": True, "access_method": "api",
         "license_clear": True, "formats": ["json", "csv"], "update_age_days": 10,
         "provenance_url": "https://appraisal.travis.gov/data", "adapter": "travis_appraisal"},
    ]}


def _register_travis_fake():
    from app.agents.gabriel.sources.source_selection import register_adapter_binding
    register_adapter_binding("travis_appraisal", publishers=["appraisal.travis.gov"],
                             endpoints=["appraisal.travis.gov"], record_types=["parcels", "appraisal"])


def test_non_dallas_finder_ranks_and_selects_strongest():
    from app.agents.gabriel.sources.discovery import select_for_mission
    from app.agents.gabriel.sources.source_selection import DiscoveryBudget, SourceMission
    _register_travis_fake()
    http, calls = _fake_catalog(_travis_payload())
    mission = SourceMission(jurisdiction="Travis County", approved=True,
                            budget=DiscoveryBudget(max_requests=1, max_results=10))
    import os
    old = os.environ.get("GABRIEL_CATALOG_API_URL")
    os.environ["GABRIEL_CATALOG_API_URL"] = "https://catalog.example.org/api"
    try:
        decision = select_for_mission(mission, http_get=http, supported_adapters={"travis_appraisal"})
    finally:
        if old is None: os.environ.pop("GABRIEL_CATALOG_API_URL", None)
        else: os.environ["GABRIEL_CATALOG_API_URL"] = old
    assert calls and decision["status"] == "selected"
    assert decision["selected_candidate"] == "Travis Appraisal API"
    assert decision["selected_adapter"] == "travis_appraisal" and decision["digest"]
    # Deterministic replay
    http2, _ = _fake_catalog(_travis_payload())
    os.environ["GABRIEL_CATALOG_API_URL"] = "https://catalog.example.org/api"
    try:
        again = select_for_mission(mission, http_get=http2, supported_adapters={"travis_appraisal"})
    finally:
        if old is None: os.environ.pop("GABRIEL_CATALOG_API_URL", None)
        else: os.environ["GABRIEL_CATALOG_API_URL"] = old
    assert again["digest"] == decision["digest"]


def test_spoofed_and_unknown_publisher_cannot_be_selected():
    from app.agents.gabriel.sources.source_selection import DiscoveryBudget, SourceMission, select_sources
    from app.agents.gabriel.sources.source_finder import _candidate
    mission = SourceMission(jurisdiction="Travis County", approved=True, budget=DiscoveryBudget(max_requests=1))
    spoof = _candidate({"name": "Spoof", "publisher_name": "Fake Gov", "publisher_domain": "evil.example.com",
                        "claimed_official": True, "access_method": "api", "license_clear": True, "formats": ["json"],
                        "update_age_days": 1, "provenance_url": "https://appraisal.travis.gov/x", "adapter": "overpass"},
                       mission, tier=1, provider="official_catalog")
    decision = select_sources(mission, [spoof], supported_adapters={"overpass"})
    assert decision["status"] != "selected"
    assert any("spoof" in r for r in decision["ranked"][0]["reasons"])


def test_no_provider_refuses_without_network_and_bounds_enforced():
    from app.agents.gabriel.sources.source_finder import find_candidates
    from app.agents.gabriel.sources.source_selection import DiscoveryBudget, SourceMission, SourceSelectionError
    mission = SourceMission(jurisdiction="Travis County", approved=True, budget=DiscoveryBudget(max_requests=1))
    called = []
    with pytest.raises(SourceSelectionError):
        find_candidates(mission, http_get=lambda *a, **k: called.append(1), env={})
    assert called == []
    with pytest.raises(SourceSelectionError):
        find_candidates(SourceMission(jurisdiction="X", approved=True, budget=DiscoveryBudget(max_requests=1)),
                        http_get=lambda *a, **k: called.append(1),
                        env={"GABRIEL_CATALOG_API_URL": "http://127.0.0.1:8080/x"})
    assert called == []


def test_brave_fallback_reserves_before_transmit():
    from app.agents.gabriel.sources.source_finder import find_candidates
    from app.agents.gabriel.sources.source_selection import DiscoveryBudget, SourceMission
    order = []
    mission = SourceMission(jurisdiction="Travis County", approved=True,
                            budget=DiscoveryBudget(max_requests=1, max_paid_requests=1))
    found = find_candidates(mission, env={}, reserve=lambda n: order.append("reserve"),
                            brave_execute=lambda q, a, t: order.append("transmit") or [])
    assert order == ["reserve", "transmit"] and found == []


def _generic_decision():
    from app.agents.gabriel.sources.source_finder import _candidate
    from app.agents.gabriel.sources.source_selection import (
        DiscoveryBudget, SourceMission, select_sources)
    mission = SourceMission(jurisdiction="Travis County", approved=True,
                            budget=DiscoveryBudget(max_requests=1))
    raw = {"name": "Travis Parcels", "publisher_name": "Travis County",
           "publisher_domain": "data.travis.gov", "claimed_official": True,
           "jurisdiction": "Travis County", "country": "US",
           "record_types": ["parcels", "appraisal"], "access_method": "api",
           "license_clear": True, "formats": ["json"], "update_age_days": 3,
           "provenance_url": "https://data.travis.gov/resource/abc1.json",
           "adapter": "generic_socrata",
           "resource": {"resource_url": "https://data.travis.gov/resource/abc1.json",
                        "platform": "socrata", "dataset_id": "abc1",
                        "license_id": "cc0-1.0", "field_mapping": {}, "fields": []}}
    found = _candidate(raw, mission, tier=1, provider="socrata_catalog")
    return select_sources(mission, [found], supported_adapters={"generic_socrata"})


def test_generic_selection_binds_exact_resource():
    decision = _generic_decision()
    assert decision["status"] == "selected"
    assert decision["selected_resource"]["resource_url"].endswith("abc1.json")
    assert decision["selected_resource"]["license_id"] == "cc0-1.0"
    assert decision["resource_digest"]


@pytest.mark.parametrize("mutate", [
    lambda d: d["selected_resource"].update(resource_url="https://data.travis.gov/resource/evil.json"),
    lambda d: d["selected_resource"].update(publisher_domain="evil.example.gov"),
    lambda d: d["selected_resource"].update(jurisdiction="Cook County"),
    lambda d: d.update(resource_digest="0" * 64),
])
def test_generic_resource_swapping_or_forged_decision_refuses(mutate):
    import copy
    from app.agents.gabriel.sources.source_selection import (
        SourceSelectionError, enforce_selection)
    forged = copy.deepcopy(_generic_decision())
    mutate(forged)
    # Recompute the outer digest so only the resource binding can refuse.
    from app.agents.gabriel.sources.source_selection import _digest, _RETAINED
    forged["digest"] = _digest(forged)
    _RETAINED[forged["digest"]] = copy.deepcopy(forged)
    with pytest.raises(SourceSelectionError):
        enforce_selection(source="generic_socrata", jurisdiction="Travis County",
                          selection=forged)


def test_generic_cannot_bootstrap_via_service_path():
    from app.agents.gabriel.sources.source_selection import (
        SourceSelectionError, bootstrap_selection)
    with pytest.raises(SourceSelectionError, match="bootstrap"):
        bootstrap_selection(source="generic_socrata", jurisdiction="Travis County")


def test_http_policy_refuses_private_redirect_and_oversize():
    import socket
    from app.agents.gabriel.sources.http_policy import HttpPolicyError, fetch_bytes

    with pytest.raises(HttpPolicyError):
        fetch_bytes("https://127.0.0.1/x", transport=lambda *a, **k: b"{}")
    with pytest.raises(HttpPolicyError):
        fetch_bytes("https://public.example/x", resolver=lambda *a, **k:
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
                    transport=lambda *a, **k: b"{}")
    with pytest.raises(HttpPolicyError):
        fetch_bytes("https://dns-fails.example/x",
                    resolver=lambda *a, **k: (_ for _ in ()).throw(OSError("no")),
                    transport=lambda *a, **k: b"{}")

    class _Resp:
        status_code = 302
        headers = {}

        def iter_bytes(self):
            yield b""

    with pytest.raises(HttpPolicyError, match="redirect"):
        fetch_bytes("https://ok.example/x", transport=lambda *a, **k: _Resp(),
                    resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])

    class _Big:
        status_code = 200
        headers = {}

        def iter_bytes(self):
            yield b"x" * 600
            yield b"x" * 600

    with pytest.raises(HttpPolicyError, match="byte bound"):
        fetch_bytes("https://ok.example/x", transport=lambda *a, **k: _Big(),
                    max_bytes=1000,
                    resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])


def test_duplicate_dataset_titles_resolve_by_identity():
    from app.agents.gabriel.sources.source_finder import _candidate, _dedupe_identity
    from app.agents.gabriel.sources.source_selection import SourceMission
    mission = SourceMission(jurisdiction="Travis County", approved=True)
    base = {"name": "Same Title", "publisher_domain": "data.travis.gov",
            "provenance_url": "https://data.travis.gov/resource/a.json"}
    a = _candidate({**base, "resource": {"resource_url": base["provenance_url"], "dataset_id": "a"}},
                   mission, tier=1, provider="socrata_catalog")
    b = _candidate({**base, "provenance_url": "https://data.travis.gov/resource/b.json",
                    "resource": {"resource_url": "https://data.travis.gov/resource/b.json",
                                 "dataset_id": "b"}},
                   mission, tier=1, provider="socrata_catalog")
    dup = _candidate({**base, "resource": {"resource_url": base["provenance_url"], "dataset_id": "a"}},
                     mission, tier=1, provider="socrata_catalog")
    assert len(_dedupe_identity([a, b, dup])) == 2


def test_selection_events_from_same_decision_have_unique_ids():
    # Regression (live PostgreSQL, 2026-11-02): the audit event ID was
    # derived from the decision digest, so independent runs sharing a
    # decision collided on the agent_events primary key.
    from app.agents.gabriel.sources.source_selection import (
        build_selection_event,
    )

    decision = {
        "digest": "a" * 64, "status": "selected",
        "selected_adapter": "stub", "selected_candidate": "Stub",
        "mission": {"jurisdiction": "Example County"},
        "ranked": [], "blockers": [],
    }
    first = build_selection_event(decision, run_id="run-1")
    second = build_selection_event(decision, run_id="run-2")
    assert first.event_id != second.event_id
    assert first.details["digest"] == "a" * 64
    assert second.details["digest"] == "a" * 64


def test_pilot_plan_annotation_matches_discovery():
    from app.pilot import source_note_for

    note = source_note_for("tarrant county", "high_rise")
    assert note["selected_adapter"] is None
    assert note["top_status"] == "needs_review"
    dallas = source_note_for("Dallas County", "commercial_property")
    assert dallas["selected_adapter"] == "dcad_bulk"


# Independent Super-Coach acceptance regressions: no production network/data.
@pytest.mark.parametrize("jurisdiction", [None, "", "Tarrant County"])
def test_selection_cannot_be_omitted_or_forged(jurisdiction):
    from app.agents.gabriel.sources.source_selection import enforce_selection, SourceSelectionError
    with pytest.raises(SourceSelectionError):
        enforce_selection(source="dcad_bulk", jurisdiction=jurisdiction, selection=None)
    forged = {"status": "selected", "selected_adapter": "dcad_bulk", "digest": "a" * 64,
              "mission": {"jurisdiction": jurisdiction or "Dallas County"}}
    with pytest.raises(SourceSelectionError):
        enforce_selection(source="dcad_bulk", jurisdiction=jurisdiction, selection=forged)


def test_catalog_cannot_bind_a_foreign_publisher_to_dcad_adapter():
    from app.agents.gabriel.sources.source_selection import SourceMission, select_sources
    from app.agents.gabriel.sources.source_finder import _candidate
    mission = SourceMission(jurisdiction="Travis County", approved=True)
    raw = {"name": "Foreign publisher", "publisher_name": "Foreign agency",
           "publisher_domain": "foreign.example.gov", "claimed_official": True,
           "jurisdiction": "Travis County", "country": "US", "free": True,
           "requires_auth": False, "robots_restricted": False, "property_level": True,
           "access_method": "api", "license_clear": True, "formats": ["json"],
           "update_age_days": 1, "provenance_url": "https://foreign.example.gov/data",
           "adapter": "dcad_bulk"}
    candidate = _candidate(raw, mission, tier=1, provider="official_catalog")
    decision = select_sources(mission, [candidate], supported_adapters={"dcad_bulk"})
    assert decision["status"] != "selected"


def test_candidate_jurisdiction_cannot_be_invented_from_search_context():
    from app.agents.gabriel.sources.source_selection import SourceMission
    from app.agents.gabriel.sources.source_finder import _candidate
    candidate = _candidate({"name": "A result", "url": "https://example.gov/data"},
                           SourceMission(jurisdiction="Tarrant County"),
                           tier=2, provider="searxng").candidate
    assert not candidate.jurisdiction, "a query location is not observed dataset coverage"


@pytest.mark.parametrize("field,value", [("max_requests", True), ("max_requests", 1.5),
                                         ("max_paid_requests", True), ("max_results", 1.5)])
def test_discovery_budget_rejects_noninteger_counts(field, value):
    from app.agents.gabriel.sources.source_selection import DiscoveryBudget, SourceSelectionError
    values = {"max_requests": 2, field: value}
    with pytest.raises(SourceSelectionError):
        DiscoveryBudget(**values).validate()


def test_private_dns_resolution_refused_before_http(monkeypatch):
    import socket
    from app.agents.gabriel.sources.source_selection import SourceMission, DiscoveryBudget, SourceSelectionError
    from app.agents.gabriel.sources.source_finder import find_candidates
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    calls = []
    with pytest.raises(SourceSelectionError):
        find_candidates(SourceMission(jurisdiction="Dallas County", approved=True,
                        budget=DiscoveryBudget(max_requests=1)),
                        env={"GABRIEL_CATALOG_API_URL": "https://public-looking.example.org/api"},
                        http_get=lambda *a, **k: calls.append(1))
    assert calls == []


def test_real_ckan_catalog_shape_yields_observed_candidates(monkeypatch):
    import socket
    import json
    from app.agents.gabriel.sources.source_selection import SourceMission, DiscoveryBudget
    from app.agents.gabriel.sources.source_finder import find_candidates
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    payload = {"success": True, "result": {"results": [{
        "name": "parcels", "title": "Observed parcel resource", "organization": {"title": "Example publisher"},
        "resources": [{"url": "https://data.example.gov/parcels.csv", "format": "CSV"}]
    }]}}
    found = find_candidates(SourceMission(jurisdiction="Tarrant County", approved=True,
                            budget=DiscoveryBudget(max_requests=1)),
                            env={"GABRIEL_CATALOG_API_URL": "https://catalog.example.gov/api/3/action/package_search"},
                            http_get=lambda *a, **k: {"status_code": 200, "content": json.dumps(payload).encode()})
    assert len(found) == 1, "concrete catalog integration cannot require an invented sources-only protocol"
    assert found[0].candidate.provenance_url
    assert not found[0].candidate.license_clear, "absent terms cannot be guessed"


def _require_source_gate():
    from app.config import get_database_config
    from app.properties_db import get_connection
    cfg = get_database_config()
    assert cfg.name == "growup_mvp_gate" and cfg.user == "growup_app", "isolated source gate required"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS db, current_user AS role")
            row = cur.fetchone()
            assert row["db"] == "growup_mvp_gate" and row["role"] == "growup_app"


def _source_gate_usage(key):
    from app.properties_db import get_connection
    from app.workflow_db import get_run_by_key_in_transaction
    with get_connection() as conn:
        run = get_run_by_key_in_transaction(conn, "gabriel-source-finding:" + key)
        assert run is not None
        return run.usage


def _cleanup_source_gate(key):
    _require_source_gate()
    from app.properties_db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Child records reference the run (workflow_records_run_id_fkey);
            # delete them first or PostgreSQL refuses the run delete.
            cur.execute(
                "DELETE FROM workflow_records WHERE run_id IN "
                "(SELECT run_id FROM workflow_runs WHERE idempotency_key = %s)",
                ("gabriel-source-finding:" + key,))
            cur.execute("DELETE FROM workflow_runs WHERE idempotency_key = %s",
                        ("gabriel-source-finding:" + key,))


@pytest.mark.skipif(os.getenv("GROWUP_PG_INTEGRATION") != "1", reason="isolated PostgreSQL source gate")
def test_live_source_claim_replay_and_cold_cache(monkeypatch):
    from uuid import uuid4
    from app.agents.gabriel.sources.discovery import select_for_mission
    from app.agents.gabriel.sources.source_selection import (
        WorkflowSelectionStore, SourceMission, DiscoveryBudget, clear_retained, enforce_selection)
    _require_source_gate()
    key = "pg-source-" + uuid4().hex
    store = WorkflowSelectionStore()
    mission = SourceMission(jurisdiction="Travis County", approved=True,
                            budget=DiscoveryBudget(max_requests=1))
    _register_travis_fake()
    http, calls = _fake_catalog(_travis_payload())
    def find(transport):
        return select_for_mission(mission, assignment_key=key, durable_store=store, http_get=transport,
            supported_adapters={"travis_appraisal"}, env={"GABRIEL_CATALOG_API_URL": "https://catalog.example.org/api"})
    try:
        decision = find(http)
        clear_retained()
        assert enforce_selection(source="travis_appraisal", jurisdiction="Travis County",
                                 selection=decision, durable_store=store) == decision
        import socket
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("replay DNS forbidden"))
        assert find(lambda *a, **k: pytest.fail("replay HTTP forbidden")) == decision
        assert len(calls) == 1 and _source_gate_usage(key)["source_requests_reserved"] == 1
    finally:
        _cleanup_source_gate(key)


@pytest.mark.skipif(os.getenv("GROWUP_PG_INTEGRATION") != "1", reason="isolated PostgreSQL source gate")
def test_live_source_claim_single_winner_and_uncertain_replay():
    from uuid import uuid4
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.agents.gabriel.sources.discovery import select_for_mission
    from app.agents.gabriel.sources.source_selection import (
        WorkflowSelectionStore, SourceMission, DiscoveryBudget, UncertainFindingError)
    _require_source_gate()
    key = "pg-source-" + uuid4().hex
    entered, release = threading.Event(), threading.Event()
    store = WorkflowSelectionStore()
    mission = SourceMission(jurisdiction="Travis County", approved=True,
                            budget=DiscoveryBudget(max_requests=1))
    calls = []
    def interrupted(*a, **k):
        calls.append(1); entered.set()
        assert release.wait(10), "test release timed out"
        raise TimeoutError("test-only interrupted transmission")
    def find():
        return select_for_mission(mission, assignment_key=key, durable_store=store, http_get=interrupted,
            env={"GABRIEL_CATALOG_API_URL": "https://catalog.example.org/api"})
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(find)
            try:
                assert entered.wait(10), "winner did not reach fake HTTP"
                with pytest.raises(UncertainFindingError):
                    find()
            finally:
                release.set()
            with pytest.raises(UncertainFindingError):
                first.result(timeout=10)
        with pytest.raises(UncertainFindingError):
            find()
        assert calls == [1] and _source_gate_usage(key)["source_requests_reserved"] == 1
    finally:
        release.set()
        _cleanup_source_gate(key)
