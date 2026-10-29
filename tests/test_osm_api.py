"""osm_api provider tests (mocked HTTP; no live OpenStreetMap calls)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.agents.gabriel.service as gabriel_service
from app.agents.gabriel.dedup import AddressMatch, AddressMatchStatus
from app.agents.gabriel.sources.base import (
    SOURCES,
    BoundingBox,
    SourceError,
    SourceTimeout,
    get_source,
)
from app.agents.gabriel.sources.osm_api import OsmApiSource, check_bbox, parse_osm_xml
from app.main import app
from app.properties import DataStatus, PropertyCreate

client = TestClient(app, raise_server_exceptions=False)

_SMALL_BBOX = BoundingBox(south=32.780, west=-96.797, north=32.781, east=-96.796)
_SMALL_BBOX_INPUT = {"south": 32.780, "west": -96.797, "north": 32.781, "east": -96.796}
_EVIDENCE_AT = "2026-10-27T00:00:00+00:00"
_ALL_CATS = ["high_rise", "downtown_commercial", "warehouse_industrial", "other"]

TEST_INTAKE_KEY = "unit-test-intake-key-only"


def _headers():
    return {"X-Intake-Key": TEST_INTAKE_KEY}

SAMPLE_OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="unit-test">
  <node id="11" lat="32.78010" lon="-96.79650">
    <tag k="building" v="yes"/>
    <tag k="name" v="Test Corner Shop"/>
    <tag k="addr:housenumber" v="100"/>
    <tag k="addr:street" v="Main St"/>
    <tag k="addr:city" v="Dallas"/>
    <tag k="addr:state" v="TX"/>
    <tag k="addr:postcode" v="75202"/>
  </node>
  <node id="12" lat="32.78000" lon="-96.79600"/>
  <node id="13" lat="32.78020" lon="-96.79620"/>
  <node id="14" lat="32.78020" lon="-96.79600"/>
  <node id="15" lat="32.78000" lon="-96.79620"/>
  <way id="21">
    <nd ref="12"/><nd ref="13"/><nd ref="14"/><nd ref="15"/><nd ref="12"/>
    <tag k="building" v="warehouse"/>
    <tag k="name" v="Test Storage Depot"/>
    <tag k="addr:housenumber" v="900"/>
    <tag k="addr:street" v="Industrial Blvd"/>
    <tag k="addr:city" v="Dallas"/>
  </way>
  <way id="22">
    <nd ref="999"/>
    <tag k="building" v="commercial"/>
  </way>
  <way id="23">
    <nd ref="12"/>
    <tag k="building:part" v="yes"/>
  </way>
  <way id="24">
    <nd ref="12"/>
    <tag k="building" v="no"/>
  </way>
</osm>
"""


class _FakeResponse:
    def __init__(self, status_code=200, text="") -> None:
        self.status_code = status_code
        self.text = text


def _configured_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")


# --- XML parsing ---


def test_parse_way_and_node_with_addresses() -> None:
    listings = parse_osm_xml(SAMPLE_OSM_XML, _EVIDENCE_AT)
    assert [item.external_id for item in listings] == ["node/11", "way/21", "way/22"]
    shop = listings[0]
    assert shop.name == "Test Corner Shop"
    assert shop.street_address == "100 Main St"
    assert (shop.city, shop.state, shop.zip_code) == ("Dallas", "TX", "75202")
    assert shop.element_url == "https://www.openstreetmap.org/node/11"
    assert shop.retrieved_at == _EVIDENCE_AT
    depot = listings[1]
    assert depot.street_address == "900 Industrial Blvd"
    assert depot.city == "Dallas"
    assert depot.state is None  # missing tags stay missing
    assert depot.element_url == "https://www.openstreetmap.org/way/21"


def test_parse_reuses_canonical_categories() -> None:
    listings = parse_osm_xml(SAMPLE_OSM_XML, _EVIDENCE_AT)
    assert [item.category_hint for item in listings] == [
        "other",  # plain building=yes node
        "warehouse_industrial",
        "downtown_commercial",
    ]


