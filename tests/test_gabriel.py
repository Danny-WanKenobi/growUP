"""Gabriel discovery tests (mocked providers and database; no network)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.agents.gabriel.service as gabriel_service
import app.routes.discovery as discovery_routes
from app.agents.gabriel.dedup import (
    AddressMatch,
    AddressMatchStatus,
    dedup_key,
    match_existing_address,
    normalize_text,
)
from app.agents.gabriel.normalize import normalize_listing
from app.agents.gabriel.schemas import DiscoveryRunRequest
from app.agents.gabriel.sources.base import (
    BoundingBox,
    RawListing,
    SourceError,
    SourceTimeout,
    UnknownSourceError,
    get_source,
)
from app.agents.gabriel.sources.overpass import (
    OverpassSource,
    build_query,
    map_category,
    parse_elements,
)
from app.db import DatabaseNotConfiguredError
from app.main import app
from app.properties import DataStatus, PropertyCreate

client = TestClient(app, raise_server_exceptions=False)

_BBOX = BoundingBox(south=32.770, west=-96.810, north=32.790, east=-96.785)
_BBOX_INPUT = {"south": 32.770, "west": -96.810, "north": 32.790, "east": -96.785}
_EVIDENCE_AT = "2026-10-27T00:00:00+00:00"

TEST_INTAKE_KEY = "unit-test-intake-key-only"


def _headers():
    return {"X-Intake-Key": TEST_INTAKE_KEY}


def _listing(**overrides) -> RawListing:
    base = {
        "source": "overpass",
        "external_id": "way/1001",
        "element_url": "https://www.openstreetmap.org/way/1001",
        "name": "Test Commerce Building",
        "street_address": "500 Commerce St",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
        "category_hint": "downtown_commercial",
        "latitude": 32.780,
        "longitude": -96.797,
        "retrieved_at": _EVIDENCE_AT,
    }
    base.update(overrides)
    return RawListing(**base)


def _configured_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")


# --- Overpass query building, parsing, and categories ---


def test_build_query_is_bounded() -> None:
    query = build_query(_BBOX, 30, 15.0)
    assert "32.77" in query and "-96.81" in query
    assert "out center 30" in query
    assert "[timeout:15]" in query
    assert "warehouse" in query and "commercial" in query


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"building": "warehouse"}, "warehouse_industrial"),
        ({"building": "industrial"}, "warehouse_industrial"),
        ({"building": "commercial"}, "downtown_commercial"),
        ({"building": "retail"}, "downtown_commercial"),
        ({"building": "office"}, "downtown_commercial"),
        ({"building": "skyscraper"}, "high_rise"),
        ({"building": "apartments", "building:levels": "12"}, "high_rise"),
        ({"building": "office", "building:levels": "3"}, "downtown_commercial"),
        ({"building": "yes", "height": "30 m"}, "high_rise"),
        ({"building": "residential", "building:levels": "2"}, "other"),
        ({"building": "church"}, "other"),
    ],
)
def test_map_category(tags, expected) -> None:
    assert map_category(tags) == expected


def test_parse_elements_assembles_address_and_links() -> None:
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 1001,
                "center": {"lat": 32.78, "lon": -96.797},
                "tags": {
                    "name": "Test Commerce Building",
                    "building": "commercial",
                    "addr:housenumber": "500",
                    "addr:street": "Commerce St",
                    "addr:city": "Dallas",
                    "addr:state": "TX",
                    "addr:postcode": "75202",
                },
            },
            {"type": "node", "id": 2002, "lat": 32.78, "lon": -96.79, "tags": {}},
            {"type": "node", "tags": {}},  # missing id: skipped
        ]
    }
    listings = parse_elements(payload, _EVIDENCE_AT)
    assert len(listings) == 2
    first = listings[0]
    assert first.street_address == "500 Commerce St"
    assert first.city == "Dallas"
    assert first.category_hint == "downtown_commercial"
    assert first.element_url == "https://www.openstreetmap.org/way/1001"
    assert first.retrieved_at == _EVIDENCE_AT
    assert listings[1].street_address is None


def test_parse_elements_rejects_malformed_payload() -> None:
    with pytest.raises(SourceError):
        parse_elements({"oops": True}, _EVIDENCE_AT)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"elements": []}

    def json(self):
        return self._payload


def test_search_filters_categories_and_limits(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    payload = {
        "elements": [
            {"type": "way", "id": 1, "tags": {"building": "warehouse", "name": "W"}},
            {"type": "way", "id": 2, "tags": {"building": "commercial", "name": "C"}},
            {"type": "way", "id": 3, "tags": {"building": "skyscraper", "name": "S"}},
        ]
    }
    seen: dict = {}

    def _post(url, data=None, headers=None, timeout=None):
        seen.update({"url": url, "timeout": timeout, "agent": headers.get("User-Agent")})
        return _FakeResponse(payload=payload)

    monkeypatch.setattr(httpx, "post", _post)
    source = OverpassSource()
    # Equal priority: provider order preserved across categories.
    found = source.search(_BBOX, ["high_rise", "downtown_commercial", "warehouse_industrial"], 10, 15.0)
    assert [item.external_id for item in found] == ["way/1", "way/2", "way/3"]
    assert seen["url"] == "https://overpass-api.de/api/interpreter"
    assert seen["timeout"] == 15.0
    assert "GrowUp" in seen["agent"]
    filtered = source.search(_BBOX, ["warehouse_industrial"], 10, 15.0)
    assert [item.external_id for item in filtered] == ["way/1"]
    limited = source.search(_BBOX, ["high_rise", "downtown_commercial", "warehouse_industrial"], 2, 15.0)
    assert len(limited) == 2


def test_search_maps_failures_without_fabrication(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    source = OverpassSource()
    cats = ["downtown_commercial"]

    def _timeout(*args, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "post", _timeout)
    with pytest.raises(SourceTimeout):
        source.search(_BBOX, cats, 5, 5.0)

    def _unreachable(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", _unreachable)
    with pytest.raises(SourceError):
        source.search(_BBOX, cats, 5, 5.0)

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(status_code=500))
    with pytest.raises(SourceError):
        source.search(_BBOX, cats, 5, 5.0)


def test_search_empty_results_returns_empty_list(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse())
    assert OverpassSource().search(_BBOX, ["other"], 5, 5.0) == []


# --- Normalization: estimated/unknown only, never verified ---


def test_normalize_builds_sourced_unknown_record() -> None:
    payload = normalize_listing(_listing(), "bbox(0,0,0,0)")
    assert payload is not None
    assert payload["name"] == "Test Commerce Building"
    assert payload["street_address"] == "500 Commerce St"
    assert payload["category"] == "downtown_commercial"
    assert "overpass" in payload["provenance"]
    assert "openstreetmap.org/way/1001" in payload["provenance"]
    assert _EVIDENCE_AT in payload["provenance"]
    assert "Missing" in payload["notes"]
    assert "not treated as proof of financial distress" in payload["notes"]
    assert payload.get("field_evidence") is None
    for key, value in payload.items():
        assert not key.endswith("_verified") or value is None
    record = PropertyCreate.model_validate(payload)
    assert record.data_status is DataStatus.UNKNOWN
    assert record.property_value_estimated is None


def test_normalize_derives_fallback_names() -> None:
    unnamed = normalize_listing(_listing(name=None), "bbox(0,0,0,0)")
    assert unnamed is not None
    assert "500 Commerce St" in unnamed["name"]
    coords_only = normalize_listing(
        _listing(name=None, street_address=None, city=None), "bbox(0,0,0,0)"
    )
    assert coords_only is not None
    assert "Unnamed" in coords_only["name"] and "unknown" in coords_only["name"].lower()
    assert normalize_listing(
        _listing(name=None, street_address=None, latitude=None, longitude=None),
        "bbox(0,0,0,0)",
    ) is None


def test_normalize_never_fabricates_markers() -> None:
    payload = normalize_listing(_listing(), "bbox(0,0,0,0)")
    assert payload is not None
    text = " ".join(str(v) for v in payload.values() if v is not None).lower()
    assert "synthetic" not in text
    assert "hypothetical" not in text
    assert "invented" not in text


# --- Dedup ---


def test_normalize_text_and_keys() -> None:
    assert normalize_text("  500 Commerce St. ") == "500 commerce st"
    assert normalize_text(None) == ""
    key_a = dedup_key({"street_address": "500 Commerce St.", "city": "Dallas"})
    key_b = dedup_key({"street_address": "500 commerce st", "city": "dallas "})
    assert key_a == key_b
    assert dedup_key({"name": "X", "city": "Dallas"}) is None


def test_dedup_key_addressless_identifiers() -> None:
    payload = {"name": "Twin Hall", "city": "Dallas"}
    # Provider element id distinguishes same-name candidates.
    key_1 = dedup_key(payload, source="overpass", external_id="way/1")
    assert key_1 == dedup_key(payload, source="overpass", external_id="way/1")
    assert key_1 != dedup_key(payload, source="overpass", external_id="way/2")
    # Exact coordinates distinguish when no element id exists.
    geo_1 = dedup_key(payload, latitude=32.78, longitude=-96.79)
    assert geo_1 == dedup_key(payload, latitude=32.78, longitude=-96.79)
    assert geo_1 != dedup_key(payload, latitude=32.79, longitude=-96.79)
    assert dedup_key(payload, latitude=32.78, longitude=None) is None
    # Addressed keys ignore identifiers entirely (behavior unchanged).
    addr = {"street_address": "500 Commerce St.", "city": "Dallas"}
    assert dedup_key(addr, external_id="way/1") == dedup_key(addr, external_id="way/2")


def test_match_existing_address_skips_addressless(monkeypatch) -> None:
    called = []

    def _must_not_query(**kwargs):
        called.append(kwargs)
        raise AssertionError("no DB lookup without an address")

    monkeypatch.setattr("app.properties_db.find_by_address", _must_not_query)
    assert match_existing_address({"name": "X"}) == AddressMatch(AddressMatchStatus.NONE)
    assert called == []


def test_match_existing_address_tri_state(monkeypatch) -> None:
    seen: dict = {}
    rows: list = []

    def _fake_find(**kwargs):
        seen.update(kwargs)
        return list(rows)

    monkeypatch.setattr("app.properties_db.find_by_address", _fake_find)
    payload = {"street_address": "500 Commerce St.", "city": "Dallas"}
    assert match_existing_address(payload) == AddressMatch(AddressMatchStatus.NONE)
    assert seen["street"] == "500 commerce st"
    assert seen["city"] == "dallas"
    rows.append({"id": "stored-id-1"})
    assert match_existing_address(payload) == AddressMatch(
        AddressMatchStatus.UNIQUE, "stored-id-1"
    )
    rows.append({"id": "stored-id-2"})
    assert match_existing_address(payload) == AddressMatch(
        AddressMatchStatus.AMBIGUOUS, None, ("stored-id-1", "stored-id-2")
    )


# --- Service orchestration ---


class _StubSource:
    name = "stub"
    description = "test stub"
    docs_url = "https://example.invalid"
    requires_credentials = False

    def __init__(self, listings=None, error=None) -> None:
        self._listings = listings if listings is not None else []
        self._error = error
        self.calls = 0

    def search(self, bbox, categories, limit, timeout_seconds):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._listings


def _run_request(**overrides) -> DiscoveryRunRequest:
    body = {"source": "stub", "bbox": dict(_BBOX_INPUT), "limit": 10, "timeout_seconds": 15.0}
    body.update(overrides)
    return DiscoveryRunRequest.model_validate(body)


def test_run_creates_new_and_skips_duplicates(monkeypatch) -> None:
    _configured_env(monkeypatch)
    stub = _StubSource(
        listings=[
            _listing(external_id="way/1", street_address="100 Main St"),
            _listing(external_id="way/2", street_address="100 Main St"),  # in-run dup
            _listing(external_id="way/3", street_address="200 Main St"),  # stored dup
            _listing(  # unusable: no location at all
                external_id="way/4", name=None, street_address=None,
                latitude=None, longitude=None,
            ),
        ]
    )
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)
    created: list = []
    monkeypatch.setattr(
        gabriel_service, "_create_with_observation",
        lambda payload, canonical, external_id: created.append(payload) or {"id": f"id-{len(created)}"},
    )
    monkeypatch.setattr(gabriel_service, "find_observation", lambda source, external_id: None)

    def _existing(payload):
        if payload.get("street_address") == "200 Main St":
            return AddressMatch(AddressMatchStatus.UNIQUE, "stored-existing")
        return AddressMatch(AddressMatchStatus.NONE)

    monkeypatch.setattr(gabriel_service, "match_existing_address", _existing)
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.returned == 4
    assert result.counts.created == 1
    assert result.counts.skipped_duplicate == 2
    assert result.counts.skipped_unusable == 1
    assert result.created_ids == ["id-1"]
    assert len(created) == 1  # duplicates never reach persistence (no overwrite)
    assert created[0]["street_address"] == "100 Main St"
    reasons = " ".join(item.reason for item in result.skipped)
    assert "already stored" in reasons and "within this run" in reasons


def test_run_reports_ambiguity_without_selecting(monkeypatch) -> None:
    _configured_env(monkeypatch)
    stub = _StubSource(listings=[_listing(external_id="way/1")])
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)
    monkeypatch.setattr(
        gabriel_service,
        "match_existing_address",
        lambda payload: AddressMatch(AddressMatchStatus.AMBIGUOUS),
    )

    def _must_not_create(*args):
        raise AssertionError("ambiguous matches must not create records")

    monkeypatch.setattr(gabriel_service, "_create_with_observation", _must_not_create)
    monkeypatch.setattr(gabriel_service, "create_property", _must_not_create)
    monkeypatch.setattr(gabriel_service, "find_observation", lambda source, external_id: None)
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.created == 0
    assert result.counts.skipped_duplicate == 1
    assert len(result.skipped) == 1
    assert "multiple stored properties" in result.skipped[0].reason
    assert "ambiguous" in result.skipped[0].reason
    assert "property " not in result.skipped[0].reason  # no id claimed


def test_run_keeps_distinct_addressless_candidates(monkeypatch) -> None:
    _configured_env(monkeypatch)
    stub = _StubSource(
        listings=[
            _listing(external_id="way/1", name="Twin Hall", street_address=None),
            _listing(external_id="way/2", name="Twin Hall", street_address=None),
            _listing(external_id="way/1", name="Twin Hall", street_address=None),
        ]
    )
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)
    created: list = []
    monkeypatch.setattr(
        gabriel_service, "_create_with_observation",
        lambda payload, canonical, external_id: created.append(payload) or {"id": f"id-{len(created)}"},
    )
    monkeypatch.setattr(gabriel_service, "find_observation", lambda source, external_id: None)
    # Real matcher: addressless payloads return NONE without touching the DB.
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.created == 2
    assert result.counts.skipped_duplicate == 1
    assert "within this run" in result.skipped[0].reason


def test_run_distinguishes_addressless_candidates_by_coordinates(monkeypatch) -> None:
    _configured_env(monkeypatch)
    stub = _StubSource(
        listings=[
            _listing(external_id=None, name="Twin Hall", street_address=None,
                     latitude=32.780, longitude=-96.790),
            _listing(external_id=None, name="Twin Hall", street_address=None,
                     latitude=32.790, longitude=-96.800),
        ]
    )
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)
    created: list = []
    monkeypatch.setattr(
        gabriel_service, "create_property", lambda data: created.append(data) or {"id": f"id-{len(created)}"}
    )
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.created == 2
    assert result.counts.skipped_duplicate == 0


def test_run_never_discards_identifierless_candidates(monkeypatch) -> None:
    _configured_env(monkeypatch)
    stub = _StubSource(
        listings=[
            _listing(external_id=None, name="Twin Hall", street_address=None,
                     latitude=None, longitude=None),
            _listing(external_id=None, name="Twin Hall", street_address=None,
                     latitude=None, longitude=None),
        ]
    )
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)
    created: list = []
    monkeypatch.setattr(
        gabriel_service, "create_property", lambda data: created.append(data) or {"id": f"id-{len(created)}"}
    )
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.created == 2
    assert result.counts.skipped_duplicate == 0


def test_run_aborts_on_source_failure_without_persisting(monkeypatch) -> None:
    _configured_env(monkeypatch)
    stub = _StubSource(error=SourceTimeout("slow"))
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)

    def _must_not_create(data):
        raise AssertionError("nothing may persist when the source fails")

    monkeypatch.setattr(gabriel_service, "create_property", _must_not_create)
    with pytest.raises(SourceTimeout):
        gabriel_service.run_discovery(_run_request())


def test_run_empty_results_persists_nothing(monkeypatch) -> None:
    _configured_env(monkeypatch)
    stub = _StubSource(listings=[])
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)

    def _must_not_create(data):
        raise AssertionError("empty results must not fabricate records")

    monkeypatch.setattr(gabriel_service, "create_property", _must_not_create)
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.returned == 0
    assert result.counts.created == 0
    assert result.created_ids == []


def test_run_requires_database_before_source_call(monkeypatch) -> None:
    for var in ("DATABASE_HOST", "DATABASE_PORT", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    stub = _StubSource(listings=[_listing()])
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)
    with pytest.raises(DatabaseNotConfiguredError):
        gabriel_service.run_discovery(_run_request())
    assert stub.calls == 0


def test_run_skips_candidates_failing_validation_gate(monkeypatch) -> None:
    _configured_env(monkeypatch)
    bad = _listing(external_id="way/9", category_hint="castle")
    stub = _StubSource(listings=[bad])
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: stub)

    def _must_not_create(*args):
        raise AssertionError("invalid candidates must not persist")

    monkeypatch.setattr(gabriel_service, "create_property", _must_not_create)
    monkeypatch.setattr(gabriel_service, "_create_with_observation", _must_not_create)
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.skipped_unusable == 1
    assert result.counts.created == 0


def test_unknown_source_name_raises() -> None:
    with pytest.raises(UnknownSourceError):
        get_source("no-such-provider")


# --- Storage lookup ---


class _FakeCursor:
    def __init__(self, recorder: dict, row: dict | None) -> None:
        self._recorder = recorder
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self._recorder["query"] = query
        self._recorder["params"] = params

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row is not None else []


class _FakeConnection:
    def __init__(self, recorder: dict, row: dict | None) -> None:
        self._recorder = recorder
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return _FakeCursor(self._recorder, self._row)

    def commit(self) -> None:
        pass


def test_find_by_address_queries_normalized_parts(monkeypatch) -> None:
    import app.properties_db as store
    from app.config import DatabaseConfig

    psycopg = pytest.importorskip("psycopg")
    recorder: dict = {}
    config = DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw")
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, {"id": "abc"})
    )
    assert store.find_by_address("500 commerce st", "dallas", config=config) == [{"id": "abc"}]
    assert "lower(btrim(street_address))" in recorder["query"]
    assert "regexp_replace" in recorder["query"]
    assert "LIMIT 2" in recorder["query"]
    assert recorder["params"]["street"] == "500 commerce st"
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, None))
    assert store.find_by_address("elsewhere", config=config) == []


# --- Discovery routes ---


def test_list_sources_reports_provider_metadata() -> None:
    response = client.get("/discovery/sources")
    assert response.status_code == 200
    body = response.json()
    overpass = next(item for item in body if item["name"] == "overpass")
    assert overpass["requires_credentials"] is False
    assert overpass["docs_url"].startswith("https://")


def test_start_run_returns_result(monkeypatch) -> None:
    from app.agents.gabriel.schemas import DiscoveryRunCounts, DiscoveryRunResult

    expected = DiscoveryRunResult(
        source="overpass",
        retrieved_at=_EVIDENCE_AT,
        categories=["downtown_commercial"],
        limit=3,
        counts=DiscoveryRunCounts(returned=1, created=1),
        created_ids=["id-1"],
    )
    monkeypatch.setattr(discovery_routes, "run_discovery", lambda body: expected)
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)
    response = client.post(
        "/discovery/runs",
        json={"source": "overpass", "bbox": _BBOX_INPUT, "limit": 3, "timeout_seconds": 10.0},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["created_ids"] == ["id-1"]


def test_start_run_maps_failures(monkeypatch) -> None:
    from app.db import DatabaseUnavailableError

    body = {"source": "overpass", "bbox": _BBOX_INPUT}
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)

    def _boom(error):
        def _raise(_body):
            raise error

        return _raise

    monkeypatch.setattr(discovery_routes, "run_discovery", _boom(UnknownSourceError("x")))
    assert client.post("/discovery/runs", json=body, headers=_headers()).status_code == 422
    monkeypatch.setattr(discovery_routes, "run_discovery", _boom(SourceTimeout("slow")))
    response = client.post("/discovery/runs", json=body, headers=_headers())
    assert response.status_code == 502
    assert response.json() == {"detail": "source_unavailable"}
    monkeypatch.setattr(discovery_routes, "run_discovery", _boom(SourceError("down")))
    assert client.post("/discovery/runs", json=body, headers=_headers()).status_code == 502
    monkeypatch.setattr(discovery_routes, "run_discovery", _boom(DatabaseUnavailableError("x")))
    assert client.post("/discovery/runs", json=body, headers=_headers()).status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"source": "overpass", "bbox": {**_BBOX_INPUT, "north": 32.0}},  # north < south
        {"source": "overpass", "bbox": {**_BBOX_INPUT, "east": -97.5}},  # span too big
        {"source": "overpass", "bbox": _BBOX_INPUT, "limit": 99},  # over cap
        {"source": "overpass", "bbox": _BBOX_INPUT, "categories": ["castle"]},
        {"source": "overpass", "bbox": _BBOX_INPUT, "timeout_seconds": 300.0},
    ],
)
def test_start_run_rejects_unbounded_input(monkeypatch, body) -> None:
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)
    assert client.post("/discovery/runs", json=body, headers=_headers()).status_code == 422
