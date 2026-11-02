"""Universal source-qualification policy tests (offline, deterministic)."""
from __future__ import annotations

from app.agents.gabriel.sources.qualification import (
    SourceCandidate,
    qualify_source,
    rank_sources,
)


def _candidate(**overrides) -> SourceCandidate:
    base = dict(
        name="Example County Appraisal District",
        publisher_name="Example County Appraisal District",
        publisher_domain="appraisal.example.gov",
        claimed_official=True,
        jurisdiction="Example County",
        country="US",
        record_types=("parcels", "appraisal"),
        access_method="bulk_download",
        free=True,
        requires_auth=False,
        robots_restricted=False,
        license_clear=True,
        formats=("csv", "shapefile"),
        property_level=True,
        update_age_days=30,
        provenance_url="https://appraisal.example.gov/data",
    )
    base.update(overrides)
    return SourceCandidate(**base)


def test_official_bulk_source_qualifies_as_p1_candidate():
    result = qualify_source(_candidate())
    assert result.status == "qualified"
    assert result.p1_eligible is True
    assert result.authority_candidates == ("parcels", "appraisal")
    assert result.score > 0


def test_paid_or_authenticated_or_restricted_sources_rejected():
    for overrides in ({"free": False}, {"requires_auth": True},
                      {"robots_restricted": True},
                      {"access_method": "scrape"}):
        assert qualify_source(_candidate(**overrides)).status == "rejected"


def test_unverified_official_claim_needs_publisher_proof():
    # Appraisal districts legitimately publish on .org domains, so a
    # non-.gov official claim is neither trusted nor rejected: it
    # routes to publisher verification and earns no P1 candidacy.
    result = qualify_source(_candidate(
        publisher_name="Example Appraisal District",
        publisher_domain="appraisal.example.org"))
    assert result.status == "needs_review"
    assert result.p1_eligible is False
    assert result.authority_candidates == ()
    assert any("publisher identity" in reason for reason in result.reasons)


def test_nonofficial_source_cannot_be_p1():
    result = qualify_source(_candidate(
        publisher_name="Commercial Data Co",
        publisher_domain="data.example.com", claimed_official=False))
    assert result.p1_eligible is False
    assert result.authority_candidates == ()
    assert result.status in ("qualified", "needs_review")


def test_unsupported_format_and_aggregate_only_rejected():
    assert qualify_source(_candidate(formats=("scanned_images",))).status \
        == "rejected"
    assert qualify_source(_candidate(property_level=False)).status \
        == "rejected"


def test_stale_and_unknown_freshness_flagged():
    stale = qualify_source(_candidate(update_age_days=2000))
    assert any("stale" in reason for reason in stale.reasons)
    unknown = qualify_source(_candidate(update_age_days=None))
    assert unknown.status == "needs_review"


def test_unverified_license_needs_review_not_authority():
    result = qualify_source(_candidate(license_clear=False))
    assert result.status == "needs_review"


def test_ranking_is_mission_dynamic():
    parcels = _candidate(name="Parcels R Us",
                         record_types=("parcels",))
    zoning = _candidate(name="Zoning Office",
                        publisher_domain="zoning.example.gov",
                        record_types=("zoning",))
    rejected = _candidate(name="Paywall", free=False)
    ranked = rank_sources([rejected, zoning, parcels],
                          needed_record_types=("zoning",))
    assert ranked[0][0].name == "Zoning Office"
    assert ranked[-1][0].name == "Paywall"
    ranked2 = rank_sources([zoning, parcels],
                           needed_record_types=("parcels",))
    assert ranked2[0][0].name == "Parcels R Us"
