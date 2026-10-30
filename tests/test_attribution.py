"""Attribution contract and pure evaluator tests (B5-2A step 1).

Deterministic; no network, no database, no provider calls.
"""

from __future__ import annotations

import json

import pytest

from app.agents.gabriel.attribution import (
    AttributionReviewStatus,
    CategoryAttribution,
    CategoryEvidenceStatus,
    CategoryObservation,
    CountyAttribution,
    CountyClaim,
    CountyEvidenceStatus,
    TerritoryEligibility,
    evaluate_category_evidence,
    evaluate_county_evidence,
    normalize_category_value,
    normalize_county_claim,
)


def _claim(original: str, domain: str = "broker.example", **overrides) -> CountyClaim:
    data = {
        "original": original,
        "normalized": normalize_county_claim(original),
        "source_url": f"https://{domain}/listing",
        "source_domain": domain,
        "excerpt": original,
    }
    data.update(overrides)
    return CountyClaim.model_validate(data)


def test_missing_county_is_unresolved() -> None:
    result = evaluate_county_evidence([], search_county="Dallas County")
    assert result.evidence_status is CountyEvidenceStatus.UNRESOLVED
    assert result.value is None
    assert result.territory is TerritoryEligibility.UNKNOWN
    assert result.review_status is AttributionReviewStatus.NOT_REQUESTED
    assert result.search_county == "Dallas County"


def test_malformed_claims_preserved_without_invention() -> None:
    for malformed in ("", "Dallas", "   ", "County", "12345"):
        assert normalize_county_claim(malformed) is None
    assert normalize_county_claim("  Hunt   County, TX ") == "hunt county"
    result = evaluate_county_evidence([_claim("Dallas"), _claim("")])
    assert result.evidence_status is CountyEvidenceStatus.UNRESOLVED
    assert [claim.original for claim in result.claims] == ["", "Dallas"]
    assert all(claim.normalized is None for claim in result.claims)
    assert result.value is None


def test_single_uncorroborated_claim_stays_observed() -> None:
    result = evaluate_county_evidence([_claim("Hunt County")])
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED
    assert result.value is None
    assert result.territory is TerritoryEligibility.UNKNOWN


def test_independent_corroboration() -> None:
    result = evaluate_county_evidence(
        [_claim("Hunt County", "broker.example"), _claim("Hunt County", "news.example")]
    )
    assert result.evidence_status is CountyEvidenceStatus.CORROBORATED
    assert result.value is None  # corroborated is not verified