def test_parse_way_centroid_and_missing_nodes() -> None:
    listings = parse_osm_xml(SAMPLE_OSM_XML, _EVIDENCE_AT)
    depot = listings[1]
    assert depot.latitude == pytest.approx(32.78008)
    assert depot.longitude == pytest.approx(-96.79608)
    dangling = listings[2]
    assert dangling.latitude is None and dangling.longitude is None


def test_parse_skips_parts_and_non_buildings() -> None:
    ids = [item.external_id for item in parse_osm_xml(SAMPLE_OSM_XML, _EVIDENCE_AT)]
    assert "way/23" not in ids  # bare building:part is not a property
    assert "way/24" not in ids  # building=no is not a building


def test_parse_rejects_malformed_xml() -> None:
    with pytest.raises(SourceError):
        parse_osm_xml("<osm><node", _EVIDENCE_AT)
    with pytest.raises(SourceError):
        parse_osm_xml("", _EVIDENCE_AT)
    with pytest.raises(SourceError):
        parse_osm_xml("<html></html>", _EVIDENCE_AT)


# --- Request bounds ---


def test_check_bbox_accepts_small_controlled_box() -> None:
    check_bbox(_SMALL_BBOX)  # must not raise


@pytest.mark.parametrize(
    "bbox",
    [
        BoundingBox(south=32.0, west=-97.0, north=32.1, east=-96.99),  # lat span
        BoundingBox(south=32.0, west=-97.0, north=32.01, east=-96.9),  # lon span
        BoundingBox(south=32.0, west=-97.0, north=32.05, east=-96.95),  # area
    ],
)
def test_check_bbox_rejects_oversized_boxes(bbox) -> None:
    with pytest.raises(SourceError, match="bbox_too_large"):
        check_bbox(bbox)


