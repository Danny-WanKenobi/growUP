"""Property API and storage tests (mocked database; no real PostgreSQL).

The operational record separates estimated from verified values with a
server-derived data_status. These tests use disposable mock rows; the
synthetic Dallas fixture is read for isolated assertions but never
inserted anywhere.
"""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.routes.properties as property_routes
from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.main import app
from app.properties import DataStatus, PropertyCreate, derive_data_status

client = TestClient(app, raise_server_exceptions=False)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_dallas_property.json"
LOADER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "load_synthetic_fixture.py"

_EVIDENCE = "unit-test evidence record QA-001, county file R-1234"
_FIELD_REF = "unit-test deed book 12, page 34"


def _stored_row(**overrides):
    row = {
        "id": "12345678-1234-5678-1234-567812345678",
        "name": "Unit Test Property",
        "street_address": "1 Example Way",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75201",
        "category": "downtown_commercial",
        "building_area_sqft_estimated": 50000.0,
        "building_area_sqft_verified": None,
        "vacant_area_sqft_estimated": 20000.0,
        "vacant_area_sqft_verified": None,
        "occupancy": "partially leased (test)",
        "gross_monthly_rent_income_estimated": Decimal("45000.50"),
        "gross_monthly_rent_income_verified": None,
        "owner": "Test Owner",
        "owner_monthly_costs_estimated": None,
        "owner_monthly_costs_verified": None,
        "property_value_estimated": Decimal("4500000.10"),
        "property_value_verified": None,
        "liens_total_estimated": None,
        "liens_total_verified": None,
        "provenance": _EVIDENCE,
        "field_evidence": None,
        "data_status": "estimated",
        "notes": "test row",
        "created_at": "2026-10-26T00:00:00+00:00",
        "updated_at": "2026-10-26T00:00:00+00:00",
    }
    row.update(overrides)
    return row


_CREATE_BODY = {
    "name": "Unit Test Property",
    "city": "Dallas",
    "state": "TX",
    "category": "downtown_commercial",
    "building_area_sqft_estimated": 50000,
    "vacant_area_sqft_estimated": 20000,
    "property_value_estimated": "4500000.10",
    "provenance": _EVIDENCE,
}


def test_create_sourced_estimate_returns_201_with_derived_status(monkeypatch) -> None:
    monkeypatch.setattr(property_routes, "create_property", lambda data: _stored_row())
    response = client.post("/properties", json=_CREATE_BODY)
    assert response.status_code == 201
    body = response.json()
    assert body["data_status"] == "estimated"
    assert body["property_value_estimated"] == "4500000.10"  # exact Decimal, never float
    assert body["property_value_verified"] is None  # unknown stays null, never zero
    assert body["id"] == _stored_row()["id"]
    assert response.headers["Location"] == f"/properties/{_stored_row()['id']}"


