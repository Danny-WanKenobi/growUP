"""DCAD evidence admission and same-dossier successor acceptance.

Offline fixtures under ``fixtures/dcad_account_valid.html`` are TEST
MATERIAL ONLY with a representative page structure -- not retained
live source evidence. Live DCAD compatibility and real authorization
are not proven here; live retrieval stays owner-executed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.agents.gabriel.sources import dcad as dcad_module
from app.agents.gabriel.sources.dcad import (
    DcadConfigError,
    DcadFetchError,
    DcadParseError,
    build_dcad_assessment,
    collect_account,
    dcad_account_url,
    parse_account_page,
)

ACCOUNT = "00000700090000000"
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dcad_account_valid.html"


def _body() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _collection(body: str | None = None, **overrides: Any) -> dict[str, Any]:
    text = _body() if body is None else body
    payload = {
        "collector": "dcad-commercial-account-html",
        "collector_revision": "dcad-1",
        "account": ACCOUNT,
        "url": dcad_account_url(ACCOUNT),
        "retrieved_at": "2026-11-01T00:00:00+00:00",
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "body_bytes": len(text.encode("utf-8")),
        "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "body_text": text,
        "principal": "wondrue-operator",
        "collection_provenance": "adapter_collected",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def _dcad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", ACCOUNT)


# --- Parser ---------------------------------------------------------------


_TITLE_TEXT = "Commercial Account #00000700090000000"
_TITLE_OPEN = '<span id="lblPageTitle" class="PageTitle">'
_HEADING_OPEN = '<span id="lblMainImp" class="DtlSectionHdr">'
_OFFICE_ROW = (
    "<tr><td><b>Desc:</b>&nbsp; OFFICE BUILDING</td>"
    "<td><b>Total Area:</b>&nbsp; 1,440 sqft</td>"
    "<td><b>Year Built:</b>&nbsp; 1968</td></tr>"
)
_STORAGE_YEAR = "19,440 sqft</td><td><b>Year Built:</b>&nbsp; 1968"
_OFFICE_YEAR = "1,440 sqft</td><td><b>Year Built:</b>&nbsp; 1968</td></tr>"
_MISLEADING_AREAS = {17200, 18000, 1100, 1200}


def test_parse_valid_fixture() -> None:
    parsed = parse_account_page(_body(), expected_account=ACCOUNT)
    assert parsed["account"] == ACCOUNT
    assert parsed["account_locator"] == "#lblPageTitle"
    assert parsed["address_raw"] == "2323 CHALK HILL RD"
    assert parsed["address_locator"] == "#PropAddr1_lblPropAddr"
    improvements = parsed["improvements"]
    assert [
        (entry["description_raw"], entry["area_sqft"], entry["year_built"])
        for entry in improvements
    ] == [
        ("STORAGE WAREHOUSE", 19440, 1968),
        ("OFFICE BUILDING", 1440, 1968),
    ]
    assert [entry["area_raw"] for entry in improvements] == [
        "19,440 sqft",
        "1,440 sqft",
    ]
    assert [entry["year_built_raw"] for entry in improvements] == ["1968", "1968"]
    assert all(entry["locator"] for entry in improvements)
    assert {entry["area_sqft"] for entry in improvements}.isdisjoint(_MISLEADING_AREAS)
    assert set(improvements[0]) == {
        "description_raw", "area_raw", "area_sqft",
        "year_built_raw", "year_built", "locator",
    }
    labels = [entry["label"] for entry in parsed["period_labels"]]
    assert "Improvements (Current 2027)" in labels
    assert "Property Location - Current 2027" in labels
    assert "Certified 2026 Appraisal Values" in labels
    assert parsed["skipped_rows"] == 0


def test_parse_missing_address_fails() -> None:
    body = _body().replace('<span id="PropAddr1_lblPropAddr">2323 CHALK HILL RD</span>', "")
    with pytest.raises(DcadParseError, match="source address"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_duplicate_address_is_ambiguous() -> None:
    span = '<span id="PropAddr1_lblPropAddr">2323 CHALK HILL RD</span>'
    body = _body().replace(span, span + span)
    with pytest.raises(DcadParseError, match="ambiguous"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_account_mismatch_fails() -> None:
    with pytest.raises(DcadParseError, match="account title"):
        parse_account_page(_body(), expected_account="00000700090000002")


def test_parse_missing_title_fails() -> None:
    body = _body().replace(_TITLE_TEXT, "Commercial Account #00000000000000000")
    assert ACCOUNT in body  # still present in script/style/form action only
    with pytest.raises(DcadParseError, match="account title"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_nearby_link_cannot_bind_account() -> None:
    body = _body().replace(_TITLE_TEXT, "Commercial Account #00000000000000000")
    # Requested account appears only in a (labeled) nearby-account link.
    body = body.replace(
        '<a href="AcctDetailCom.aspx?ID=00000700090000001">00000700090000001</a>',
        '<a id="nearbyLink" href="AcctDetailCom.aspx?ID=00000700090000000">'
        "00000700090000000</a>",
    )
    with pytest.raises(DcadParseError, match="account title"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_unlabeled_title_cannot_bind_account() -> None:
    body = _body().replace(_TITLE_OPEN, '<span class="PageTitle">')
    with pytest.raises(DcadParseError, match="account title"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_title_with_other_account_fails() -> None:
    # Title names a different account while the requested one echoes
    # in page text outside the title: identity must not bind.
    body = _body().replace(_TITLE_TEXT, "Commercial Account #00000700090000001")
    assert ACCOUNT in body
    with pytest.raises(DcadParseError, match="account title"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_ignores_table_before_section() -> None:
    decoy = (
        "<table><tr><td><b>Desc:</b> DECOY SHACK</td>"
        "<td><b>Total Area:</b> 11 sqft</td></tr></table>"
    )
    body = _body().replace(_HEADING_OPEN, decoy + _HEADING_OPEN)
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    assert [entry["description_raw"] for entry in parsed["improvements"]] == [
        "STORAGE WAREHOUSE",
        "OFFICE BUILDING",
    ]


def test_parse_ignores_table_after_section() -> None:
    # The fixture's Land marker already bounds the section; trailing
    # labeled rows past it (here and in the Land decoy) are excluded.
    decoy = (
        "<table><tr><td><b>Desc:</b> DECOY SHACK</td>"
        "<td><b>Total Area:</b> 11 sqft</td></tr></table>"
    )
    body = _body().replace("</form>", decoy + "</form>")
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    assert [entry["description_raw"] for entry in parsed["improvements"]] == [
        "STORAGE WAREHOUSE",
        "OFFICE BUILDING",
    ]


def test_parse_stops_at_next_dcad_section() -> None:
    # The Land section marker ends the improvements section: its
    # fully labeled decoy row must not parse as an entry, while
    # both genuine entries remain. The FieldTitle/FieldValue spans
    # between the improvement tables must not bound the section.
    parsed = parse_account_page(_body(), expected_account=ACCOUNT)
    descriptions = [entry["description_raw"] for entry in parsed["improvements"]]
    assert descriptions == ["STORAGE WAREHOUSE", "OFFICE BUILDING"]
    assert "DECOY LOT" not in descriptions
    assert all(entry["area_sqft"] != 99999 for entry in parsed["improvements"])
    labels = [entry["label"] for entry in parsed["period_labels"]]
    assert "Land (2026 Certified Values)" in labels


def test_parse_heading_tags_are_not_boundaries() -> None:
    # Real DCAD sectioning uses span.DtlSectionHdr; an h-tag inside
    # the section must not truncate genuine entries.
    body = _body().replace(
        '<table id="MainImp_imp2">',
        "<h3>Mid-section Note</h3>" + '<table id="MainImp_imp2">',
    )
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    assert [entry["description_raw"] for entry in parsed["improvements"]] == [
        "STORAGE WAREHOUSE",
        "OFFICE BUILDING",
    ]


def test_parse_nested_improvements_parsed_once() -> None:
    # The valid fixture already nests each improvement table inside a
    # wrapper table; ancestor aggregation must not duplicate entries.
    parsed = parse_account_page(_body(), expected_account=ACCOUNT)
    assert [(entry["description_raw"], entry["area_sqft"]) for entry in parsed["improvements"]] == [
        ("STORAGE WAREHOUSE", 19440),
        ("OFFICE BUILDING", 1440),
    ]
    # One more wrapper level still yields each entry exactly once.
    # (The first </table> close in the base fixture is the imp1 table.)
    body = _body().replace(
        '<table id="MainImp_imp1">',
        "<table><tr><td>" + '<table id="MainImp_imp1">',
    )
    body = body.replace("</table>", "</table></td></tr></table>", 1)
    reparsed = parse_account_page(body, expected_account=ACCOUNT)
    assert [(entry["description_raw"], entry["area_sqft"]) for entry in reparsed["improvements"]] == [
        ("STORAGE WAREHOUSE", 19440),
        ("OFFICE BUILDING", 1440),
    ]


def test_parse_duplicate_heading_is_ambiguous() -> None:
    body = _body().replace(_HEADING_OPEN, _HEADING_OPEN + "DUPLICATE</span>" + _HEADING_OPEN)
    with pytest.raises(DcadParseError, match="ambiguous"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_empty_description_row_skipped() -> None:
    body = _body().replace("<b>Desc:</b>&nbsp; OFFICE BUILDING", "<b>Desc:</b>")
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    assert [entry["description_raw"] for entry in parsed["improvements"]] == [
        "STORAGE WAREHOUSE"
    ]
    assert parsed["skipped_rows"] == 1


def test_parse_year_built_stays_with_its_entry() -> None:
    body = _body().replace(_STORAGE_YEAR, _STORAGE_YEAR.replace("1968", "1952"))
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    assert [(entry["description_raw"], entry["year_built"]) for entry in parsed["improvements"]] == [
        ("STORAGE WAREHOUSE", 1952),
        ("OFFICE BUILDING", 1968),
    ]


def test_parse_missing_total_area_stays_unknown() -> None:
    body = _body().replace(
        _OFFICE_ROW,
        "<tr><td><b>Desc:</b>&nbsp; OFFICE BUILDING</td>"
        "<td><b>Year Built:</b>&nbsp; 1968</td></tr>",
    )
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    office = parsed["improvements"][1]
    assert office["description_raw"] == "OFFICE BUILDING"
    assert office["area_sqft"] is None
    assert office["area_raw"] == ""
    # The neighboring foundation/lease values must not fill the gap.
    assert office["area_sqft"] not in _MISLEADING_AREAS


def test_parse_missing_year_built_stays_unknown() -> None:
    body = _body().replace(
        _OFFICE_ROW,
        "<tr><td><b>Desc:</b>&nbsp; OFFICE BUILDING</td>"
        "<td><b>Total Area:</b>&nbsp; 1,440 sqft</td></tr>",
    )
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    office = parsed["improvements"][1]
    assert office["description_raw"] == "OFFICE BUILDING"
    assert office["area_sqft"] == 1440
    assert office["year_built"] is None
    assert office["year_built_raw"] == ""


def test_parse_duplicate_total_area_row_skipped() -> None:
    body = _body().replace(
        _OFFICE_YEAR,
        "1,440 sqft</td><td><b>Total Area:</b>&nbsp; 9,999 sqft</td>"
        "<td><b>Year Built:</b>&nbsp; 1968</td></tr>",
    )
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    assert [entry["description_raw"] for entry in parsed["improvements"]] == [
        "STORAGE WAREHOUSE"
    ]
    assert parsed["skipped_rows"] == 1


def test_parse_missing_entries_fail() -> None:
    body = _body().replace("<table", "<!--table").replace("</table>", "table-->")
    with pytest.raises(DcadParseError, match="improvement entries"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_unrecognized_labels_fail() -> None:
    body = _body().replace("<b>Desc:</b>", "<b>Item:</b>").replace(
        "<b>Total Area:</b>", "<b>Size:</b>"
    )
    with pytest.raises(DcadParseError, match="improvement entries"):
        parse_account_page(body, expected_account=ACCOUNT)


def test_parse_unparseable_area_stays_unknown() -> None:
    body = _body().replace("<b>Total Area:</b>&nbsp; 1,440 sqft", "<b>Total Area:</b>&nbsp; N/A")
    parsed = parse_account_page(body, expected_account=ACCOUNT)
    office = parsed["improvements"][1]
    assert office["description_raw"] == "OFFICE BUILDING"
    assert office["area_sqft"] is None
    assert office["area_raw"] == "N/A"


def test_parse_empty_body_fails() -> None:
    with pytest.raises(DcadParseError, match="empty"):
        parse_account_page("", expected_account=ACCOUNT)


# --- Collector --------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._chunks = chunks if chunks is not None else [b"<html></html>"]

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
    monkeypatch.delenv("DCAD_COLLECTION_ENABLED", raising=False)
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", ACCOUNT)

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(httpx, "stream", _boom)
    with pytest.raises(DcadConfigError, match="not enabled"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_non_allowlisted_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", "00000700090000001")
    with pytest.raises(DcadConfigError, match="allowlisted"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_malformed_account_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", "anything")
    with pytest.raises(DcadConfigError, match="8-24 digits"):
        collect_account("anything", principal="wondrue-operator")
    with pytest.raises(DcadConfigError, match="8-24 digits"):
        dcad_account_url("https://evil.example/x")


def test_collect_success_is_single_bounded_call(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    body = _body().encode("utf-8")
    calls = _patch_stream(monkeypatch, _FakeResponse(chunks=[body]))
    collected = collect_account(ACCOUNT, principal="wondrue-operator")
    assert calls["count"] == 1
    assert calls["method"] == "GET"
    assert calls["url"] == (
        "https://www.dallascad.org/AcctDetailCom.aspx?ID=00000700090000000"
    )
    assert calls["kwargs"]["timeout"] == 12.0
    assert calls["kwargs"]["follow_redirects"] is False
    assert calls["kwargs"]["trust_env"] is False  # explicit: no proxy
    assert "verify" not in calls["kwargs"]  # TLS validation stays enabled
    assert collected.collection_provenance == "adapter_collected"
    assert collected.body_sha256 == hashlib.sha256(body).hexdigest()
    assert collected.body_text == _body()
    assert collected.principal == "wondrue-operator"


def test_collect_redirect_refused(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(status=302))
    with pytest.raises(DcadFetchError, match="redirect"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_bad_status_refused(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(status=500))
    with pytest.raises(DcadFetchError, match="unexpected status"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_content_type_refused(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(content_type="application/json"))
    with pytest.raises(DcadFetchError, match="content type"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_oversize_refused(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    big = b"x" * (dcad_module.DCAD_MAX_BODY_BYTES + 1)
    _patch_stream(monkeypatch, _FakeResponse(chunks=[big]))
    with pytest.raises(DcadFetchError, match="too large"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_timeout_is_single_attempt(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    import httpx

    calls = {"count": 0}

    def _timeout(*args: Any, **kwargs: Any) -> None:
        calls["count"] += 1
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "stream", _timeout)
    with pytest.raises(DcadFetchError, match="timed out"):
        collect_account(ACCOUNT, principal="wondrue-operator")
    assert calls["count"] == 1  # no automatic retry


def test_collect_timeout_bound_enforced(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    with pytest.raises(DcadConfigError, match="timeout"):
        collect_account(ACCOUNT, principal="wondrue-operator", timeout_seconds=99.0)
    with pytest.raises(DcadConfigError, match="timeout"):
        collect_account(ACCOUNT, principal="wondrue-operator", timeout_seconds=0)


def test_collect_private_destination_refused(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(chunks=[b"<html></html>"]))
    _patch_dns(monkeypatch, ip="10.1.2.3")
    with pytest.raises(DcadFetchError, match="not a public address"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_multicast_destination_refused(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    _patch_stream(monkeypatch, _FakeResponse(chunks=[b"<html></html>"]))
    _patch_dns(monkeypatch, ip="224.0.0.1")
    with pytest.raises(DcadFetchError, match="not a public address"):
        collect_account(ACCOUNT, principal="wondrue-operator")


def test_collect_unresolvable_destination_refused(monkeypatch: pytest.MonkeyPatch, _dcad_env: None) -> None:
    import socket

    _patch_stream(monkeypatch, _FakeResponse(chunks=[b"<html></html>"]))

    def _fail(host: object, port: object, *args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(DcadFetchError, match="does not resolve"):
        collect_account(ACCOUNT, principal="wondrue-operator")


# --- Admission ----------------------------------------------------------------


def _assess(collection: dict[str, Any], street: str = "2323 Chalk Hill Rd") -> dict[str, Any]:
    return build_dcad_assessment(
        collection=collection,
        collection_digest="c" * 64,
        property_record={"id": "ada28dfd-6cda-47e1-8cdb-0b1ad0cc65ed", "street_address": street},
        dossier_key="addr:2323 chalk hill rd|dallas|tx|75212",
        claim_scope="warehouse_industrial",
    )


def test_admission_supported_alignment_never_verifies(_dcad_env: None) -> None:
    assessment = _assess(_collection())
    assert assessment["alignment"]["aligned"] is True
    assert "exact normalized street-address agreement" in assessment["alignment"]["basis"]
    assert "never" in assessment["alignment"]["scope"]
    verdict = assessment["dcad_only_verification"]
    assert verdict["outcome"] == "not_verified"
    assert verdict["reasons"]  # explicit insufficient-evidence reasons
    assert "office" in assessment["scope_note"] and "warehouse" in assessment["scope_note"]
    assert assessment["address"]["source_raw"] == "2323 CHALK HILL RD"
    assert "city" not in assessment and "zip_code" not in assessment
    assert "use_status" not in json.dumps(assessment)


def test_admission_address_mismatch_stays_unaligned(_dcad_env: None) -> None:
    assessment = _assess(_collection(), street="999 Other St")
    assert assessment["alignment"]["aligned"] is False
    assert "does not agree" in assessment["alignment"]["basis"]
    assert assessment["dcad_only_verification"]["outcome"] == "not_verified"


def test_admission_forged_provenance_refused(_dcad_env: None) -> None:
    with pytest.raises(DcadParseError, match="provenance"):
        _assess(_collection(collection_provenance="caller_submitted"))


def test_admission_tampered_bytes_refused(_dcad_env: None) -> None:
    forged = _collection()
    forged["body_text"] = _body().replace("2323 CHALK HILL RD", "9999 FORGED RD")
    with pytest.raises(DcadParseError, match="digest"):
        _assess(forged)


def test_admission_url_mismatch_refused(_dcad_env: None) -> None:
    with pytest.raises(DcadParseError, match="URL"):
        _assess(_collection(url="https://www.dallascad.org/AcctDetailCom.aspx?ID=1"))


def test_admission_wrong_revision_refused(_dcad_env: None) -> None:
    with pytest.raises(DcadParseError, match="revision"):
        _assess(_collection(collector_revision="dcad-99"))


def test_admission_config_rechecked(_dcad_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", "")
    with pytest.raises(DcadConfigError, match="allowlisted"):
        _assess(_collection())


def test_admission_missing_principal_refused(_dcad_env: None) -> None:
    with pytest.raises(DcadParseError, match="principal"):
        _assess(_collection(principal=" "))


# --- Fake PostgreSQL backend ------------------------------------------------


class _FakeDB:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.runs_by_key: dict[str, str] = {}
        self.records: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        self.versions: dict[tuple[str, int], dict[str, Any]] = {}
        self.snapshots: dict[tuple[str, int], dict[str, Any]] = {}
        self.receipts: list[dict[str, Any]] = []
        self.pilot_cells: set[tuple[str, str]] = set()
        self.pilot_events: set[tuple[str, str]] = set()
        self.statements: list[str] = []


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
        p = {key: (str(value) if not isinstance(value, (dict, list)) else value) for key, value in dict(params or {}).items()}
        raw_params = dict(params or {})
        db = self._db
        db.statements.append(sql.split("(")[0].strip()[:60])
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            self._rows = [{"locked": 1}]
            return
        if sql.startswith("INSERT INTO workflow_runs"):
            if p["key"] in db.runs_by_key:
                self._rows = []
                return
            row = {
                "run_id": raw_params["run_id"],
                "idempotency_key": p["key"],
                "stage": p["stage"],
                "status": "open",
                "correlation_id": p["correlation"],
                "limits": raw_params["limits"],
                "usage": "{}",
                "errors": "[]",
            }
            db.runs[str(raw_params["run_id"])] = row
            db.runs_by_key[p["key"]] = str(raw_params["run_id"])
            self._rows = [row]
            return
        if sql.startswith("SELECT * FROM workflow_runs WHERE idempotency_key"):
            run_id = db.runs_by_key.get(p["key"])
            self._rows = [db.runs[run_id]] if run_id else []
            return
        if sql.startswith("SELECT * FROM workflow_runs WHERE run_id"):
            row = db.runs.get(p["rid"])
            self._rows = [row] if row else []
            return
        if sql.startswith("UPDATE workflow_runs SET stage"):
            row = db.runs.get(p["rid"])
            if row is not None and row["stage"] == p["expected"]:
                row["stage"] = p["next"]
                row["status"] = p["status"]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if sql.startswith("UPDATE workflow_runs SET status"):
            row = db.runs.get(p["rid"])
            if row is not None:
                row["status"] = p["status"]
                self.rowcount = 1
            return
        if sql.startswith("INSERT INTO workflow_records"):
            key = (p["run"], p["kind"], p["key"], int(raw_params["version"]))
            if key in db.records:
                self._rows = []
                return
            row = {
                "record_id": raw_params["rid"],
                "run_id": raw_params["run"],
                "kind": p["kind"],
                "record_key": p["key"],
                "version": int(raw_params["version"]),
                "payload": raw_params["payload"],
                "digest": p["digest"],
            }
            db.records[key] = row
            self._rows = [row]
            return
        if sql.startswith("SELECT * FROM workflow_records WHERE run_id"):
            run, kind = p.get("run"), p.get("kind")
            matches = [
                row for (r, k, _key, _v), row in db.records.items() if r == run and (kind is None or k == kind)
            ]
            if "key" in p:
                matches = [row for row in matches if str(row["record_key"]) == p["key"]]
            if "version" in p:
                matches = [row for row in matches if int(row["version"]) == int(p["version"])]
            else:
                matches.sort(key=lambda row: int(row["version"]), reverse=True)
                matches = matches[:1] if "key" in p else matches
            if kind is None:
                matches.sort(key=lambda row: (row["kind"], str(row["record_key"]), int(row["version"])))
            self._rows = matches
            return
        if sql.startswith("SELECT COALESCE(MAX(version)"):
            versions = [version for (dossier, version) in db.versions if dossier == p["dossier"]]
            self._rows = [{"max_version": max(versions) if versions else 0}]
            return
        if sql.startswith("INSERT INTO dossier_versions"):
            row = {
                "dossier_id": raw_params["dossier"],
                "version": int(raw_params["version"]),
                "property_id": raw_params["property"],
                "payload": raw_params["payload"],
                "digest": p["digest"],
                "supersedes": raw_params.get("supersedes"),
                "amendment": raw_params.get("amendment"),
                "created_by": p["created_by"],
            }
            db.versions[(p["dossier"], int(raw_params["version"]))] = row
            self._rows = [row]
            return
        if sql.startswith("SELECT * FROM dossier_versions WHERE dossier_id"):
            if "AND version" in sql:
                row = db.versions.get((p["dossier"], int(p["version"])))
                self._rows = [row] if row else []
            else:
                matches = [row for (dossier, _v), row in db.versions.items() if dossier == p["dossier"]]
                matches.sort(key=lambda row: int(row["version"]), reverse=True)
                self._rows = matches[:1]
            return
        if sql.startswith("INSERT INTO custody_snapshots"):
            key = (p["dossier"], int(raw_params["version"]))
            if key in db.snapshots:
                self._rows = []
                return
            row = {
                "snapshot_id": raw_params["sid"],
                "dossier_id": raw_params["dossier"],
                "version": int(raw_params["version"]),
                "payload": raw_params["payload"],
                "digest": p["digest"],
            }
            db.snapshots[key] = row
            self._rows = [row]
            return
        if sql.startswith("SELECT * FROM custody_snapshots WHERE dossier_id"):
            row = db.snapshots.get((p["dossier"], int(p["version"])))
            self._rows = [row] if row else []
            return
        if sql.startswith("SELECT * FROM custody_snapshots WHERE snapshot_id"):
            found = [row for row in db.snapshots.values() if str(row["snapshot_id"]) == p["sid"]]
            self._rows = found[:1]
            return
        if sql.startswith("SELECT * FROM custody_snapshots_archive"):
            self._rows = []
            return
        if sql.startswith("SELECT * FROM custody_receipts WHERE dossier_id"):
            self._rows = [row for row in db.receipts if str(row["dossier_id"]) == p["dossier"]]
            return
        if sql.startswith("INSERT INTO custody_receipts"):
            row = {
                "receipt_id": raw_params["rid"],
                "dossier_id": raw_params["dossier"],
                "version": int(raw_params["version"]),
                "snapshot_id": raw_params["snapshot"],
                "kind": p["kind"],
                "from_agent": p["from"],
                "to_agent": p["to"],
                "payload": raw_params["payload"],
                "mac": p["mac"],
                "prev_receipt": raw_params.get("prev"),
                "pipeline_version": p["pipeline"],
            }
            db.receipts.append(row)
            self._rows = [row]
            return
        if sql.startswith("SELECT county FROM pilot_cells"):
            self._rows = [{"county": p["county"]}] if (p["county"], p["category"]) in db.pilot_cells else []
            return
        if sql.startswith("INSERT INTO pilot_events"):
            key = (p["dossier"], p["transition"])
            if key in db.pilot_events:
                self._rows = []
                return
            db.pilot_events.add(key)
            self._rows = [{"event_id": raw_params["eid"]}]
            return
        if sql.startswith("UPDATE pilot_cells SET"):
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL in DCAD fixture: {sql[:80]}")

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> Any:
        return list(self._rows)


class _FakeConn:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


PROP_ID = "ada28dfd-6cda-47e1-8cdb-0b1ad0cc65ed"
SNIPPET = "2323 Chalk Hill Rd, Dallas, TX 75212 available office space."


def _seed_parent(db: _FakeDB) -> dict[str, Any]:
    """Seed a parent run + dossier + custody v1 + receipts via real code."""
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.extractor import extract_research_intelligence
    from app.agents.gabriel.producers import build_candidate_verifications
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult
    from app.custody import dossier_id_for_property
    from app.custody_db import (
        create_version_in_transaction,
        issue_receipt_in_transaction,
        snapshot_in_transaction,
    )
    from app.workflow import KIND_DOSSIER_VERSION, KIND_RESEARCH_ARTIFACT, digest_payload
    from app.workflow_db import (
        create_run_in_transaction,
        put_record_in_transaction,
    )

    conn = _FakeConn(db)
    parent_run = create_run_in_transaction(conn, idempotency_key="dcad-test-parent")
    result = BraveSearchResult(
        assignment_id="a1",
        query="Dallas commercial listings",
        title="Listings",
        url="https://listings.example.com/1",
        snippet=SNIPPET,
        domain="listings.example.com",
        retrieved_at="2026-10-30T00:00:00+00:00",
        rank=1,
    )
    artifact = {
        "provider": "brave",
        "assignment_id": "a1",
        "query": "Dallas commercial listings",
        "results": [
            {
                "url": result.url,
                "title": result.title,
                "snippet": result.snippet,
                "domain": result.domain,
                "published_at": None,
                "retrieved_at": result.retrieved_at,
                "rank": 1,
            }
        ],
        "retrieved_at": "2026-10-30T00:00:00+00:00",
        "collection_provenance": "adapter_collected",
        "attempt": "completed",
        "error": "",
    }
    stored_artifact = put_record_in_transaction(
        conn, parent_run.run_id, kind=KIND_RESEARCH_ARTIFACT, record_key="a1",
        version=1, payload=artifact, digest=digest_payload(artifact),
    )
    intel = extract_research_intelligence([result])
    ranking = rank_intelligence(intel)
    assert len(ranking.candidates) == 1
    key = ranking.candidates[0].key
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    assert dossier_set.dossiers[0].key == key
    verdicts = build_candidate_verifications(ranking, intel)
    dossier = dossier_set.dossiers[0].model_dump(mode="json")
    dossier["county_attribution"] = {"evidence_status": "verified", "value": "Dallas County"}
    payload = {
        "version": 1,
        "builder_version": "test",
        "artifact_refs": ["a1"],
        "dossier": dossier,
        "candidate_verification": verdicts[key].model_dump(mode="json"),
        "property_id": PROP_ID,
    }
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    stored_dossier = put_record_in_transaction(
        conn, parent_run.run_id, kind=KIND_DOSSIER_VERSION, record_key=key,
        version=1, payload=payload, digest=str(payload["digest"]),
    )
    version = create_version_in_transaction(
        conn, property_id=PROP_ID,
        payload={
            "dossier": dossier,
            "dossier_digest": stored_dossier.digest,
            "run_id": str(parent_run.run_id),
            "dossier_key": key,
        },
        created_by="gabriel",
    )
    snapshot_in_transaction(conn, dossier_id=version.dossier_id, version=version.version)
    issue_receipt_in_transaction(
        conn, dossier_id=version.dossier_id, version=version.version,
        kind="forward", from_agent="gabriel", to_agent="raphael",
        issued_by="test", note="assess",
    )
    issue_receipt_in_transaction(
        conn, dossier_id=version.dossier_id, version=version.version,
        kind="return", from_agent="raphael", to_agent="gabriel",
        issued_by="test", note="needs evidence",
    )
    db.pilot_cells.add(("dallas", "commercial_property"))
    return {
        "run_id": parent_run.run_id,
        "dossier_key": key,
        "dossier_digest": stored_dossier.digest,
        "artifact_digest": stored_artifact.digest,
        "dossier_id": version.dossier_id,
    }


@pytest.fixture()
def _custody_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "dcad-test-custody-key-0123456789")
    monkeypatch.setenv("CUSTODY_AMENDERS", "wondrue-operator")
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", ACCOUNT)


def _seed_collection(
    db: _FakeDB, parent: dict[str, Any], *, body: str | None = None, suffix: str = ""
) -> dict[str, Any]:
    from app.workflow import (
        KIND_DCAD_ASSESSMENT,
        KIND_DCAD_COLLECTION,
        digest_payload,
    )
    from app.workflow_db import create_run_in_transaction, put_record_in_transaction

    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=f"dcad-test-collection{suffix}")
    collection = _collection() if body is None else _collection(body=body)
    stored_collection = put_record_in_transaction(
        conn, run.run_id, kind=KIND_DCAD_COLLECTION,
        record_key=f"dcad:{ACCOUNT}", version=1,
        payload=collection, digest=digest_payload(collection),
    )
    assessment = build_dcad_assessment(
        collection=collection,
        collection_digest=str(stored_collection.digest or ""),
        property_record={"id": PROP_ID, "street_address": "2323 Chalk Hill Rd"},
        dossier_key=parent["dossier_key"],
        claim_scope="commercial_property",
    )
    stored_assessment = put_record_in_transaction(
        conn, run.run_id, kind=KIND_DCAD_ASSESSMENT,
        record_key=f"dcad:{ACCOUNT}", version=1,
        payload=assessment, digest=digest_payload(assessment),
    )
    return {
        "run_id": run.run_id,
        "collection_digest": stored_collection.digest,
        "assessment_digest": stored_assessment.digest,
    }


def _patch_owned(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> None:
    monkeypatch.setattr(
        "app.properties_db.get_connection", lambda config=None: _FakeConn(db)
    )
    # workflow_db bound the same factory at import time: patch both.
    monkeypatch.setattr(
        "app.workflow_db.get_connection", lambda config=None: _FakeConn(db)
    )
    monkeypatch.setattr(
        "app.properties_db.get_property",
        lambda pid, config=None: {
            "id": str(pid),
            "street_address": "2323 Chalk Hill Rd",
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75212",
        },
    )

    def _shim(*, operation_class: Any, event: Any, mutate: Any, config: Any = None) -> Any:
        return mutate(_FakeConn(db))

    monkeypatch.setattr(
        "app.observability.transactions.run_consequential_transaction", _shim
    )


# --- Producer extension -------------------------------------------------------


def test_producer_appends_admitted_voice() -> None:
    from app.agents.gabriel.extractor import extract_research_intelligence
    from app.agents.gabriel.producers import build_candidate_verification
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult
    from app.agents.gabriel.sources.dcad import build_dcad_voice
    from app.agents.gabriel.verification import IdentityAttestation

    result = BraveSearchResult(
        assignment_id="a1", query="q", title="t",
        url="https://listings.example.com/1", snippet=SNIPPET,
        domain="listings.example.com",
        retrieved_at="2026-10-30T00:00:00+00:00", rank=1,
    )
    intel = extract_research_intelligence([result])
    ranking = rank_intelligence(intel)
    candidate = ranking.candidates[0]
    verdict = build_candidate_verification(
        candidate, [0], intel.results, basis="test",
        extra_voices=[build_dcad_voice(dcad_account_url(ACCOUNT))],
        attestation=IdentityAttestation(group_key=candidate.key, aligned=True, note="test"),
    )
    assert verdict.outcome.value == "not_verified"
    assert len(verdict.voices) == 2
    assert verdict.voices[1].host == "www.dallascad.org"


# --- Successor service --------------------------------------------------------


def test_successor_commit_needs_evidence(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.pilot import canonical_quota_id

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    quota = canonical_quota_id(PROP_ID)
    for transition in ("discovered", "qualified", "assessed", "incomplete"):
        db.pilot_events.add((str(quota), transition))
    import copy

    parent_records_before = copy.deepcopy(
        {k: v for k, v in db.records.items() if k[0] == str(parent["run_id"])}
    )
    parent_version_before = copy.deepcopy(
        db.versions[(str(parent["dossier_id"]), 1)]
    )
    receipts_before = copy.deepcopy(db.receipts)
    versions_before = len(db.versions)
    events_before = set(db.pilot_events)
    _patch_owned(monkeypatch, db)
    summary = mvp_module.commit_successor_audited(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    assert summary["replayed"] is False
    assert summary["decision"] == "needs_evidence"
    assert summary["custody_version"] == 2
    assert summary["receipts"]["forward:gabriel->raphael"]
    assert summary["receipts"]["return:raphael->gabriel"]
    assert len(db.versions) == versions_before + 1
    assert len(db.receipts) == len(receipts_before) + 2
    assert db.pilot_events == events_before  # counted once: transitions replay
    assert {k: v for k, v in db.records.items() if k[0] == str(parent["run_id"])} == parent_records_before
    assert db.versions[(str(parent["dossier_id"]), 1)] == parent_version_before
    assert db.receipts[: len(receipts_before)] == receipts_before
    successor = db.records[(str(summary["run_id"]), "dossier_version", parent["dossier_key"], 1)]
    block = successor["payload"]["successor"] if isinstance(successor["payload"], dict) else json.loads(successor["payload"])["successor"]
    assert block["parent_digest"] == parent["dossier_digest"]
    assert block["dcad_assessment"]["digest"] == collection["assessment_digest"]
    assert block["processing_revision"] == "successor-1"
    assert block["actor"] == "wondrue-operator"


def test_successor_idempotent_retry_replays(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    db.pilot_cells.add(("dallas", "commercial_property"))
    _patch_owned(monkeypatch, db)
    args = dict(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    first = mvp_module.commit_successor_audited(**args)
    versions = len(db.versions)
    receipts = len(db.receipts)
    records = len(db.records)
    events = set(db.pilot_events)
    second = mvp_module.commit_successor_audited(**args)
    assert second["replayed"] is True
    assert second["dossier_digest"] == first["dossier_digest"]
    assert second["decision"] == first["decision"]
    assert len(db.versions) == versions
    assert len(db.receipts) == receipts
    assert len(db.records) == records
    assert set(db.pilot_events) == events


def test_successor_replay_validates_bindings(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    """A replay with mismatched operation bindings is refused, not served."""
    import uuid as uuid_module

    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)
    args = dict(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    first = mvp_module.commit_successor_audited(**args)
    assert first["replayed"] is False
    # Same key-forming evidence bindings, but a foreign property: the run
    # key matches, so only explicit replay validation can refuse this.
    foreign = dict(args, property_id=str(uuid_module.uuid4()))
    with pytest.raises(WorkflowError, match="replay binding mismatch"):
        mvp_module.commit_successor_audited(**foreign)
    # The identical retry still replays the committed result.
    second = mvp_module.commit_successor_audited(**args)
    assert second["replayed"] is True
    assert second["dossier_digest"] == first["dossier_digest"]


def test_successor_replay_requires_authority(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    """An unenrolled principal cannot retrieve a commit through replay."""
    from app import mvp as mvp_module
    from app.custody import CustodyAuthError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)
    args = dict(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    first = mvp_module.commit_successor_audited(**args)
    assert first["replayed"] is False
    # Enrollment revoked after the commit: the identical retry must be
    # refused before any replay output, not served from stored records.
    monkeypatch.setenv("CUSTODY_AMENDERS", "")
    with pytest.raises(CustodyAuthError, match="enrolled authorizer"):
        mvp_module.commit_successor_audited(**args)


def test_successor_replay_rejects_foreign_principal(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    """Replay binds the principal: another enrolled caller is refused."""
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    monkeypatch.setenv("CUSTODY_AMENDERS", "wondrue-operator,second-operator")
    _patch_owned(monkeypatch, db)
    args = dict(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    first = mvp_module.commit_successor_audited(**args)
    assert first["replayed"] is False
    foreign = dict(args, principal="second-operator")
    with pytest.raises(WorkflowError, match="replay binding mismatch"):
        mvp_module.commit_successor_audited(**foreign)
    second = mvp_module.commit_successor_audited(**args)
    assert second["replayed"] is True
    assert second["dossier_digest"] == first["dossier_digest"]


def test_successor_chain_moved_conflicts(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowConflictError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)
    args = dict(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    mvp_module.commit_successor_audited(**args)
    # New evidence under a stale chain expectation fails safely (new key,
    # moved chain): no duplicate version, no fork.
    variant = _body() + "\n<!-- retrieval variant -->\n"
    second = _seed_collection(db, parent, body=variant, suffix="-v2")
    assert second["assessment_digest"] != collection["assessment_digest"]
    versions = len(db.versions)
    with pytest.raises(WorkflowConflictError, match="moved since approval"):
        mvp_module.commit_successor_audited(
            **{**args, "expected_custody_version": 1,
               "collection_run_id": str(second["run_id"]),
               "collection_digest": str(second["collection_digest"]),
               "assessment_digest": str(second["assessment_digest"])}
        )
    assert len(db.versions) == versions


def test_successor_holder_not_gabriel_refused(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.custody_db import issue_receipt_in_transaction
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    conn = _FakeConn(db)
    issue_receipt_in_transaction(
        conn, dossier_id=parent["dossier_id"], version=1,
        kind="forward", from_agent="gabriel", to_agent="raphael",
        issued_by="test", note="holder moves on",
    )
    versions = len(db.versions)
    receipts = len(db.receipts)
    records = len(db.records)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="held by raphael"):
        mvp_module.commit_successor_audited(
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            parent_version=1,
            parent_digest=str(parent["dossier_digest"]),
            expected_custody_version=1,
            collection_run_id=str(collection["run_id"]),
            collection_digest=str(collection["collection_digest"]),
            assessment_digest=str(collection["assessment_digest"]),
            reason="assess admitted DCAD evidence",
            principal="wondrue-operator",
            approved=True,
        )
    assert len(db.versions) == versions
    assert len(db.receipts) == receipts
    assert len(db.records) == records


def test_successor_digest_mismatch_refused(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    versions = len(db.versions)
    receipts = len(db.receipts)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="does not match the approval"):
        mvp_module.commit_successor_audited(
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            parent_version=1,
            parent_digest="0" * 64,
            expected_custody_version=1,
            collection_run_id=str(collection["run_id"]),
            collection_digest=str(collection["collection_digest"]),
            assessment_digest=str(collection["assessment_digest"]),
            reason="assess admitted DCAD evidence",
            principal="wondrue-operator",
            approved=True,
        )
    assert len(db.versions) == versions
    assert len(db.receipts) == receipts


def test_successor_unenrolled_authorizer_refused(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.custody import CustodyAuthError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    monkeypatch.setenv("CUSTODY_AMENDERS", "")
    _patch_owned(monkeypatch, db)
    with pytest.raises(CustodyAuthError, match="enrolled authorizer"):
        mvp_module.commit_successor_audited(
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            parent_version=1,
            parent_digest=str(parent["dossier_digest"]),
            expected_custody_version=1,
            collection_run_id=str(collection["run_id"]),
            collection_digest=str(collection["collection_digest"]),
            assessment_digest=str(collection["assessment_digest"]),
            reason="assess admitted DCAD evidence",
            principal="wondrue-operator",
            approved=True,
        )


def test_amendment_attributes_authenticated_principal(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    """No caller name can impersonate an enrolled amender (no such arg)."""
    import json as json_module

    from app import mvp as mvp_module

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)
    summary = mvp_module.commit_successor_audited(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    version = db.versions[(str(parent["dossier_id"]), summary["custody_version"])]
    amendment = version["amendment"]
    if isinstance(amendment, str):
        amendment = json_module.loads(amendment)
    assert amendment["authorized_by"] == "wondrue-operator"
    # A differently-enrolled handle does not authorize this principal:
    # fresh evidence (new key) reaches the permission check and refuses.
    variant = _body() + "\n<!-- retrieval variant -->\n"
    second = _seed_collection(db, parent, body=variant, suffix="-v2")
    monkeypatch.setenv("CUSTODY_AMENDERS", "owner")

    from app.custody import CustodyAuthError

    with pytest.raises(CustodyAuthError, match="enrolled authorizer"):
        mvp_module.commit_successor_audited(
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            parent_version=1,
            parent_digest=str(parent["dossier_digest"]),
            expected_custody_version=2,
            collection_run_id=str(second["run_id"]),
            collection_digest=str(second["collection_digest"]),
            assessment_digest=str(second["assessment_digest"]),
            reason="assess admitted DCAD evidence",
            principal="wondrue-operator",
            approved=True,
        )


def test_successor_requires_approval_and_enabled(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)
    args = dict(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=str(parent["dossier_digest"]),
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
    )
    with pytest.raises(WorkflowError, match="explicit approval"):
        mvp_module.commit_successor_audited(**args, approved=False)
    monkeypatch.delenv("MVP_WORKFLOW_ENABLED", raising=False)
    with pytest.raises(WorkflowError, match="disabled"):
        mvp_module.commit_successor_audited(**args, approved=True)


# --- Collect service ------------------------------------------------------------


def test_collect_transmit_then_persist(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    parent = _seed_parent(db)
    body = _body().encode("utf-8")
    _patch_stream(monkeypatch, _FakeResponse(chunks=[body]))
    _patch_owned(monkeypatch, db)
    summary = mvp_module.collect_dcad_audited(
        account=ACCOUNT,
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        principal="wondrue-operator",
        approved=True,
    )
    assert summary["replayed"] is False
    assert summary["aligned"] is True
    assert summary["dcad_only_outcome"] == "not_verified"
    assert len(summary["collection_digest"]) == 64
    assert len(summary["assessment_digest"]) == 64


def test_collect_replays_without_retransmit(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    parent = _seed_parent(db)
    body = _body().encode("utf-8")
    _patch_stream(monkeypatch, _FakeResponse(chunks=[body]))
    _patch_owned(monkeypatch, db)
    first = mvp_module.collect_dcad_audited(
        account=ACCOUNT,
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        principal="wondrue-operator",
        approved=True,
    )
    assert first["replayed"] is False

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network must not be touched on replay")

    monkeypatch.setattr(httpx, "stream", _boom)
    second = mvp_module.collect_dcad_audited(
        account=ACCOUNT,
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        principal="wondrue-operator",
        approved=True,
    )
    assert second["replayed"] is True
    assert second["collection_digest"] == first["collection_digest"]
    assert second["assessment_digest"] == first["assessment_digest"]


def test_collect_unapproved_refused(monkeypatch: pytest.MonkeyPatch, _custody_env: None) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="explicit approval"):
        mvp_module.collect_dcad_audited(
            account=ACCOUNT,
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            principal="wondrue-operator",
            approved=False,
        )


# --- Wondrue boundaries ---------------------------------------------------------


def test_previews_do_no_writes_or_network(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import wondrue as wondrue_module

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network must not be touched by previews")

    monkeypatch.setattr(httpx, "stream", _boom)
    conn = _FakeConn(db)
    writes_before = [
        statement
        for statement in db.statements
        if statement.startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    preview = wondrue_module.handle_command(
        mode="read-only",
        max_mode="read-only",
        text="",
        target="real",
        intent_override={
            "intent": "preview_dcad_collection",
            "args": {
                "account": ACCOUNT,
                "property_id": PROP_ID,
                "parent_run_id": str(parent["run_id"]),
                "dossier_key": parent["dossier_key"],
            },
        },
        conn=conn,
        principal="wondrue-reader",
    )
    assert preview["outcome"] == "result"
    assert preview["data"]["collection_preview"]["writes"] == "none in preview"
    successor = wondrue_module.handle_command(
        mode="read-only",
        max_mode="read-only",
        text="",
        target="real",
        intent_override={
            "intent": "preview_successor",
            "args": {
                "property_id": PROP_ID,
                "parent_run_id": str(parent["run_id"]),
                "dossier_key": parent["dossier_key"],
                "collection_run_id": str(collection["run_id"]),
            },
        },
        conn=conn,
        principal="wondrue-reader",
    )
    assert successor["outcome"] == "result"
    assert successor["data"]["successor_preview"]["ready"] is True
    writes_after = [
        statement
        for statement in db.statements
        if statement.startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes_after == writes_before


def test_consequential_intents_need_operator_grant(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import wondrue as wondrue_module

    db = _FakeDB()
    parent = _seed_parent(db)
    _patch_owned(monkeypatch, db)
    conn = _FakeConn(db)
    with pytest.raises(Exception, match="blocked in read-only mode"):
        wondrue_module.handle_command(
            mode="read-only",
            max_mode="read-only",
            text="",
            target="real",
            intent_override={"intent": "collect_dcad_evidence", "args": {}},
            conn=conn,
            principal="wondrue-reader",
        )
    preview = wondrue_module.handle_command(
        mode="ask",
        max_mode="ask",
        text="",
        target="real",
        intent_override={
            "intent": "collect_dcad_evidence",
            "args": {
                "account": ACCOUNT,
                "property_id": PROP_ID,
                "parent_run_id": str(parent["run_id"]),
                "dossier_key": parent["dossier_key"],
            },
        },
        conn=conn,
        principal="wondrue-reader",
    )
    assert preview["outcome"] == "preview"
    with pytest.raises(Exception, match="operator grant"):
        wondrue_module.handle_command(
            mode="ask",
            max_mode="ask",
            text="",
            target="real",
            approval_token=preview["approval_token"],
            intent_override={
                "intent": "collect_dcad_evidence",
                "args": {
                    "account": ACCOUNT,
                    "property_id": PROP_ID,
                    "parent_run_id": str(parent["run_id"]),
                    "dossier_key": parent["dossier_key"],
                },
            },
            conn=conn,
            principal="wondrue-reader",
        )


def test_ask_token_binds_exact_args(monkeypatch: pytest.MonkeyPatch, _custody_env: None) -> None:
    from app import wondrue as wondrue_module

    db = _FakeDB()
    parent = _seed_parent(db)
    _patch_owned(monkeypatch, db)
    conn = _FakeConn(db)
    seen: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("app.mvp.collect_dcad_audited", _capture)
    preview = wondrue_module.handle_command(
        mode="ask",
        max_mode="full",
        text="",
        target="real",
        intent_override={
            "intent": "collect_dcad_evidence",
            "args": {
                "account": ACCOUNT,
                "property_id": PROP_ID,
                "parent_run_id": str(parent["run_id"]),
                "dossier_key": parent["dossier_key"],
                "attempt": "2",
            },
        },
        conn=conn,
        principal="wondrue-operator",
    )
    wondrue_module.handle_command(
        mode="ask",
        max_mode="full",
        text="",
        target="real",
        approval_token=preview["approval_token"],
        intent_override={
            "intent": "collect_dcad_evidence",
            "args": {
                "account": "00000700090000009",
                "property_id": PROP_ID,
                "parent_run_id": str(parent["run_id"]),
                "dossier_key": parent["dossier_key"],
                "attempt": "9",
            },
        },
        conn=conn,
        principal="wondrue-operator",
    )
    assert seen["account"] == ACCOUNT  # approved args win, not the second request
    assert seen["attempt"] == "2"
    assert seen["approved"] is True


def test_consequential_previews_disclose_bounds() -> None:
    from app.wondrue import preview_intent

    collect = preview_intent(
        "collect_dcad_evidence",
        {"account": ACCOUNT, "property_id": PROP_ID},
        "real",
    )
    assert collect["cost_bounds"] == {"brave_attempts": 0, "meta_calls": 0, "dcad_requests": 1}
    commit = preview_intent("commit_successor", {"property_id": PROP_ID}, "real")
    assert commit["cost_bounds"]["meta_calls"] == 0
    assert "authorized_by" not in commit["inputs"]  # principal-resolved, not caller-named
    assert "reason" in commit["inputs"]


# --- Claim, retention, refusal, content, integrity, alignment --------------------


def _collect_key_for(parent: dict[str, Any], attempt: str = "1") -> str:
    from app import mvp as mvp_module

    return mvp_module._dcad_run_key(
        account=ACCOUNT,
        revision="dcad-1",
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        attempt=attempt,
    )


def _insert_claim_row(db: _FakeDB, run_id: Any) -> None:
    from app import mvp as mvp_module

    mvp_module._dcad_insert_claim(
        _FakeConn(db), run_id, ACCOUNT, "wondrue-operator", "dcad-1"
    )


def test_collect_uncertain_claim_refuses_without_transmit(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError, create_run_in_transaction

    db = _FakeDB()
    parent = _seed_parent(db)
    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=_collect_key_for(parent))
    _insert_claim_row(db, run.run_id)  # started, nothing retained: uncertain
    _patch_owned(monkeypatch, db)

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("uncertain attempt must not retransmit")

    monkeypatch.setattr(httpx, "stream", _boom)
    with pytest.raises(WorkflowError, match="uncertain"):
        mvp_module.collect_dcad_audited(
            account=ACCOUNT,
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            principal="wondrue-operator",
            approved=True,
        )


def test_collect_attempt_recovers_after_uncertain(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError, create_run_in_transaction

    db = _FakeDB()
    parent = _seed_parent(db)
    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=_collect_key_for(parent, "1"))
    _insert_claim_row(db, run.run_id)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="uncertain"):
        mvp_module.collect_dcad_audited(
            account=ACCOUNT,
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            attempt="1",
            principal="wondrue-operator",
            approved=True,
        )
    body = _body().encode("utf-8")
    _patch_stream(monkeypatch, _FakeResponse(chunks=[body]))
    summary = mvp_module.collect_dcad_audited(
        account=ACCOUNT,
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        attempt="2",
        principal="wondrue-operator",
        approved=True,
    )
    assert summary["replayed"] is False
    assert summary["aligned"] is True


def test_collect_refusal_retains_bytes_and_reraises(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    bad = b"<html><body>no account structure here</body></html>"
    _patch_stream(monkeypatch, _FakeResponse(chunks=[bad]))
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="refused"):
        mvp_module.collect_dcad_audited(
            account=ACCOUNT,
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            principal="wondrue-operator",
            approved=True,
        )
    collect_keys = [
        key for key in db.runs_by_key if key not in ("dcad-test-parent", "dcad-test-collection")
    ]
    assert len(collect_keys) == 1
    run_uuid = db.runs[db.runs_by_key[collect_keys[0]]]["run_id"]
    collection = db.records[(str(run_uuid), "dcad_collection", f"dcad:{ACCOUNT}", 1)]
    payload = collection["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["body_text"] == bad.decode("utf-8")  # bytes retained
    refusal = db.records[(str(run_uuid), "dcad_refusal", f"dcad:{ACCOUNT}", 1)]
    refusal_payload = refusal["payload"]
    if isinstance(refusal_payload, str):
        refusal_payload = json.loads(refusal_payload)
    assert refusal_payload["stage"] == "assess"
    assert "account title" in refusal_payload["reason"]

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("stored refusal must not re-retrieve")

    monkeypatch.setattr(httpx, "stream", _boom)
    with pytest.raises(WorkflowError, match="prior collection refused"):
        mvp_module.collect_dcad_audited(
            account=ACCOUNT,
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            principal="wondrue-operator",
            approved=True,
        )


def test_collect_resumes_assessment_without_transmit(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow import digest_payload
    from app.workflow_db import create_run_in_transaction, put_record_in_transaction

    db = _FakeDB()
    parent = _seed_parent(db)
    conn = _FakeConn(db)
    run = create_run_in_transaction(conn, idempotency_key=_collect_key_for(parent))
    _insert_claim_row(db, run.run_id)
    collection = _collection()
    put_record_in_transaction(
        conn, run.run_id, kind="dcad_collection",
        record_key=f"dcad:{ACCOUNT}", version=1,
        payload=collection, digest=digest_payload(collection),
    )
    _patch_owned(monkeypatch, db)

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("resume must reuse retained bytes")

    monkeypatch.setattr(httpx, "stream", _boom)
    summary = mvp_module.collect_dcad_audited(
        account=ACCOUNT,
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        principal="wondrue-operator",
        approved=True,
    )
    assert summary["replayed"] is False
    assert summary["aligned"] is True
    assert summary["dcad_only_outcome"] == "not_verified"


def test_successor_persists_rebuilt_content_with_changes(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow import digest_payload

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    # Simulate stale parent content (non-key field): the snippet
    # evidence rebuilds commercial_property, the parent says warehouse.
    key = (str(parent["run_id"]), "dossier_version", parent["dossier_key"], 1)
    row = db.records[key]
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
    payload["dossier"]["city"] = "Stale City"
    payload.pop("digest", None)
    new_digest = digest_payload(payload)
    payload["digest"] = new_digest
    row["payload"] = json.dumps(payload, sort_keys=True)
    row["digest"] = new_digest
    _patch_owned(monkeypatch, db)
    summary = mvp_module.commit_successor_audited(
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        parent_version=1,
        parent_digest=new_digest,
        expected_custody_version=1,
        collection_run_id=str(collection["run_id"]),
        collection_digest=str(collection["collection_digest"]),
        assessment_digest=str(collection["assessment_digest"]),
        reason="assess admitted DCAD evidence",
        principal="wondrue-operator",
        approved=True,
    )
    successor = db.records[(str(summary["run_id"]), "dossier_version", parent["dossier_key"], 1)]
    stored = json.loads(successor["payload"]) if isinstance(successor["payload"], str) else successor["payload"]
    assert stored["dossier"]["city"] == "Dallas"
    fields = [change["field"] for change in stored["successor"]["content_changes"]]
    assert "city" in fields
    assert stored["successor"]["voice_alignment"][0]["aligned"] is False
    assert stored["successor"]["voice_alignment"][-1]["aligned"] is True


def test_successor_preview_shows_intended_outcomes(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)
    preview = mvp_module.preview_successor_in_transaction(
        _FakeConn(db),
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        collection_run_id=str(collection["run_id"]),
    )
    assert preview["ready"] is True
    assert preview["intended"]["qualification"]["outcome"] == "qualified"
    assert preview["intended"]["qualification"]["county_credit"] == ""
    assert preview["intended"]["decision"] == "needs_evidence"
    assert preview["intended"]["verification_outcome"] == "not_verified"
    fields = [change["field"] for change in preview["content_changes"]]
    assert "county_attribution.evidence_status" in fields  # disclosed downgrade
    assert preview["voice_alignment"][-1]["aligned"] is True
    assert preview["voice_alignment"][0]["aligned"] is False


def test_successor_tampered_parent_refused(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    key = (str(parent["run_id"]), "dossier_version", parent["dossier_key"], 1)
    row = db.records[key]
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
    payload["dossier"]["city"] = "Forged City"  # digest left stale
    row["payload"] = json.dumps(payload, sort_keys=True)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="digest verification"):
        mvp_module.preview_successor_in_transaction(
            _FakeConn(db),
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            collection_run_id=str(collection["run_id"]),
        )
    versions = len(db.versions)
    with pytest.raises(WorkflowError, match="digest verification"):
        mvp_module.commit_successor_audited(
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            parent_version=1,
            parent_digest=str(parent["dossier_digest"]),
            expected_custody_version=1,
            collection_run_id=str(collection["run_id"]),
            collection_digest=str(collection["collection_digest"]),
            assessment_digest=str(collection["assessment_digest"]),
            reason="assess admitted DCAD evidence",
            principal="wondrue-operator",
            approved=True,
        )
    assert len(db.versions) == versions


def test_successor_tampered_assessment_refused(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    key = (str(collection["run_id"]), "dcad_assessment", f"dcad:{ACCOUNT}", 1)
    row = db.records[key]
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
    payload["alignment"]["aligned"] = False  # seed aligns; flip with digest left stale
    row["payload"] = json.dumps(payload, sort_keys=True)
    _patch_owned(monkeypatch, db)
    with pytest.raises(WorkflowError, match="digest verification"):
        mvp_module.preview_successor_in_transaction(
            _FakeConn(db),
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            collection_run_id=str(collection["run_id"]),
        )


def test_successor_unsupported_revision_blocked(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    from app import mvp as mvp_module
    from app.workflow import digest_payload
    from app.workflow_db import WorkflowError

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    key = (str(collection["run_id"]), "dcad_assessment", f"dcad:{ACCOUNT}", 1)
    row = db.records[key]
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
    payload["assessment_version"] = "dcad-assess-0"
    row["payload"] = json.dumps(payload, sort_keys=True)
    row["digest"] = digest_payload(json.loads(row["payload"]))
    _patch_owned(monkeypatch, db)
    preview = mvp_module.preview_successor_in_transaction(
        _FakeConn(db),
        property_id=PROP_ID,
        parent_run_id=str(parent["run_id"]),
        dossier_key=parent["dossier_key"],
        collection_run_id=str(collection["run_id"]),
    )
    assert preview["ready"] is False
    assert any("supported revisions" in blocker for blocker in preview["blockers"])
    with pytest.raises(WorkflowError, match="supported revisions"):
        mvp_module.commit_successor_audited(
            property_id=PROP_ID,
            parent_run_id=str(parent["run_id"]),
            dossier_key=parent["dossier_key"],
            parent_version=1,
            parent_digest=str(parent["dossier_digest"]),
            expected_custody_version=1,
            collection_run_id=str(collection["run_id"]),
            collection_digest=str(collection["collection_digest"]),
            assessment_digest=str(row["digest"]),
            reason="assess admitted DCAD evidence",
            principal="wondrue-operator",
            approved=True,
        )


def test_cumulative_attestation_never_inherits() -> None:
    from app.agents.gabriel.sources.dcad import cumulative_attestation

    attestation, voices = cumulative_attestation(
        dossier_key="k",
        brave_voice_urls=["https://listings.example.com/1"],
        dcad_voice_url=dcad_account_url(ACCOUNT),
        dcad_aligned=True,
        dcad_basis="exact street agreement",
        dcad_scope="account scope only",
        basis="test",
        disputes=0,
    )
    assert attestation.aligned is False
    assert [entry["aligned"] for entry in voices] == [False, True]
    assert "unsupported" in attestation.note
    assert "Phase 2A" in voices[0]["basis"]


def test_commit_ignores_caller_authorizer_name(
    monkeypatch: pytest.MonkeyPatch, _custody_env: None
) -> None:
    import json as json_module

    from app import wondrue as wondrue_module

    db = _FakeDB()
    parent = _seed_parent(db)
    collection = _seed_collection(db, parent)
    _patch_owned(monkeypatch, db)
    result = wondrue_module.handle_command(
        mode="full",
        max_mode="full",
        text="",
        target="real",
        intent_override={
            "intent": "commit_successor",
            "args": {
                "property_id": PROP_ID,
                "parent_run_id": str(parent["run_id"]),
                "dossier_key": parent["dossier_key"],
                "parent_version": "1",
                "parent_digest": str(parent["dossier_digest"]),
                "expected_custody_version": "1",
                "collection_run_id": str(collection["run_id"]),
                "collection_digest": str(collection["collection_digest"]),
                "assessment_digest": str(collection["assessment_digest"]),
                "authorized_by": "owner",
                "reason": "assess admitted DCAD evidence",
            },
        },
        conn=_FakeConn(db),
        principal="wondrue-operator",
    )
    assert result["outcome"] == "result"
    version = db.versions[(str(parent["dossier_id"]), 2)]
    amendment = version["amendment"]
    if isinstance(amendment, str):
        amendment = json_module.loads(amendment)
    assert amendment["authorized_by"] == "wondrue-operator"


# --- Live PostgreSQL (GROWUP_PG_INTEGRATION=1, mocked network) ------------------

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

# The only database these mutating PG checks may touch. Verified
# against the live connection (not just configuration) before any
# write, so a misconfigured run fails instead of touching the
# operational database and its preserved history.
_PG_GATE_DATABASE = "growup_mvp_gate"


def _require_gate_database() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS db")
            name = str((cur.fetchone() or {}).get("db") or "")
    if name != _PG_GATE_DATABASE:
        pytest.fail(
            f"refusing PG-mutating DCAD test on database {name!r}: "
            f"owner must run against {_PG_GATE_DATABASE!r}",
            pytrace=False,
        )


def _require_successor_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.workflow_runs') AS runs, "
                "to_regclass('public.workflow_records') AS records, "
                "to_regclass('public.dossier_versions') AS versions, "
                "to_regclass('public.custody_snapshots') AS snapshots, "
                "to_regclass('public.custody_receipts') AS receipts, "
                "to_regclass('public.pilot_cells') AS cells, "
                "to_regclass('public.pilot_events') AS events, "
                "to_regclass('public.agent_events') AS audit"
            )
            row = cur.fetchone()
    names = ("runs", "records", "versions", "snapshots", "receipts", "cells", "events", "audit")
    if row is None or any(row[name] is None for name in names):
        pytest.skip("successor tables not present (migrations not applied)")


def _pg_cleanup(
    run_ids: list[str],
    dossier_ids: list[str],
    property_ids: list[str] | None = None,
) -> None:
    """Remove disposable rows; append-only custody rows are retained.

    ``dossier_versions`` and ``custody_receipts`` reject DELETE by
    production trigger, so committed test rows there are deliberately
    retained (the established convention for committed custody test
    data). Every PG test here mints fresh property/dossier UUIDs, so
    retained rows can never collide with repeat runs or other tests;
    receipts carry ``issued_by='pg-test'`` and successor amendments
    carry ``actor='pg-test-owner'`` for review attribution. Hot
    snapshots, pilot events, workflow rows, and the clearly named
    test property rows are deleted.
    """
    from app.properties_db import get_connection

    run_ids = [str(item) for item in run_ids if item]
    dossier_ids = [str(item) for item in dossier_ids if item]
    property_ids = [str(item) for item in (property_ids or []) if item]
    with get_connection() as conn:
        with conn.cursor() as cur:
            if dossier_ids:
                # Hot snapshots allow DELETE (archival path); versions
                # and receipts are permanent and stay.
                cur.execute(
                    "DELETE FROM custody_snapshots WHERE dossier_id = ANY(%(ids)s)",
                    {"ids": dossier_ids},
                )
                cur.execute(
                    "DELETE FROM pilot_events WHERE dossier_id = ANY(%(ids)s)",
                    {"ids": dossier_ids},
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
def test_successor_commit_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end successor commit on disposable PostgreSQL (owner-run)."""
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
    from app.properties_db import get_connection
    from app.workflow import (
        KIND_DCAD_ASSESSMENT,
        KIND_DCAD_COLLECTION,
        KIND_DOSSIER_VERSION,
        KIND_RESEARCH_ARTIFACT,
        digest_payload,
    )
    from app.workflow_db import (
        WorkflowError,
        create_run_in_transaction,
        get_record_in_transaction,
        put_record_in_transaction,
    )

    _require_gate_database()
    _require_successor_tables()
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "dcad-pg-test-custody-key-0123456789")
    monkeypatch.setenv("CUSTODY_AMENDERS", "pg-test-owner")
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", ACCOUNT)
    prop_id = str(uuid_module.uuid4())
    dossier_uuid = dossier_id_for_property(prop_id)
    created_runs: list[str] = []
    try:
        with get_connection() as conn:
            ensure_ledger_in_transaction(conn)
            parent_run = create_run_in_transaction(conn, idempotency_key=f"dcad-pg-parent-{uuid_module.uuid4().hex}")
            created_runs.append(str(parent_run.run_id))
            result = BraveSearchResult(
                assignment_id="a1", query="q", title="t",
                url="https://listings.example.com/1", snippet=SNIPPET,
                domain="listings.example.com",
                retrieved_at="2026-10-30T00:00:00+00:00", rank=1,
            )
            artifact = {
                "provider": "brave", "assignment_id": "a1", "query": "q",
                "results": [
                    {
                        "url": result.url, "title": result.title,
                        "snippet": "2323 Chalk Hill Rd, Dallas, TX 75212 office.",
                        "domain": result.domain, "published_at": None,
                        "retrieved_at": result.retrieved_at, "rank": 1,
                    }
                ],
                "retrieved_at": "2026-10-30T00:00:00+00:00",
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
            dossier["county_attribution"] = {"evidence_status": "verified", "value": "Dallas County"}
            payload = {
                "version": 1, "builder_version": "pg-test", "artifact_refs": ["a1"],
                "dossier": dossier,
                "candidate_verification": verdicts[key].model_dump(mode="json"),
                "property_id": prop_id,
            }
            payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
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
            collection_run = create_run_in_transaction(conn, idempotency_key=f"dcad-pg-collect-{uuid_module.uuid4().hex}")
            created_runs.append(str(collection_run.run_id))
            collection = _collection()
            stored_collection = put_record_in_transaction(
                conn, collection_run.run_id, kind=KIND_DCAD_COLLECTION,
                record_key=f"dcad:{ACCOUNT}", version=1,
                payload=collection, digest=digest_payload(collection),
            )
            assessment = build_dcad_assessment(
                collection=collection,
                collection_digest=str(stored_collection.digest or ""),
                property_record={"id": prop_id, "street_address": "2323 Chalk Hill Rd"},
                dossier_key=key,
                claim_scope="commercial_property",
            )
            stored_assessment = put_record_in_transaction(
                conn, collection_run.run_id, kind=KIND_DCAD_ASSESSMENT,
                record_key=f"dcad:{ACCOUNT}", version=1,
                payload=assessment, digest=digest_payload(assessment),
            )
            conn.commit()
        parent_digest = str(stored_parent.digest or "")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM pilot_events WHERE dossier_id = %(d)s",
                    {"d": str(canonical_quota_id(prop_id))},
                )
                events_before = int(cur.fetchone()["n"])
        summary = mvp_module.commit_successor_audited(
            property_id=prop_id,
            parent_run_id=str(parent_run.run_id),
            dossier_key=key,
            parent_version=1,
            parent_digest=parent_digest,
            expected_custody_version=1,
            collection_run_id=str(collection_run.run_id),
            collection_digest=str(stored_collection.digest or ""),
            assessment_digest=str(stored_assessment.digest or ""),
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
            collection_run_id=str(collection_run.run_id),
            collection_digest=str(stored_collection.digest or ""),
            assessment_digest=str(stored_assessment.digest or ""),
            reason="pg acceptance",
            principal="pg-test-owner",
            approved=True,
        )
        assert replay["replayed"] is True
        assert replay["dossier_digest"] == summary["dossier_digest"]
        with get_connection() as conn:
            latest = latest_version_in_transaction(conn, dossier_uuid)
            assert latest is not None and latest.version == 2
            reread = get_record_in_transaction(conn, parent_run.run_id, KIND_DOSSIER_VERSION, key)
            assert reread is not None and str(reread.digest or "") == parent_digest
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT transition, COUNT(*) AS n FROM pilot_events "
                    "WHERE dossier_id = %(d)s GROUP BY transition",
                    {"d": str(canonical_quota_id(prop_id))},
                )
                counts = {row["transition"]: int(row["n"]) for row in cur.fetchall()}
        assert all(n == 1 for n in counts.values()), counts  # counted once
        assert sum(counts.values()) >= events_before
        # Rollback atomicity: an unenrolled authorizer leaves no partial rows.
        monkeypatch.setenv("CUSTODY_AMENDERS", "")
        with pytest.raises(Exception, match="enrolled authorizer"):
            mvp_module.commit_successor_audited(
                property_id=prop_id,
                parent_run_id=str(parent_run.run_id),
                dossier_key=key,
                parent_version=1,
                parent_digest=parent_digest,
                expected_custody_version=2,
                collection_run_id=str(collection_run.run_id),
                collection_digest=str(stored_collection.digest or ""),
                assessment_digest=str(stored_assessment.digest or ""),
                        reason="pg acceptance",
                principal="pg-test-owner",
                approved=True,
            )
        with get_connection() as conn:
            latest = latest_version_in_transaction(conn, dossier_uuid)
            assert latest is not None and latest.version == 2
    finally:
        _pg_cleanup(created_runs, [str(dossier_uuid)])