def test_search_rejects_oversized_box_before_http(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")

    def _must_not_call(*args, **kwargs):
        raise AssertionError("oversized box must not reach the network")

    monkeypatch.setattr(httpx, "get", _must_not_call)
    big = BoundingBox(south=32.0, west=-97.0, north=32.1, east=-96.9)
    with pytest.raises(SourceError, match="bbox_too_large"):
        OsmApiSource().search(big, _ALL_CATS, 5, 10.0)


# --- HTTP behavior (mocked) ---


def test_search_uses_map_endpoint_with_ordered_bbox(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    seen: dict = {}

    def _get(url, params=None, headers=None, timeout=None):
        seen.update({"url": url, "params": params, "timeout": timeout,
                     "agent": (headers or {}).get("User-Agent")})
        return _FakeResponse(text=SAMPLE_OSM_XML)

    monkeypatch.setattr(httpx, "get", _get)
    found = OsmApiSource().search(_SMALL_BBOX, _ALL_CATS, 10, 12.0)
    assert seen["url"] == "https://api.openstreetmap.org/api/0.6/map"
    assert seen["params"] == {"bbox": "-96.797,32.78,-96.796,32.781"}
    assert seen["timeout"] == 12.0
    assert "GrowUp" in seen["agent"]
    assert len(found) == 3
    warehouses = OsmApiSource().search(_SMALL_BBOX, ["warehouse_industrial"], 10, 12.0)
    assert [item.external_id for item in warehouses] == ["way/21"]
    limited = OsmApiSource().search(_SMALL_BBOX, _ALL_CATS, 2, 12.0)
    assert len(limited) == 2


def test_search_maps_failures_without_fabrication(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    source = OsmApiSource()

    def _timeout(*args, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "get", _timeout)
    with pytest.raises(SourceTimeout):
        source.search(_SMALL_BBOX, _ALL_CATS, 5, 5.0)

    def _unreachable(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _unreachable)
    with pytest.raises(SourceError):
        source.search(_SMALL_BBOX, _ALL_CATS, 5, 5.0)

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(status_code=400))
    with pytest.raises(SourceError):
        source.search(_SMALL_BBOX, _ALL_CATS, 5, 5.0)

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(text="<osm><bad"))
    with pytest.raises(SourceError):
        source.search(_SMALL_BBOX, _ALL_CATS, 5, 5.0)


# --- Registration and pipeline integration ---


def test_provider_attributes_and_registration() -> None:
    source = get_source("osm_api")
    assert source.name == "osm_api"
    assert source.requires_credentials is False
    assert "listing" in source.description.lower()
    assert source.docs_url.startswith("https://wiki.openstreetmap.org/")
    assert set(SOURCES) >= {"overpass", "osm_api"}


def test_sources_endpoint_lists_both_providers() -> None:
    response = client.get("/discovery/sources")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert {"overpass", "osm_api"} <= names


def test_osm_api_run_through_existing_pipeline(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    _configured_env(monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(text=SAMPLE_OSM_XML))
    created: list = []
    monkeypatch.setattr(
        gabriel_service, "_create_with_observation",
        lambda payload, canonical, external_id, **_: created.append(payload) or {"id": f"id-{len(created)}"},
    )
    monkeypatch.setattr(gabriel_service, "find_observation", lambda source, external_id: None)
    monkeypatch.setattr(gabriel_service, "_persist_decision_dedicated", lambda event: None)
    monkeypatch.setattr(
        gabriel_service, "match_existing_address",
        lambda payload: AddressMatch(AddressMatchStatus.NONE),
    )
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)
    response = client.post(
        "/discovery/runs",
        json={"source": "osm_api", "bbox": _SMALL_BBOX_INPUT,
              "categories": ["high_rise", "downtown_commercial", "warehouse_industrial", "other"],
              "limit": 10, "timeout_seconds": 10.0},
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "osm_api"
    # way/22 is unusable (no name, address, or coordinates from dangling refs).
    assert body["counts"] == {"returned": 3, "created": 2, "skipped_duplicate": 0, "skipped_unusable": 1}
    assert len(body["created_ids"]) == 2
    for payload in created:
        record = PropertyCreate.model_validate(payload)
        assert record.data_status is DataStatus.UNKNOWN
        assert "api.openstreetmap.org" in (record.provenance or "") or "osm_api" in (record.provenance or "")
    # No fabricated financial, vacancy, or verified data anywhere.
    for payload in created:
        for key, value in payload.items():
            if key.endswith("_verified"):
                assert value is None
        assert payload.get("field_evidence") is None
        assert payload.get("property_value_estimated") is None
        assert payload.get("months_vacant_estimated") is None
        assert payload.get("owner") is None


def test_osm_api_run_skips_stored_duplicates(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    _configured_env(monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(text=SAMPLE_OSM_XML))
    created: list = []
    monkeypatch.setattr(
        gabriel_service, "_create_with_observation",
        lambda payload, canonical, external_id, **_: created.append(payload) or {"id": "new"},
    )
    monkeypatch.setattr(gabriel_service, "find_observation", lambda source, external_id: None)
    monkeypatch.setattr(gabriel_service, "_persist_decision_dedicated", lambda event: None)

    def _existing(payload):
        if (payload.get("street_address") or "") == "100 Main St":
            return AddressMatch(AddressMatchStatus.UNIQUE, "stored-existing")
        return AddressMatch(AddressMatchStatus.NONE)

    monkeypatch.setattr(gabriel_service, "match_existing_address", _existing)
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)
    response = client.post(
        "/discovery/runs",
        json={"source": "osm_api", "bbox": _SMALL_BBOX_INPUT,
              "categories": ["high_rise", "downtown_commercial", "warehouse_industrial", "other"],
              "limit": 10, "timeout_seconds": 10.0},
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["created"] == 1
    assert body["counts"]["skipped_duplicate"] == 1
    assert body["counts"]["skipped_unusable"] == 1
    assert len(created) == 1  # stored record never touched (no overwrite)