def test_syndicated_duplicates_not_independent() -> None:
    result = evaluate_county_evidence(
        [
            _claim("Hunt County", "a.example", syndication_id="wire-1"),
            _claim("Hunt County", "b.example", syndication_id="wire-1"),
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED
    same_domain = evaluate_county_evidence(
        [_claim("Hunt County", "a.example"), _claim("Hunt County", "a.example")]
    )
    assert same_domain.evidence_status is CountyEvidenceStatus.OBSERVED


def test_authoritative_label_alone_cannot_verify() -> None:
    result = evaluate_county_evidence(
        [
            _claim(
                "Hunt County",
                "appraisal.example",
                authority="authoritative",
                property_match="same street address on parcel record",
            )
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED
    assert result.value is None
    assert result.territory is TerritoryEligibility.UNKNOWN
    assert result.claims[0].authority == "authoritative"
    assert result.claims[0].property_match == "same street address on parcel record"


def test_fabricated_match_and_parcel_cannot_verify() -> None:
    result = evaluate_county_evidence(
        [
            _claim(
                "Hunt County",
                "dallascounty.example",
                authority="authoritative",
                property_match="parcel R-12345-678, 100 Main St",
                excerpt="Parcel R-12345-678 places 100 Main St in Hunt County",
            )
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED
    assert result.value is None
    assert result.territory is TerritoryEligibility.UNKNOWN


def test_government_looking_host_cannot_verify() -> None:
    result = evaluate_county_evidence(
        [_claim("Hunt County", "huntcounty.gov.example")]
    )
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED
    assert result.value is None


def test_authority_requires_property_match() -> None:
    result = evaluate_county_evidence(
        [_claim("Hunt County", "gov.example", authority="authoritative", property_match="")]
    )
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED
    assert result.value is None


def test_conflicting_evidence_preserved() -> None:
    result = evaluate_county_evidence(
        [_claim("Hunt County", "a.example"), _claim("Rockwall County", "b.example")]
    )
    assert result.evidence_status is CountyEvidenceStatus.CONTRADICTORY
    assert result.conflicting_values == ["hunt county", "rockwall county"]
    assert result.value is None
    assert len(result.claims) == 2


def test_authoritative_conflict_preserved_without_winner() -> None:
    result = evaluate_county_evidence(
        [
            _claim("Hunt County", "a.example", authority="authoritative", property_match="parcel A"),
            _claim("Rockwall County", "b.example", authority="authoritative", property_match="parcel B"),
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.CONTRADICTORY
    assert result.value is None


def test_labeled_claim_conflict_preserved_without_winner() -> None:
    result = evaluate_county_evidence(
        [
            _claim("Hunt County", "a.example", authority="authoritative", property_match="parcel record"),
            _claim("Rockwall County", "b.example"),
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.CONTRADICTORY
    assert result.value is None
    assert result.conflicting_values == ["hunt county", "rockwall county"]
    assert len(result.claims) == 2


def test_city_name_alone_never_establishes_county() -> None:
    for city in ("Frisco", "Grand Prairie", "Royse City"):
        result = evaluate_county_evidence([_claim(city)])
        assert result.evidence_status is CountyEvidenceStatus.UNRESOLVED
        assert result.value is None


def test_search_county_never_becomes_value() -> None:
    result = evaluate_county_evidence([], search_county="Hunt County")
    assert result.value is None
    observed = evaluate_county_evidence(
        [_claim("Rockwall County")], search_county="Hunt County"
    )
    assert observed.value is None
    assert observed.search_county == "Hunt County"


def test_out_of_territory_evidence_preserved_without_verification() -> None:
    single = evaluate_county_evidence(
        [
            _claim(
                "Harris County",
                "appraisal.example",
                authority="authoritative",
                property_match="same street address on parcel record",
            )
        ]
    )
    assert single.evidence_status is CountyEvidenceStatus.OBSERVED
    assert single.value is None
    assert single.territory is TerritoryEligibility.UNKNOWN
    assert single.claims[0].original == "Harris County"
    corroborated = evaluate_county_evidence(
        [
            _claim("Harris County", "a.example"),
            _claim("Harris County", "b.example"),
        ]
    )
    assert corroborated.evidence_status is CountyEvidenceStatus.CORROBORATED
    assert corroborated.value is None
    assert corroborated.territory is TerritoryEligibility.UNKNOWN


def test_evaluation_is_order_independent() -> None:
    claims = [
        _claim("Hunt County", "b.example"),
        _claim("Rockwall County", "a.example"),
        _claim(""),
    ]
    forward = evaluate_county_evidence(claims).model_dump(mode="json")
    backward = evaluate_county_evidence(list(reversed(claims))).model_dump(mode="json")
    assert forward == backward


def test_unknown_category() -> None:
    result = evaluate_category_evidence([], search_category="high_rise")
    assert result.status is CategoryEvidenceStatus.UNKNOWN
    assert result.verified is None
    assert result.search_category == "high_rise"
    assert normalize_category_value("not a category") is None
    assert normalize_category_value("High-Rise") == "high_rise"


def test_conflicting_category_evidence() -> None:
    result = evaluate_category_evidence(
        [
            CategoryObservation(value="high_rise", origin="listing_text"),
            CategoryObservation(value="warehouse_industrial", origin="osm_tags"),
        ]
    )
    assert result.status is CategoryEvidenceStatus.CONTRADICTORY
    assert result.conflicting_values == ["high_rise", "warehouse_industrial"]
    assert result.verified is None


def test_search_category_not_automatically_verified() -> None:
    result = evaluate_category_evidence(
        [CategoryObservation(value="high_rise", origin="search")],
        search_category="high_rise",
    )
    assert result.status is CategoryEvidenceStatus.OBSERVED
    assert result.verified is None


def test_independent_origins_verify_category() -> None:
    result = evaluate_category_evidence(
        [
            CategoryObservation(value="high_rise", origin="osm_tags"),
            CategoryObservation(value="high-rise", origin="listing_text"),
        ],
        search_category="high_rise",
    )
    assert result.status is CategoryEvidenceStatus.VERIFIED
    assert result.verified == "high_rise"


def test_other_category_preserved() -> None:
    result = evaluate_category_evidence(
        [CategoryObservation(value="other", origin="listing_text")]
    )
    assert result.status is CategoryEvidenceStatus.OBSERVED
    assert result.observed == "other"


def test_existing_models_accept_attribution_fields() -> None:
    from app.agents.gabriel.dossier import PropertyDossier
    from app.agents.gabriel.ranking import CountyEligibility, RankedCandidate

    candidate = RankedCandidate.model_validate({"key": "addr:x", "county": "county_unknown"})
    assert candidate.county_attribution is None
    assert candidate.category_attribution is None
    assert candidate.county is CountyEligibility.COUNTY_UNKNOWN
    county = evaluate_county_evidence([_claim("Hunt County", "a.example")])
    category = evaluate_category_evidence(
        [CategoryObservation(value="high_rise", origin="listing_text")]
    )
    filled = RankedCandidate.model_validate(
        {
            "key": "addr:x",
            "county_attribution": county.model_dump(mode="json"),
            "category_attribution": category.model_dump(mode="json"),
        }
    )
    assert filled.county_attribution is not None
    assert filled.county_attribution.evidence_status is CountyEvidenceStatus.OBSERVED
    dossier = PropertyDossier.model_validate({"key": "addr:x"})
    assert dossier.county_attribution is None
    assert dossier.category_attribution is None


def test_intake_assertion_alone_cannot_verify_category() -> None:
    result = evaluate_category_evidence(
        [CategoryObservation(value="high_rise", origin="intake")],
        search_category="high_rise",
    )
    assert result.status is CategoryEvidenceStatus.OBSERVED
    assert result.verified is None
    assert result.observed == "high_rise"  # preserved, not promoted


def test_intake_plus_single_listing_claim_cannot_verify_category() -> None:
    result = evaluate_category_evidence(
        [
            CategoryObservation(value="high_rise", origin="intake"),
            CategoryObservation(value="high_rise", origin="listing_text"),
        ],
        search_category="high_rise",
    )
    assert result.status is CategoryEvidenceStatus.OBSERVED
    assert result.verified is None


def test_two_evidentiary_origins_still_verify_category() -> None:
    result = evaluate_category_evidence(
        [
            CategoryObservation(value="warehouse_industrial", origin="provider"),
            CategoryObservation(value="warehouse_industrial", origin="listing_text"),
        ]
    )
    assert result.status is CategoryEvidenceStatus.VERIFIED
    assert result.verified == "warehouse_industrial"


def test_county_model_rejects_inconsistent_states() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CountyAttribution.model_validate({"evidence_status": "verified"})
    with pytest.raises(ValidationError):
        CountyAttribution.model_validate(
            {"evidence_status": "verified", "value": "Hunt County", "territory": "within_territory"}
        )
    with pytest.raises(ValidationError):
        CountyAttribution.model_validate(
            {"evidence_status": "observed", "value": "hunt county"}
        )
    with pytest.raises(ValidationError):
        CountyAttribution.model_validate(
            {"evidence_status": "corroborated", "territory": "within_territory"}
        )
    with pytest.raises(ValidationError):
        CountyAttribution.model_validate(
            {"evidence_status": "contradictory", "conflicting_values": ["hunt county"]}
        )
    valid = CountyAttribution.model_validate(
        {
            "evidence_status": "verified",
            "value": "hunt county",
            "territory": "within_territory",
        }
    )
    assert valid.value == "hunt county"


def test_category_model_rejects_inconsistent_states() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CategoryAttribution.model_validate({"status": "verified"})
    with pytest.raises(ValidationError):
        CategoryAttribution.model_validate({"status": "verified", "verified": "castle"})
    with pytest.raises(ValidationError):
        CategoryAttribution.model_validate({"status": "observed", "verified": "high_rise"})
    with pytest.raises(ValidationError):
        CategoryAttribution.model_validate(
            {"status": "contradictory", "conflicting_values": ["high_rise"]}
        )


def test_county_normalization_accepts_texas_names() -> None:
    assert normalize_county_claim("De Witt County") == "de witt county"
    assert normalize_county_claim("San Augustine County, TX") == "san augustine county"
    assert normalize_county_claim("St. Louis County") == "st. louis county"
    assert normalize_county_claim("El Paso County") == "el paso county"


def test_county_normalization_rejects_non_names() -> None:
    for malformed in ("12345 County", "12 County", "9th County", "County", "12345"):
        assert normalize_county_claim(malformed) is None
    result = evaluate_county_evidence([_claim("12345 County")])
    assert result.evidence_status is CountyEvidenceStatus.UNRESOLVED
    assert result.claims[0].original == "12345 County"
    assert result.claims[0].normalized is None


def test_attribution_details_representation_round_trips() -> None:
    county = evaluate_county_evidence(
        [_claim("Hunt County", "a.example"), _claim("Hunt County", "b.example")],
        search_county="Hunt County",
    )
    category = evaluate_category_evidence(
        [CategoryObservation(value="high_rise", origin="listing_text")]
    )
    details = {
        "county_attribution": county.model_dump(mode="json"),
        "category_attribution": category.model_dump(mode="json"),
    }
    restored_county = CountyAttribution.model_validate(json.loads(json.dumps(details))["county_attribution"])
    restored_category = CategoryAttribution.model_validate(json.loads(json.dumps(details))["category_attribution"])
    assert restored_county == county
    assert restored_category == category


def test_constructor_builds_non_authoritative_claim() -> None:
    from app.agents.gabriel.claims import build_county_claim

    claim = build_county_claim(
        mention="Hunt County",
        source_url="https://News.Example.COM:443/listing?id=7",
        excerpt="Hunt County treasure",
        observed_at="2026-10-27T12:00:00+00:00",
        round_no=2,
    )
    assert claim.original == "Hunt County"
    assert claim.normalized == "hunt county"
    assert claim.source_url == "https://News.Example.COM:443/listing?id=7"
    assert claim.source_domain == "news.example.com"
    assert claim.excerpt == "Hunt County treasure"
    assert claim.observed_at == "2026-10-27T12:00:00+00:00"
    assert claim.round_no == 2
    assert claim.authority == "listing"
    assert claim.property_match == ""
    assert claim.syndication_id is None


def test_constructor_preserves_malformed_mentions() -> None:
    from app.agents.gabriel.claims import build_county_claim

    claim = build_county_claim(mention="Frisco", source_url="https://example.com/x")
    assert claim.original == "Frisco"
    assert claim.normalized is None
    result = evaluate_county_evidence([claim])
    assert result.evidence_status is CountyEvidenceStatus.UNRESOLVED


def test_repeated_canonical_urls_form_one_voice() -> None:
    from app.agents.gabriel.claims import build_county_claim

    first = build_county_claim(
        mention="Hunt County", source_url="https://example.com/listing"
    )
    second = build_county_claim(
        mention="Hunt County", source_url="https://EXAMPLE.com:443/listing"
    )
    result = evaluate_county_evidence([first, second])
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED
    assert result.value is None


def test_same_host_pages_form_one_voice() -> None:
    from app.agents.gabriel.claims import build_county_claim

    result = evaluate_county_evidence(
        [
            build_county_claim(
                mention="Hunt County", source_url="https://example.com/a"
            ),
            build_county_claim(
                mention="Hunt County", source_url="https://example.com/b"
            ),
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED


def test_caller_labels_cannot_split_one_host() -> None:
    first = _claim("Hunt County", "a.example", syndication_id="voice-1")
    second = CountyClaim.model_validate(
        {**first.model_dump(), "syndication_id": "voice-2"}
    )
    result = evaluate_county_evidence([first, second])
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED


def test_url_identity_overrides_contradictory_domain_metadata() -> None:
    first = _claim("Hunt County", "voice-a.example")
    first = CountyClaim.model_validate(
        {**first.model_dump(), "source_url": "https://shared.example/a"}
    )
    second = _claim("Hunt County", "voice-b.example")
    second = CountyClaim.model_validate(
        {**second.model_dump(), "source_url": "https://shared.example/b"}
    )
    result = evaluate_county_evidence([first, second])
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED


def test_two_independent_hosts_corroborate_without_verifying() -> None:
    from app.agents.gabriel.claims import build_county_claim

    result = evaluate_county_evidence(
        [
            build_county_claim(
                mention="Hunt County", source_url="https://a.example/x"
            ),
            build_county_claim(
                mention="Hunt County", source_url="https://b.example/y"
            ),
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.CORROBORATED
    assert result.value is None
    assert result.territory is TerritoryEligibility.UNKNOWN


def test_unhosted_claims_form_no_voice() -> None:
    result = evaluate_county_evidence(
        [
            CountyClaim(original="Hunt County", normalized="hunt county"),
            CountyClaim(original="Hunt County", normalized="hunt county"),
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.OBSERVED


# Future official-record adapter verification obligations (2A.2a has no
# trusted producer, so these scenarios are documented, not executed):
#
# 1. A genuinely authenticated official parcel record establishes the
#    county of the matching property (VERIFIED + correct territory).
# 2. A validated geographic boundary determination establishes the
#    property's county (VERIFIED + correct territory).
# 3. Verified evidence outside the twelve-county pilot territory
#    remains preserved and classified outside_territory.
# 4. Conflicting authoritative determinations remain visible
#    (CONTRADICTORY, both preserved, no winner selected).
# 5. Fabricated records cannot independently authorize verification
#    (wrong host, address mismatch, county mismatch, or stale record
#    each independently prevent verification).
#
# No production bypass may be added merely to make these executable.