def test_create_money_stays_decimal_without_float(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(property_routes, "create_property", lambda data: seen.update(data) or _stored_row())
    assert client.post("/properties", json=_CREATE_BODY).status_code == 201
    assert seen["property_value_estimated"] == Decimal("4500000.10")
    assert isinstance(seen["property_value_estimated"], Decimal)


def test_client_status_label_is_derived_not_trusted(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(property_routes, "create_property", lambda data: seen.update(data) or _stored_row())
    body = dict(_CREATE_BODY, data_status="verified")  # estimates only: cannot be verified
    assert client.post("/properties", json=body).status_code == 201
    assert seen["data_status"] is DataStatus.ESTIMATED


def test_create_unknown_record_without_values(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        property_routes, "create_property", lambda data: seen.update(data) or _stored_row(
            building_area_sqft_estimated=None,
            vacant_area_sqft_estimated=None,
            gross_monthly_rent_income_estimated=None,
            property_value_estimated=None,
            provenance=None,
            data_status="unknown",
        )
    )
    response = client.post("/properties", json={"name": "X", "category": "other"})
    assert response.status_code == 201
    assert response.json()["data_status"] == "unknown"
    assert seen["data_status"] is DataStatus.UNKNOWN


def test_create_mixed_record(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(property_routes, "create_property", lambda data: seen.update(data) or _stored_row(
        building_area_sqft_verified=48000.0,
        field_evidence={"building_area_sqft_verified": _FIELD_REF},
        data_status="mixed",
    ))
    body = dict(
        _CREATE_BODY,
        building_area_sqft_verified=48000.0,
        field_evidence={"building_area_sqft_verified": _FIELD_REF},
    )
    response = client.post("/properties", json=body)
    assert response.status_code == 201
    assert response.json()["data_status"] == "mixed"
    assert seen["data_status"] is DataStatus.MIXED


def test_create_rejects_hypothetical_status() -> None:
    body = dict(_CREATE_BODY, data_status="hypothetical")
    response = client.post("/properties", json=body)
    assert response.status_code == 422


def test_create_rejects_estimates_without_provenance() -> None:
    body = {k: v for k, v in _CREATE_BODY.items() if k != "provenance"}
    assert client.post("/properties", json=body).status_code == 422
    body = dict(_CREATE_BODY, provenance="too short")
    assert client.post("/properties", json=body).status_code == 422
    body = dict(_CREATE_BODY, provenance="synthetic test source for unit checks")
    assert client.post("/properties", json=body).status_code == 422


def test_create_rejects_verified_field_without_evidence() -> None:
    body = dict(_CREATE_BODY, property_value_verified="4400000.00")
    assert client.post("/properties", json=body).status_code == 422
    body = dict(
        _CREATE_BODY,
        property_value_verified="4400000.00",
        field_evidence={"property_value_verified": "  "},
    )
    assert client.post("/properties", json=body).status_code == 422
    body = dict(
        _CREATE_BODY,
        property_value_verified="4400000.00",
        field_evidence={"not_a_field": _FIELD_REF},
    )
    assert client.post("/properties", json=body).status_code == 422
    body = dict(
        _CREATE_BODY,
        property_value_verified="4400000.00",
        field_evidence={"property_value_verified": "synthetic guess"},
    )
    assert client.post("/properties", json=body).status_code == 422


def test_create_rejects_same_class_vacant_excess() -> None:
    body = dict(_CREATE_BODY, building_area_sqft_estimated=100, vacant_area_sqft_estimated=200)
    assert client.post("/properties", json=body).status_code == 422
    body = dict(
        _CREATE_BODY,
        building_area_sqft_verified=100.0,
        vacant_area_sqft_verified=200.0,
        field_evidence={
            "building_area_sqft_verified": _FIELD_REF,
            "vacant_area_sqft_verified": _FIELD_REF,
        },
    )
    assert client.post("/properties", json=body).status_code == 422


def test_create_allows_cross_class_vacant_difference(monkeypatch) -> None:
    # Estimated vacant may exceed a verified building figure: different
    # evidence classes are not comparable, so no constraint applies.
    monkeypatch.setattr(property_routes, "create_property", lambda data: _stored_row(
        building_area_sqft_estimated=None,
        vacant_area_sqft_estimated=200.0,
        building_area_sqft_verified=100.0,
        field_evidence={"building_area_sqft_verified": _FIELD_REF},
        data_status="mixed",
    ))
    body = {
        "name": "X",
        "category": "other",
        "provenance": _EVIDENCE,
        "vacant_area_sqft_estimated": 200.0,
        "building_area_sqft_verified": 100.0,
        "field_evidence": {"building_area_sqft_verified": _FIELD_REF},
    }
    assert client.post("/properties", json=body).status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {"category": "other", "provenance": _EVIDENCE},  # missing name
        {"name": "X", "category": "castle", "provenance": _EVIDENCE},  # bad category
        {"name": "X", "category": "other", "provenance": _EVIDENCE,  # negative area
         "building_area_sqft_estimated": -5},
        {"name": "X", "category": "other", "provenance": _EVIDENCE,  # negative money
         "liens_total_verified": "-1",
         "field_evidence": {"liens_total_verified": _FIELD_REF}},
    ],
)
def test_create_property_rejects_invalid_input(body) -> None:
    assert client.post("/properties", json=body).status_code == 422


def test_float_money_input_keeps_decimal_exactness() -> None:
    record = PropertyCreate(
        name="X", category="other", provenance=_EVIDENCE, property_value_estimated=0.1
    )
    assert record.property_value_estimated == Decimal("0.1")
    assert isinstance(record.property_value_estimated, Decimal)


def test_derive_data_status_covers_all_classes() -> None:
    assert derive_data_status({}) is DataStatus.UNKNOWN
    assert derive_data_status({"property_value_estimated": Decimal("1")}) is DataStatus.ESTIMATED
    assert derive_data_status({"property_value_verified": Decimal("1")}) is DataStatus.VERIFIED
    assert derive_data_status({
        "property_value_estimated": Decimal("1"),
        "property_value_verified": Decimal("1"),
    }) is DataStatus.MIXED
    assert derive_data_status({"property_value_estimated": None}) is DataStatus.UNKNOWN


def test_create_sourced_distress_estimates(monkeypatch) -> None:
    seen: dict = {}

    def _store(data):
        seen.update(data)
        return _stored_row(
            building_area_sqft_estimated=None,
            vacant_area_sqft_estimated=None,
            gross_monthly_rent_income_estimated=None,
            property_value_estimated=None,
            months_on_market_estimated=9,
            months_vacant_estimated=None,
            data_status="estimated",
        )

    monkeypatch.setattr(property_routes, "create_property", _store)
    body = {
        "name": "X",
        "category": "other",
        "provenance": _EVIDENCE,
        "months_on_market_estimated": 9,
    }
    response = client.post("/properties", json=body)
    assert response.status_code == 201
    assert response.json()["data_status"] == "estimated"
    assert response.json()["months_on_market_estimated"] == 9
    # Listed-for-sale and vacant are separate: one never implies the other.
    assert response.json()["months_vacant_estimated"] is None
    assert seen["data_status"] is DataStatus.ESTIMATED


def test_create_verified_distress_requires_evidence(monkeypatch) -> None:
    body = {
        "name": "X",
        "category": "other",
        "provenance": _EVIDENCE,
        "months_vacant_verified": 14,
    }
    assert client.post("/properties", json=body).status_code == 422

    seen: dict = {}
    monkeypatch.setattr(
        property_routes,
        "create_property",
        lambda data: seen.update(data)
        or _stored_row(
            building_area_sqft_estimated=None,
            vacant_area_sqft_estimated=None,
            gross_monthly_rent_income_estimated=None,
            property_value_estimated=None,
            months_vacant_verified=14,
            field_evidence={"months_vacant_verified": _FIELD_REF},
            data_status="verified",
        ),
    )
    body["field_evidence"] = {"months_vacant_verified": _FIELD_REF}
    response = client.post("/properties", json=body)
    assert response.status_code == 201
    assert response.json()["data_status"] == "verified"
    assert seen["data_status"] is DataStatus.VERIFIED


@pytest.mark.parametrize(
    "body",
    [
        {"name": "X", "category": "other", "provenance": _EVIDENCE,  # negative
         "months_on_market_estimated": -1},
        {"name": "X", "category": "other", "provenance": _EVIDENCE,  # fractional
         "months_vacant_estimated": 2.5},
        {"name": "X", "category": "other",  # unsourced distress estimate
         "months_on_market_estimated": 6},
    ],
)
def test_create_rejects_invalid_distress_input(body) -> None:
    assert client.post("/properties", json=body).status_code == 422


def test_get_property_returns_record(monkeypatch) -> None:
    monkeypatch.setattr(property_routes, "get_property", lambda pid: _stored_row())
    response = client.get(f"/properties/{_stored_row()['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["data_status"] == "estimated"
    assert body["gross_monthly_rent_income_estimated"] == "45000.50"


def test_get_property_missing_id_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(property_routes, "get_property", lambda pid: None)
    response = client.get("/properties/12345678-1234-5678-1234-567812345678")
    assert response.status_code == 404
    assert response.json() == {"detail": "property_not_found"}


def test_get_property_invalid_id_returns_422() -> None:
    assert client.get("/properties/not-a-uuid").status_code == 422


def test_list_properties_returns_records(monkeypatch) -> None:
    monkeypatch.setattr(
        property_routes, "list_properties", lambda limit=100, offset=0: [_stored_row()]
    )
    response = client.get("/properties")
    assert response.status_code == 200
    assert response.json()[0]["id"] == _stored_row()["id"]


def test_list_properties_empty(monkeypatch) -> None:
    monkeypatch.setattr(property_routes, "list_properties", lambda limit=100, offset=0: [])
    response = client.get("/properties")
    assert response.status_code == 200
    assert response.json() == []


def test_property_endpoints_map_database_failure_to_503(monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise DatabaseUnavailableError("connection_failed")

    monkeypatch.setattr(property_routes, "create_property", _fail)
    monkeypatch.setattr(property_routes, "get_property", _fail)
    monkeypatch.setattr(property_routes, "list_properties", _fail)
    assert client.post("/properties", json=_CREATE_BODY).status_code == 503
    assert client.get("/properties").status_code == 503
    response = client.get(f"/properties/{_stored_row()['id']}")
    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}


def test_property_create_not_configured_returns_503(monkeypatch) -> None:
    def _fail(data):
        raise DatabaseNotConfiguredError("database_not_configured")

    monkeypatch.setattr(property_routes, "create_property", _fail)
    response = client.post("/properties", json=_CREATE_BODY)
    assert response.status_code == 503
    assert response.json() == {"detail": "database_not_configured"}


# --- Storage-layer checks with a stubbed psycopg driver ---


class _FakeCursor:
    def __init__(self, recorder: dict, row: dict | None) -> None:
        self._recorder = recorder
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self._recorder.setdefault("queries", []).append(query)
        self._recorder["params"] = params

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._recorder.get("rows", [])


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
        self._recorder["committed"] = True


_CONFIGURED = DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw")


def test_storage_create_derives_status_and_returns_row(monkeypatch) -> None:
    import app.properties_db as store

    psycopg = pytest.importorskip("psycopg")
    recorder: dict = {}
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, _stored_row())
    )
    row = store.create_property(
        {"name": "X", "category": "other", "provenance": _EVIDENCE,
         "property_value_estimated": Decimal("10.25"), "data_status": "verified"},
        config=_CONFIGURED,
    )
    assert row["id"] == _stored_row()["id"]
    assert recorder["committed"] is True
    assert "INSERT INTO properties" in recorder["queries"][0]
    assert recorder["params"]["data_status"] == "estimated"  # derived, not trusted
    assert recorder["params"]["property_value_estimated"] == Decimal("10.25")
    assert isinstance(recorder["params"]["property_value_estimated"], Decimal)


