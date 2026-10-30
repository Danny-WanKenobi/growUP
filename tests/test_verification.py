"""Isolated verification-policy tests (synthetic fixtures, no network)."""

import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.gabriel.source_registry import (
    PRODUCTION_REGISTRY,
    RegistryEntry,
    SourceClass,
    SourceRegistry,
)
from app.agents.gabriel.verification import (
    CategoryVerification,
    IdentityAttestation,
    VerificationFailureReason as R,
    VerificationOutcome,
    VerificationPathway,
    VerificationRequest,
    VoiceInput,
    verify_category,
)

USE_FACTS = {"use": "office"}
TOWER_FACTS = {"levels": "12"}


def _registry() -> SourceRegistry:
    return SourceRegistry(
        entries={
            "broker-a.example": RegistryEntry(
                hostname="broker-a.example",
                source_class=SourceClass.VETTED_COMMERCIAL,
                outlet_name="Broker A",
                publisher="Broker A",
            ),
            "broker-b.example": RegistryEntry(
                hostname="broker-b.example",
                source_class=SourceClass.VETTED_COMMERCIAL,
                outlet_name="Broker B",
                publisher="Broker B",
            ),
            "govparcel.example": RegistryEntry(
                hostname="govparcel.example",
                source_class=SourceClass.AUTHORITATIVE,
                outlet_name="County Appraisal",
                publisher="County Appraisal",
                record_types=["parcel"],
            ),
            "news.example": RegistryEntry(
                hostname="news.example",
                source_class=SourceClass.SECONDARY,
                outlet_name="News Outlet",
                publisher="News Outlet",
            ),
            "novpub-a.example": RegistryEntry(
                hostname="novpub-a.example",
                source_class=SourceClass.VETTED_COMMERCIAL,
            ),
            "novpub-b.example": RegistryEntry(
                hostname="novpub-b.example",
                source_class=SourceClass.VETTED_COMMERCIAL,
            ),
            "platform.example": RegistryEntry(
                hostname="platform.example",
                source_class=SourceClass.VETTED_COMMERCIAL,
                outlet_name="Listing Platform",
            ),
            "samepub-a.example": RegistryEntry(
                hostname="samepub-a.example",
                source_class=SourceClass.VETTED_COMMERCIAL,
                outlet_name="Same Publisher A",
                publisher="Same Publisher",
            ),
            "samepub-b.example": RegistryEntry(
                hostname="samepub-b.example",
                source_class=SourceClass.VETTED_COMMERCIAL,
                outlet_name="Same Publisher B",
                publisher="Same Publisher",
            ),
            "press.example": RegistryEntry(
                hostname="press.example",
                source_class=SourceClass.SECONDARY,
                outlet_name="Press Outlet",
                publisher="Press Outlet",
            ),
        }
    )


def _identity() -> IdentityAttestation:
    return IdentityAttestation(group_key="addr:123 main", aligned=True)


def _voice(url: str, facts: dict | None = None, **kwargs) -> VoiceInput:  # type: ignore[no-untyped-def]
    params: dict = {"url": url, "provenance": "adapter_collected", "defining_facts": dict(USE_FACTS) if facts is None else facts}
    params.update(kwargs)
    return VoiceInput(**params)


def _request(claim: str, voices: list, **kwargs) -> VerificationRequest:  # type: ignore[no-untyped-def]
    params: dict = {"claim": claim, "voices": voices, "identity": _identity(), "registry": _registry()}
    params.update(kwargs)
    return VerificationRequest(**params)


def test_no_recognized_evidence() -> None:
    result = verify_category(_request("commercial_property", []))
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert result.reasons == [R.NO_RECOGNIZED_EVIDENCE]
    result = verify_category(_request("strip_mall", [_voice("https://broker-a.example/x")]))
    assert result.reasons == [R.NO_RECOGNIZED_EVIDENCE]


def test_one_unvetted_source() -> None:
    result = verify_category(_request("commercial_property", [_voice("https://unknown.example/x")]))
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert result.reasons == [R.UNVETTED_SOURCES_ONLY, R.AUTHORITY_INSUFFICIENT]


def test_two_unvetted_hosts() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://unknown-a.example/x"), _voice("https://unknown-b.example/y")],
        )
    )
    assert result.reasons == [R.UNVETTED_SOURCES_ONLY, R.AUTHORITY_INSUFFICIENT]


def test_one_vetted_listing_insufficient() -> None:
    result = verify_category(_request("commercial_property", [_voice("https://broker-a.example/x")]))
    assert result.reasons == [R.SINGLE_VOICE_ONLY, R.AUTHORITY_INSUFFICIENT]


def test_two_distinguishable_vetted_sources_verify() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")],
        )
    )
    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.pathway is VerificationPathway.CREDIBLE_CORROBORATION
    assert result.reasons == []
    assert len(result.voices) == 2
    assert "broker-a.example" in result.rationale and "broker-b.example" in result.rationale