def _pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "dcad-pg-test-custody-key-0123456789")
    monkeypatch.setenv("CUSTODY_AMENDERS", "pg-test-owner")
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", ACCOUNT)


def _pg_seed_parent(prop_id: str) -> dict[str, Any]:
    """Seed a parent run + artifact + dossier record on live PG (committed)."""
    import uuid as uuid_module

    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.extractor import extract_research_intelligence
    from app.agents.gabriel.producers import build_candidate_verifications
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult
    from app.pilot import ensure_ledger_in_transaction
    from app.properties_db import get_connection
    from app.workflow import (
        KIND_DOSSIER_VERSION,
        KIND_RESEARCH_ARTIFACT,
        digest_payload,
    )
    from app.workflow_db import create_run_in_transaction, put_record_in_transaction

    with get_connection() as conn:
        ensure_ledger_in_transaction(conn)
        parent_run = create_run_in_transaction(
            conn, idempotency_key=f"dcad-pg-parent-{uuid_module.uuid4().hex}"
        )
        result = BraveSearchResult(
            assignment_id="a1", query="q", title="t",
            url="https://listings.example.com/1", snippet=SNIPPET,
            domain="listings.example.com",
            retrieved_at="2026-10-30T00:00:00+00:00", rank=1,
        )
        artifact = {
            "provider": "brave", "assignment_id": "a1", "query": "q",
            "results": [
                {
                    "url": result.url, "title": result.title,
                    "snippet": result.snippet, "domain": result.domain,
                    "published_at": None, "retrieved_at": result.retrieved_at,
                    "rank": 1,
                }
            ],
            "retrieved_at": "2026-10-30T00:00:00+00:00",
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
        conn.commit()
    return {
        "run_id": str(parent_run.run_id),
        "dossier_key": key,
        "parent_digest": str(stored_parent.digest or ""),
        "dossier": dossier,
    }


def _pg_seed_custody_v1(prop_id: str, dossier: dict[str, Any]) -> str:
    """Seed custody v1 + snapshot + Gabriel -> Raphael receipts on live PG."""
    from app.custody_db import (
        create_version_in_transaction,
        issue_receipt_in_transaction,
        snapshot_in_transaction,
    )
    from app.properties_db import get_connection

    with get_connection() as conn:
        version = create_version_in_transaction(
            conn, property_id=prop_id, payload={"dossier": dossier},
            created_by="gabriel",
        )
        snapshot_in_transaction(
            conn, dossier_id=version.dossier_id, version=version.version
        )
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
    return str(version.dossier_id)


def _pg_seed_collection_pair(prop_id: str, dossier_key: str) -> dict[str, str]:
    """Seed a DCAD collection run + collection/assessment pair on live PG."""
    import uuid as uuid_module

    from app.agents.gabriel.sources.dcad import build_dcad_assessment
    from app.properties_db import get_connection
    from app.workflow import (
        KIND_DCAD_ASSESSMENT,
        KIND_DCAD_COLLECTION,
        digest_payload,
    )
    from app.workflow_db import create_run_in_transaction, put_record_in_transaction

    with get_connection() as conn:
        run = create_run_in_transaction(
            conn, idempotency_key=f"dcad-pg-collect-{uuid_module.uuid4().hex}"
        )
        collection = _collection()
        stored_collection = put_record_in_transaction(
            conn, run.run_id, kind=KIND_DCAD_COLLECTION,
            record_key=f"dcad:{ACCOUNT}", version=1,
            payload=collection, digest=digest_payload(collection),
        )
        assessment = build_dcad_assessment(
            collection=collection,
            collection_digest=str(stored_collection.digest or ""),
            property_record={"id": prop_id, "street_address": "2323 Chalk Hill Rd"},
            dossier_key=dossier_key,
            claim_scope="commercial_property",
        )
        stored_assessment = put_record_in_transaction(
            conn, run.run_id, kind=KIND_DCAD_ASSESSMENT,
            record_key=f"dcad:{ACCOUNT}", version=1,
            payload=assessment, digest=digest_payload(assessment),
        )
        conn.commit()
    return {
        "run_id": str(run.run_id),
        "collection_digest": str(stored_collection.digest or ""),
        "assessment_digest": str(stored_assessment.digest or ""),
    }


@needs_live_pg
def test_collect_single_request_pg_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent identical collects transmit exactly once (owner-run).

    The provider stays mocked; only the claim/retention concurrency is
    live PostgreSQL. The loser replays or is refused without transmitting.
    """
    import threading
    import time
    from types import SimpleNamespace

    from app import mvp as mvp_module
    from app.agents.gabriel.sources import dcad as dcad_module
    from app.mvp import _dcad_run_key
    from app.properties_db import create_property, get_connection
    from app.workflow_db import get_run_by_key_in_transaction

    _require_gate_database()
    _require_successor_tables()
    _pg_env(monkeypatch)
    prop = create_property(
        {
            "name": "DCAD PG collect-race test property",
            "street_address": "2323 Chalk Hill Rd", "city": "Dallas",
            "state": "TX", "zip_code": "75212", "category": "commercial_property",
        }
    )
    prop_id = str(prop["id"])
    parent = _pg_seed_parent(prop_id)
    created_runs = [parent["run_id"]]
    try:
        calls: list[str] = []
        body_payload = _collection()

        def _fake_collect(account: str, principal: str = "") -> Any:
            calls.append(str(account))
            time.sleep(0.3)  # widen the claim race window
            return SimpleNamespace(
                model_dump=lambda mode="json": dict(body_payload)
            )

        monkeypatch.setattr(dcad_module, "collect_account", _fake_collect)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, bool, str]] = []

        def _worker() -> None:
            try:
                barrier.wait(timeout=15)
                summary = mvp_module.collect_dcad_audited(
                    account=ACCOUNT, property_id=prop_id,
                    parent_run_id=parent["run_id"],
                    dossier_key=parent["dossier_key"],
                    principal="pg-test-owner", approved=True,
                )
            except Exception as exc:  # collected for assertions below
                outcomes.append(("err", False, f"{type(exc).__name__}: {exc}"))
            else:
                outcomes.append(
                    (
                        "ok", bool(summary.get("replayed")),
                        str(summary.get("collection_digest", "")),
                    )
                )

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not any(thread.is_alive() for thread in threads)
        assert len(outcomes) == 2
        assert len(calls) == 1, outcomes
        oks = [item for item in outcomes if item[0] == "ok"]
        assert len(oks) >= 1, outcomes
        assert len([item for item in oks if item[1] is False]) == 1, outcomes
        assert len({item[2] for item in oks}) == 1, outcomes  # same bytes
        for kind, _replayed, detail in outcomes:
            if kind == "err":
                assert (
                    "another caller holds" in detail or "uncertain prior" in detail
                ), detail
        key = _dcad_run_key(
            account=ACCOUNT, revision=dcad_module.DCAD_COLLECTOR_REVISION,
            property_id=prop_id, parent_run_id=parent["run_id"],
            dossier_key=parent["dossier_key"], attempt="1",
        )
        with get_connection() as conn:
            run = get_run_by_key_in_transaction(conn, key)
            assert run is not None
            created_runs.append(str(run.run_id))
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT kind, COUNT(*) AS n FROM workflow_records "
                    "WHERE run_id = %(run)s GROUP BY kind",
                    {"run": str(run.run_id)},
                )
                counts = {row["kind"]: int(row["n"]) for row in cur.fetchall()}
        assert counts.get("dcad_collection") == 1, counts
        assert counts.get("dcad_assessment") == 1, counts
    finally:
        _pg_cleanup(created_runs, [], [prop_id])


@needs_live_pg
def test_successor_single_version_pg_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent identical commits append exactly one v2 (owner-run)."""
    import threading

    from app import mvp as mvp_module
    from app.custody_db import latest_version_in_transaction
    from app.mvp import successor_run_key
    from app.pilot import canonical_quota_id
    from app.properties_db import create_property, get_connection
    from app.workflow_db import get_run_by_key_in_transaction

    _require_gate_database()
    _require_successor_tables()
    _pg_env(monkeypatch)
    prop = create_property(
        {
            "name": "DCAD PG successor-race test property",
            "street_address": "2323 Chalk Hill Rd", "city": "Dallas",
            "state": "TX", "zip_code": "75212", "category": "commercial_property",
        }
    )
    prop_id = str(prop["id"])
    parent = _pg_seed_parent(prop_id)
    dossier_uuid = _pg_seed_custody_v1(prop_id, parent["dossier"])
    pair = _pg_seed_collection_pair(prop_id, parent["dossier_key"])
    created_runs = [parent["run_id"], pair["run_id"]]
    try:
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, bool, str]] = []

        def _worker() -> None:
            try:
                barrier.wait(timeout=15)
                summary = mvp_module.commit_successor_audited(
                    property_id=prop_id, parent_run_id=parent["run_id"],
                    dossier_key=parent["dossier_key"], parent_version=1,
                    parent_digest=parent["parent_digest"],
                    expected_custody_version=1,
                    collection_run_id=pair["run_id"],
                    collection_digest=pair["collection_digest"],
                    assessment_digest=pair["assessment_digest"],
                    reason="pg race acceptance", principal="pg-test-owner",
                    approved=True,
                )
            except Exception as exc:  # collected for assertions below
                outcomes.append(("err", False, f"{type(exc).__name__}: {exc}"))
            else:
                outcomes.append(
                    (
                        "ok", bool(summary.get("replayed")),
                        str(summary.get("dossier_digest", "")),
                    )
                )

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not any(thread.is_alive() for thread in threads)
        assert len(outcomes) == 2
        oks = [item for item in outcomes if item[0] == "ok"]
        assert len([item for item in oks if item[1] is False]) == 1, outcomes
        assert len({item[2] for item in oks}) == 1, outcomes
        for kind, _replayed, detail in outcomes:
            if kind == "err":
                assert "re-preview" in detail or "conflict" in detail.lower(), detail
        key = successor_run_key(
            parent_run_id=parent["run_id"], parent_version=1,
            parent_digest=parent["parent_digest"],
            collection_digest=pair["collection_digest"],
            assessment_digest=pair["assessment_digest"],
        )
        with get_connection() as conn:
            run = get_run_by_key_in_transaction(conn, key)
            assert run is not None
            created_runs.append(str(run.run_id))
            latest = latest_version_in_transaction(conn, dossier_uuid)
            assert latest is not None and latest.version == 2
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM dossier_versions "
                    "WHERE dossier_id = %(d)s",
                    {"d": dossier_uuid},
                )
                assert int(cur.fetchone()["n"]) == 2
                cur.execute(
                    "SELECT kind, from_agent, to_agent FROM custody_receipts "
                    "WHERE dossier_id = %(d)s AND version = 2",
                    {"d": dossier_uuid},
                )
                receipts = {
                    (row["kind"], row["from_agent"], row["to_agent"])
                    for row in cur.fetchall()
                }
                assert receipts == {
                    ("forward", "gabriel", "raphael"),
                    ("return", "raphael", "gabriel"),
                }, receipts
                cur.execute(
                    "SELECT transition, COUNT(*) AS n FROM pilot_events "
                    "WHERE dossier_id = %(d)s GROUP BY transition",
                    {"d": str(canonical_quota_id(prop_id))},
                )
                counts = {row["transition"]: int(row["n"]) for row in cur.fetchall()}
                # The successor evidence verifies no county, so no quota
                # credit is recorded at all -- nothing to duplicate.
                assert not counts, counts
        # Legitimate sequential replay still serves the committed result.
        replay = mvp_module.commit_successor_audited(
            property_id=prop_id, parent_run_id=parent["run_id"],
            dossier_key=parent["dossier_key"], parent_version=1,
            parent_digest=parent["parent_digest"],
            expected_custody_version=1,
            collection_run_id=pair["run_id"],
            collection_digest=pair["collection_digest"],
            assessment_digest=pair["assessment_digest"],
            reason="pg race acceptance", principal="pg-test-owner",
            approved=True,
        )
        assert replay["replayed"] is True
        assert replay["dossier_digest"] == oks[0][2]
    finally:
        _pg_cleanup(created_runs, [dossier_uuid], [prop_id])
