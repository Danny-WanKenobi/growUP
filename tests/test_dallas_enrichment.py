"""Offline City of Dallas GIS / Open Data enrichment tests (mocked HTTP)."""
from __future__ import annotations

import pytest

from app.agents.gabriel.sources.base import SourceError
from app.agents.gabriel.sources.dallas_gis import (
    ZONING_QUERY_URL,
    parse_zoning_response,
    zoning_at_point,
)
from app.agents.gabriel.sources.dallas_open_data import (
    BASE_ZONING_DATASET,
    query_dataset,
)


def test_zoning_point_query_builds_bounded_request():
    captured = {}

    def transport(url, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return {"features": [{"attributes": {"ZONING": "IR"}}]}

    claim = zoning_at_point(32.78, -96.80, transport=transport)
    assert captured["url"] == ZONING_QUERY_URL
    assert captured["params"]["geometry"] == "-96.8,32.78"
    assert claim["zoning_labels"] == ["IR"]
    assert claim["ambiguous"] is False
    assert "vacancy" in claim["scope"]


def test_zoning_multiple_districts_are_ambiguous():
    payload = {"features": [{"attributes": {"ZONING": "IR"}},
                            {"attributes": {"ZONING": "MU-2"}}]}
    claim = parse_zoning_response(payload, latitude=1.0, longitude=2.0,
                                  retrieved_at="t")
    assert claim["zoning_labels"] == ["IR", "MU-2"]
    assert claim["ambiguous"] is True


def test_zoning_malformed_and_empty_responses():
    with pytest.raises(SourceError):
        parse_zoning_response({}, latitude=1.0, longitude=2.0,
                              retrieved_at="t")
    claim = parse_zoning_response({"features": []}, latitude=1.0,
                                  longitude=2.0, retrieved_at="t")
    assert claim["zoning_labels"] == []
    assert claim["ambiguous"] is False


def test_socrata_query_paginates_and_caps():
    captured = {}

    def transport(url, params, timeout):
        captured.update(url=url, params=params)
        return [{"zoning": "IR"}]

    result = query_dataset(where="zoning='IR'", limit=9999, offset=40,
                           transport=transport)
    assert captured["url"].endswith(f"/resource/{BASE_ZONING_DATASET}.json")
    assert captured["params"]["$limit"] == "500"
    assert captured["params"]["$offset"] == "40"
    assert captured["params"]["$where"] == "zoning='IR'"
    assert result["rows"] == [{"zoning": "IR"}]
    assert result["dataset_id"] == BASE_ZONING_DATASET


def test_socrata_rejects_bad_inputs():
    with pytest.raises(SourceError):
        query_dataset("../etc", transport=lambda u, p, t: [])
    with pytest.raises(SourceError):
        query_dataset(limit=0, transport=lambda u, p, t: [])