def test_unresolved_independence_withholds_verification() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://novpub-a.example/x"), _voice("https://novpub-b.example/y")],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.INDEPENDENCE_UNCERTAIN in result.reasons


def test_authoritative_record_verifies() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://govparcel.example/parcel/9", record_type="parcel")],
        )
    )
    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.pathway is VerificationPathway.AUTHORITY
    assert len(result.voices) == 1


def test_authoritative_record_without_facts_fails() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://govparcel.example/parcel/9", facts={}, record_type="parcel")],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert result.reasons == [R.MISSING_DEFINING_FACTS]


def test_brokerage_plus_authority_verifies_via_authority() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://broker-a.example/x"),
                _voice("https://govparcel.example/parcel/9", record_type="parcel"),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.pathway is VerificationPathway.AUTHORITY


def test_material_contradiction_blocks() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")],
            material_contradiction=True,
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MATERIAL_CONTRADICTION in result.reasons
    assert "does not mean the claim is false" in result.rationale


def test_identity_mismatch_blocks() -> None:
    request = _request(
        "commercial_property",
        [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")],
    )
    request.identity.aligned = False
    result = verify_category(request)
    assert R.IDENTITY_UNRESOLVED in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED


def test_caller_only_evidence_blocked() -> None:
    voices = [
        VoiceInput(url="https://broker-a.example/x", provenance="caller_submitted", defining_facts=dict(USE_FACTS)),
        VoiceInput(url="https://broker-b.example/y", provenance="caller_submitted", defining_facts=dict(USE_FACTS)),
    ]
    result = verify_category(_request("commercial_property", voices))
    assert result.reasons == [R.CLIENT_ASSERTION_ONLY, R.AUTHORITY_INSUFFICIENT]


def test_known_syndicated_copies_count_once() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://broker-a.example/x", originating_id="ad-1"),
                _voice("https://broker-b.example/y", originating_id="ad-1"),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.SINGLE_VOICE_ONLY in result.reasons


def test_missing_urls_block() -> None:
    result = verify_category(
        _request("commercial_property", [_voice("", facts=dict(USE_FACTS)), _voice("")])
    )
    assert R.HOSTLESS_EVIDENCE in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED


def test_missing_defining_facts_block() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://broker-a.example/x", facts={}), _voice("https://broker-b.example/y", facts={})],
        )
    )
    assert R.MISSING_DEFINING_FACTS in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED


def test_unknown_registry_host() -> None:
    registry = _registry()
    assert registry.lookup("https://nope.example/x") is None
    assert registry.source_class_of("https://nope.example/x") is SourceClass.UNVETTED


def test_production_registry_empty() -> None:
    assert PRODUCTION_REGISTRY.entries == {}


def test_no_automatic_gov_authority() -> None:
    assert _registry().lookup("https://county.gov/parcel/1") is None
    assert PRODUCTION_REGISTRY.source_class_of("https://county.gov/parcel/1") is SourceClass.UNVETTED


def test_same_host_duplicates_count_once() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://broker-a.example/x"), _voice("https://broker-a.example/y")],
        )
    )
    assert R.SINGLE_VOICE_ONLY in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED


def test_distinct_hosts_one_advertisement_count_once() -> None:
    result = verify_category(
        _request(
            "warehouse_industrial",
            [
                _voice("https://broker-a.example/x", facts={"use": "warehouse"}, originating_id="ad-9"),
                _voice("https://news.example/y", facts={"use": "warehouse"}, originating_id="ad-9"),
            ],
        )
    )
    assert R.SINGLE_VOICE_ONLY in result.reasons


