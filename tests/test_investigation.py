"""Investigation planner tests (5A): deterministic, no network, no database."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from pydantic import ValidationError

from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
from app.agents.gabriel.investigation import (
    InvestigationConfig,
    QuestionTopic,
    _render_query,
    plan_investigations,
)
from app.agents.gabriel.ranking import CountyEligibility, rank_intelligence
from app.agents.gabriel.sources.brave import BraveSearchResult

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


def _result(title, snippet, url="https://example.com/story", published_at=None):
    global _RANK
    _RANK += 1
    return BraveSearchResult(
        assignment_id="gsp-inv001",
        query="test query investigation",
        title=title,
        url=url,
        snippet=snippet,
        domain=urlparse(url).netloc,
        provider="brave",
        retrieved_at=_RETRIEVED,
        published_at=published_at,
        rank=_RANK,
    )


def _intel(*results):
    return ResearchIntelligence(results=[extract_intelligence(item) for item in results])


def _strong():
    return _result(
        "Meridian Tower listed for sale",
        "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale with "
        "an asking price of $40 million. The Dallas County office tower drew bids.",
        url="https://example.com/meridian",
        published_at="2026-10-20",
    )


def _slugs(investigation, topic=None):
    return [
        (question.topic, question.slug)
        for question in investigation.questions
        if topic is None or question.topic is topic
    ]


def test_high_value_candidate_planned_first() -> None:
    intel = _intel(
        _strong(),
        _result(
            "Garland warehouse listed",
            "Logport warehouse at 77 Industrial Blvd, Garland, TX is listed for sale. "
            "The Dallas County industrial property seeks buyers.",
            url="https://example.com/logport",
        ),
    )
    plan = plan_investigations(intel)
    assert len(plan.investigations) == 2
    first = plan.investigations[0]
    assert first.research_priority == 1
    assert first.name == "Meridian Tower"
    assert first.value_rank == 1
    assert first.opportunity_score is not None
    assert plan.investigations[1].research_priority == 2


def test_missing_price_yields_question_and_query() -> None:
    intel = _intel(
        _result(
            "Summit Tower profile",
            "Summit Tower at 200 Main St, Dallas, TX operates as offices. "
            "The Dallas County tower is well known.",
            url="https://example.com/summit",
        )
    )
    plan = plan_investigations(intel)
    investigation = plan.investigations[0]
    assert (QuestionTopic.TRANSACTION, "asking-price") in _slugs(investigation)
    templates = [query.template for query in investigation.queries]
    assert "asking_price" in templates
    query = next(q for q in investigation.queries if q.template == "asking_price")
    assert "Summit Tower" in query.query
    assert query.explanation and query.sought


def test_unknown_occupancy_yields_followup() -> None:
    plan = plan_investigations(_intel(_strong()))
    investigation = plan.investigations[0]
    assert (QuestionTopic.UNDERUSE, "occupancy") in _slugs(investigation)
    assert "occupancy" in [query.template for query in investigation.queries]


def test_historical_distress_yields_current_status_question() -> None:
    intel = _intel(
        _result(
            "Old bankruptcy sale",
            "Beacon Tower at 900 Elm St, Dallas, TX sold after a 2023 bankruptcy filing. "
            "The Dallas County office tower changed hands.",
            url="https://example.com/beacon-old",
            published_at="2023-05-01",
        )
    )
    plan = plan_investigations(intel)
    investigation = plan.investigations[0]
    assert (QuestionTopic.DISTRESS, "distress-current") in _slugs(investigation)


def test_conflicting_county_yields_geographic_clarification() -> None:
    intel = _intel(
        _result(
            "Metro portfolio",
            "Metro Tower at 10 Metro Ave, Dallas, TX spans Dallas County and "
            "Tarrant County holdings.",
            url="https://example.com/metro",
        )
    )
    plan = plan_investigations(intel)
    investigation = plan.investigations[0]
    assert investigation.county is CountyEligibility.COUNTY_UNKNOWN
    assert (QuestionTopic.GEOGRAPHY, "county") in _slugs(investigation)
    assert investigation.contradictions


def test_weak_identity_yields_questions_only() -> None:
    assert _render_query("asking_price", None, None, "Dallas") is None
    assert _render_query("county", "", "", "") is None
    # Quotes and newlines never break out of the quoted phrase.
    rendered = _render_query("listing", 'Tower"\nDROP', "1 Main St", "Dallas")
    assert rendered is not None
    assert '"Tower" "Dallas" commercial real estate listing' in rendered or "Tower" in rendered
    assert "\n" not in rendered
    assert rendered.count('"') == 4


def test_repeated_sources_produce_no_duplicate_queries() -> None:
    intel = _intel(
        _result(
            "Meridian Tower listed",
            "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale.",
            url="https://example.com/a-meridian",
        ),
        _result(
            "Meridian Tower listing repeated",
            "Meridian Tower at 510 Commerce St, Dallas, TX is listed for sale.",
            url="https://example.com/b-meridian",
        ),
    )
    plan = plan_investigations(intel)
    assert len(plan.investigations) == 2
    queries = [q.query for inv in plan.investigations for q in inv.queries]
    assert len(queries) == len(set(queries))


def test_per_property_and_total_limits_enforced() -> None:
    intel = _intel(
        _strong(),
        _result(
            "Logport warehouse listed",
            "Logport warehouse at 77 Industrial Blvd, Garland, TX is listed for sale.",
            url="https://example.com/logport",
        ),
    )
    plan = plan_investigations(
        intel, config=InvestigationConfig(max_queries_per_property=1, max_total_queries=1)
    )
    assert plan.total_queries == 1
    assert all(len(inv.queries) <= 1 for inv in plan.investigations)
    assert plan.investigations[0].research_priority == 1


def test_max_properties_cap_reports_unplanned() -> None:
    intel = _intel(
        _strong(),
        _result(
            "Logport warehouse listed",
            "Logport warehouse at 77 Industrial Blvd, Garland, TX is listed for sale.",
            url="https://example.com/logport",
        ),
    )
    plan = plan_investigations(intel, config=InvestigationConfig(max_properties=1))
    assert len(plan.investigations) == 1
    assert len(plan.unplanned) == 1
    assert "max_properties" in plan.unplanned[0].reason


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValidationError):
        InvestigationConfig(max_properties=0)
    with pytest.raises(ValidationError):
        InvestigationConfig(max_total_queries=99)
    with pytest.raises(ValidationError):
        InvestigationConfig(max_depth=99)


def test_depth_and_iterations_bounded() -> None:
    intel = _intel(_strong())
    deep = plan_investigations(intel, depth=2, config=InvestigationConfig(max_depth=2))
    assert deep.investigations == []
    assert deep.unplanned and "depth" in deep.unplanned[0].reason
    over_iterated = plan_investigations(intel, iteration=2)
    assert over_iterated.investigations == []
    assert over_iterated.unplanned and "iterations" in over_iterated.unplanned[0].reason
    normal = plan_investigations(intel)
    assert all(query.depth == 1 for inv in normal.investigations for query in inv.queries)


def test_plan_deterministic_with_stable_ids() -> None:
    intel = _intel(_strong())
    first = plan_investigations(intel)
    second = plan_investigations(intel)
    assert first == second
    assert first.investigations[0].queries
    assert first.investigations[0].queries[0].id.startswith("gip-")
    assert first.investigations[0].questions[0].id.startswith("giq-")


def test_plan_recomputes_ranking_when_omitted() -> None:
    intel = _intel(_strong())
    implied = plan_investigations(intel)
    explicit = plan_investigations(intel, ranking=rank_intelligence(intel))
    assert implied == explicit


def test_plan_makes_no_provider_or_database_calls(monkeypatch) -> None:
    import app.agents.gabriel.sources.brave as brave
    import app.research_budget_db as budget

    def _forbidden(*args, **kwargs):
        raise AssertionError("planner must not call providers or the database")

    monkeypatch.setattr(brave, "execute_query", _forbidden)
    monkeypatch.setattr(budget, "get_connection", _forbidden)
    plan = plan_investigations(_intel(_strong()))
    assert plan.total_queries > 0


def test_plans_endpoint_returns_plan() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_strong())
    response = client.post(
        "/discovery/investigation-plans",
        json={"intelligence": intel.model_dump(mode="json")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["plan_version"] == "004B-5A.0"
    assert len(body["investigations"]) == 1
    assert body["total_queries"] == 2


def test_plans_endpoint_rejects_invalid_config() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_strong())
    response = client.post(
        "/discovery/investigation-plans",
        json={
            "intelligence": intel.model_dump(mode="json"),
            "config": {"max_properties": 99},
        },
    )
    assert response.status_code == 422


def test_excluded_and_deferred_candidates_unplanned() -> None:
    intel = _intel(
        _result(
            "Old mill demolished",
            "The Old Mill at 9 River Rd, Dallas, TX was demolished last year. "
            "The Dallas County site is cleared.",
            url="https://example.com/mill",
        ),
        _result(
            "Houston tower listed",
            "Bayou Tower at 500 Smith St, Houston, TX is listed for sale. "
            "The Harris County office tower drew bids.",
            url="https://example.com/bayou",
        ),
    )
    plan = plan_investigations(intel)
    assert plan.investigations == []
    assert plan.total_queries == 0
    reasons = sorted(item.reason for item in plan.unplanned)
    assert reasons == ["deferred: outside operating area", "excluded by quality control"]