def test_storage_create_rejects_hypothetical_payload() -> None:
    import app.properties_db as store

    with pytest.raises(ValueError, match="hypothetical"):
        store.create_property(
            {"name": "X", "category": "other", "data_status": "hypothetical"},
            config=_CONFIGURED,
        )


def test_storage_get_returns_none_when_missing(monkeypatch) -> None:
    import app.properties_db as store

    psycopg = pytest.importorskip("psycopg")
    recorder: dict = {}
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, None))
    from uuid import UUID

    assert store.get_property(UUID(_stored_row()["id"]), config=_CONFIGURED) is None


def test_storage_wraps_driver_errors_generically(monkeypatch) -> None:
    import app.properties_db as store

    psycopg = pytest.importorskip("psycopg")

    def _fail(**kwargs):
        raise ConnectionError("dial tcp refused")

    monkeypatch.setattr(psycopg, "connect", _fail)
    with pytest.raises(DatabaseUnavailableError):
        store.list_properties(config=_CONFIGURED)


def test_storage_requires_configuration() -> None:
    import app.properties_db as store

    unconfigured = DatabaseConfig(host="h", port=5432, name="db", user="u", password="")
    with pytest.raises(DatabaseNotConfiguredError):
        store.list_properties(config=unconfigured)


# --- Fixture, loader, and migration checks ---