def test_listing_ids_alone_cannot_establish_independence() -> None:
    # Distinct listing ids without established publishers cannot verify,
    # on any host combination. Excerpts are not policy inputs at all, so
    # excerpt content could not change this verdict either way.
    result = verify_category(
        _request(
            "high_rise",
            [
                _voice("https://novpub-a.example/x", facts=dict(TOWER_FACTS), originating_id="t-1"),
                _voice("https://novpub-b.example/y", facts=dict(TOWER_FACTS), originating_id="t-2"),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.INDEPENDENCE_UNCERTAIN in result.reasons


def test_excerpts_play_no_role_in_common_origination() -> None:
    # Differing excerpts cannot rescue a known common origination.
    result = verify_category(
        _request(
            "high_rise",
            [
                _voice("https://broker-a.example/x", facts=dict(TOWER_FACTS), originating_id="t-1"),
                _voice("https://broker-b.example/y", facts=dict(TOWER_FACTS), originating_id="t-1"),
            ],
        )
    )
    assert R.SINGLE_VOICE_ONLY in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED


def test_success_model_validation() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")],
        )
    )
    assert result.pathway is VerificationPathway.CREDIBLE_CORROBORATION
    assert result.reasons == []
    assert result.rationale != ""


def test_failure_model_validation() -> None:
    result = verify_category(_request("commercial_property", []))
    assert result.pathway is None
    assert result.reasons != []
    with pytest.raises(ValidationError):
        CategoryVerification(outcome=VerificationOutcome.NOT_VERIFIED, rationale="x")


def test_invalid_combinations_rejected() -> None:
    with pytest.raises(ValidationError):
        CategoryVerification(
            outcome=VerificationOutcome.VERIFIED,
            pathway=VerificationPathway.AUTHORITY,
            reasons=[R.SINGLE_VOICE_ONLY],
            rationale="x",
            voices=[],
        )
    with pytest.raises(ValidationError):
        CategoryVerification(
            outcome=VerificationOutcome.NOT_VERIFIED,
            pathway=VerificationPathway.AUTHORITY,
            reasons=[R.SINGLE_VOICE_ONLY],
            rationale="x",
        )
    with pytest.raises(ValidationError):
        CategoryVerification(
            outcome=VerificationOutcome.VERIFIED,
            pathway=VerificationPathway.CREDIBLE_CORROBORATION,
            rationale="x",
            voices=[],
        )


def test_deterministic_output() -> None:
    voices = [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")]
    first = verify_category(_request("commercial_property", voices))
    shuffled = list(voices)
    random.Random(11).shuffle(shuffled)
    second = verify_category(_request("commercial_property", shuffled))
    assert first == second


def test_serialization_round_trip() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")],
        )
    )
    assert CategoryVerification.model_validate(result.model_dump()) == result


def test_historical_alias_compatible() -> None:
    result = verify_category(
        _request(
            "downtown_commercial",
            [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")],
        )
    )
    assert result.outcome is VerificationOutcome.VERIFIED
    assert "commercial_property" in result.rationale


def test_mechanical_semantics_unchanged() -> None:
    from app.agents.gabriel.attribution import (
        CategoryEvidenceStatus,
        CategoryObservation,
        evaluate_category_evidence,
    )

    result = evaluate_category_evidence(
        [
            CategoryObservation(value="high_rise", origin="osm_tags"),
            CategoryObservation(value="high_rise", origin="listing_text"),
        ]
    )
    assert result.status is CategoryEvidenceStatus.OBSERVED
    assert result.verified is None


def test_no_network_activity() -> None:
    root = Path(__file__).resolve().parent.parent / "app" / "agents" / "gabriel"
    for name in ("verification.py", "source_registry.py"):
        text = (root / name).read_text(encoding="utf-8")
        for token in ("import socket", "import requests", "import httpx", "http.client", "urllib.request", "urlopen("):
            assert token not in text


def test_same_host_ids_alone_cannot_verify() -> None:
    # One platform host, two listing ids, unknown publishers: ids identify
    # advertisements but do not establish independent origination.
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://platform.example/a/1", originating_id="ad-1"),
                _voice("https://platform.example/b/2", originating_id="ad-2"),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.INDEPENDENCE_UNCERTAIN in result.reasons


def test_same_host_modeled_originators_verify_policy_only() -> None:
    # Same platform host with distinct modeled originator attributions:
    # eligible under the isolated policy (documented as unauthenticated;
    # production trust awaits adapter-extracted attribution).
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://platform.example/a/1", originating_id="ad-1", publisher="Broker A"),
                _voice("https://platform.example/b/2", originating_id="ad-2", publisher="Broker B"),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.pathway is VerificationPathway.CREDIBLE_CORROBORATION
    assert len(result.voices) == 2


def test_same_modeled_originator_counts_once() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://novpub-a.example/x", originating_id="ad-1", publisher="Broker A"),
                _voice("https://novpub-b.example/y", originating_id="ad-2", publisher="Broker A"),
            ],
        )
    )
    assert R.SINGLE_VOICE_ONLY in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED


def test_registry_voice_publisher_disagreement_blocks() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://broker-a.example/x", publisher="Someone Else"),
                _voice("https://broker-b.example/y"),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.INDEPENDENCE_UNCERTAIN in result.reasons


def test_different_hosts_same_publisher_count_once() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://samepub-a.example/x", originating_id="ad-1"),
                _voice("https://samepub-b.example/y", originating_id="ad-2"),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.SINGLE_VOICE_ONLY in result.reasons


def test_same_host_unknown_basis_stays_single_voice() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://novpub-a.example/x"), _voice("https://novpub-a.example/y")],
        )
    )
    assert R.SINGLE_VOICE_ONLY in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED


def test_conflicting_publisher_one_advertisement_noted() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice("https://broker-a.example/x", originating_id="ad-1"),
                _voice("https://broker-b.example/y", originating_id="ad-1"),
            ],
        )
    )
    assert R.SINGLE_VOICE_ONLY in result.reasons
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert any("conflicting publisher metadata" in note for note in result.limitations)


