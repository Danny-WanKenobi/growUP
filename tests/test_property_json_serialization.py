"""JSONB serialization for property/evidence writes (fake driver, no PG).

Guards the Psycopg 3 requirement that dict parameters bound for JSONB
columns travel as serialized text: raw dicts raise
``ProgrammingError: cannot adapt type 'dict'`` on a real server while
passing silently against fakes. Covers all three write paths plus
None-means-NULL preservation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


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


def _configured():
    from app.config import DatabaseConfig

    return DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw")


def _connect(monkeypatch, recorder, row):
    psycopg = pytest.importorskip("psycopg")
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, row)
    )


def test_create_property_serializes_field_evidence(monkeypatch) -> None:
    import app.properties_db as store

    recorder: dict = {}
    _connect(monkeypatch, recorder, {"id": "p-1"})
    evidence = {"building_area_sqft_estimated": "Advertised in unit-test source"}
    store.create_property(
        {
            "name": "X",
            "category": "other",
            "building_area_sqft_estimated": 10.0,
            "field_evidence": evidence,
            "provenance": "unit-test evidence record QA-001",
        },
        config=_configured(),
    )
    sent = recorder["params"]["field_evidence"]
    assert isinstance(sent, str)
    assert sent == json.dumps(evidence)
    assert json.loads(sent) == evidence


def test_create_property_preserves_null_field_evidence(monkeypatch) -> None:
    import app.properties_db as store

    recorder: dict = {}
    _connect(monkeypatch, recorder, {"id": "p-2"})
    store.create_property(
        {"name": "X", "category": "other", "field_evidence": None},
        config=_configured(),
    )
    assert recorder["params"]["field_evidence"] is None


def test_create_listing_evidence_serializes_payload(monkeypatch) -> None:
    import app.listings_db as listings

    recorder: dict = {}
    _connect(monkeypatch, recorder, {"id": "ev-1"})
    payload = {"asking_price": "1250000.00", "price_reductions": ["cut $1"]}
    listings.create_listing_evidence(
        "prop-1",
        "Test Source Records",
        "https://example.com/x",
        datetime(2026, 10, 20, tzinfo=timezone.utc),
        "sale",
        payload,
        config=_configured(),
    )
    sent = recorder["params"]["payload"]
    assert isinstance(sent, str)
    assert sent == json.dumps(payload)
    assert json.loads(sent) == payload


def test_create_listing_evidence_serializes_empty_payload(monkeypatch) -> None:
    import app.listings_db as listings

    recorder: dict = {}
    _connect(monkeypatch, recorder, {"id": "ev-2"})
    listings.create_listing_evidence(
        "prop-1",
        "Test Source Records",
        "https://example.com/x",
        datetime(2026, 10, 20, tzinfo=timezone.utc),
        "unknown",
        {},
        config=_configured(),
    )
    assert recorder["params"]["payload"] == "{}"


def test_enrich_serializes_field_evidence(monkeypatch) -> None:
    import app.properties_db as store

    recorder: dict = {}
    _connect(monkeypatch, recorder, {"id": "p-3"})
    filled = store.enrich_property_estimates(
        "prop-1",
        {"property_value_estimated": "100"},
        {"property_value_estimated": "unit-test ref"},
        "estimated",
        config=_configured(),
    )
    assert filled == ["property_value_estimated"]
    sent = recorder["params"]["field_evidence"]
    assert isinstance(sent, str)
    assert json.loads(sent) == {"property_value_estimated": "unit-test ref"}


def test_enrich_preserves_null_field_evidence(monkeypatch) -> None:
    import app.properties_db as store

    recorder: dict = {}
    _connect(monkeypatch, recorder, {"id": "p-4"})
    store.enrich_property_estimates(
        "prop-1", {"property_value_estimated": "100"}, None, "estimated",
        config=_configured(),
    )
    assert recorder["params"]["field_evidence"] is None
