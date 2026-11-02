"""Census county admission and bounded collection acceptance.

Inline JSON fixtures below are TEST MATERIAL ONLY with the documented
response structure -- not retained live source evidence. Live Census
compatibility and real county results are not proven here; live
retrieval stays owner-executed after separate approval.
"""

from __future__ import annotations

import hashlib
import json
import os as _os
from typing import Any
from uuid import UUID

import pytest

from app.agents.gabriel.sources import census as census_module
from app.agents.gabriel.sources.census import (
    CensusConfigError,
    CensusError,
    CensusFetchError,
    CensusParseError,
    apply_county_to_dossier,
    assess_county,
    census_request_url,
    collect_address,
    has_collector_county_claim,
    parse_geographies,
)

PROP_ID = "11111111-2222-3333-4444-555555555555"
STREET = "2323 Chalk Hill Rd"
CITY = "Dallas"
STATE = "TX"
ZIP = "75212"
MATCHED_STREET = "2323 CHALK HILL RD"
MATCHED_ADDRESS = "2323 CHALK HILL RD, DALLAS, TX, 75212"


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = {key: (dict(value) if isinstance(value, dict) else value) for key, value in base.items()}
    for key, value in overrides.items():
        if value and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _match(**overrides: Any) -> dict[str, Any]:
    match = {
        "tigerLine": {"tigerLineId": "19316628", "side": "L"},
        "coordinates": {"x": -96.876, "y": 32.789},
        "addressComponents": {
            "zip": ZIP,
            "streetName": "CHALK HILL",
            "preType": "",
            "city": "DALLAS",
            "preDirection": "",
            "suffixDirection": "",
            "fromAddress": "2300",
            "state": "TX",
            "suffixType": "RD",
            "toAddress": "2398",
            "preQualifier": "",
            "suffixQualifier": "",
        },
        "matchedAddress": MATCHED_ADDRESS,
        "geographies": {
            "Counties": [
                {
                    "GEOID": "48113",
                    "STATE": "48",
                    "COUNTY": "113",
                    "NAME": "Dallas County",
                }
            ],
            "States": [{"GEOID": "48", "STATE": "48", "NAME": "Texas"}],
        },
    }
    return _merge(match, overrides)


def _body(
    matches: list[dict[str, Any]] | None = None,
    *,
    benchmark: str = "Public_AR_Current",
    vintage: str = "Current_Current",
) -> str:
    return json.dumps(
        {
            "result": {
                "input": {
                    "address": {
                        "street": STREET,
                        "city": CITY,
                        "state": STATE,
                        "zip": ZIP,
                    },
                    "benchmark": {
                        "id": "4",
                        "benchmarkName": benchmark,
                        "isDefault": True,
                    },
                    "vintage": {
                        "id": "4",
                        "vintageName": vintage,
                        "isDefault": True,
                    },
                },
                "addressMatches": [dict(_match())] if matches is None else matches,
            }
        }
    )


def _property_record(**overrides: Any) -> dict[str, Any]:
    record = {
        "id": PROP_ID,
        "street_address": STREET,
        "city": CITY,
        "state": STATE,
        "zip_code": ZIP,
    }
    record.update(overrides)
    return record


def _collection(body: str | None = None, **overrides: Any) -> dict[str, Any]:
    text = _body() if body is None else body
    payload = {
        "collector": "census-geocoder-geographies-address",
        "collector_revision": "census-1",
        "submitted_street": STREET,
        "submitted_city": CITY,
        "submitted_state": STATE,
        "submitted_zip": ZIP,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "url": census_request_url(STREET, CITY, STATE, ZIP),
        "retrieved_at": "2026-11-01T00:00:00+00:00",
        "status_code": 200,
        "content_type": "application/json",
        "body_bytes": len(text.encode("utf-8")),
        "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "body_text": text,
        "principal": "wondrue-operator",
        "collection_provenance": "adapter_collected",
    }
    payload.update(overrides)
    return payload


def _county_result(**overrides: Any) -> dict[str, Any]:
    collection = _collection()
    result = assess_county(
        collection=collection,
        collection_digest="digest-under-test",
        property_record=_property_record(),
    )
    result.update(overrides)
    return result


@pytest.fixture()
def _census_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("CENSUS_COLLECTION_ENABLED", "1")


# --- Request construction ----------------------------------------------------


def test_request_url_uses_fixed_documented_form() -> None:
    assert census_request_url(STREET, CITY, STATE, ZIP) == (
        "https://geocoding.geo.census.gov/geocoder/geographies/address"
        "?street=2323+Chalk+Hill+Rd&city=Dallas&state=TX&zip=75212"
        "&benchmark=Public_AR_Current&vintage=Current_Current&format=json"
    )


def test_request_url_normalizes_texas_state() -> None:
    assert "state=TX" in census_request_url(STREET, CITY, "Texas", ZIP)
    assert "state=TX" in census_request_url(STREET, CITY, "tx", ZIP)


def test_request_url_rejects_unusable_input() -> None:
    with pytest.raises(CensusConfigError, match="street address"):
        census_request_url("", CITY, STATE, ZIP)
    with pytest.raises(CensusConfigError, match="Texas"):
        census_request_url(STREET, CITY, "CA", ZIP)
    with pytest.raises(CensusConfigError, match="required"):
        census_request_url(STREET, CITY, "", ZIP)
    with pytest.raises(CensusConfigError, match="too long"):
        census_request_url("1 " + "x" * 300, CITY, STATE, ZIP)


# --- Collector -----------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        content_type: str = "application/json",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._chunks = chunks if chunks is not None else [b"{}"]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


