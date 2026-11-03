"""Lead-to-source conversion: leads claim nothing until verified."""
from __future__ import annotations

import json
import time

import pytest

from app.agents.gabriel.sources.lead_conversion import (
    LeadConversionError,
    convert_leads,
    resolve_lead,
)


def test_resolve_socrata_dataset_page_directly():
    out = resolve_lead({"name": "Parcels",
                        "lead_url": "https://data.city.gov/d/abcd-1234"})
    assert out["outcome"] == "resource"
    assert out["resource"]["resource_url"] == "https://data.city.gov/resource/abcd-1234.json"
    assert out["resource"]["platform"] == "socrata"


def test_resolve_arcgis_layer_directly():
    out = resolve_lead({"name": "Parcels",
                        "lead_url": "https://mapit.county.com/arcgis/rest/services/Tax/Parcels/MapServer/0"})
    assert out["outcome"] == "resource"
    assert out["resource"]["platform"] == "arcgis"
    assert out["resource"]["resource_url"].endswith("/MapServer/0")


def test_resolve_hub_item_via_item_record():
    def fetch(url, params=None):
        assert "sharing/rest/content/items" in url
        return json.dumps({
            "id": "a" * 32, "owner": "county_gis",
            "url": "https://mapit.county.com/arcgis/rest/services/Tax/Parcels/FeatureServer/0",
        }).encode()

    out = resolve_lead(
        {"name": "County Parcels",
         "lead_url": "https://hub.arcgis.com/datasets/" + "a" * 32},
        fetch=fetch)
    assert out["outcome"] == "resource"
    assert out["resource"]["dataset_id"] == "a" * 32
    assert out["resource"]["publisher_name"] == "county_gis"


def test_resolve_arcgis_service_root_picks_parcel_layer():
    def fetch(url, params=None):
        assert url.endswith("FeatureServer?f=pjson")
        return json.dumps({"layers": [
            {"id": 1, "name": "County Line"},
            {"id": 4, "name": "Parcels"},
            {"id": 5, "name": "Subdivisions"}]}).encode()

    out = resolve_lead(
        {"name": "CCAD Parcels",
         "lead_url": "https://services2.arcgis.com/abc/ArcGIS/rest/services/CCAD/FeatureServer"},
        fetch=fetch)
    assert out["outcome"] == "resource"
    assert out["resource"]["resource_url"].endswith("/FeatureServer/4")


def test_resolve_hub_maps_link_via_item_record():
    def fetch(url, params=None):
        return json.dumps({
            "id": "b" * 32, "owner": "ccad",
            "url": "https://services2.arcgis.com/abc/arcgis/rest/services/CCAD/FeatureServer/4",
        }).encode()

    out = resolve_lead(
        {"name": "CCAD Parcel Feature Set",
         "lead_url": "https://hub.arcgis.com/maps/" + "b" * 32},
        fetch=fetch)
    assert out["outcome"] == "resource"
    assert out["resource"]["resource_url"].endswith("/FeatureServer/4")


def test_resolve_download_page_records_bulk_candidate():
    page = b'<a href="/data/parcels.zip">Parcels (zipped shapefile)</a>'

    def fetch(url, params=None):
        return page

    out = resolve_lead({"name": "CAD Data Downloads",
                        "lead_url": "https://www.cad.example.org/downloads"},
                       fetch=fetch)
    assert out["outcome"] == "bulk_candidate"
    assert out["download_url"].endswith("parcels.zip")


def test_resolve_plain_page_unresolved():
    def fetch(url, params=None):
        return b"<html><body>Welcome to our county site</body></html>"

    out = resolve_lead({"name": "County Home",
                        "lead_url": "https://www.county.example.org/"},
                       fetch=fetch)
    assert out["outcome"] == "unresolved"


def test_resolve_non_https_lead_unresolved_without_network():
    calls = []
    out = resolve_lead({"name": "x", "lead_url": "http://example.org/x"},
                       fetch=lambda u, p=None: calls.append(u) or b"")
    assert out["outcome"] == "unresolved"
    assert calls == []


class _FakeSelectionStore:
    def __init__(self):
        self.completed = None

    def claim(self, assignment_key, fingerprint, mission_data):
        return {"status": "winner", "run_id": "run-lead-1"}

    def complete(self, assignment_key, *, decision, evidence):
        self.completed = (decision, evidence)


def _socrata_fetch(calls=None):
    view = {
        "id": "abcd-1234", "name": "City Parcels",
        "attribution": "City of Example", "license": {"name": "Public Domain"},
        "rowsUpdatedAt": int(time.time()) - 86400,
        "columns": [{"fieldName": "parcel_id"}, {"fieldName": "address"},
                    {"fieldName": "latitude"}, {"fieldName": "longitude"}],
    }

    def fetch(url, params=None):
        if calls is not None:
            calls.append(url)
        if "/api/views/" in url:
            return json.dumps(view).encode()
        if url.endswith("/data.json"):
            return json.dumps({"dataset": []}).encode()
        return json.dumps([{"city": "Example"}]).encode()
    return fetch


def test_convert_lead_to_selected_source():
    ranked = [{"name": "City Parcels",
               "lead_url": "https://data.city.gov/d/abcd-1234"}]
    out = convert_leads(ranked, jurisdiction="Example", assignment_key="lead-test-1",
                        selection_store=_FakeSelectionStore(), fetch=_socrata_fetch())
    assert out["selection"] is not None
    assert out["selection"]["status"] == "selected"
    assert out["attempts"][0]["outcome"] == "selected"


def test_convert_unresolvable_lead_selects_nothing():
    ranked = [{"name": "County Home", "lead_url": "https://www.county.example.org/"}]
    out = convert_leads(ranked, jurisdiction="Example", assignment_key="lead-test-2",
                        selection_store=_FakeSelectionStore(),
                        fetch=lambda u, p=None: b"<html>nothing here</html>")
    assert out["selection"] is None
    assert out["attempts"][0]["outcome"] == "unresolved"