def test_exact_duplicate_observations_collapse() -> None:
    voice = _voice("https://broker-a.example/x", originating_id="ad-1")
    first = verify_category(_request("commercial_property", [voice, voice]))
    assert R.SINGLE_VOICE_ONLY in first.reasons
    again = verify_category(_request("commercial_property", [voice, voice]))
    assert first == again


def test_reversed_order_independence_case() -> None:
    voices = [
        _voice("https://platform.example/a/1", originating_id="ad-1"),
        _voice("https://platform.example/b/2", originating_id="ad-2"),
    ]
    first = verify_category(_request("commercial_property", voices))
    second = verify_category(_request("commercial_property", list(reversed(voices))))
    assert first == second
    # Listing-id-only same-host evidence cannot verify, but the failure
    # itself must be order-independent.
    assert first.outcome is VerificationOutcome.NOT_VERIFIED


def test_two_secondaries_insufficient() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://news.example/x"), _voice("https://press.example/y")],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.SECONDARY_ONLY in result.reasons


def test_wrong_record_type_excluded() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://govparcel.example/tax/9", record_type="tax-roll")],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert result.reasons == [R.AUTHORITY_INSUFFICIENT]


def test_uncertain_contradiction_blocks() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [_voice("https://broker-a.example/x"), _voice("https://broker-b.example/y")],
            contradiction_uncertain=True,
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MATERIAL_CONTRADICTION in result.reasons


def test_registry_rejects_noncanonical_hostname() -> None:
    with pytest.raises(ValidationError):
        RegistryEntry(hostname="Broker-A.Example", source_class=SourceClass.VETTED_COMMERCIAL)
    with pytest.raises(ValidationError):
        RegistryEntry(hostname="broker-a.example.", source_class=SourceClass.VETTED_COMMERCIAL)


def test_registry_lookup_canonicalizes() -> None:
    registry = _registry()
    assert registry.lookup("HTTPS://BROKER-A.EXAMPLE:443/x") is not None
    assert registry.lookup("https://broker-a.example./x") is not None
    assert registry.source_class_of("HTTPS://BROKER-A.EXAMPLE:443/x") is SourceClass.VETTED_COMMERCIAL


def test_mixed_order_facts_keep_their_own_source_credibility() -> None:
    # Input order is deliberately hostile to alphabetical host sorting:
    # the two unvetted listings carry facts, the alphabetically-first
    # vetted listings carry none. Facts must not slide onto vetted
    # records and manufacture two qualifying voices (false VERIFIED).
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice(
                    "https://mango.example/a",
                    facts=dict(USE_FACTS),
                    publisher="Mango Listings",
                    originating_id="ad-1",
                ),
                _voice(
                    "https://zebra.example/b",
                    facts=dict(USE_FACTS),
                    publisher="Zebra Listings",
                    originating_id="ad-2",
                ),
                _voice(
                    "https://broker-a.example/c",
                    facts={},
                    publisher="Broker A",
                    originating_id="ad-3",
                ),
                _voice(
                    "https://broker-b.example/d",
                    facts={},
                    publisher="Broker B",
                    originating_id="ad-4",
                ),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert result.pathway is None
    assert R.MISSING_DEFINING_FACTS in result.reasons


def test_office_use_cannot_verify_warehouse_claim() -> None:
    result = verify_category(
        _request(
            "warehouse_industrial",
            [
                _voice(
                    "https://broker-a.example/x",
                    facts={"use": "office"},
                    publisher="Broker A",
                    originating_id="ad-1",
                ),
                _voice(
                    "https://broker-b.example/y",
                    facts={"use": "office"},
                    publisher="Broker B",
                    originating_id="ad-2",
                ),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS in result.reasons
    assert result.pathway is None


def test_warehouse_use_cannot_verify_commercial_claim() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice(
                    "https://broker-a.example/x",
                    facts={"use": "warehouse"},
                    publisher="Broker A",
                    originating_id="ad-1",
                ),
                _voice(
                    "https://broker-b.example/y",
                    facts={"use": "warehouse"},
                    publisher="Broker B",
                    originating_id="ad-2",
                ),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS in result.reasons
    assert result.pathway is None


def test_unrecognized_use_fails_closed() -> None:
    result = verify_category(
        _request(
            "commercial_property",
            [
                _voice(
                    "https://broker-a.example/x",
                    facts={"use": "residential"},
                    publisher="Broker A",
                    originating_id="ad-1",
                ),
                _voice(
                    "https://broker-b.example/y",
                    facts={"use": "residential"},
                    publisher="Broker B",
                    originating_id="ad-2",
                ),
            ],
        )
    )
    assert result.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS in result.reasons
    assert result.pathway is None