def _patch_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "8.8.8.8") -> None:
    """Stub DNS resolution (offline tests must never resolve for real)."""

    import socket

    def _resolve(host: object, port: object, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


def _patch_stream(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> dict[str, Any]:
    calls: dict[str, Any] = {"count": 0, "kwargs": {}}

    def _stream(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls["count"] += 1
        calls["method"] = method
        calls["url"] = url
        calls["kwargs"] = kwargs
        return response

    import httpx

    _patch_dns(monkeypatch)
    monkeypatch.setattr(httpx, "stream", _stream)
    return calls


def test_collect_disabled_refused_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CENSUS_COLLECTION_ENABLED", raising=False)

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(httpx, "stream", _boom)
    with pytest.raises(CensusConfigError, match="not enabled"):
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")


def test_collect_requires_principal(monkeypatch: pytest.MonkeyPatch, _census_env: None) -> None:
    with pytest.raises(CensusConfigError, match="principal"):
        collect_address(STREET, CITY, STATE, ZIP, principal="  ")


def test_collect_success_is_single_bounded_call(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    body = _body().encode("utf-8")
    calls = _patch_stream(monkeypatch, _FakeResponse(chunks=[body]))
    collected = collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert calls["count"] == 1
    assert calls["method"] == "GET"
    assert calls["url"] == census_request_url(STREET, CITY, STATE, ZIP)
    assert calls["kwargs"]["timeout"] == 12.0
    assert calls["kwargs"]["follow_redirects"] is False
    assert calls["kwargs"]["trust_env"] is False  # explicit: no proxy
    assert "verify" not in calls["kwargs"]  # TLS validation stays enabled
    assert collected.collection_provenance == "adapter_collected"
    assert collected.collector_revision == "census-1"
    assert collected.body_sha256 == hashlib.sha256(body).hexdigest()
    assert collected.body_text == _body()
    assert collected.principal == "wondrue-operator"
    assert collected.submitted_street == STREET


def test_collect_redirect_refused(monkeypatch: pytest.MonkeyPatch, _census_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(status=302))
    with pytest.raises(CensusFetchError, match="redirect") as exc_info:
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert exc_info.value.transmitted is True


def test_collect_bad_status_refused(monkeypatch: pytest.MonkeyPatch, _census_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(status=500))
    with pytest.raises(CensusFetchError, match="unexpected status") as exc_info:
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert exc_info.value.transmitted is True


def test_collect_content_type_refused(monkeypatch: pytest.MonkeyPatch, _census_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(content_type="text/html"))
    with pytest.raises(CensusFetchError, match="content type") as exc_info:
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert exc_info.value.transmitted is True


def test_collect_oversize_refused(monkeypatch: pytest.MonkeyPatch, _census_env: None) -> None:
    big = b"x" * (256 * 1024 + 1)
    _patch_stream(monkeypatch, _FakeResponse(chunks=[big]))
    with pytest.raises(CensusFetchError, match="too large") as exc_info:
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert exc_info.value.transmitted is True


def test_collect_timeout_is_single_charged_attempt(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    import httpx

    calls = {"count": 0}

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        raise httpx.TimeoutException("slow")

    _patch_dns(monkeypatch)
    monkeypatch.setattr(httpx, "stream", _timeout)
    with pytest.raises(CensusFetchError, match="timed out") as exc_info:
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert calls["count"] == 1  # no retries
    assert exc_info.value.transmitted is True


def test_collect_timeout_bound_enforced(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    with pytest.raises(CensusConfigError, match="timeout"):
        collect_address(
            STREET, CITY, STATE, ZIP, principal="wondrue-operator", timeout_seconds=13.0
        )


def test_collect_unresolvable_destination_not_transmitted(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    import socket

    import httpx

    def _fail(host: object, port: object, *args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("no such host")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    monkeypatch.setattr(httpx, "stream", _boom)
    with pytest.raises(CensusFetchError, match="resolve") as exc_info:
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert exc_info.value.transmitted is False


def test_collect_private_destination_refused(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network must not be touched")

    _patch_dns(monkeypatch, ip="10.9.8.7")
    monkeypatch.setattr(httpx, "stream", _boom)
    with pytest.raises(CensusFetchError, match="public") as exc_info:
        collect_address(STREET, CITY, STATE, ZIP, principal="wondrue-operator")
    assert exc_info.value.transmitted is False


# --- Response parsing ----------------------------------------------------------


def test_parse_valid_document() -> None:
    parsed = parse_geographies(_body())
    assert len(parsed["addressMatches"]) == 1
    assert parsed["input"]["benchmark"]["benchmarkName"] == "Public_AR_Current"
    assert parsed["input"]["vintage"]["vintageName"] == "Current_Current"


def test_parse_malformed_documents_refused() -> None:
    with pytest.raises(CensusParseError, match="not JSON"):
        parse_geographies("not json")
    with pytest.raises(CensusParseError, match="not a JSON object"):
        parse_geographies("[]")
    with pytest.raises(CensusParseError, match="no result"):
        parse_geographies("{}")
    with pytest.raises(CensusParseError, match="no address match list"):
        parse_geographies(json.dumps({"result": {}}))
    with pytest.raises(CensusParseError, match="not an object"):
        parse_geographies(json.dumps({"result": {"addressMatches": [1]}}))


# --- County assessment ---------------------------------------------------------


def _assess(body: str | None = None, **collection_overrides: Any) -> dict[str, Any]:
    collection = _collection(body, **collection_overrides)
    return assess_county(
        collection=collection,
        collection_digest="digest-under-test",
        property_record=_property_record(),
    )


def test_assess_unique_match_admits_in_territory_county() -> None:
    result = _assess()
    assert result["property_id"] == PROP_ID
    assert result["county"]["canonical"] == "dallas county"
    assert result["county"]["geoid"] == "48113"
    assert result["county"]["territory"] == "within_territory"
    assert result["scope_label"] == census_module.CENSUS_COUNTY_SCOPE
    assert result["collection_digest"] == "digest-under-test"
    assert result["matched_address"] == MATCHED_ADDRESS
    assert result["coordinates"] == {"x": -96.876, "y": 32.789}
    assert result["benchmark_requested"] == "Public_AR_Current"
    assert result["vintage_requested"] == "Current_Current"
    assert result["benchmark_returned"]["benchmarkName"] == "Public_AR_Current"
    assert result["vintage_returned"]["vintageName"] == "Current_Current"
    assert result["comparisons"]["house_number"]["compared"] is True
    assert result["method"] == "census_unique_address_match"


def test_assess_outside_territory_stays_outside() -> None:
    match = _match(
        matchedAddress="1000 LOUISIANA ST, HOUSTON, TX, 77002",
        addressComponents={"city": "HOUSTON", "zip": "77002", "streetName": "LOUISIANA",
                           "suffixType": "ST", "fromAddress": "1000", "toAddress": "1098"},
        geographies={"Counties": [{"GEOID": "48201", "STATE": "48", "COUNTY": "201",
                                    "NAME": "Harris County"}]},
    )
    collection = _collection(
        _body([match]),
        submitted_street="1000 Louisiana St",
        submitted_city="Houston",
        submitted_zip="77002",
    )
    result = assess_county(
        collection=collection,
        collection_digest="digest-under-test",
        property_record=_property_record(
            street_address="1000 Louisiana St", city="Houston", zip_code="77002"
        ),
    )
    assert result["county"]["canonical"] == "harris county"
    assert result["county"]["territory"] == "outside_territory"


def test_assess_no_match_and_ambiguous_match_refused() -> None:
    with pytest.raises(CensusError, match="no Census address match"):
        _assess(_body([]))
    with pytest.raises(CensusError, match="ambiguous"):
        _assess(_body([_match(), _match()]))


def test_assess_incompatible_address_components_refused() -> None:
    wrong_number = _match(matchedAddress="9999 CHALK HILL RD, DALLAS, TX, 75212")
    with pytest.raises(CensusError, match="house number"):
        _assess(_body([wrong_number]))
    wrong_street = _match(matchedAddress="2323 MAPLE AVE, DALLAS, TX, 75212")
    with pytest.raises(CensusError, match="street"):
        _assess(_body([wrong_street]))
    no_number = _match(matchedAddress="CHALK HILL RD, DALLAS, TX, 75212")
    with pytest.raises(CensusError, match="house number"):
        _assess(_body([no_number]))
    wrong_state = _match(addressComponents={"state": "CA"})
    with pytest.raises(CensusError, match="Texas"):
        _assess(_body([wrong_state]))
    no_components = _match(addressComponents="Dallas")
    with pytest.raises(CensusError, match="components"):
        _assess(_body([no_components]))


def test_assess_city_zip_discrepancies_refused() -> None:
    wrong_city = _match(addressComponents={"city": "FORT WORTH"})
    with pytest.raises(CensusError, match="city disagrees"):
        _assess(_body([wrong_city]))
    wrong_zip = _match(addressComponents={"zip": "76102"})
    with pytest.raises(CensusError, match="zip disagrees"):
        _assess(_body([wrong_zip]))


def test_assess_absent_sides_recorded_not_compared() -> None:
    collection = _collection(
        submitted_city="", submitted_zip="",
        url=census_request_url(STREET, "", STATE, ""),
    )
    result = assess_county(
        collection=collection,
        collection_digest="digest-under-test",
        property_record=_property_record(city="", zip_code=""),
    )
    assert result["comparisons"]["city"]["compared"] is False
    assert result["comparisons"]["zip"]["compared"] is False
    assert result["county"]["canonical"] == "dallas county"


def test_assess_invalid_geography_refused() -> None:
    no_geo = _match(geographies={})
    with pytest.raises(CensusError, match="exactly one county"):
        _assess(_body([no_geo]))
    two_counties = _match(
        geographies={"Counties": [
            {"GEOID": "48113", "STATE": "48", "COUNTY": "113", "NAME": "Dallas County"},
            {"GEOID": "48439", "STATE": "48", "COUNTY": "439", "NAME": "Tarrant County"},
        ]}
    )
    with pytest.raises(CensusError, match="exactly one county"):
        _assess(_body([two_counties]))
    bad_geoid = _match(
        geographies={"Counties": [{"GEOID": "48114", "STATE": "48", "COUNTY": "113",
                                    "NAME": "Dallas County"}]}
    )
    with pytest.raises(CensusError, match="invalid or inconsistent"):
        _assess(_body([bad_geoid]))
    other_state = _match(
        geographies={"Counties": [{"GEOID": "06037", "STATE": "06", "COUNTY": "037",
                                    "NAME": "Los Angeles County"}]}
    )
    with pytest.raises(CensusError, match="not in Texas"):
        _assess(_body([other_state]))
    swapped_name = _match(
        geographies={"Counties": [{"GEOID": "48439", "STATE": "48", "COUNTY": "439",
                                    "NAME": "Dallas County"}]}
    )
    with pytest.raises(CensusError, match="disagree"):
        _assess(_body([swapped_name]))
    unknown_geoid = _match(
        geographies={"Counties": [{"GEOID": "48999", "STATE": "48", "COUNTY": "999",
                                    "NAME": "Nowhere County"}]}
    )
    with pytest.raises(CensusError, match="not a known Texas county"):
        _assess(_body([unknown_geoid]))
    bad_coords = _match(coordinates={"x": "east", "y": "north"})
    with pytest.raises(CensusError, match="coordinates"):
        _assess(_body([bad_coords]))


def test_assess_refuses_uploads_and_tampered_bytes() -> None:
    with pytest.raises(CensusError, match="provenance"):
        _assess(collection_provenance="caller_supplied")
    with pytest.raises(CensusError, match="not a Census"):
        _assess(collector="user-upload")
    with pytest.raises(CensusError, match="revision"):
        _assess(collector_revision="census-99")
    with pytest.raises(CensusError, match="digest"):
        _assess(body_sha256="0" * 64)
    with pytest.raises(CensusError, match="no response body"):
        _assess(body_text="")


# --- Dossier application ---------------------------------------------------------


def _current_address() -> dict[str, Any]:
    return {"street": STREET, "city": CITY, "state": STATE, "zip": ZIP}


def test_apply_admits_verified_county_attribution() -> None:
    result = _county_result()
    dossier = dict(
        _dossier_address(),
        county_attribution={
            "evidence_status": "observed",
            "value": "dallas county",
            "territory": "within_territory",
            "claims": [
                {"original": "Dallas County", "normalized": "dallas county"}
            ],
            "search_county": "dallas county",
        },
    )
    outcome = apply_county_to_dossier(
        dossier,
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=_current_address(),
    )
    assert outcome["applied"] is True
    attribution = outcome["county_attribution"]
    assert attribution["evidence_status"] == "verified"
    assert attribution["value"] == "dallas county"
    assert attribution["territory"] == "within_territory"
    assert attribution["method"] == census_module.CENSUS_COUNTY_SCOPE
    assert attribution["search_county"] == "dallas county"
    assert len(attribution["claims"]) == 2  # agreeing listing claim preserved
    assert attribution["claims"][0]["normalized"] == "dallas county"
    claim = attribution["claims"][1]
    assert claim["authority"] == "census_geocoder"
    assert claim["source_domain"] == "geocoding.geo.census.gov"
    assert claim["normalized"] == "dallas county"
    assert has_collector_county_claim(attribution) is True
    # The input dossier is never mutated.
    assert dossier["county_attribution"]["evidence_status"] == "observed"


def test_apply_refuses_forged_and_mismatched_bindings() -> None:
    dossier = {"county_attribution": {"evidence_status": "unknown"}}
    good = _county_result()
    forged = dict(good, collection_provenance="caller_supplied")
    assert apply_county_to_dossier(
        dossier, forged, collection_digest="digest-under-test",
        property_id=PROP_ID, current_address=_current_address(),
    )["applied"] is False
    assert apply_county_to_dossier(
        dossier, good, collection_digest="other-digest",
        property_id=PROP_ID, current_address=_current_address(),
    )["reason"] == "county result does not match the retained collection"
    assert apply_county_to_dossier(
        dossier, good, collection_digest="digest-under-test",
        property_id="00000000-0000-0000-0000-000000000000",
        current_address=_current_address(),
    )["reason"] == "county result binds a different property"
    stale = dict(good, assessment_version="census-county-99")
    assert apply_county_to_dossier(
        dossier, stale, collection_digest="digest-under-test",
        property_id=PROP_ID, current_address=_current_address(),
    )["applied"] is False


def test_apply_refuses_conflicting_retained_claims() -> None:
    result = _county_result()
    dossier = dict(
        _dossier_address(),
        county_attribution={
            "evidence_status": "observed",
            "value": "tarrant county",
            "claims": [{"original": "Tarrant County", "normalized": "tarrant county"}],
        },
    )
    outcome = apply_county_to_dossier(
        dossier,
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=_current_address(),
    )
    assert outcome["applied"] is False
    assert "tarrant county" in outcome["reason"]
    assert "retained" in outcome["reason"]


def test_apply_refuses_changed_address() -> None:
    result = _county_result()
    moved = dict(_current_address(), street="9999 Other St")
    outcome = apply_county_to_dossier(
        dict(
            _dossier_address(),
            county_attribution={"evidence_status": "unknown"},
        ),
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=moved,
    )
    assert outcome["applied"] is False
    assert "changed since collection" in outcome["reason"]


def _dossier_address(street: str = STREET, city: str = CITY, state: str = STATE) -> dict[str, Any]:
    return {"street_address": street, "city": city, "state": state}


def test_apply_refuses_dossier_address_mismatch() -> None:
    """Evidence collected for B must never credit a dossier describing A."""
    result = _county_result()  # collected for 2323 Chalk Hill Rd (submitted A)
    dossier = dict(
        _dossier_address(street="8702 S Lancaster Rd", city="Dallas", state="TX"),
        county_attribution={"evidence_status": "unknown"},
    )
    outcome = apply_county_to_dossier(
        dossier,
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=_current_address(),
    )
    assert outcome["applied"] is False
    assert "dossier" in outcome["reason"]
    assert "8702" in outcome["reason"] or "match" in outcome["reason"]


def test_apply_preserves_agreeing_claims() -> None:
    result = _county_result()
    dossier = dict(
        _dossier_address(),
        county_attribution={
            "evidence_status": "observed",
            "value": "dallas county",
            "claims": [
                {
                    "original": "Dallas County",
                    "normalized": "dallas county",
                    "source_url": "https://listings.example.com/1",
                    "authority": "listing",
                }
            ],
            "search_county": "dallas county",
        },
    )
    outcome = apply_county_to_dossier(
        dossier,
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=_current_address(),
    )
    assert outcome["applied"] is True
    claims = outcome["county_attribution"]["claims"]
    assert len(claims) == 2
    assert claims[0]["authority"] == "listing"
    assert claims[0]["source_url"] == "https://listings.example.com/1"
    assert claims[1]["authority"] == "census_geocoder"
    assert outcome["county_attribution"]["evidence_status"] == "verified"
    # The input dossier is never mutated.
    assert len(dossier["county_attribution"]["claims"]) == 1


def test_apply_dedups_repeated_census_claim() -> None:
    result = _county_result()
    first = apply_county_to_dossier(
        dict(_dossier_address(), county_attribution={"evidence_status": "unknown"}),
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=_current_address(),
    )
    assert first["applied"] is True
    repeat = apply_county_to_dossier(
        dict(
            _dossier_address(),
            county_attribution=first["county_attribution"],
        ),
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=_current_address(),
    )
    assert repeat["applied"] is True
    claims = repeat["county_attribution"]["claims"]
    assert len(claims) == 1
    assert claims[0]["authority"] == "census_geocoder"


def test_apply_refuses_unresolved_dossier_address() -> None:
    result = _county_result()
    dossier = {
        "street_address": None,
        "city": "Dallas",
        "state": "TX",
        "county_attribution": {"evidence_status": "unknown"},
    }
    outcome = apply_county_to_dossier(
        dossier,
        result,
        collection_digest="digest-under-test",
        property_id=PROP_ID,
        current_address=_current_address(),
    )
    assert outcome["applied"] is False
    assert "unresolved" in outcome["reason"]


def test_collector_claim_marker() -> None:
    assert has_collector_county_claim({}) is False
    assert has_collector_county_claim({"claims": "x"}) is False
    assert has_collector_county_claim({"claims": [{"authority": "listing"}]}) is False
    assert has_collector_county_claim({"claims": [{
        "authority": "census_geocoder",
        "source_url": "https://broker-a.example/x",
    }]}) is False
    assert has_collector_county_claim({"claims": [{
        "authority": "census_geocoder",
        "source_url": census_request_url(STREET, CITY, STATE, ZIP),
    }]}) is True


# --- Audited collection flow (offline fakes) ------------------------------------


class _FakeDB:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.runs_by_key: dict[str, str] = {}
        self.records: dict[tuple[str, str, str, int], dict[str, Any]] = {}


class _FakeCursor:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db
        self._rows: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        sql = " ".join(str(sql).split())
        params = dict(params or {})
        self._rows = []
        self.rowcount = 0
        if sql.startswith("INSERT INTO workflow_runs"):
            key = str(params["key"])
            if key in self._db.runs_by_key:
                return
            row = {
                "run_id": params["run_id"],
                "idempotency_key": key,
                "stage": params["stage"],
                "status": "open",
                "correlation_id": params["correlation"],
                "limits": params["limits"],
                "usage": "{}",
                "errors": "[]",
            }
            self._db.runs[str(params["run_id"])] = row
            self._db.runs_by_key[key] = str(params["run_id"])
            self._rows = [dict(row)]
            self.rowcount = 1
        elif sql.startswith("SELECT * FROM workflow_runs WHERE idempotency_key"):
            run_id = self._db.runs_by_key.get(str(params["key"]))
            if run_id is not None:
                self._rows = [dict(self._db.runs[run_id])]
        elif sql.startswith("SELECT * FROM workflow_runs WHERE run_id"):
            row = self._db.runs.get(str(params["rid"]))
            if row is not None:
                self._rows = [dict(row)]
        elif sql.startswith("UPDATE workflow_runs SET stage"):
            row = self._db.runs.get(str(params["rid"]))
            if row is not None and row["stage"] == params["expected"]:
                row["stage"] = params["next"]
                row["status"] = params["status"]
                self.rowcount = 1
        elif sql.startswith("UPDATE workflow_runs SET errors"):
            row = self._db.runs.get(str(params["rid"]))
            if row is not None:
                errors = json.loads(row["errors"])
                errors.append(json.loads(params["error"]))
                row["errors"] = json.dumps(errors)
                self.rowcount = 1
        elif sql.startswith("UPDATE workflow_runs SET status"):
            row = self._db.runs.get(str(params["rid"]))
            if row is not None:
                row["status"] = params["status"]
                self.rowcount = 1
        elif sql.startswith("UPDATE workflow_runs SET usage"):
            row = self._db.runs.get(str(params["rid"]))
            if row is not None:
                usage = json.loads(row["usage"])
                name = str(params["key"])
                usage[name] = int(usage.get(name, 0)) + int(params["amount"])
                row["usage"] = json.dumps(usage)
                self.rowcount = 1
        elif sql.startswith("INSERT INTO workflow_records"):
            key = (
                str(params["run"]),
                str(params["kind"]),
                str(params["key"]),
                int(params["version"]),
            )
            if key in self._db.records:
                return
            row = {
                "record_id": params["rid"],
                "run_id": params["run"],
                "kind": params["kind"],
                "record_key": params["key"],
                "version": int(params["version"]),
                "payload": params["payload"],
                "digest": params["digest"],
            }
            self._db.records[key] = row
            self._rows = [dict(row)]
            self.rowcount = 1
        elif sql.startswith("SELECT * FROM workflow_records WHERE run_id"):
            run = str(params["run"])
            kind = params.get("kind")
            record_key = params.get("key")
            version = params.get("version")
            rows = [
                row
                for (r, k, rk, v), row in self._db.records.items()
                if r == run
                and (kind is None or k == kind)
                and (record_key is None or rk == record_key)
                and (version is None or v == version)
            ]
            rows.sort(key=lambda row: (row["kind"], row["record_key"], row["version"]))
            if record_key is not None and version is None:
                rows = rows[-1:]
            self._rows = [dict(row) for row in rows]
        else:  # pragma: no cover - unexpected SQL in these focused tests
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> Any:
        return list(self._rows)


class _FakeConn:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _patch_owned(
    monkeypatch: pytest.MonkeyPatch, db: _FakeDB, *, address: dict[str, Any] | None = None
) -> None:
    row = {"id": PROP_ID, "street_address": STREET, "city": CITY,
           "state": STATE, "zip_code": ZIP}
    if address is not None:
        row.update(address)
    monkeypatch.setattr(
        "app.properties_db.get_connection", lambda config=None: _FakeConn(db)
    )
    monkeypatch.setattr(
        "app.workflow_db.get_connection", lambda config=None: _FakeConn(db)
    )
    monkeypatch.setattr(
        "app.properties_db.get_property",
        lambda pid, config=None: dict(row) if str(pid) == PROP_ID else None,
    )

    def _shim(*, operation_class: Any, event: Any, mutate: Any, config: Any = None) -> Any:
        return mutate(_FakeConn(db))

    monkeypatch.setattr(
        "app.observability.transactions.run_consequential_transaction", _shim
    )


def _boom_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network must not be touched")

    _patch_dns(monkeypatch)
    monkeypatch.setattr(httpx, "stream", _boom)


def test_collect_transmit_then_persist(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    body = _body().encode("utf-8")
    calls = _patch_stream(monkeypatch, _FakeResponse(chunks=[body]))
    _patch_owned(monkeypatch, db)
    summary = mvp_module.collect_census_audited(
        property_id=PROP_ID, principal="wondrue-operator", approved=True
    )
    assert calls["count"] == 1
    assert summary["replayed"] is False
    assert summary["county"] == "dallas county"
    assert summary["territory"] == "within_territory"
    assert len(summary["collection_digest"]) == 64
    assert len(summary["county_digest"]) == 64


def test_collect_replays_without_retransmit(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    _patch_stream(monkeypatch, _FakeResponse(chunks=[_body().encode("utf-8")]))
    _patch_owned(monkeypatch, db)
    first = mvp_module.collect_census_audited(
        property_id=PROP_ID, principal="wondrue-operator", approved=True
    )
    assert first["replayed"] is False
    _boom_stream(monkeypatch)
    second = mvp_module.collect_census_audited(
        property_id=PROP_ID, principal="wondrue-operator", approved=True
    )
    assert second["replayed"] is True
    assert second["collection_digest"] == first["collection_digest"]
    assert second["county_digest"] == first["county_digest"]


def test_collect_pre_transmit_refusals_spend_nothing(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    _boom_stream(monkeypatch)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="explicit approval"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=False
        )
    with pytest.raises(WorkflowError, match="principal"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="  ", approved=True
        )
    with pytest.raises(WorkflowError, match="unknown operational property"):
        mvp_module.collect_census_audited(
            property_id="00000000-0000-0000-0000-000000000000",
            principal="wondrue-operator",
            approved=True,
        )
    with pytest.raises(WorkflowError, match="attempt"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, attempt="0",
            principal="wondrue-operator", approved=True,
        )
    assert db.records == {}  # no claim, no retention


def test_collect_disabled_and_unusable_address_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.delenv("CENSUS_COLLECTION_ENABLED", raising=False)
    db = _FakeDB()
    _boom_stream(monkeypatch)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="not enabled"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True
        )
    monkeypatch.setenv("CENSUS_COLLECTION_ENABLED", "1")
    db2 = _FakeDB()
    _patch_owned(monkeypatch, db2, address={"street_address": "", "state": ""})
    with pytest.raises(WorkflowError, match="no usable"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True
        )


def test_collect_uncertain_claim_blocks_retransmit(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow import KIND_CENSUS_CLAIM, digest_payload
    from app.workflow_db import (
        WorkflowError,
        create_run_in_transaction,
        put_record_in_transaction,
    )

    db = _FakeDB()
    _boom_stream(monkeypatch)
    _patch_owned(monkeypatch, db)
    key = census_module.census_admission_run_key(
        property_id=PROP_ID, street=STREET, city=CITY, state=STATE,
        zip_code=ZIP, revision="census-1", attempt="1",
    )
    record_key = census_module.census_record_key(
        street=STREET, city=CITY, state=STATE, zip_code=ZIP
    )
    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=key)
    claim = {"state": "started", "property_id": PROP_ID}
    put_record_in_transaction(
        conn, run.run_id, kind=KIND_CENSUS_CLAIM,
        record_key=f"{record_key}:claim", version=1,
        payload=claim, digest=digest_payload(claim),
    )
    with pytest.raises(WorkflowError, match="uncertain prior"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True
        )


def test_collect_timeout_charged_and_preserved(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    import httpx

    from app import mvp as mvp_module
    from app.mvp import _CensusChargedError
    from app.workflow_db import WorkflowError, get_run_in_transaction

    db = _FakeDB()
    _patch_dns(monkeypatch)

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "stream", _timeout)
    _patch_owned(monkeypatch, db)
    with pytest.raises(_CensusChargedError, match="ambiguous"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True
        )
    run_id = next(iter(db.runs))
    stored = get_run_in_transaction(_FakeConn(db), UUID(run_id))
    assert stored is not None
    assert any(
        entry.get("census_attempt") == "ambiguous" for entry in stored.errors
    )
    _boom_stream(monkeypatch)
    with pytest.raises(WorkflowError, match="uncertain prior"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True
        )


def test_collect_refusal_retains_bytes_and_replays_refusal(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from app import mvp as mvp_module
    from app.mvp import _CensusChargedError
    from app.workflow import KIND_CENSUS_COLLECTION, KIND_CENSUS_REFUSAL
    from app.workflow_db import WorkflowError, get_record_in_transaction

    db = _FakeDB()
    refused_body = _body([])
    _patch_stream(monkeypatch, _FakeResponse(chunks=[refused_body.encode("utf-8")]))
    _patch_owned(monkeypatch, db)
    with pytest.raises(_CensusChargedError, match="refused"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True
        )
    run_id = next(iter(db.runs))
    record_key = census_module.census_record_key(
        street=STREET, city=CITY, state=STATE, zip_code=ZIP
    )
    collection = get_record_in_transaction(
        _FakeConn(db), UUID(run_id), KIND_CENSUS_COLLECTION, record_key
    )
    assert collection is not None
    assert collection.payload["body_text"] == refused_body  # bytes preserved
    refusal = get_record_in_transaction(
        _FakeConn(db), UUID(run_id), KIND_CENSUS_REFUSAL, record_key
    )
    assert refusal is not None and "no Census address match" in refusal.payload["reason"]
    _boom_stream(monkeypatch)
    with pytest.raises(WorkflowError, match="refused"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True
        )


def _spend_run(db: _FakeDB) -> UUID:
    from app.workflow_db import create_run_in_transaction

    run = create_run_in_transaction(_FakeConn(db), idempotency_key="eval-spend-1")
    return run.run_id


def _usage(db: _FakeDB, run_id: UUID, name: str) -> int:
    from app.workflow_db import get_run_in_transaction

    stored = get_run_in_transaction(_FakeConn(db), run_id)
    assert stored is not None
    return int((stored.usage or {}).get(name, 0))


def test_collect_charges_spend_run_at_claim_before_transmit(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    """A timeout after the claim commit must already show the charge."""
    import httpx

    from app import mvp as mvp_module
    from app.mvp import _CensusChargedError

    db = _FakeDB()
    _patch_dns(monkeypatch)

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "stream", _timeout)
    _patch_owned(monkeypatch, db)
    spend = _spend_run(db)
    with pytest.raises(_CensusChargedError, match="ambiguous"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True,
            spend_run_id=spend,
        )
    assert _usage(db, spend, "census_attempts") == 1


def test_collect_crash_after_claim_charges_once_and_blocks_retransmit(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    """A dead process between claim and retention leaves exactly one charge."""
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    class _SimulatedCrash(BaseException):
        pass

    def _die(*args: Any, **kwargs: Any) -> Any:
        raise _SimulatedCrash("process died mid-transmit")

    db = _FakeDB()
    _patch_owned(monkeypatch, db)
    monkeypatch.setattr(census_module, "collect_address", _die)
    spend = _spend_run(db)
    with pytest.raises(_SimulatedCrash):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True,
            spend_run_id=spend,
        )
    assert _usage(db, spend, "census_attempts") == 1
    _boom_stream(monkeypatch)
    with pytest.raises(WorkflowError, match="uncertain prior"):
        mvp_module.collect_census_audited(
            property_id=PROP_ID, principal="wondrue-operator", approved=True,
            spend_run_id=spend,
        )
    assert _usage(db, spend, "census_attempts") == 1  # no double charge


def test_collect_resume_after_retention_charges_nothing_more(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    """Completing an interrupted assessment reuses the original charge."""
    from app import mvp as mvp_module
    from app.workflow import KIND_CENSUS_CLAIM, KIND_CENSUS_COLLECTION, digest_payload
    from app.workflow_db import create_run_in_transaction, put_record_in_transaction

    db = _FakeDB()
    _boom_stream(monkeypatch)
    _patch_owned(monkeypatch, db)
    spend = _spend_run(db)
    key = census_module.census_admission_run_key(
        property_id=PROP_ID, street=STREET, city=CITY, state=STATE,
        zip_code=ZIP, revision="census-1", attempt="1",
    )
    record_key = census_module.census_record_key(
        street=STREET, city=CITY, state=STATE, zip_code=ZIP
    )
    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=key)
    claim = {"state": "started", "property_id": PROP_ID}
    put_record_in_transaction(
        conn, run.run_id, kind=KIND_CENSUS_CLAIM,
        record_key=f"{record_key}:claim", version=1,
        payload=claim, digest=digest_payload(claim),
    )
    collection = _collection()
    put_record_in_transaction(
        conn, run.run_id, kind=KIND_CENSUS_COLLECTION,
        record_key=record_key, version=1,
        payload=collection, digest=digest_payload(collection),
    )
    summary = mvp_module.collect_census_audited(
        property_id=PROP_ID, principal="wondrue-operator", approved=True,
        spend_run_id=spend,
    )
    assert summary["replayed"] is False
    assert summary["charged"] is False  # the original claim already charged
    assert summary["county"] == "dallas county"
    assert _usage(db, spend, "census_attempts") == 0


def test_collect_replay_charges_nothing(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    _patch_stream(monkeypatch, _FakeResponse(chunks=[_body().encode("utf-8")]))
    _patch_owned(monkeypatch, db)
    spend = _spend_run(db)
    first = mvp_module.collect_census_audited(
        property_id=PROP_ID, principal="wondrue-operator", approved=True,
        spend_run_id=spend,
    )
    assert first["charged"] is True
    assert _usage(db, spend, "census_attempts") == 1
    _boom_stream(monkeypatch)
    second = mvp_module.collect_census_audited(
        property_id=PROP_ID, principal="wondrue-operator", approved=True,
        spend_run_id=spend,
    )
    assert second["replayed"] is True
    assert second["charged"] is False
    assert _usage(db, spend, "census_attempts") == 1


def test_collect_for_evaluation_reports_charge_truth(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    _patch_stream(monkeypatch, _FakeResponse(chunks=[_body().encode("utf-8")]))
    _patch_owned(monkeypatch, db)
    collected = mvp_module._collect_census_for_evaluation(
        PROP_ID, principal="runner:scope"
    )
    assert collected["outcome"] == "collected"
    assert collected["charged"] is True
    assert collected["county"] == "dallas county"
    replayed = mvp_module._collect_census_for_evaluation(
        PROP_ID, principal="runner:scope"
    )
    assert replayed["outcome"] == "replayed"
    assert replayed["charged"] is False
    unknown = mvp_module._collect_census_for_evaluation(
        "00000000-0000-0000-0000-000000000000", principal="runner:scope"
    )
    assert unknown == {
        "outcome": "refused",
        "charged": False,
        "reason": unknown["reason"],
    }


def test_collect_for_evaluation_timeout_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    import httpx

    from app import mvp as mvp_module

    db = _FakeDB()
    _patch_dns(monkeypatch)

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "stream", _timeout)
    _patch_owned(monkeypatch, db)
    outcome = mvp_module._collect_census_for_evaluation(
        PROP_ID, principal="runner:scope"
    )
    assert outcome["outcome"] == "ambiguous"
    assert outcome["charged"] is True


def test_validate_census_attempt_limit() -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    assert mvp_module.validate_census_attempt_limit(0) == 0
    assert mvp_module.validate_census_attempt_limit(1) == 1
    for bad in (-1, 2, True, "1", None):
        with pytest.raises(WorkflowError, match="0\\.\\.1"):
            mvp_module.validate_census_attempt_limit(bad)


# --- In-stage application --------------------------------------------------------


def _dallas_intel(snippet: str):  # type: ignore[no-untyped-def]
    from app.agents.gabriel.sources.brave import BraveSearchResult

    result = BraveSearchResult(
        assignment_id="a1", query="q", title="t",
        url="https://listings.example.com/1", snippet=snippet,
        domain="listings.example.com",
        retrieved_at="2026-11-01T00:00:00+00:00", rank=1,
    )
    from app.agents.gabriel.extractor import extract_research_intelligence

    return extract_research_intelligence([result])


def _seed_admission(db: _FakeDB) -> dict[str, Any]:
    from uuid import uuid4

    from app.workflow import (
        KIND_CENSUS_COLLECTION,
        KIND_CENSUS_COUNTY,
        digest_payload,
    )
    from app.workflow_db import create_run_in_transaction, put_record_in_transaction

    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=f"census-test-{uuid4().hex}")
    collection = _collection()
    stored_collection = put_record_in_transaction(
        conn, run.run_id, kind=KIND_CENSUS_COLLECTION,
        record_key="census:test", version=1,
        payload=collection, digest=digest_payload(collection),
    )
    county = assess_county(
        collection=collection,
        collection_digest=str(stored_collection.digest or ""),
        property_record=_property_record(),
    )
    stored_county = put_record_in_transaction(
        conn, run.run_id, kind=KIND_CENSUS_COUNTY,
        record_key="census:test", version=1,
        payload=county, digest=digest_payload(county),
    )
    return {
        "run_id": run.run_id,
        "collection_digest": str(stored_collection.digest or ""),
        "county_digest": str(stored_county.digest or ""),
    }


B_STREET = "8702 S Lancaster Rd"
B_ZIP = "75241"
B_MATCHED = "8702 S LANCASTER RD, DALLAS, TX, 75241"


def _seed_admission_for(
    db: _FakeDB, *, street: str, city: str, state: str, zip_code: str
) -> dict[str, Any]:
    from uuid import uuid4

    from app.workflow import (
        KIND_CENSUS_COLLECTION,
        KIND_CENSUS_COUNTY,
        digest_payload,
    )
    from app.workflow_db import create_run_in_transaction, put_record_in_transaction

    matched_street = " ".join(part for part in street.replace(".", " ").split()).upper()
    body = _body(
        [
            _match(
                matchedAddress=f"{matched_street}, {city.upper()}, {state.upper()}, {zip_code}",
                addressComponents={"city": city.upper(), "state": state.upper(), "zip": zip_code},
            )
        ]
    )
    collection = _collection(
        body,
        submitted_street=street,
        submitted_city=city,
        submitted_state=state,
        submitted_zip=zip_code,
        url=census_module.census_request_url(street, city, state, zip_code),
    )
    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=f"census-test-{uuid4().hex}")
    stored_collection = put_record_in_transaction(
        conn, run.run_id, kind=KIND_CENSUS_COLLECTION,
        record_key="census:test", version=1,
        payload=collection, digest=digest_payload(collection),
    )
    county = assess_county(
        collection=collection,
        collection_digest=str(stored_collection.digest or ""),
        property_record=_property_record(),
    )
    stored_county = put_record_in_transaction(
        conn, run.run_id, kind=KIND_CENSUS_COUNTY,
        record_key="census:test", version=1,
        payload=county, digest=digest_payload(county),
    )
    return {
        "run_id": run.run_id,
        "collection_digest": str(stored_collection.digest or ""),
        "county_digest": str(stored_county.digest or ""),
    }


def test_apply_in_stage_sets_verified_county(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from uuid import uuid4

    from app import mvp as mvp_module
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers

    db = _FakeDB()
    _patch_owned(monkeypatch, db)
    admission = _seed_admission(db)
    intel = _dallas_intel("2323 Chalk Hill Rd, Dallas, TX 75212 office space available.")
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    assert len(dossier_set.dossiers) == 1
    key = dossier_set.dossiers[0].key
    mvp_module._apply_census_in_stage(
        _FakeConn(db),
        uuid4(),
        dossier_set,
        {key: PROP_ID},
        PROP_ID,
        {
            "collection_run_id": str(admission["run_id"]),
            "collection_digest": admission["collection_digest"],
            "county_digest": admission["county_digest"],
        },
    )
    attribution = dossier_set.dossiers[0].county_attribution
    assert attribution is not None
    assert attribution.evidence_status.value == "verified"
    assert attribution.value == "dallas county"
    locator = dossier_set.dossiers[0].census_evidence
    assert locator is not None
    assert locator["admission_run_id"] == str(admission["run_id"])
    assert locator["collection_digest"] == admission["collection_digest"]


def test_apply_in_stage_records_conflicts_without_mutating(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from uuid import uuid4

    from app import mvp as mvp_module
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers

    db = _FakeDB()
    _patch_owned(monkeypatch, db)
    admission = _seed_admission(db)
    intel = _dallas_intel(
        "2323 Chalk Hill Rd, Dallas, TX 75212. Portfolio spans Tarrant County assets."
    )
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    key = dossier_set.dossiers[0].key
    attribution = dossier_set.dossiers[0].county_attribution
    assert attribution is not None
    assert any(
        "tarrant" in str(claim.normalized) for claim in attribution.claims
    ), "fixture must carry a conflicting retained county claim"
    run_id = uuid4()
    from app.workflow_db import create_run_in_transaction, get_run_in_transaction

    create_run_in_transaction(_FakeConn(db), idempotency_key=f"census-eval-{run_id.hex}")
    eval_run_id = UUID(db.runs_by_key[f"census-eval-{run_id.hex}"])
    mvp_module._apply_census_in_stage(
        _FakeConn(db),
        eval_run_id,
        dossier_set,
        {key: PROP_ID},
        PROP_ID,
        {
            "collection_run_id": str(admission["run_id"]),
            "collection_digest": admission["collection_digest"],
            "county_digest": admission["county_digest"],
        },
    )
    assert dossier_set.dossiers[0].county_attribution is attribution
    assert attribution.evidence_status.value != "verified"
    stored = get_run_in_transaction(_FakeConn(db), eval_run_id)
    assert stored is not None
    refusals = [
        entry for entry in stored.errors if entry.get("census_county") == "not_applied"
    ]
    assert len(refusals) == 1
    assert "tarrant county" in refusals[0]["reason"]


def test_apply_in_stage_rejects_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    from uuid import uuid4

    from app import mvp as mvp_module
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.workflow_db import create_run_in_transaction, get_run_in_transaction

    db = _FakeDB()
    _patch_owned(monkeypatch, db)
    admission = _seed_admission(db)
    intel = _dallas_intel("2323 Chalk Hill Rd, Dallas, TX 75212 office space available.")
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    key = dossier_set.dossiers[0].key
    run_id = uuid4()
    create_run_in_transaction(_FakeConn(db), idempotency_key=f"census-eval-{run_id.hex}")
    eval_run_id = UUID(db.runs_by_key[f"census-eval-{run_id.hex}"])
    mvp_module._apply_census_in_stage(
        _FakeConn(db),
        eval_run_id,
        dossier_set,
        {key: PROP_ID},
        PROP_ID,
        {
            "collection_run_id": str(admission["run_id"]),
            "collection_digest": admission["collection_digest"],
            "county_digest": "tampered-digest",
        },
    )
    assert dossier_set.dossiers[0].county_attribution is not None
    assert dossier_set.dossiers[0].county_attribution.evidence_status.value != "verified"
    stored = get_run_in_transaction(_FakeConn(db), eval_run_id)
    assert stored is not None
    assert any(
        "do not match the evaluation bindings" in str(entry.get("reason", ""))
        for entry in stored.errors
        if entry.get("census_county") == "not_applied"
    )


def test_apply_in_stage_refuses_dossier_address_mismatch(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    """A moved property row must not launder B-evidence onto an A-dossier."""
    from uuid import uuid4

    from app import mvp as mvp_module
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.workflow_db import create_run_in_transaction, get_run_in_transaction

    db = _FakeDB()
    _patch_owned(
        monkeypatch, db,
        address={"street_address": B_STREET, "zip_code": B_ZIP},
    )
    admission = _seed_admission_for(
        db, street=B_STREET, city=CITY, state=STATE, zip_code=B_ZIP
    )
    intel = _dallas_intel("2323 Chalk Hill Rd, Dallas, TX 75212 office space available.")
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    key = dossier_set.dossiers[0].key
    assert "2323" in str(dossier_set.dossiers[0].street_address)
    run_id = uuid4()
    create_run_in_transaction(_FakeConn(db), idempotency_key=f"census-eval-{run_id.hex}")
    eval_run_id = UUID(db.runs_by_key[f"census-eval-{run_id.hex}"])
    mvp_module._apply_census_in_stage(
        _FakeConn(db),
        eval_run_id,
        dossier_set,
        {key: PROP_ID},
        PROP_ID,
        {
            "collection_run_id": str(admission["run_id"]),
            "collection_digest": admission["collection_digest"],
            "county_digest": admission["county_digest"],
        },
    )
    assert dossier_set.dossiers[0].county_attribution is not None
    assert dossier_set.dossiers[0].county_attribution.evidence_status.value != "verified"
    stored = get_run_in_transaction(_FakeConn(db), eval_run_id)
    assert stored is not None
    assert any(
        "dossier street address" in str(entry.get("reason", ""))
        for entry in stored.errors
        if entry.get("census_county") == "not_applied"
    )


def test_census_evidence_refs_locate_stored_rows(
    monkeypatch: pytest.MonkeyPatch, _census_env: None
) -> None:
    """A persisted dossier finds its exact Census rows without URL parsing."""
    from app import mvp as mvp_module

    db = _FakeDB()
    _patch_owned(monkeypatch, db)
    admission = _seed_admission(db)
    refs = census_module.census_evidence_refs(
        admission_run_id=str(admission["run_id"]),
        collection_record_key="census:test",
        county_record_key="census:test",
        collection_digest=admission["collection_digest"],
        county_digest=admission["county_digest"],
        county_result=_county_result(
            collection_digest=admission["collection_digest"]
        ),
    )
    assert refs["admission_run_id"] == str(admission["run_id"])
    dossier = dict(_dossier_address(), census_evidence=refs)
    found = mvp_module.load_census_evidence_for_dossier(_FakeConn(db), dossier)
    assert found["collection_digest"] == admission["collection_digest"]
    assert found["county_digest"] == admission["county_digest"]
    assert found["county"] == "dallas county"
    forged = dict(
        dossier,
        census_evidence=dict(refs, county_digest="0" * 64),
    )
    from app.workflow_db import WorkflowError

    with pytest.raises(WorkflowError, match="digest"):
        mvp_module.load_census_evidence_for_dossier(_FakeConn(db), forged)
    with pytest.raises(WorkflowError, match="census evidence"):
        mvp_module.load_census_evidence_for_dossier(_FakeConn(db), {})


# --- Successor evaluation (pure) ---------------------------------------------------


def _artifact(snippet: str) -> dict[str, Any]:
    return {
        "provider": "brave",
        "assignment_id": "a1",
        "query": "q",
        "results": [
            {
                "url": "https://listings.example.com/1",
                "title": "t",
                "snippet": snippet,
                "domain": "listings.example.com",
                "published_at": None,
                "retrieved_at": "2026-11-01T00:00:00+00:00",
                "rank": 1,
            }
        ],
        "retrieved_at": "2026-11-01T00:00:00+00:00",
        "collection_provenance": "adapter_collected",
        "attempt": "completed",
        "error": "",
    }


def test_successor_evaluation_applies_county_without_authorizing(
    _census_env: None,
) -> None:
    from app import mvp as mvp_module
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.extractor import extract_research_intelligence
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult

    snippet = "2323 Chalk Hill Rd, Dallas, TX 75212 office space available."
    result = BraveSearchResult(
        assignment_id="a1", query="q", title="t",
        url="https://listings.example.com/1", snippet=snippet,
        domain="listings.example.com",
        retrieved_at="2026-11-01T00:00:00+00:00", rank=1,
    )
    intel = extract_research_intelligence([result])
    key = rank_intelligence(intel).candidates[0].key
    prior = build_dossiers(DossierRequest(rounds=[intel])).dossiers[0].model_dump(mode="json")
    from uuid import uuid4

    collection_hex, county_hex = "ab" * 32, "cd" * 32
    admission_run = str(uuid4())
    evaluated = mvp_module._evaluate_successor_evidence(
        parent_payload={"dossier": prior},
        retained_payloads=[_artifact(snippet)],
        assessment=_county_result(collection_digest=collection_hex),
        collection_url=census_request_url(STREET, CITY, STATE, ZIP),
        dossier_key=key,
        evidence_source="census",
        collection_digest=collection_hex,
        property_id=PROP_ID,
        current_address=_current_address(),
        collection_run_id=admission_run,
        collection_record_key="census:test",
        assessment_record_key="census:test",
        assessment_digest=county_hex,
    )
    rebuilt = evaluated["rebuilt_dossier"]
    assert rebuilt["county_attribution"]["evidence_status"] == "verified"
    assert rebuilt["county_attribution"]["value"] == "dallas county"
    assert rebuilt["census_evidence"]["admission_run_id"] == admission_run
    assert rebuilt["census_evidence"]["county_digest"] == county_hex
    assert rebuilt["census_evidence"]["county"] == "dallas county"
    assert evaluated["qualification"]["outcome"] == "qualified"
    assert evaluated["qualification"]["county_credit"] == "dallas county"
    assert evaluated["verdict"]["outcome"] == "not_verified"  # ordinary path
    assert evaluated["raphael"]["outcome"] == "needs_evidence"
    assert evaluated["voice_alignment"] == {}
    assert evaluated["county_application"]["applied"] is True
    assert evaluated["county_application"]["county"] == "dallas county"


def test_successor_evaluation_county_conflict_refuses() -> None:
    from app import mvp as mvp_module
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.extractor import extract_research_intelligence
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult
    from app.workflow_db import WorkflowError

    snippet = (
        "2323 Chalk Hill Rd, Dallas, TX 75212. Portfolio spans Tarrant County assets."
    )
    result = BraveSearchResult(
        assignment_id="a1", query="q", title="t",
        url="https://listings.example.com/1", snippet=snippet,
        domain="listings.example.com",
        retrieved_at="2026-11-01T00:00:00+00:00", rank=1,
    )
    intel = extract_research_intelligence([result])
    key = rank_intelligence(intel).candidates[0].key
    prior = build_dossiers(DossierRequest(rounds=[intel])).dossiers[0].model_dump(mode="json")
    with pytest.raises(WorkflowError, match="not applicable"):
        mvp_module._evaluate_successor_evidence(
            parent_payload={"dossier": prior},
            retained_payloads=[_artifact(snippet)],
            assessment=_county_result(),
            collection_url=census_request_url(STREET, CITY, STATE, ZIP),
            dossier_key=key,
            evidence_source="census",
            collection_digest="digest-under-test",
            property_id=PROP_ID,
            current_address=_current_address(),
        )


# --- Live PostgreSQL (GROWUP_PG_INTEGRATION=1, mocked network) ----------------------

_LIVE_PG = _os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_PG_GATE_DATABASE = "growup_mvp_gate"


def _require_gate_database() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS db")
            name = str((cur.fetchone() or {}).get("db") or "")
    if name != _PG_GATE_DATABASE:
        pytest.fail(
            f"refusing PG-mutating Census test on database {name!r}: "
            f"owner must run against {_PG_GATE_DATABASE!r}",
            pytrace=False,
        )


def _require_successor_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.dossier_versions') AS v, "
                "to_regclass('public.custody_receipts') AS r, "
                "to_regclass('public.pilot_events') AS p"
            )
            row = cur.fetchone() or {}
    missing = [name for name in ("v", "r", "p") if not row.get(name)]
    if missing:
        pytest.fail(
            "successor/pilot tables are missing; owner must apply migrations "
            "001-017 to growup_mvp_gate (never the operational database)",
            pytrace=False,
        )


def _pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "census-pg-test-custody-key-0123456789")
    monkeypatch.setenv("CUSTODY_AMENDERS", "pg-test-owner")
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("CENSUS_COLLECTION_ENABLED", "1")


def _pg_cleanup_census(
    run_ids: list[str],
    quota_ids: list[str],
    property_ids: list[str] | None = None,
) -> None:
    """Remove disposable Census rows (no custody rows are created here)."""
    from app.properties_db import get_connection

    run_ids = [str(item) for item in run_ids if item]
    quota_ids = [str(item) for item in quota_ids if item]
    property_ids = [str(item) for item in (property_ids or []) if item]
    with get_connection() as conn:
        with conn.cursor() as cur:
            if quota_ids:
                cur.execute(
                    "DELETE FROM pilot_events WHERE dossier_id = ANY(%(ids)s)",
                    {"ids": quota_ids},
                )
            if run_ids:
                cur.execute(
                    "DELETE FROM workflow_records WHERE run_id = ANY(%(ids)s)",
                    {"ids": run_ids},
                )
                cur.execute(
                    "DELETE FROM workflow_runs WHERE run_id = ANY(%(ids)s)",
                    {"ids": run_ids},
                )
            if property_ids:
                cur.execute(
                    "DELETE FROM properties WHERE id = ANY(%(ids)s)",
                    {"ids": property_ids},
                )
        conn.commit()


@needs_live_pg
def test_census_collect_single_request_pg_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent identical collects transmit exactly once (owner-run).

    The provider stays mocked; only the claim/retention concurrency is
    live PostgreSQL. The loser replays or is refused without
    transmitting, and the transmitted attempt is charged once.
    """
    import threading
    import time

    import httpx

    from app import mvp as mvp_module
    from app.properties_db import create_property

    _require_gate_database()
    _pg_env(monkeypatch)
    prop = create_property(
        {
            "name": "census-pg-race-property",
            "street_address": STREET,
            "city": CITY,
            "state": STATE,
            "zip_code": ZIP,
        }
    )
    prop_id = str(prop["id"])
    created_runs: list[str] = []
    calls = {"count": 0}
    lock = threading.Lock()

    def _stream(method: str, url: str, **kwargs: Any) -> Any:
        with lock:
            calls["count"] += 1
        time.sleep(0.2)
        return _FakeResponse(chunks=[_body().encode("utf-8")])

    _patch_dns(monkeypatch)
    monkeypatch.setattr(httpx, "stream", _stream)
    outcomes: dict[str, Any] = {}

    def _worker(slot: str) -> None:
        try:
            outcomes[slot] = mvp_module.collect_census_audited(
                property_id=prop_id, principal="pg-test-owner", approved=True
            )
        except Exception as exc:  # recorded, then asserted below
            outcomes[slot] = exc

    try:
        first = threading.Thread(target=_worker, args=("a",))
        second = threading.Thread(target=_worker, args=("b",))
        first.start()
        second.start()
        first.join(timeout=60)
        second.join(timeout=60)
        assert not first.is_alive() and not second.is_alive()
        assert calls["count"] == 1
        transmitted = [
            outcome
            for outcome in outcomes.values()
            if isinstance(outcome, dict) and outcome.get("replayed") is False
        ]
        assert len(transmitted) == 1
        assert transmitted[0]["county"] == "dallas county"
        created_runs.extend(
            str(outcome["run_id"])
            for outcome in outcomes.values()
            if isinstance(outcome, dict)
        )
        loser = outcomes["b"] if outcomes["a"] is transmitted[0] else outcomes["a"]
        if isinstance(loser, dict):
            assert loser["replayed"] is True
        else:
            assert "uncertain" in str(loser) or "another caller" in str(loser)
    finally:
        _pg_cleanup_census(created_runs, [], [prop_id])


@needs_live_pg
def test_census_successor_commit_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Census successor earns one county credit on disposable PG (owner-run)."""
    import uuid as uuid_module

    from app import mvp as mvp_module
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.extractor import extract_research_intelligence
    from app.agents.gabriel.producers import build_candidate_verifications
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult
    from app.custody import dossier_id_for_property
    from app.custody_db import (
        create_version_in_transaction,
        issue_receipt_in_transaction,
        latest_version_in_transaction,
        snapshot_in_transaction,
    )
    from app.pilot import canonical_quota_id, ensure_ledger_in_transaction
    from app.properties_db import create_property, get_connection
    from app.workflow import (
        KIND_CENSUS_COLLECTION,
        KIND_CENSUS_COUNTY,
        KIND_DOSSIER_VERSION,
        KIND_RESEARCH_ARTIFACT,
        digest_payload,
    )
    from app.workflow_db import (
        create_run_in_transaction,
        get_record_in_transaction,
        put_record_in_transaction,
    )

    _require_gate_database()
    _require_successor_tables()
    _pg_env(monkeypatch)
    prop = create_property(
        {
            "name": "census-pg-successor-property",
            "street_address": STREET,
            "city": CITY,
            "state": STATE,
            "zip_code": ZIP,
        }
    )
    prop_id = str(prop["id"])
    quota_id = str(canonical_quota_id(prop_id))
    dossier_uuid = dossier_id_for_property(prop_id)
    created_runs: list[str] = []
    try:
        body = _body().encode("utf-8")
        _patch_stream(monkeypatch, _FakeResponse(chunks=[body]))
        collected = mvp_module.collect_census_audited(
            property_id=prop_id, principal="pg-test-owner", approved=True
        )
        assert collected["replayed"] is False
        created_runs.append(str(collected["run_id"]))
        with get_connection() as conn:
            ensure_ledger_in_transaction(conn)
            parent_run = create_run_in_transaction(
                conn, idempotency_key=f"census-pg-parent-{uuid_module.uuid4().hex}"
            )
            created_runs.append(str(parent_run.run_id))
            snippet = "2323 Chalk Hill Rd, Dallas, TX 75212 office space available."
            result = BraveSearchResult(
                assignment_id="a1", query="q", title="t",
                url="https://listings.example.com/1", snippet=snippet,
                domain="listings.example.com",
                retrieved_at="2026-11-01T00:00:00+00:00", rank=1,
            )
            artifact = {
                "provider": "brave", "assignment_id": "a1", "query": "q",
                "results": [
                    {
                        "url": result.url, "title": result.title, "snippet": snippet,
                        "domain": result.domain, "published_at": None,
                        "retrieved_at": result.retrieved_at, "rank": 1,
                    }
                ],
                "retrieved_at": "2026-11-01T00:00:00+00:00",
                "collection_provenance": "adapter_collected",
                "attempt": "completed", "error": "",
            }
            put_record_in_transaction(
                conn, parent_run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                record_key="a1", version=1, payload=artifact,
                digest=digest_payload(artifact),
            )
            intel = extract_research_intelligence([result])
            ranking = rank_intelligence(intel)
            key = ranking.candidates[0].key
            dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
            verdicts = build_candidate_verifications(ranking, intel)
            dossier = dossier_set.dossiers[0].model_dump(mode="json")
            payload = {
                "version": 1, "builder_version": "pg-test", "artifact_refs": ["a1"],
                "dossier": dossier,
                "candidate_verification": verdicts[key].model_dump(mode="json"),
                "property_id": prop_id,
            }
            payload["digest"] = digest_payload(
                {k: v for k, v in payload.items() if k != "digest"}
            )
            stored_parent = put_record_in_transaction(
                conn, parent_run.run_id, kind=KIND_DOSSIER_VERSION,
                record_key=key, version=1, payload=payload,
                digest=str(payload["digest"]),
            )
            version = create_version_in_transaction(
                conn, property_id=prop_id, payload={"dossier": dossier}, created_by="gabriel"
            )
            snapshot_in_transaction(conn, dossier_id=version.dossier_id, version=version.version)
            issue_receipt_in_transaction(
                conn, dossier_id=version.dossier_id, version=version.version,
                kind="forward", from_agent="gabriel", to_agent="raphael",
                issued_by="pg-test", note="assess",
            )
            issue_receipt_in_transaction(
                conn, dossier_id=version.dossier_id, version=version.version,
                kind="return", from_agent="raphael", to_agent="gabriel",
                issued_by="pg-test", note="needs evidence",
            )
            conn.commit()
        parent_digest = str(stored_parent.digest or "")
        with get_connection() as conn:
            preview = mvp_module.preview_successor_in_transaction(
                conn,
                property_id=prop_id,
                parent_run_id=str(parent_run.run_id),
                dossier_key=key,
                expected_parent_version=1,
                expected_custody_version=1,
                collection_run_id=str(collected["run_id"]),
            )
        assert preview["ready"] is True, preview["blockers"]
        assert preview["assessment"]["evidence_source"] == "census"
        assert preview["intended"]["qualification"]["county_credit"] == "dallas county"
        summary = mvp_module.commit_successor_audited(
            property_id=prop_id,
            parent_run_id=str(parent_run.run_id),
            dossier_key=key,
            parent_version=1,
            parent_digest=parent_digest,
            expected_custody_version=1,
            collection_run_id=str(collected["run_id"]),
            collection_digest=str(collected["collection_digest"]),
            assessment_digest=str(collected["county_digest"]),
            reason="pg acceptance",
            principal="pg-test-owner",
            approved=True,
        )
        created_runs.append(str(summary["run_id"]))
        assert summary["decision"] == "needs_evidence"
        assert summary["custody_version"] == 2
        replay = mvp_module.commit_successor_audited(
            property_id=prop_id,
            parent_run_id=str(parent_run.run_id),
            dossier_key=key,
            parent_version=1,
            parent_digest=parent_digest,
            expected_custody_version=1,
            collection_run_id=str(collected["run_id"]),
            collection_digest=str(collected["collection_digest"]),
            assessment_digest=str(collected["county_digest"]),
            reason="pg acceptance",
            principal="pg-test-owner",
            approved=True,
        )
        assert replay["replayed"] is True
        assert replay["dossier_digest"] == summary["dossier_digest"]
        with get_connection() as conn:
            latest = latest_version_in_transaction(conn, dossier_uuid)
            assert latest is not None and latest.version == 2
            reread = get_record_in_transaction(
                conn, parent_run.run_id, KIND_DOSSIER_VERSION, key
            )
            assert reread is not None and str(reread.digest or "") == parent_digest
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT transition, COUNT(*) AS n FROM pilot_events "
                    "WHERE dossier_id = %(d)s GROUP BY transition",
                    {"d": quota_id},
                )
                counts = {row["transition"]: int(row["n"]) for row in cur.fetchall()}
        assert counts.get("qualified", 0) == 1  # one county credit, never duplicated
    finally:
        _pg_cleanup_census(created_runs, [quota_id, str(dossier_uuid)], [prop_id])


@needs_live_pg
def test_census_evaluation_county_credit_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """New evaluation: evidence to cell credit on disposable PG (owner-run).

    Brave acquisition is stubbed at the owned-artifact seam (no live
    Brave call, no budget rows); Census HTTP stays mocked; every
    other step -- collection claim, retention, assessment,
    dossier/qualification persistence, Raphael routing, pilot
    recording -- runs against live PostgreSQL.
    """
    import uuid as uuid_module

    from app import mvp as mvp_module
    from app.pilot import canonical_quota_id, ensure_ledger_in_transaction
    from app.properties_db import create_property, get_connection
    from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
    from app.workflow_db import (
        get_record_in_transaction,
        put_record_in_transaction,
    )

    _require_gate_database()
    _require_successor_tables()
    _pg_env(monkeypatch)
    prop = create_property(
        {
            "name": "census-pg-evaluation-property",
            "street_address": STREET,
            "city": CITY,
            "state": STATE,
            "zip_code": ZIP,
        }
    )
    prop_id = str(prop["id"])
    quota_id = str(canonical_quota_id(prop_id))
    created_runs: list[str] = []
    try:
        with get_connection() as conn:
            ensure_ledger_in_transaction(conn)
            conn.commit()
        _patch_stream(monkeypatch, _FakeResponse(chunks=[_body().encode("utf-8")]))

        def _stub_ensure(run_id: Any, aid: str, query: str, **kwargs: Any) -> dict[str, Any]:
            artifact = {
                "provider": "brave", "assignment_id": aid, "query": query,
                "results": [
                    {
                        "url": "https://listings.example.com/1",
                        "title": "t",
                        "snippet": "2323 Chalk Hill Rd, Dallas, TX 75212 office space.",
                        "domain": "listings.example.com",
                        "published_at": None,
                        "retrieved_at": "2026-11-01T00:00:00+00:00",
                        "rank": 1,
                    }
                ],
                "retrieved_at": "2026-11-01T00:00:00+00:00",
                "collection_provenance": "adapter_collected",
                "attempt": "completed", "error": "",
            }
            with get_connection() as owned:
                put_record_in_transaction(
                    owned, run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key=aid, version=1, payload=artifact,
                    digest=digest_payload(artifact),
                )
                owned.commit()
            return artifact

        monkeypatch.setattr(mvp_module, "ensure_artifact_owned", _stub_ensure)
        summary = mvp_module.start_evaluation_audited(
            f"census-pg-eval-{uuid_module.uuid4().hex}",
            "2323 Chalk Hill Rd Dallas TX",
            approved=True,
            property_id=prop_id,
            census_attempt_limit=1,
            census_principal="pg-test-owner",
        )
        created_runs.append(str(summary["run_id"]))
        assert summary["census"]["outcome"] == "collected"
        assert summary["census"]["charged"] is True
        assert summary["census"]["county"] == "dallas county"
        created_runs.append(str(summary["census"]["admission_run_id"]))
        assert summary["decision"] == "needs_evidence"
        with get_connection() as conn:
            quals = [
                record
                for record in mvp_module.list_records_in_transaction(
                    conn, summary["run_id"], "qualification"
                )
            ]
            assert len(quals) == 1
            assert quals[0].payload["county_credit"] == "dallas county"
            dossier_record = get_record_in_transaction(
                conn, summary["run_id"], "dossier_version", summary["dossier_keys"][0]
            )
            assert dossier_record is not None
            county = dossier_record.payload["dossier"]["county_attribution"]
            assert county["evidence_status"] == "verified"
            assert county["value"] == "dallas county"
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT transition, COUNT(*) AS n FROM pilot_events "
                    "WHERE dossier_id = %(d)s GROUP BY transition",
                    {"d": quota_id},
                )
                counts = {row["transition"]: int(row["n"]) for row in cur.fetchall()}
        assert counts.get("qualified", 0) == 1
    finally:
        _pg_cleanup_census(created_runs, [quota_id], [prop_id])
