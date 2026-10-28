"""Commercial listing intake tests (mocked database; no network fetching)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.agents.gabriel.intake as intake
from app.agents.gabriel.dedup import AddressMatch, AddressMatchStatus
from app.main import app

_NO_MATCH = AddressMatch(AddressMatchStatus.NONE)


def _unique_match(property_id: str) -> AddressMatch:
    return AddressMatch(AddressMatchStatus.UNIQUE, property_id)

client = TestClient(app, raise_server_exceptions=False)

_OBSERVED = "2026-10-20T12:00:00+00:00"


def _body(**overrides):
    body = {
        "name": "Intake Test Building",
        "street_address": "700 Intake Ave",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75201",
        "category": "downtown_commercial",
        "source_name": "Unit Test Brokerage Records",
        "listing_url": "https://example.com/listings/700-intake-ave",
        "observed_at": _OBSERVED,
    }
    body.update(overrides)
    return body


TEST_INTAKE_KEY = "unit-test-intake-key-only"


@pytest.fixture(autouse=True)
def _intake_auth(monkeypatch):
    """Provide the test-only intake key for every intake request."""
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)
    real_post = client.post

    def _post_with_key(url, *args, **kwargs):
        if url == "/discovery/intake":
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-Intake-Key", TEST_INTAKE_KEY)
            kwargs["headers"] = headers
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(client, "post", _post_with_key)


def _configured_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")


def _stored_row(**overrides):
    row = {
        "id": "12345678-1234-5678-1234-567812345678",
        "name": "Intake Test Building",
        "street_address": "700 Intake Ave",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75201",
        "category": "downtown_commercial",
        "building_area_sqft_estimated": None,
        "building_area_sqft_verified": None,
        "vacant_area_sqft_estimated": None,
        "vacant_area_sqft_verified": None,
        "occupancy": None,
        "gross_monthly_rent_income_estimated": None,
        "gross_monthly_rent_income_verified": None,
        "owner": None,
        "owner_monthly_costs_estimated": None,
        "owner_monthly_costs_verified": None,
        "property_value_estimated": None,
        "property_value_verified": None,
        "liens_total_estimated": None,
        "liens_total_verified": None,
        "months_on_market_estimated": None,
        "months_on_market_verified": None,
        "months_vacant_estimated": None,
        "months_vacant_verified": None,
        "provenance": "unit-test prior source record QA-009",
        "field_evidence": None,
        "data_status": "unknown",
        "notes": None,
        "created_at": "2026-10-20T00:00:00+00:00",
        "updated_at": "2026-10-20T00:00:00+00:00",
    }
    row.update(overrides)
    return row


# 1. Valid commercial listing intake.


def test_valid_intake_creates_record_with_evidence(monkeypatch) -> None:
    _configured_env(monkeypatch)
    created: dict = {}
    evidence: dict = {}
    distress: dict = {}
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _NO_MATCH)
    monkeypatch.setattr(
        intake, "create_property", lambda data: created.update(data) or {"id": "new-prop-1"}
    )
    monkeypatch.setattr(
        intake,
        "create_listing_evidence",
        lambda **kwargs: evidence.update(kwargs) or {"id": "ev-1"},
    )
    monkeypatch.setattr(
        intake,
        "create_distress_observations",
        lambda property_id, rows, **_: distress.update({"rows": rows}) or ["d-1"],
    )
    response = client.post(
        "/discovery/intake",
        json=_body(
            asking_price="1250000.00",
            advertised_building_area_sqft=20000,
            asking_rent_monthly="8000.00",
            owner_advertised="Advertised Holdings LLC",
            listing_type="sale",
            listing_date="2026-10-18",
            listing_notes="Corner unit with loading dock.",
            price_reductions=["Reduced from $1.4M on 2026-10-10."],
            distress_observations=[
                {
                    "indicator_type": "price_reduction",
                    "description": "Asking price cut from $1.4M to $1.25M per listing history.",
                }
            ],
        ),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["outcome"] == "created"
    assert body["property_id"] == "new-prop-1"
    assert set(body["filled_fields"]) == {"building_area_sqft_estimated"}
    assert body["listing_evidence_id"] == "ev-1"
    assert body["distress_observation_ids"] == ["d-1"]
    # Mapping discipline: asking price is evidence only, never a value estimate.
    assert created["property_value_estimated"] is None
    assert created["property_value_verified"] is None
    assert created["building_area_sqft_estimated"] == 20000  # building area still maps
    assert created["owner"] is None  # advertised owner stays out of ownership
    assert created["gross_monthly_rent_income_estimated"] is None  # asking rent is not income
    assert created["field_evidence"]["building_area_sqft_estimated"].startswith("Advertised in")
    assert "property_value_estimated" not in created["field_evidence"]
    assert evidence["property_id"] == "new-prop-1"
    assert evidence["payload"]["asking_price"] == "1250000.00"  # exact decimal string
    assert evidence["payload"]["asking_rent_monthly"] == "8000.00"
    assert evidence["payload"]["owner_advertised"] == "Advertised Holdings LLC"
    assert evidence["payload"]["listing_type"] == "sale"
    assert evidence["payload"]["price_reductions"] == ["Reduced from $1.4M on 2026-10-10."]
    assert distress["rows"][0]["status"] == "preliminary"
    assert distress["rows"][0]["source_url"] == "https://example.com/listings/700-intake-ave"


# 2-4. Provenance, URL, and timestamp validation.


@pytest.mark.parametrize(
    "override",
    [
        {"source_name": "x"},  # too short to be traceable
        {"source_name": "test"},  # placeholder
        {"observed_at": "2999-01-01T00:00:00+00:00"},  # future
        {"observed_at": "not-a-timestamp"},
        {"listing_url": "not-a-url"},
        {"listing_url": "ftp://example.com/file"},
        {"listing_date": "2999-01-01"},
    ],
)
def test_invalid_provenance_rejected(override) -> None:
    assert client.post("/discovery/intake", json=_body(**override)).status_code == 422


def test_missing_provenance_rejected() -> None:
    body = _body()
    del body["source_name"]
    assert client.post("/discovery/intake", json=body).status_code == 422
    body = _body()
    del body["listing_url"]
    assert client.post("/discovery/intake", json=body).status_code == 422


def test_identity_required() -> None:
    body = _body(name=None, street_address=None)
    assert client.post("/discovery/intake", json=body).status_code == 422


# 5-7. Unknowns stay unknown; advertised never becomes verified.


def test_missing_financials_stay_unknown(monkeypatch) -> None:
    _configured_env(monkeypatch)
    created: dict = {}
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _NO_MATCH)
    monkeypatch.setattr(
        intake, "create_property", lambda data: created.update(data) or {"id": "new-prop-2"}
    )
    monkeypatch.setattr(intake, "create_listing_evidence", lambda **kwargs: {"id": "ev-2"})
    monkeypatch.setattr(intake, "create_distress_observations", lambda pid, rows, **_: [])
    response = client.post("/discovery/intake", json=_body())
    assert response.status_code == 201
    assert response.json()["filled_fields"] == []
    for key in (
        "property_value_estimated",
        "gross_monthly_rent_income_estimated",
        "owner_monthly_costs_estimated",
        "liens_total_estimated",
    ):
        assert created[key] is None
    assert "Unknown" in created["notes"]


def test_available_area_preserved_as_evidence_only(monkeypatch) -> None:
    _configured_env(monkeypatch)
    created: dict = {}
    evidence: dict = {}
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _NO_MATCH)
    monkeypatch.setattr(
        intake, "create_property", lambda data: created.update(data) or {"id": "new-prop-3"}
    )
    monkeypatch.setattr(
        intake, "create_listing_evidence", lambda **kwargs: evidence.update(kwargs) or {"id": "ev-3"}
    )
    monkeypatch.setattr(intake, "create_distress_observations", lambda pid, rows, **_: [])
    response = client.post(
        "/discovery/intake",
        json=_body(advertised_building_area_sqft=30000, advertised_available_area_sqft=5000),
    )
    assert response.status_code == 201
    # Available space may still be occupied: no vacancy inference.
    assert created["vacant_area_sqft_estimated"] is None
    assert created["vacant_area_sqft_verified"] is None
    assert created["building_area_sqft_estimated"] == 30000  # building area still maps
    assert evidence["payload"]["advertised_available_area_sqft"] == 5000
    assert response.json()["filled_fields"] == ["building_area_sqft_estimated"]


def test_asking_price_preserved_exactly_as_decimal_string(monkeypatch) -> None:
    _configured_env(monkeypatch)
    created: dict = {}
    evidence: dict = {}
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _NO_MATCH)
    monkeypatch.setattr(
        intake, "create_property", lambda data: created.update(data) or {"id": "new-prop-4"}
    )
    monkeypatch.setattr(
        intake, "create_listing_evidence", lambda **kwargs: evidence.update(kwargs) or {"id": "ev-5"}
    )
    monkeypatch.setattr(intake, "create_distress_observations", lambda pid, rows, **_: [])
    response = client.post("/discovery/intake", json=_body(asking_price="1250000.10"))
    assert response.status_code == 201
    assert evidence["payload"]["asking_price"] == "1250000.10"
    assert isinstance(evidence["payload"]["asking_price"], str)
    assert created["property_value_estimated"] is None
    assert created["property_value_verified"] is None
    assert response.json()["filled_fields"] == []


# 8. Evidence preservation covered in test 1; distress validation below.


# 9. Distress observation validation.


def test_distress_verified_status_rejected() -> None:
    body = _body(
        distress_observations=[
            {"indicator_type": "other", "description": "Long enough description here.", "status": "verified"}
        ]
    )
    assert client.post("/discovery/intake", json=body).status_code == 422


def test_distress_invalid_indicator_rejected() -> None:
    body = _body(
        distress_observations=[
            {"indicator_type": "haunted", "description": "Long enough description here."}
        ]
    )
    assert client.post("/discovery/intake", json=body).status_code == 422


# 10-11. Duplicates and verified protection.


def test_duplicate_listing_skipped_without_changes(monkeypatch) -> None:
    _configured_env(monkeypatch)
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _unique_match("22345678-1234-5678-1234-567812345678"))
    monkeypatch.setattr(intake, "get_property", lambda pid: _stored_row(id="22345678-1234-5678-1234-567812345678"))
    monkeypatch.setattr(intake, "get_listing_evidence", lambda pid, url: {"id": "ev-old"})

    def _must_not_write(*args, **kwargs):
        raise AssertionError("duplicate resubmission must not write")

    monkeypatch.setattr(intake, "create_property", _must_not_write)
    monkeypatch.setattr(intake, "enrich_property_estimates", _must_not_write)
    monkeypatch.setattr(intake, "create_listing_evidence", _must_not_write)
    monkeypatch.setattr(intake, "create_distress_observations", _must_not_write)
    response = client.post("/discovery/intake", json=_body())
    assert response.status_code == 200
    assert response.json()["outcome"] == "skipped_duplicate"
    assert response.json()["property_id"] == "22345678-1234-5678-1234-567812345678"


def test_verified_values_protected_during_enrichment(monkeypatch) -> None:
    _configured_env(monkeypatch)
    existing = _stored_row(
        id="32345678-1234-5678-1234-567812345678",
        property_value_verified="900000.00",
        field_evidence={"property_value_verified": "county appraisal record"},
    )
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _unique_match("32345678-1234-5678-1234-567812345678"))
    monkeypatch.setattr(intake, "get_property", lambda pid: dict(existing))
    monkeypatch.setattr(intake, "get_listing_evidence", lambda pid, url: None)
    enriched: dict = {}
    monkeypatch.setattr(
        intake,
        "enrich_property_estimates",
        lambda pid, fills, ev, st: enriched.update(
            {"fills": fills, "evidence": ev, "status": st}) or list(fills),
    )
    monkeypatch.setattr(intake, "create_listing_evidence", lambda **kwargs: {"id": "ev-new"})
    monkeypatch.setattr(intake, "create_distress_observations", lambda pid, rows, **_: [])
    response = client.post(
        "/discovery/intake", json=_body(asking_price="1250000.00", advertised_building_area_sqft=20000)
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "enriched"
    # New estimates fill NULLs; the verified appraisal value is untouched.
    # Asking price stays evidence-only and never enters the fills.
    assert set(enriched["fills"]) == {"building_area_sqft_estimated"}
    assert "property_value_verified" not in enriched["fills"]
    assert "property_value_estimated" not in enriched["fills"]
    assert enriched["evidence"]["property_value_verified"] == "county appraisal record"
    assert "building_area_sqft_estimated" in enriched["evidence"]


# 12. Safe enrichment / conservative skip.


def test_conflicting_enrichment_skipped_but_evidence_kept(monkeypatch) -> None:
    _configured_env(monkeypatch)
    existing = _stored_row(id="42345678-1234-5678-1234-567812345678", vacant_area_sqft_estimated=5000.0)
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _unique_match("42345678-1234-5678-1234-567812345678"))
    monkeypatch.setattr(intake, "get_property", lambda pid: dict(existing))
    monkeypatch.setattr(intake, "get_listing_evidence", lambda pid, url: None)
    monkeypatch.setattr(
        intake, "enrich_property_estimates", lambda pid, fills, ev, st: list(fills)
    )
    recorded: dict = {}
    monkeypatch.setattr(
        intake, "create_listing_evidence", lambda **kwargs: recorded.update(kwargs) or {"id": "ev-4"}
    )
    monkeypatch.setattr(intake, "create_distress_observations", lambda pid, rows, **_: [])
    # Building 100 would undercut the stored vacant figure of 5000: conflict.
    response = client.post(
        "/discovery/intake", json=_body(advertised_building_area_sqft=100)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "enriched"
    assert body["filled_fields"] == []
    assert body["listing_evidence_id"] == "ev-4"
    assert "conflict" in body["message"]


# 13. Synthetic/hypothetical rejection.


@pytest.mark.parametrize(
    "source_name",
    ["synthetic test listings feed for checks", "hypothetical brokerage demo data feed"],
)
def test_unverified_sources_rejected(source_name) -> None:
    assert client.post("/discovery/intake", json=_body(source_name=source_name)).status_code == 422


# 14. Oversized requests.


def test_oversized_requests_rejected() -> None:
    obs = {"indicator_type": "other", "description": "Long enough description here."}
    assert client.post("/discovery/intake", json=_body(distress_observations=[obs] * 11)).status_code == 422
    assert client.post("/discovery/intake", json=_body(price_reductions=["x"] * 11)).status_code == 422
    assert client.post("/discovery/intake", json=_body(listing_notes="n" * 2001)).status_code == 422


# 15. No automatic network fetching of supplied URLs.


def test_intake_fetches_nothing(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    _configured_env(monkeypatch)

    def _must_not_fetch(*args, **kwargs):
        raise AssertionError("intake must never fetch submitted URLs")

    monkeypatch.setattr(httpx, "get", _must_not_fetch)
    monkeypatch.setattr(httpx, "post", _must_not_fetch)
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _NO_MATCH)
    monkeypatch.setattr(intake, "create_property", lambda data: {"id": "new-prop-9"})
    monkeypatch.setattr(intake, "create_listing_evidence", lambda **kwargs: {"id": "ev-9"})
    monkeypatch.setattr(intake, "create_distress_observations", lambda pid, rows, **_: [])
    assert client.post("/discovery/intake", json=_body()).status_code == 201


# 16-17. Existing provider and property regression markers.


def test_existing_discovery_endpoints_intact(monkeypatch) -> None:
    response = client.get("/discovery/sources")
    assert response.status_code == 200
    assert {item["name"] for item in response.json()} >= {"overpass", "osm_api"}


def test_existing_property_endpoint_intact(monkeypatch) -> None:
    import app.routes.properties as property_routes

    row = _stored_row()
    monkeypatch.setattr(property_routes, "create_property", lambda data: row)
    response = client.post(
        "/properties",
        json={"name": "X", "category": "other", "provenance": "unit-test evidence record QA-001"},
        headers={"X-Intake-Key": TEST_INTAKE_KEY},
    )
    assert response.status_code == 201


# 18. Documented security limitation.


def test_security_limitation_is_explicit() -> None:
    assert "X-Intake-Key" in intake.SECURITY_LIMITATION
    assert "INTAKE_API_KEY" in intake.SECURITY_LIMITATION
    assert "unauthenticated" in intake.SECURITY_LIMITATION


def test_intake_missing_key_configuration_fails_closed(monkeypatch) -> None:
    _configured_env(monkeypatch)
    monkeypatch.delenv("INTAKE_API_KEY", raising=False)
    response = client.request(
        "POST", "/discovery/intake", json=_body(),
        headers={"X-Intake-Key": TEST_INTAKE_KEY},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "intake_not_configured"}
    assert TEST_INTAKE_KEY not in response.text


def test_intake_empty_key_configuration_fails_closed(monkeypatch) -> None:
    _configured_env(monkeypatch)
    monkeypatch.setenv("INTAKE_API_KEY", "   ")
    response = client.request(
        "POST", "/discovery/intake", json=_body(),
        headers={"X-Intake-Key": TEST_INTAKE_KEY},
    )
    assert response.status_code == 503


def test_intake_missing_header_rejected() -> None:
    response = client.request("POST", "/discovery/intake", json=_body())
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_intake_incorrect_header_rejected() -> None:
    response = client.request(
        "POST", "/discovery/intake", json=_body(),
        headers={"X-Intake-Key": "wrong-key"},
    )
    assert response.status_code == 401
    assert "wrong-key" not in response.text
    assert TEST_INTAKE_KEY not in response.text


def test_intake_valid_header_accepted(monkeypatch) -> None:
    _configured_env(monkeypatch)
    monkeypatch.setattr(intake, "match_existing_address", lambda payload: _NO_MATCH)
    monkeypatch.setattr(intake, "create_property", lambda data: {"id": "auth-prop-1"})
    monkeypatch.setattr(intake, "create_listing_evidence", lambda **kwargs: {"id": "ev-a"})
    monkeypatch.setattr(intake, "create_distress_observations", lambda pid, rows, **_: [])
    response = client.request(
        "POST", "/discovery/intake", json=_body(),
        headers={"X-Intake-Key": TEST_INTAKE_KEY},
    )
    assert response.status_code == 201
    assert response.json()["outcome"] == "created"


def test_public_endpoints_need_no_key_while_properties_fails_closed(monkeypatch) -> None:
    import app.routes.properties as property_routes

    monkeypatch.delenv("INTAKE_API_KEY", raising=False)
    assert client.get("/discovery/sources").status_code == 200
    assert client.get("/health").status_code == 200
    monkeypatch.setattr(
        property_routes, "create_property", lambda data: _stored_row()
    )
    # Step 73A: /properties now requires the intake key. With no
    # server key configured it fails closed instead of creating.
    response = client.post(
        "/properties",
        json={
            "name": "X",
            "category": "other",
            "provenance": "unit-test evidence record QA-001",
        },
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "intake_not_configured"}


# Storage-layer checks with a stubbed driver.


class _FakeCursor:
    def __init__(self, recorder: dict, rows: list) -> None:
        self._recorder = recorder
        self._rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self._recorder.setdefault("queries", []).append(query)
        self._recorder["params"] = params

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self, recorder: dict, rows: list) -> None:
        self._recorder = recorder
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return _FakeCursor(self._recorder, self._rows)

    def commit(self) -> None:
        self._recorder["committed"] = True


def _configured():
    from app.config import DatabaseConfig

    return DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw")


def test_listings_db_inserts_evidence_and_observations(monkeypatch) -> None:
    import app.listings_db as listings

    psycopg = pytest.importorskip("psycopg")
    recorder: dict = {}
    config = _configured()
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, [{"id": "ev-1"}])
    )
    row = listings.create_listing_evidence(
        "prop-1", "Test Source Records", "https://example.com/x",
        datetime(2026, 10, 20, tzinfo=timezone.utc), "sale", {"a": 1}, config=config,
    )
    assert row == {"id": "ev-1"}
    assert "INSERT INTO listing_evidence" in recorder["queries"][0]
    assert recorder["params"]["listing_type"] == "sale"

    recorder.clear()
    monkeypatch.setattr(
        psycopg, "connect",
        lambda **kwargs: _FakeConnection(recorder, [{"id": "d-1"}, {"id": "d-2"}]),
    )
    obs = {
        "indicator_type": "other",
        "source_name": "Test Source Records",
        "source_url": "https://example.com/x",
        "observed_at": datetime(2026, 10, 20, tzinfo=timezone.utc),
        "description": "test observation",
        "status": "preliminary",
    }
    assert listings.create_distress_observations("prop-1", [obs, obs], config=config) == ["d-1", "d-2"]
    assert len(recorder["queries"]) == 2
    assert listings.create_distress_observations("prop-1", [], config=config) == []


def test_enrich_updates_null_estimates_only(monkeypatch) -> None:
    import app.properties_db as store

    psycopg = pytest.importorskip("psycopg")
    recorder: dict = {}
    config = _configured()
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, [{"id": "p"}])
    )
    filled = store.enrich_property_estimates(
        "prop-1", {"property_value_estimated": "100"}, {"property_value_estimated": "ref"},
        "estimated", config=config,
    )
    assert filled == ["property_value_estimated"]
    assert "UPDATE properties SET property_value_estimated" in recorder["queries"][0]
    assert "property_value_estimated IS NULL" in recorder["queries"][0]
    assert recorder["committed"] is True
    with pytest.raises(ValueError, match="estimated fields only"):
        store.enrich_property_estimates("prop-1", {"property_value_verified": "1"}, None, "x")


def test_migration_002_adds_evidence_tables_only() -> None:
    sql_path = Path(__file__).resolve().parent.parent / "migrations" / "002_listing_evidence.sql"
    sql = sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS listing_evidence" in sql
    assert "CREATE TABLE IF NOT EXISTS distress_observations" in sql
    assert "REFERENCES properties(id)" in sql
    assert "ALTER TABLE properties" not in sql
    assert "CREATE TABLE IF NOT EXISTS properties" not in sql


def test_intake_database_failure_maps_to_503(monkeypatch) -> None:
    from app.db import DatabaseUnavailableError

    _configured_env(monkeypatch)

    def _fail(*args, **kwargs):
        raise DatabaseUnavailableError("connection_failed")

    monkeypatch.setattr(intake, "match_existing_address", _fail)
    response = client.post("/discovery/intake", json=_body())
    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}
