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


def test_authoritative_property_specific_verification() -> None:
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
    assert result.evidence_status is CountyEvidenceStatus.VERIFIED
    assert result.value == "hunt county"
    assert result.territory is TerritoryEligibility.WITHIN_TERRITORY
    assert result.method == "authoritative property-specific determination"


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


def test_authoritative_verification_keeps_dissent_visible() -> None:
    result = evaluate_county_evidence(
        [
            _claim("Hunt County", "a.example", authority="authoritative", property_match="parcel record"),
            _claim("Rockwall County", "b.example"),
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.VERIFIED
    assert result.value == "hunt county"
    assert result.conflicting_values == ["rockwall county"]


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


def test_verified_out_of_territory_stays_verified() -> None:
    result = evaluate_county_evidence(
        [
            _claim(
                "Harris County",
                "appraisal.example",
                authority="authoritative",
                property_match="same street address on parcel record",
            )
        ]
    )
    assert result.evidence_status is CountyEvidenceStatus.VERIFIED
    assert result.value == "harris county"
    assert result.territory is TerritoryEligibility.OUTSIDE_TERRITORY


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
