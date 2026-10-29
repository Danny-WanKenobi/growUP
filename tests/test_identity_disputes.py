"""G3-1/G3-2 conflicted research-identity tests (deterministic; no network, no database).

Disputed identity components must be flagged as disputed: they must never
silently become trusted display values or investigation search anchors.
Cross-address observations must never supply another group's identity.
Absent values are not conflicts.
"""

from __future__ import annotations

from app.agents.gabriel.dossier import DossierRequest, build_dossiers
from app.agents.gabriel.extractor import (
    ExtractedProperty,
    ResearchIntelligence,
    ResultIntelligence,
    group_by_property,
)
from app.agents.gabriel.investigation import plan_investigations
from app.agents.gabriel.ranking import QCStatus, rank_intelligence


def _item(url: str, props: list[dict], title: str = "t") -> ResultIntelligence:
    return ResultIntelligence(
        assignment_id="a",
        query="q",
        result_url=url,
        domain="example.com",
        title=title,
        properties=[ExtractedProperty(**prop) for prop in props],
    )


def _intel(*items: ResultIntelligence) -> ResearchIntelligence:
    return ResearchIntelligence(results=list(items))


def _disputes(candidate) -> dict:
    return dict(candidate.identity_disputes)


# 1. Same address, conflicting building names.