def _load_loader_module():
    spec = importlib.util.spec_from_file_location("synthetic_fixture_loader", LOADER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_synthetic_fixture_is_labeled_and_rejected_by_operational_gate() -> None:
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    assert "SYNTHETIC" in raw
    assert "NOT A REAL" in raw
    payload = json.loads(raw)
    payload.pop("_label", None)
    assert payload["data_status"] == "hypothetical"
    with pytest.raises(Exception, match="[Hh]ypothetical"):
        PropertyCreate.model_validate(payload)


def test_loader_refuses_synthetic_fixture(capsys) -> None:
    loader = _load_loader_module()
    assert loader.main() == 1
    captured = capsys.readouterr()
    assert "refused" in captured.err


def test_migration_defines_estimated_verified_schema() -> None:
    sql_path = Path(__file__).resolve().parent.parent / "migrations" / "001_create_properties.sql"
    sql = sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS properties" in sql
    for column in (
        "building_area_sqft_estimated DOUBLE PRECISION",
        "building_area_sqft_verified DOUBLE PRECISION",
        "vacant_area_sqft_estimated DOUBLE PRECISION",
        "vacant_area_sqft_verified DOUBLE PRECISION",
        "gross_monthly_rent_income_estimated NUMERIC",
        "gross_monthly_rent_income_verified NUMERIC",
        "owner_monthly_costs_estimated NUMERIC",
        "owner_monthly_costs_verified NUMERIC",
        "property_value_estimated NUMERIC",
        "property_value_verified NUMERIC",
        "liens_total_estimated NUMERIC",
        "liens_total_verified NUMERIC",
        "months_on_market_estimated INTEGER",
        "months_on_market_verified INTEGER",
        "months_vacant_estimated INTEGER",
        "months_vacant_verified INTEGER",
        "field_evidence JSONB",
    ):
        assert column in sql
    assert "'verified', 'estimated', 'mixed', 'unknown'" in sql
    assert "'hypothetical'" not in sql
    assert "properties_vacant_within_building_est_check" in sql
    assert "properties_vacant_within_building_ver_check" in sql


def test_migrate_reports_not_configured(monkeypatch, capsys) -> None:
    import app.migrate as migrate

    def _fail():
        raise DatabaseNotConfiguredError("database_not_configured")

    monkeypatch.setattr(migrate, "get_connection", _fail)
    assert migrate.main() == 2
    assert "database_not_configured" in capsys.readouterr().err
