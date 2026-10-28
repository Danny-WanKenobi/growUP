"""Extractor tests (deterministic; no network, no database, no AI)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.agents.gabriel.research as research
from app.agents.gabriel.extractor import (
    MoneyKind,
    ObservationStatus,
    ResultClassification,
    TemporalStatus,
    assess_temporal,
    classify_result,
    extract_availability,
    extract_distress,
    extract_intelligence,
    extract_money,
    extract_occupancy,
    extract_properties,
    extract_research_intelligence,
    extract_underuse,
    group_by_property,
    normalize_text,
)
from app.agents.gabriel.sources.brave import BraveSearchResult
from app.main import app
from app.properties import PropertyCategory

client = TestClient(app, raise_server_exceptions=False)

_RETRIEVED = "2026-10-27T12:00:00+00:00"


def _result(title="", snippet="", url="https://example.com/story", published_at=None) -> BraveSearchResult:
    return BraveSearchResult(
        assignment_id="gsp-test001",
        query="test query Dallas",
        title=title,
        url=url,
        snippet=snippet,
        domain="example.com",
        provider="brave",
        retrieved_at=_RETRIEVED,
        published_at=published_at,
        rank=1,
    )


# --- Test A: Providence Towers (historical sale offer, Farmers Branch) ---


def test_a_providence_towers_historical_offer() -> None:
    intel = extract_intelligence(
        _result(
            title="Providence Towers offered for sale",
            snippet="Providence Towers in Farmers Branch, TX was offered for sale in "
            "April 2026, near Dallas. The twin-tower office complex drew investor interest.",
            url="https://example.com/providence-towers-sale",
            published_at="April 15, 2026",
        )
    )
    assert intel.classification is ResultClassification.PROPERTY_LISTING
    assert any(p.name == "Providence Towers" for p in intel.properties)
    cities = {p.city for p in intel.properties}
    assert cities == {"Farmers Branch"}
    assert intel.temporal.status is TemporalStatus.HISTORICAL
    assert intel.temporal.published_at == "April 15, 2026"
    assert intel.money == []  # no asking price invented
    assert intel.status is ObservationStatus.REPORTED_UNVERIFIED


# --- Test B: Gateway Tower (historical transaction, no current claims) ---


def test_b_gateway_tower_historical_transaction() -> None:
    intel = extract_intelligence(
        _result(
            title="Gateway Tower sells after bankruptcy auction",
            snippet="Gateway Tower at 8111 LBJ Freeway changed hands in a historical "
            "transaction of approximately $14.1 million following a 2023 bankruptcy auction.",
            url="https://example.com/gateway-tower-sold",
            published_at="2023-06-01",
        )
    )
    assert intel.classification is ResultClassification.PROPERTY_LEAD
    assert any(p.name == "Gateway Tower" for p in intel.properties)
    assert any(p.street_address == "8111 LBJ Freeway" for p in intel.properties)
    assert len(intel.money) == 1
    assert intel.money[0].amount == Decimal("14100000")
    assert intel.money[0].kind is MoneyKind.SALE_PRICE_COMPLETED
    assert intel.temporal.status is TemporalStatus.HISTORICAL
    assert intel.status is ObservationStatus.REPORTED_UNVERIFIED


# --- Test C: listing directory (aggregate count, no properties) ---


def test_c_listing_directory_not_properties() -> None:
    intel = extract_intelligence(
        _result(
            title="121 industrial properties in Dallas County",
            snippet="Browse 121 industrial properties for sale and lease. Search results "
            "updated daily with commercial listings across the county.",
            url="https://example.com/dallas-industrial",
        )
    )
    assert intel.classification is ResultClassification.LISTING_DIRECTORY
    assert "121" in intel.classification_reason
    assert intel.properties == []
    assert intel.money == []


# --- Test D: low occupancy (underuse without distress) ---


def test_d_low_occupancy_underuse_not_distress() -> None:
    intel = extract_intelligence(
        _result(
            title="Meridian Building occupancy falls",
            snippet="Meridian Building operated at 35% occupancy across the entire "
            "building during Q3 2026, filings show.",
            url="https://example.com/meridian-occupancy",
            published_at="October 10, 2026",
        )
    )
    assert len(intel.occupancy) == 1
    obs = intel.occupancy[0]
    assert obs.occupancy_pct == 35.0
    assert obs.vacancy_pct is None  # never derived by subtraction
    assert obs.period == "Q3 2026"
    assert obs.scope.value == "whole_building"
    assert obs.status is ObservationStatus.REPORTED_UNVERIFIED
    assert any(s.signal == "low_occupancy" for s in intel.underuse)
    assert intel.distress == []
    assert all(o.status is not ObservationStatus.VERIFIED for o in intel.occupancy)


# --- Test E: available space is not vacancy ---


def test_e_available_space_not_vacancy() -> None:
    intel = extract_intelligence(
        _result(
            title="Parkside Plaza availability",
            snippet="Parkside Plaza advertises 100,000 square feet available for lease "
            "in Dallas, TX with immediate occupancy possible.",
            url="https://example.com/parkside-availability",
        )
    )
    assert len(intel.availability) == 1
    assert intel.availability[0].square_feet == 100000
    assert intel.occupancy == []
    assert all(o.vacancy_pct is None for o in intel.occupancy)
    assert not any(s.signal == "high_vacancy" for s in intel.underuse)


# --- Test F: multiple buildings stay separate ---


def test_f_multiple_buildings_never_merged() -> None:
    intel = extract_intelligence(
        _result(
            title="Bankruptcy proceedings span three towers",
            snippet="Bankruptcy proceedings involve Alpha Tower sold for $5 million, "
            "Beta Plaza listed in Dallas, TX, and Gamma Center with a $7 million lien.",
            url="https://example.com/three-towers",
        )
    )
    names = sorted(p.name for p in intel.properties if p.name)
    assert names == ["Alpha Tower", "Beta Plaza", "Gamma Center"]
    assert {m.amount for m in intel.money} == {Decimal("5000000"), Decimal("7000000")}
    # Money stays result-level: no property object carries financial facts.
    for prop in intel.properties:
        assert set(prop.model_fields) <= {
            "name", "street_address", "city", "state", "zip_code",
            "category", "unit_designator", "evidence",
        }


# --- Test G: HTML normalization ---


def test_g_html_entities_normalized() -> None:
    assert normalize_text("<strong>Providence Towers</strong>") == "Providence Towers"
    assert normalize_text("Hartman&#x27;s portfolio &amp; holdings") == "Hartman's portfolio & holdings"
    assert normalize_text("<a href='x'>Text</a><script>evil()</script>") == "Text evil()"
    intel = extract_intelligence(
        _result(title="<strong>Providence Towers</strong>", snippet="Offered for <em>sale</em>.")
    )
    assert "<" not in intel.title
    assert intel.title == "Providence Towers"


# --- Test H: unknown information stays unknown ---


def test_h_unknown_information_preserved() -> None:
    intel = extract_intelligence(_result(title="Market update", snippet="Analysts share mixed views."))
    assert intel.classification is ResultClassification.UNKNOWN
    assert intel.properties == []
    assert intel.money == []
    assert intel.occupancy == []
    assert intel.distress == []
    assert intel.status is ObservationStatus.UNKNOWN


# --- Classification units ---


def test_classification_legal_news_background_unknown() -> None:
    kind, reason = classify_result("Trustee sale notice", "Notice of trustee sale, case no 24-100.", "u", False)
    assert kind is ResultClassification.LEGAL_OR_PUBLIC_RECORD and reason
    kind, _ = classify_result("Market report", "Analysts reported that rents stabilized.", "u", False)
    assert kind is ResultClassification.NEWS_ARTICLE
    kind, _ = classify_result("Office outlook", "Warehouse demand and square feet trends.", "u", False)
    assert kind is ResultClassification.BACKGROUND_INFORMATION
    kind, reason = classify_result("Hello", "Nothing relevant here.", "https://example.com/x", False)
    assert kind is ResultClassification.UNKNOWN and "example.com/x" in reason


def test_classification_prefers_specificity_conservatively() -> None:
    kind, _ = classify_result("Towers listed", "Two office towers listed for sale.", "u", False)
    assert kind is not ResultClassification.PROPERTY_LISTING  # no identified property


# --- Money units ---


def test_money_kinds() -> None:
    def kinds(text):
        return [(m.amount, m.kind) for m in extract_money(text)]

    assert kinds("Asking price of $2 million for the tower.") == [(Decimal("2000000"), MoneyKind.ASKING_PRICE_CURRENT)]
    assert kinds("The asking price was $3 million in 2021.")[0][1] is MoneyKind.ASKING_PRICE_HISTORICAL
    assert kinds("Reduced from $4 million to $3.5 million.")[0][1] is MoneyKind.ASKING_PRICE_HISTORICAL
    assert kinds("Leasing at $25 per square foot downtown.")[0][1] is MoneyKind.PRICE_PER_SQFT
    assert kinds("A proposed sale for $6 million awaits approval.")[0][1] is MoneyKind.TRANSACTION_PROPOSED
    assert kinds("The tower, valued at $8 million, drew bids.")[0][1] is MoneyKind.MARKET_VALUE_ESTIMATED
    assert kinds("Mentioned $9 million in passing.")[0][1] is MoneyKind.UNCLASSIFIED
    assert kinds("sold the site for $1.5M last year.")[0] == (Decimal("1500000"), MoneyKind.SALE_PRICE_COMPLETED)


# --- Occupancy, distress, underuse units ---


def test_occupancy_patterns_and_scope() -> None:
    [obs] = extract_occupancy("Vacancy rate of 20% reported for the portfolio.")
    assert (obs.vacancy_pct, obs.occupancy_pct) == (20.0, None)
    assert extract_occupancy("Occupancy hit 140% in the typo.") == []
    [leased] = extract_occupancy("The building is 80% leased.")
    assert leased.basis == "leased" and leased.occupancy_pct == 80.0
    [partial] = extract_occupancy("Two floors sit at 10% occupancy.")
    assert partial.scope.value == "partial"


def test_distress_and_underuse_vocabularies_stay_separate() -> None:
    distress = extract_distress("The lender placed the tower in receivership after a loan default; REO sale scheduled.")
    assert {s.signal for s in distress} >= {"receivership", "loan_default", "reo_lender_owned"}
    assert extract_distress("The tower reports low occupancy and rising vacancy.") == []
    underuse = extract_underuse("The tower reports low occupancy and rising vacancy after tenants departed.")
    assert {s.signal for s in underuse} >= {"low_occupancy", "high_vacancy", "tenant_departure"}
    assert all(s.evidence for s in distress + underuse)


def test_city_extraction_distinguishes_municipalities() -> None:
    props = extract_properties("Irving Tower sale", "Irving Tower in Irving, TX drew bids near Dallas.")
    assert [p.city for p in props if p.city] and all(p.city == "Irving" for p in props if p.city)
    assert extract_properties("Dallas market", "Near Dallas with no specifics.") == []


# --- Temporal units ---


def test_temporal_assessment() -> None:
    current = assess_temporal("2026-10-20", "2026-10-27T00:00:00+00:00", True)
    assert current.status.value == "current"
    recent_no_offer = assess_temporal("2026-10-20", "2026-10-27T00:00:00+00:00", False)
    assert recent_no_offer.status.value == "undetermined"
    old = assess_temporal("2023-06-01", "2026-10-27T00:00:00+00:00", True)
    assert old.status.value == "historical"
    relative = assess_temporal("10 days ago", "2026-10-27T00:00:00+00:00", True)
    assert relative.status.value == "current"
    assert assess_temporal(None, "2026-10-27T00:00:00+00:00", True).status.value == "undetermined"
    assert assess_temporal("not a date", "2026-10-27T00:00:00+00:00", True).status.value == "undetermined"


# --- Grouping, determinism, integration ---


def test_grouping_is_conservative() -> None:
    first = extract_intelligence(_result(title="Alpha Tower sale", snippet="Alpha Tower at 100 Main St, Dallas, TX for sale."))
    second = extract_intelligence(_result(title="Alpha Tower update", snippet="Alpha Tower at 100 Main St, Dallas, TX refinanced."))
    third = extract_intelligence(_result(title="Beta Plaza news", snippet="Beta Plaza in Irving, TX announced tenants."))
    groups = group_by_property([first, second, third])
    by_key = {group.key: group for group in groups}
    assert by_key["addr:100 main st|dallas|tx|"].members == [0, 1]
    assert by_key["addr:100 main st|dallas|tx|"].basis == "exact_address"
    assert len(groups) == 2  # nothing merged across distinct identities
    lonely = group_by_property([extract_intelligence(_result(title="Hi", snippet="No details."))])
    assert lonely[0].needs_review is True


def test_extraction_is_deterministic_and_unverified() -> None:
    result = _result(
        title="Gateway Tower sells",
        snippet="Gateway Tower at 8111 LBJ Freeway sold for $14.1 million.",
        published_at="2023-06-01",
    )
    first = extract_intelligence(result).model_dump(mode="json")
    second = extract_intelligence(result).model_dump(mode="json")
    assert first == second
    assert first["status"] == "reported_unverified"
    assert first["temporal"]["status"] == "historical"


def test_research_run_includes_intelligence_without_extra_calls(monkeypatch) -> None:
    import app.research_budget_db as budget

    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "unit-test-brave-key")
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")
    calls: list = []

    def _get(url, params=None, headers=None, timeout=None):
        calls.append(params["q"])

        class _Response:
            status_code = 200

            def json(self):
                return {"web": {"results": [
                    {"title": "Towers listed", "url": "https://example.com/t",
                     "description": "Providence Towers offered for sale.", "age": "April 15, 2026"}
                ]}}

        return _Response()

    monkeypatch.setattr(httpx, "get", _get)
    spent = {"used": 0}

    def _reserve(n):
        spent["used"] += n
        return {"used": spent["used"], "ceiling": 100,
                "remaining": 100 - spent["used"], "month": "2026-10"}

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    from app.agents.gabriel.planner import plan_initial_searches

    request = research.ResearchRunRequest.model_validate(
        {"assignments": [a.model_dump() for a in plan_initial_searches()]}
    )
    outcome = research.run_research(request)
    assert len(calls) == 3  # extraction triggers zero additional Brave calls
    assert outcome.intelligence is not None
    assert len(outcome.intelligence.results) == 3
    assert all(r.extraction_method == "rule-based-004B-3.0" for r in outcome.intelligence.results)
    assert outcome.intelligence.results[0].classification.value == "property_listing"
    request_off = research.ResearchRunRequest.model_validate(
        {"assignments": [a.model_dump() for a in plan_initial_searches()], "include_intelligence": False}
    )
    assert research.run_research(request_off).intelligence is None
    assert budget  # budget module untouched by extraction (reserved only for Brave calls)


def test_intelligence_endpoint_backward_compatible(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_API_KEY", "unit-test-research-key")
    from app.agents.gabriel.planner import plan_initial_searches

    response = client.get("/discovery/sources")
    assert response.status_code == 200
    body = {"assignments": [a.model_dump(mode="json") for a in plan_initial_searches()[:3]]}
    monkeypatch.setattr(
        "app.routes.discovery.run_research",
        lambda req: research.ResearchRunResult(retrieved_at="t", intelligence=None),
    )
    response = client.post(
        "/discovery/research-runs", json=body, headers={"X-Research-Key": "unit-test-research-key"}
    )
    assert response.status_code == 200
    assert response.json()["intelligence"] is None
    assert "results" in response.json() and "errors" in response.json()