def test_conflicting_names_produce_dispute_metadata() -> None:
    intel = _intel(
        _item("https://x/1", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
    )
    (candidate,) = rank_intelligence(intel).candidates
    assert _disputes(candidate)["name"] == ["Tower A", "Tower B"]
    # Display preserves the observed value; the dispute flag carries the warning.
    assert candidate.name == "Tower A"
    assert "city" not in _disputes(candidate)
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED


# 2. Portfolio source with the wrong-address property listed first.


def test_portfolio_wrong_address_first_uses_group_address() -> None:
    intel = _intel(
        _item(
            "https://x/1",
            [
                {"name": "Far Hall", "city": "Dallas", "street_address": "200 Main St"},
                {"name": "Near Hall", "city": "Dallas", "street_address": "100 Main St"},
            ],
        )
    )
    groups = group_by_property(intel.results)
    assert len(groups) == 2
    near_key = next(group.key for group in groups if "100 main st" in group.key)
    ranking = rank_intelligence(intel)
    candidate = next(c for c in ranking.candidates if c.key == near_key)
    assert candidate.name == "Near Hall"
    assert candidate.city == "Dallas"
    dossier = next(d for d in build_dossiers(DossierRequest(rounds=[intel])).dossiers if d.key == near_key)
    assert dossier.street_address == "100 Main St"
    assert dossier.name == "Near Hall"
    plan = plan_investigations(intel, ranking=ranking)
    investigation = next(inv for inv in plan.investigations if inv.candidate_key == near_key)
    assert investigation.queries, "expected follow-up queries for the group identity"
    for query in investigation.queries:
        lowered = query.query.lower()
        assert "near hall" in lowered
        assert "200 main st" not in lowered
        assert "far hall" not in lowered


# 3. Conflicting unit/suite designators.


def test_conflicting_designators_produce_dispute_metadata() -> None:
    intel = _intel(
        _item("https://x/1", [{"name": "Acme Tower", "city": "Dallas", "street_address": "100 Main St",
                                "unit_designator": "Suite 100"}]),
        _item("https://x/2", [{"name": "Acme Tower", "city": "Dallas", "street_address": "100 Main St",
                                "unit_designator": "Suite 200"}]),
    )
    (candidate,) = rank_intelligence(intel).candidates
    assert _disputes(candidate)["unit_designator"] == ["Suite 100", "Suite 200"]
    assert "name" not in _disputes(candidate)
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
    plan = plan_investigations(intel, ranking=rank_intelligence(intel))
    (investigation,) = plan.investigations
    assert dict(investigation.identity_disputes)["unit_designator"] == ["Suite 100", "Suite 200"]


# 4. Incomplete but compatible identity observations.


def test_incomplete_compatible_identity_has_no_disputes() -> None:
    intel = _intel(
        _item("https://x/1", [{"city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Tower", "city": "Dallas", "street_address": "100 Main St"}]),
    )
    (candidate,) = rank_intelligence(intel).candidates
    assert _disputes(candidate) == {}
    assert candidate.name == "Tower"
    assert candidate.city == "Dallas"


# 5. Fully consistent identity observations.


def test_consistent_identity_has_no_disputes() -> None:
    intel = _intel(
        _item("https://x/1", [{"name": "Tower", "city": "Dallas", "state": "TX",
                                "street_address": "100 Main St"}]),
    )
    (candidate,) = rank_intelligence(intel).candidates
    assert _disputes(candidate) == {}
    assert (candidate.name, candidate.city, candidate.state) == ("Tower", "Dallas", "TX")
    plan = plan_investigations(intel, ranking=rank_intelligence(intel))
    (investigation,) = plan.investigations
    assert investigation.queries
    assert investigation.queries[0].query.startswith('"Tower" "Dallas" ')


# 6. Disputed name with an uncontested address.


def test_disputed_name_uncontested_address_queries_avoid_names() -> None:
    intel = _intel(
        _item("https://x/1", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
    )
    ranking = rank_intelligence(intel)
    plan = plan_investigations(intel, ranking=ranking)
    (investigation,) = plan.investigations
    assert investigation.queries, "address-anchored follow-ups must still proceed"
    for query in investigation.queries:
        lowered = query.query.lower()
        assert "tower a" not in lowered
        assert "tower b" not in lowered
        assert "100 main st" in lowered


# 7. Conflicting or missing city information.


def test_city_conflict_drops_qualifier_and_missing_city_ignored() -> None:
    conflicted = _intel(
        _item(
            "https://x/1",
            [
                {"name": "Tower", "city": "Irving"},
                {"name": "Tower", "city": "Dallas", "street_address": "100 Main St"},
            ],
        )
    )
    (candidate,) = rank_intelligence(conflicted).candidates
    assert _disputes(candidate)["city"] == ["Dallas", "Irving"]
    assert "name" not in _disputes(candidate)
    plan = plan_investigations(conflicted, ranking=rank_intelligence(conflicted))
    (investigation,) = plan.investigations
    for query in investigation.queries:
        lowered = query.query.lower()
        assert "irving" not in lowered
        assert "dallas" not in lowered
    missing = _intel(
        _item("https://x/1", [{"name": "Tower", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Tower", "city": "Dallas", "street_address": "100 Main St"}]),
    )
    assert len(group_by_property(missing.results)) == 2  # missing city splits groups; no merge
    for group_candidate in rank_intelligence(missing).candidates:
        assert "city" not in dict(group_candidate.identity_disputes)


# 8. Dossier identity rendering with unresolved disputes.


def test_dossier_identity_with_disputes() -> None:
    intel = _intel(
        _item("https://x/1", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
    )
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    (dossier,) = dossier_set.dossiers
    assert dict(dossier.identity_disputes)["name"] == ["Tower A", "Tower B"]
    assert dossier.name == "Tower A"
    assert dossier.street_address == "100 Main St"
    assert dossier.summary
    assert any("identity review:" in note for note in dossier.missing)


# 9. Addressless observations mixed with addressed observations.


def test_addressless_observation_contributes_without_overriding() -> None:
    intel = _intel(
        _item(
            "https://x/1",
            [
                {"name": "Tower"},
                {"city": "Dallas", "street_address": "100 Main St"},
            ],
        )
    )
    (candidate,) = rank_intelligence(intel).candidates
    assert _disputes(candidate) == {}
    assert candidate.name == "Tower"
    dossier = next(
        d for d in build_dossiers(DossierRequest(rounds=[intel])).dossiers if d.key == candidate.key
    )
    assert dossier.street_address == "100 Main St"
    # An addressless observation agreeing with one side must not resolve a dispute.
    conflicted = _intel(
        _item("https://x/1", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/3", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
    )
    (conflict_candidate,) = rank_intelligence(conflicted).candidates
    assert _disputes(conflict_candidate)["name"] == ["Tower A", "Tower B"]


# 10. Existing identity-review requirements stay intact.


def test_identity_review_requirements_preserved() -> None:
    intel = _intel(
        _item("https://x/1", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
    )
    (group,) = group_by_property(intel.results)
    assert group.needs_review is True
    ranking = rank_intelligence(intel)
    (candidate,) = ranking.candidates
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
    assert any("identity review:" in note for note in candidate.unresolved)
    assert any("single building" in step for step in candidate.next_steps)
    plan = plan_investigations(intel, ranking=ranking)
    (investigation,) = plan.investigations
    assert any(question.slug == "same-building" for question in investigation.questions)
