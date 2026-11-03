"""Non-Dallas national-catalog -> shared selection -> generic collection.

Offline fakes only. Changed expectations (explicit reasons):
- A candidate with unknown license/freshness/jurisdiction is
  ``needs_review`` and is *never* selected (previously
  ``discover_us_sources`` selected the first non-rejected adapter and
  invented jurisdiction/record types/property-level from the query).
- Generic adapters no longer read ``GENERIC_*`` env locators and no
  longer return outside-bbox rows as a fallback; they collect only
  the retained resource binding.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone

import pytest

from app.agents.gabriel.sources.base import BoundingBox, SOURCES, SourceError
from app.agents.gabriel.sources.generic_open_data import (
    GenericArcGISSource,
    GenericSocrataSource,
)
from app.agents.gabriel.sources.source_selection import DiscoveryBudget
from app.agents.gabriel.sources.us_discovery import discover_us_sources

BBOX = BoundingBox(29.7, -95.4, 29.8, -95.3)


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])


def _catalog(payloads):
    def http_get(url, params, timeout):
        for key, payload in payloads.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise RuntimeError("unexpected catalog")
    return http_get


def _qualified_socrata_payload():
    today = datetime.now(timezone.utc).date().isoformat()
    return {"results": [{
        "resource": {"name": "Travis Parcels", "type": "dataset", "id": "abc1",
                     "updatedAt": f"{today}T00:00:00.000Z",
                     "columns_field_name": ["account", "situs_address", "land_use"]},
        "metadata": {"domain": "data.travis.gov", "license": "cc0-1.0",
                     "jurisdiction": "Travis County", "country": "US",
                     "record_types": "parcels,appraisal"}}]}


def _find(payloads, **kwargs):
    kwargs.setdefault("approved", True)
    kwargs.setdefault("budget", DiscoveryBudget(max_requests=3))
    return discover_us_sources("Travis County", http_get=_catalog(payloads), **kwargs)


def test_non_dallas_catalog_selects_strongest_qualified_resource():
    payloads = {"socrata.com": _qualified_socrata_payload(), "arcgis.com": {"data": []}}
    result = _find(payloads)
    assert result["status"] == "selected"
    assert result["selected_adapter"] == "generic_socrata"
    assert result["selected_resource"]["resource_url"] == \
        "https://data.travis.gov/resource/abc1.json"
    assert result["selected_resource"]["platform"] == "socrata"
    assert result["resource_digest"]
    assert "never enrolls" in result["authority_basis"]


def test_candidate_with_unknown_terms_stays_review_never_selected():
    payload = _qualified_socrata_payload()
    payload["results"][0]["metadata"].pop("license")  # terms unknown
    result = _find({"socrata.com": payload, "arcgis.com": {"data": []}})
    assert result["status"] == "needs_review"
    assert result["selected_adapter"] is None
    assert result["ranked"][0]["status"] == "needs_review"


def test_searxng_primary_skips_government_catalogs():
    calls = []

    def http_get(url, params, timeout):
        calls.append(url)
        if "socrata.com" in url or "arcgis.com" in url:
            raise AssertionError("government catalog must not be queried")
        if "search.example.org" in url:
            return {"results": [
                {"title": "Tarrant County Parcels - Example Lead",
                 "url": "https://example.org/tarrant-parcels"}]}
        raise RuntimeError("unexpected endpoint")

    result = discover_us_sources(
        "Tarrant County", http_get=http_get, approved=True,
        budget=DiscoveryBudget(max_requests=3),
        env={"GABRIEL_FINDING_POLICY": "searxng_primary",
             "GABRIEL_SEARXNG_URL": "https://search.example.org"})
    assert result["finding_policy"] == "searxng_primary"
    # A SearXNG result is a lead, not a source: it claims no license,
    # freshness, or adapter, so it is honestly never selected.
    assert result["status"] in ("needs_review", "no_supported_adapter")
    assert result["selected_adapter"] is None
    assert any("search.example.org" in u for u in calls)
    assert not any("socrata.com" in u or "arcgis.com" in u for u in calls)


def test_searxng_primary_without_endpoint_refuses_without_catalog_fallback():
    calls = []

    def http_get(url, params, timeout):
        calls.append(url)
        return {"results": []}

    result = discover_us_sources(
        "Tarrant County", http_get=http_get, approved=True,
        budget=DiscoveryBudget(max_requests=3),
        env={"GABRIEL_FINDING_POLICY": "searxng_primary"})
    assert result["status"] == "blocked"
    assert result["selected_adapter"] is None
    assert calls == []


def test_default_unapproved_zero_budget_cannot_network():
    calls = []
    result = discover_us_sources(
        "Travis County", http_get=lambda *a, **k: calls.append(1))
    assert result["selected_adapter"] is None and result["ranked"] == []
    assert calls == []


def test_catalog_failure_is_honest_not_fabricated():
    def failing(url, params, timeout):
        raise RuntimeError("down")

    result = discover_us_sources(
        "Nowhere County", http_get=failing, approved=True,
        budget=DiscoveryBudget(max_requests=3))
    assert result["ranked"] == []
    assert result["selected_adapter"] is None
    assert result["blockers"]


def _binding(**overrides):
    binding = {
        "adapter": "generic_socrata", "platform": "socrata",
        "resource_url": "https://data.travis.gov/resource/abc1.json",
        "field_mapping": {}, "selection_digest": "s", "resource_digest": "r",
    }
    binding.update(overrides)
    return binding


def test_generic_socrata_strict_bbox_category_and_no_env(monkeypatch):
    monkeypatch.setenv("GENERIC_SOCRATA_URL", "https://wrong.example/x.json")
    seen = {}

    def fetch(url, params, timeout):
        seen["url"] = url
        return json.dumps([
            {"account": "A1", "situs_address": "1 Main St", "city": "Austin",
             "latitude": "29.75", "longitude": "-95.36", "land_use": "WAREHOUSE"},
            {"account": "A2", "situs_address": "2 Far St",
             "latitude": "30.5", "longitude": "-95.36", "land_use": "WAREHOUSE"},
            {"account": "A3", "situs_address": "3 NoCoord St", "land_use": "WAREHOUSE"},
            {"account": "A4", "situs_address": "4 Office St",
             "latitude": "29.75", "longitude": "-95.36", "land_use": "OFFICE"},
        ]).encode()

    source = GenericSocrataSource(fetch=fetch, binding=_binding())
    rows = source.search(BBOX, ["warehouse_industrial"], 5, 5.0)
    assert seen["url"] == "https://data.travis.gov/resource/abc1.json"
    assert [r.external_id for r in rows] == ["generic_socrata:A1"]
    assert rows[0].category_hint == "warehouse_industrial"
    assert rows[0].tags["dataset_url"].endswith("abc1.json")
    assert rows[0].state is None  # never invented


def test_generic_arcgis_retains_geometry_and_uses_4326():
    seen = {}

    def fetch(url, params, timeout):
        seen.update(url=url, params=params)
        return json.dumps({"features": [{
            "properties": {"parcel_id": "P1", "address": "9 Pine St",
                           "land_use": "OFFICE BUILDING", "stories": "12"},
            "geometry": {"type": "Point", "coordinates": [-95.36, 29.75]}}]}).encode()

    binding = _binding(adapter="generic_arcgis", platform="arcgis",
                       resource_url="https://data.travis.gov/arcgis/rest/services/Parcels/FeatureServer/0")
    source = GenericArcGISSource(fetch=fetch, binding=binding)
    rows = source.search(BBOX, ["high_rise"], 5, 5.0)
    assert seen["params"]["inSR"] == "4326" and seen["params"]["outSR"] == "4326"
    assert len(rows) == 1 and rows[0].category_hint == "high_rise"
    assert rows[0].tags["geometry"] == {"type": "Point", "coordinates": [-95.36, 29.75]}
    assert (rows[0].latitude, rows[0].longitude) == (29.75, -95.36)


class _FakeClaimStore:
    def __init__(self):
        self.state = {}

    def claim(self, run_id, assignment, fingerprint, payload):
        if assignment in self.state:
            done = self.state[assignment]
            return {"status": "completed", "outcome": done} if done else {"status": "uncertain"}
        self.state[assignment] = None
        return {"status": "winner"}

    def complete(self, run_id, assignment, fingerprint, outcome):
        self.state[assignment] = outcome


def test_collection_claim_replay_zero_network_and_uncertain_consumed():
    calls = []

    def fetch(url, params, timeout):
        calls.append(url)
        return json.dumps([{"account": "A1", "latitude": "29.75",
                            "longitude": "-95.36", "land_use": "WAREHOUSE"}]).encode()

    store = _FakeClaimStore()
    binding = _binding(assignment_key="assign-1", claim_run_id="run-1")
    source = GenericSocrataSource(fetch=fetch, binding=binding, claim_store=store)
    first = source.search(BBOX, ["warehouse_industrial"], 5, 5.0)
    second = source.search(BBOX, ["warehouse_industrial"], 5, 5.0)
    assert [r.external_id for r in first] == [r.external_id for r in second]
    assert len(calls) == 1  # same-key replay: zero new provider calls
    with pytest.raises(SourceError, match="scope"):
        source.search(BBOX, ["commercial_property"], 5, 5.0)
    with pytest.raises(SourceError, match="scope"):
        source.search(BBOX, ["warehouse_industrial"], 4, 5.0)
    assert len(calls) == 1
    store.state["assign-1"]["rows"][0]["street_address"] = "Tampered address"
    with pytest.raises(SourceError, match="digest"):
        source.search(BBOX, ["warehouse_industrial"], 5, 5.0)
    # A claimed attempt with no outcome stays consumed, never retried.
    store2 = _FakeClaimStore()
    store2.state["assign-2"] = None

    def forbidden(url, params, timeout):
        raise AssertionError("uncertain attempt must not retransmit")

    stuck = GenericSocrataSource(
        fetch=forbidden, claim_store=store2,
        binding=_binding(assignment_key="assign-2", claim_run_id="run-1"))
    with pytest.raises(SourceError, match="uncertain"):
        stuck.search(BBOX, ["warehouse_industrial"], 5, 5.0)


def test_generic_adapters_registered_and_fail_closed_unbound():
    import app.agents.gabriel.sources  # noqa: F401

    for name in ("generic_socrata", "generic_arcgis", "generic_ckan"):
        assert name in SOURCES
    with pytest.raises(SourceError):
        SOURCES["generic_socrata"].search(BBOX, [], 1, 1.0)


def test_jurisdiction_from_observed_title_and_domain_only():
    from app.agents.gabriel.sources.source_finder import _candidate
    from app.agents.gabriel.sources.source_selection import SourceMission

    mission = SourceMission(jurisdiction="Seattle", approved=True)
    by_domain = _candidate(
        {"name": "Building Benchmarking", "publisher_domain": "cos-data.seattle.gov",
         "provenance_url": "https://cos-data.seattle.gov/resource/x.json"},
        mission, tier=1, provider="socrata_catalog")
    assert by_domain.candidate.jurisdiction == "Seattle"
    unrelated = _candidate(
        {"name": "Wage Data", "publisher_domain": "data.example.org",
         "provenance_url": "https://data.example.org/resource/x.json"},
        mission, tier=1, provider="socrata_catalog")
    assert unrelated.candidate.jurisdiction == ""


def test_public_domain_and_cc_zero_licenses_are_known_clear():
    from app.agents.gabriel.sources.source_finder import _KNOWN_CLEAR_LICENSES

    assert "public domain" in _KNOWN_CLEAR_LICENSES
    assert "cc-zero" in _KNOWN_CLEAR_LICENSES


def test_discovery_module_nationwide_entry_point():
    from app.agents.gabriel.sources.discovery import discover_sources_anywhere

    payloads = {"socrata.com": _qualified_socrata_payload(), "arcgis.com": {"data": []}}
    result = discover_sources_anywhere(
        "Travis County", "high_rise", http_get=_catalog(payloads),
        approved=True, budget=DiscoveryBudget(max_requests=3))
    assert result["selected_adapter"] == "generic_socrata"
    assert result["category"] == "high_rise"
    # A Travis-observed candidate must not be miscredited to Cook County.
    other = discover_us_sources(
        "Cook County", http_get=_catalog(payloads), approved=True,
        budget=DiscoveryBudget(max_requests=3))
    assert other["selected_adapter"] is None
