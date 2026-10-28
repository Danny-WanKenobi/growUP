"""G1/G4 research-grouping identity tests (deterministic; no network, no database).

Distinct physical buildings must never be combined merely because they share
a name, city, source URL, or other weak identifier. Research grouping is
provisional evidence organization, not proof of physical-building identity.
"""

from __future__ import annotations

from pathlib import Path

from app.agents.gabriel.dossier import DossierRequest, build_dossiers
from app.agents.gabriel.extractor import (
    ExtractedProperty,
    ResearchIntelligence,
    ResultIntelligence,
    SignalObservation,
    extract_intelligence,
    group_by_property,
)
from app.agents.gabriel.investigation import plan_investigations
from app.agents.gabriel.ranking import QCStatus, rank_intelligence
from app.agents.gabriel.sources.brave import BraveSearchResult

_RETRIEVED = "2026-10-27T12:00:00+00:00"


def _item(url: str, props: list[dict], title: str = "t") -> ResultIntelligence:
    return ResultIntelligence(
        assignment_id="a",
        query="q",
        result_url=url,
        domain="example.com",
        title=title,
        properties=[ExtractedProperty(**prop) for prop in props],
    )


def _search(title: str, snippet: str, url: str) -> BraveSearchResult:
    return BraveSearchResult(
        assignment_id="a",
        query="q",
        title=title,
        url=url,
        snippet=snippet,
        domain="example.com",
        provider="brave",
        retrieved_at=_RETRIEVED,
        published_at=None,
        rank=1,
    )


def _appearances(groups, index: int) -> int:
    return sum(group.members.count(index) for group in groups)


# 1. Same name and city, different URLs stay separate.


def test_same_name_city_different_urls_stay_separate() -> None:
    items = [
        _item("https://x/1", [{"name": "Commerce Tower", "city": "Dallas"}]),
        _item("https://x/2", [{"name": "Commerce Tower", "city": "Dallas"}]),
    ]
    groups = group_by_property(items)
    assert len(groups) == 2
    assert groups[0].members == [0]
    assert groups[1].members == [1]
    assert all(not group.key.startswith("name:") for group in groups)


# 2. Same URL, different addressless building identities stay separate.


def test_same_url_different_addressless_identities_stay_separate() -> None:
    items = [
        _item("https://x/1", [{"name": "Commerce Tower", "city": "Dallas"}]),
        _item("https://x/1", [{"name": "Republic Plaza", "city": "Dallas"}]),
    ]
    groups = group_by_property(items)
    assert len(groups) == 2
    assert sorted(member for group in groups for member in group.members) == [0, 1]


# 3. Same URL and identical complete identity signatures group provisionally.


def test_same_url_identical_signature_groups_provisionally() -> None:
    props = {"name": "Commerce Tower", "city": "Dallas", "state": "TX"}
    groups = group_by_property([_item("https://x/1", [props]), _item("https://x/1", [dict(props)])])
    assert len(groups) == 1
    assert groups[0].members == [0, 1]
    assert groups[0].needs_review is False


# 4. Addressless-first, then two addressed buildings: no bridging.


