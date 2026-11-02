"""Offline DCAD bulk (P1) discovery tests over staged exchange fixtures.

Fixtures are synthetic samples in the documented exchange format,
not live DCAD records. No network or database access occurs.
"""
from __future__ import annotations

import pytest

from app.agents.gabriel.sources.base import BoundingBox, SourceError, get_source
from app.agents.gabriel.sources.dcad_bulk import (
    DCAD_BULK_ENV,
    DcadBulkSource,
    map_land_use,
    parse_exchange,
)

HEADER = (
    "account,situs_address,city,zip_code,land_use,improvement_description,"
    "improvement_area_sqft,year_built,latitude,longitude,appraised_value,"
    "value_year_label\n"
)
ROWS = (
    '10000000000000001,"100 MAIN ST, STE 200",Dallas,75201,Office,'
    "OFFICE BUILDING,50000,1980,32.78,-96.80,1000000,2027 Current\n"
    '10000000000000002,"200 ELM ST",Dallas,75201,Industrial,'
    "STORAGE WAREHOUSE,20000,1968,32.78,-96.80,500000,2027 Current\n"
    '10000000000000002,"200 ELM ST",Dallas,75201,Industrial,'
    "STORAGE WAREHOUSE,20000,1968,32.78,-96.80,500000,2027 Current\n"
    '10000000000000003,"300 FAR ST",Dallas,75230,Retail,SHOPPING CENTER,'
    "9000,2001,32.90,-96.80,700000,2027 Current\n"
    '10000000000000004,"400 NOCOORD ST",Dallas,75201,Office,'
    "OFFICE BUILDING,1000,1990,,,300000,2027 Current\n"
)
BBOX = BoundingBox(south=32.77, west=-96.81, north=32.79, east=-96.79)


def _stage(tmp_path, rows=ROWS, parcels=None):
    (tmp_path / "dcad_accounts.csv").write_text(HEADER + rows, encoding="utf-8")
    if parcels is not None:
        (tmp_path / "dcad_parcels.csv").write_text(parcels, encoding="utf-8")
    return tmp_path


def test_parse_exchange_dedupes_and_maps_categories(tmp_path):
    listings = parse_exchange(_stage(tmp_path), "2026-11-02T00:00:00Z")
    assert [item.external_id for item in listings] == [
        "dcad:10000000000000001", "dcad:10000000000000002",
        "dcad:10000000000000003", "dcad:10000000000000004"]
    by_id = {item.external_id: item for item in listings}
    assert by_id["dcad:10000000000000001"].category_hint == \
        "commercial_property"
    assert by_id["dcad:10000000000000002"].category_hint == \
        "warehouse_industrial"
    assert by_id["dcad:10000000000000002"].tags["account"] == \
        "10000000000000002"
    assert by_id["dcad:10000000000000002"].tags["value_year_label"] == \
        "2027 Current"
    assert by_id["dcad:10000000000000002"].element_url.endswith(
        "ID=10000000000000002")


def test_map_land_use_defaults_to_other():
    assert map_land_use("Residential", "SINGLE FAMILY") == "other"
    assert map_land_use("", "") == "other"


def test_search_filters_bbox_categories_and_limit(tmp_path, monkeypatch):
    monkeypatch.setenv(DCAD_BULK_ENV, str(_stage(tmp_path)))
    source = DcadBulkSource()
    found = source.search(BBOX, ["warehouse_industrial"], limit=10,
                          timeout_seconds=12)
    assert [item.external_id for item in found] == ["dcad:10000000000000002"]
    found = source.search(BBOX, ["commercial_property"], limit=1,
                          timeout_seconds=12)
    assert [item.external_id for item in found] == ["dcad:10000000000000001"]


def test_parcel_centroids_fill_missing_coordinates(tmp_path, monkeypatch):
    parcels = "account,latitude,longitude\n10000000000000004,32.78,-96.80\n"
    monkeypatch.setenv(DCAD_BULK_ENV, str(_stage(tmp_path, parcels=parcels)))
    found = DcadBulkSource().search(
        BBOX, ["commercial_property"], limit=10, timeout_seconds=12)
    assert "dcad:10000000000000004" in [item.external_id for item in found]


def test_unconfigured_and_missing_data_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv(DCAD_BULK_ENV, raising=False)
    with pytest.raises(SourceError):
        DcadBulkSource().search(BBOX, ["commercial_property"], 5, 12)
    monkeypatch.setenv(DCAD_BULK_ENV, str(tmp_path))
    with pytest.raises(SourceError):
        DcadBulkSource().search(BBOX, ["commercial_property"], 5, 12)


def test_unexpected_columns_fail_closed(tmp_path):
    (tmp_path / "dcad_accounts.csv").write_text("a,b,c\n1,2,3\n",
                                               encoding="utf-8")
    with pytest.raises(SourceError):
        parse_exchange(tmp_path, "2026-11-02T00:00:00Z")


def test_registered_in_canonical_registry():
    import app.agents.gabriel.sources  # noqa: F401  (registration imports)

    source = get_source("dcad_bulk")
    assert source.name == "dcad_bulk"
    assert source.requires_credentials is False
