"""Nationwide US discovery + generic adapter tests (offline fakes)."""
from __future__ import annotations

import pytest

from app.agents.gabriel.sources.base import BoundingBox, SOURCES
from app.agents.gabriel.sources.generic_open_data import GenericSocrataSource
from app.agents.gabriel.sources.us_discovery import discover_us_sources

BBOX = BoundingBox(29.7, -95.4, 29.8, -95.3)


def _catalog(payloads):
    def http_get(url, params, timeout):
        for key, payload in payloads.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise RuntimeError("unexpected catalog")
    return http_get


def test_nationwide_discovery_qualifies_and_selects_generic_adapter():
    payloads = {
        "socrata.com": {"results": [{
            "resource": {"name": "Parcels", "type": "dataset"},
            "metadata": {"domain": "data.example.gov"}}]},
        "arcgis.com": {"data": []},
        "catalog.data.gov": {"result": {"results": []}},
    }
    result = discover_us_sources("Travis County", http_get=_catalog(payloads))
    assert result["ranked"]
    top = result["ranked"][0]
    assert top["platform"] == "socrata"
    assert result["selected_adapter"] == "generic_socrata"
    assert "never enrolls" in result["authority_basis"]


def test_catalog_failure_is_honest_not_fabricated():
    def failing(url, params, timeout):
        raise RuntimeError("down")

    result = discover_us_sources("Nowhere County", http_get=failing)
    assert result["ranked"] == []
    assert result["selected_adapter"] is None
    assert len(result["blockers"]) >= 3


def test_generic_socrata_adapter_maps_rows_without_inventing_facts():
    import json as _json

    def fetch(url, params, timeout):
        return _json.dumps([{
            "account": "A1", "situs_address": "1 Main St",
            "city": "Houston", "zip_code": "77002",
            "latitude": "29.75", "longitude": "-95.36",
            "land_use": "WAREHOUSE",
        }]).encode()

    source = GenericSocrataSource(fetch=fetch)
    import os
    os.environ["GENERIC_SOCRATA_URL"] = "https://data.example.gov/resource/x.json"
    try:
        rows = source.search(BBOX, ["warehouse_industrial"], 5, 5.0)
    finally:
        del os.environ["GENERIC_SOCRATA_URL"]
    assert len(rows) == 1
    assert rows[0].external_id == "generic_socrata:A1"
    assert rows[0].street_address == "1 Main St"
    assert rows[0].category_hint == "other"  # hint never invented
    assert rows[0].tags["observed_use"] == "WAREHOUSE"


def test_generic_adapters_registered_and_fail_closed_unconfigured():
    import app.agents.gabriel.sources  # noqa: F401  (registers adapters)

    for name in ("generic_socrata", "generic_arcgis", "generic_ckan"):
        assert name in SOURCES
    from app.agents.gabriel.sources.base import SourceError

    with pytest.raises(SourceError):
        SOURCES["generic_socrata"].search(BBOX, [], 1, 1.0)


def test_discovery_module_nationwide_entry_point():
    from app.agents.gabriel.sources.discovery import discover_sources_anywhere

    payloads = {
        "socrata.com": {"results": [{
            "resource": {"name": "Parcels", "type": "dataset"},
            "metadata": {"domain": "data.example.gov"}}]},
        "arcgis.com": {"data": []},
        "catalog.data.gov": {"result": {"results": []}},
    }
    result = discover_sources_anywhere(
        "Cook County", "high_rise", http_get=_catalog(payloads))
    assert result["selected_adapter"] == "generic_socrata"
    assert result["category"] == "high_rise"