def test_addressless_first_then_two_addressed_buildings() -> None:
    items = [
        _item("https://x/1", [{"name": "Commerce Tower", "city": "Dallas"}]),
        _item("https://x/2", [{"name": "Commerce Tower", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/3", [{"name": "Commerce Tower", "city": "Dallas", "street_address": "200 Main St"}]),
    ]
    groups = group_by_property(items)
    assert len(groups) == 3
    by_member = {member: key for group in groups for key in [group.key] for member in group.members}
    assert len(set(by_member.values())) == 3  # no shared membership
    addr_groups = [group for group in groups if group.key.startswith("addr:")]
    assert len(addr_groups) == 2


# 5. Different observed addresses with matching names stay separate.


def test_different_addresses_matching_names_stay_separate() -> None:
    items = [
        _item("https://x/1", [{"name": "Commerce Tower", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Commerce Tower", "city": "Dallas", "street_address": "200 Main St"}]),
    ]
    groups = group_by_property(items)
    assert len(groups) == 2
    assert all(len(group.members) == 1 for group in groups)
    assert all(group.needs_review is False for group in groups)


# 6. Same address with conflicting observed names is flagged.


def test_same_address_conflicting_names_flagged() -> None:
    items = [
        _item("https://x/1", [{"name": "Commerce Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
        _item("https://x/2", [{"name": "Commerce Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
    ]
    groups = group_by_property(items)
    assert len(groups) == 1
    assert groups[0].members == [0, 1]
    assert groups[0].needs_review is True
    assert any("conflicting observed building names" in reason for reason in groups[0].review_reasons)


# 7. Same address with conflicting unit/building designators is flagged.


def test_same_address_conflicting_designators_flagged() -> None:
    items = [
        extract_intelligence(_search("Acme Tower listed", "Acme Tower at 100 Main St, Dallas, TX Suite 100.", "https://x/1")),
        extract_intelligence(_search("Acme Tower update", "Acme Tower at 100 Main St, Dallas, TX Suite 200.", "https://x/2")),
    ]
    assert items[0].properties[0].unit_designator == "Suite 100"
    assert items[1].properties[0].unit_designator == "Suite 200"
    groups = group_by_property(items)
    assert len(groups) == 1
    assert groups[0].needs_review is True
    assert any("designator" in reason for reason in groups[0].review_reasons)


# 8. Missing versus present unit designators are flagged, not assumed equal.


def test_present_vs_absent_designator_flagged() -> None:
    items = [
        extract_intelligence(_search("Acme Tower listed", "Acme Tower at 100 Main St, Dallas, TX Suite 100.", "https://x/1")),
        extract_intelligence(_search("Acme Tower news", "Acme Tower at 100 Main St, Dallas, TX announced tenants.", "https://x/2")),
    ]
    assert items[1].properties[0].unit_designator is None
    groups = group_by_property(items)
    assert len(groups) == 1
    assert groups[0].needs_review is True
    assert any("absent" in reason for reason in groups[0].review_reasons)


def test_designator_extraction_rejects_plain_language() -> None:
    intel = extract_intelligence(
        _search("Acme Tower news", "Acme Tower is building a new lobby at 100 Main St, Dallas, TX.", "https://x/1")
    )
    assert intel.properties[0].unit_designator is None


# 9. Multi-building portfolio associations span groups and stay unattributed.


def test_portfolio_item_spans_address_groups_with_review() -> None:
    item = _item(
        "https://x/1",
        [{"street_address": "100 Main St", "city": "Dallas"}, {"street_address": "200 Main St", "city": "Dallas"}],
    )
    item.distress.append(SignalObservation(signal="low_occupancy", evidence="portfolio-wide vacancy"))
    groups = group_by_property([item])
    assert len(groups) == 2
    assert all(group.members == [0] for group in groups)
    assert all(group.needs_review is True for group in groups)
    assert all(any("portfolio" in reason for reason in group.review_reasons) for group in groups)
    ranking = rank_intelligence(ResearchIntelligence(results=[item]))
    assert len(ranking.candidates) == 2
    for candidate in ranking.candidates:
        assert candidate.distress == []  # multi-building signals stay unattributed
        assert any("cannot be safely attributed" in note for note in candidate.unresolved)


# 10. Duplicate grouping keys yield single membership.


def test_duplicate_keys_yield_single_membership() -> None:
    items = [
        _item("https://x/1", [{"name": "A", "city": "Dallas", "street_address": "100 Main St"}]),
        _item(
            "https://x/2",
            [
                {"name": "A", "city": "Dallas", "street_address": "100 Main St"},
                {"name": "B", "city": "Dallas", "street_address": "100 Main St"},
            ],
        ),
    ]
    groups = group_by_property(items)
    assert len(groups) == 1
    assert groups[0].members == [0, 1]


# 11. Single-property observations appear in exactly one group.


def test_single_property_items_appear_in_exactly_one_group() -> None:
    items = [
        _item("https://x/1", [{"name": "N", "city": "C"}]),
        _item("https://x/2", [{"city": "C", "street_address": "1 A St"}]),
        _item("https://x/3", [{"name": "N", "city": "C", "street_address": "1 A St"}]),
        _item("https://x/4", [{"name": "P", "city": "C", "street_address": "2 B St"}]),
        _item("https://x/5", [{"street_address": "3 C St", "city": "C"}, {"street_address": "4 D St", "city": "C"}]),
    ]
    groups = group_by_property(items)
    for index, item in enumerate(items):
        if len(item.properties) == 1:
            assert _appearances(groups, index) == 1


# 12. Identity-review findings reach ranking, investigation, and dossiers.


def test_identity_review_reaches_ranking_investigation_and_dossier() -> None:
    intel = ResearchIntelligence(
        results=[
            _item("https://x/1", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
            _item("https://x/2", [{"name": "Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
        ]
    )
    ranking = rank_intelligence(intel)
    assert len(ranking.candidates) == 1
    candidate = ranking.candidates[0]
    assert any("identity review:" in note for note in candidate.unresolved)
    assert any("single building" in step for step in candidate.next_steps)
    plan = plan_investigations(intel, ranking=ranking)
    investigation = next(inv for inv in plan.investigations if inv.candidate_key == candidate.key)
    assert any(question.slug == "same-building" for question in investigation.questions)
    assert any("identity review:" in note for note in investigation.missing)
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    dossier = next(doc for doc in dossier_set.dossiers if doc.key == candidate.key)
    assert any("identity review:" in note for note in dossier.missing)


# 13. Flagged groups produce RESEARCH_REQUIRED, never an unjustified PASS.


def test_flagged_group_yields_research_required() -> None:
    intel = ResearchIntelligence(
        results=[
            _item("https://x/1", [{"name": "Tower A", "city": "Dallas", "street_address": "100 Main St"}]),
            _item("https://x/2", [{"name": "Tower B", "city": "Dallas", "street_address": "100 Main St"}]),
        ]
    )
    (candidate,) = rank_intelligence(intel).candidates
    assert candidate.unresolved != []
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED


# 14. No-identity observations stay ungrouped and screened, not candidates.


def test_no_identity_stays_ungrouped_and_screened() -> None:
    intel = ResearchIntelligence(results=[_item("https://x/1", [])])
    (group,) = group_by_property(intel.results)
    assert group.key == "ungrouped:0"
    assert group.needs_review is True
    assert any("insufficient identity" in reason for reason in group.review_reasons)
    ranking = rank_intelligence(intel)
    assert ranking.candidates == []
    assert len(ranking.screened_leads) == 1


# Operational guard: research grouping must never feed property persistence.


def test_grouping_never_feeds_operational_persistence() -> None:
    root = Path(__file__).resolve().parents[1]
    operational = [
        root / "app" / "agents" / "gabriel" / "service.py",
        root / "app" / "agents" / "gabriel" / "intake.py",
    ]
    for path in operational:
        assert "group_by_property" not in path.read_text(encoding="utf-8")
