"""Trusted evidence producer for the verification policy (B5-2A.2b-ii-b/2A).

Builds verification-policy inputs from research evidence collected by the
server-executed Brave adapter. This module establishes collection
provenance -- "these bytes came from an adapter call this server made" --
not verified property facts.

TRUST BOUNDARY: ``adapter_collected`` is stamped here only because the
sole production caller (``run_research``) invokes this builder over
results returned by ``execute_query`` in the same request. The builder
itself cannot authenticate its inputs: any other caller (dossiers,
investigations, tests over fabricated intelligence) must supply its own
provenance, and nothing here may be reused to launder caller-submitted
data into adapter-collected evidence.

Phase 2A is deliberately fail-closed:

- No publisher identities, listing ids, defining facts, or record
  types are fabricated. Missing fields stay missing.
- Identity attestation stays unaligned: grouping organizes evidence
  but never proves one physical building.
- Publisher claims are extracted attributions, never authenticated:
  they populate the policy's claimed (unauthenticated) originator
  tier and can satisfy no registry or independence gate by themselves.
- The production registry is empty, so no host is vetted.

Every evaluation therefore explains NOT_VERIFIED until later phases
supply genuine facts, attribution, and curated registry entries.
Deterministic; no I/O; no network.
"""

from __future__ import annotations

from app.agents.gabriel.attribution import (
    CategoryEvidenceStatus,
    CategoryObservation,
    evaluate_category_evidence,
)
from app.agents.gabriel.extractor import ResearchIntelligence, group_by_property
from app.agents.gabriel.ranking import PropertyRanking, RankedCandidate
from app.agents.gabriel.source_registry import PRODUCTION_REGISTRY, SourceRegistry
from app.agents.gabriel.verification import (
    CategoryVerification,
    IdentityAttestation,
    VerificationRequest,
    VoiceInput,
    verify_category,
)

# Origin label for extractor-derived category observations. Descriptive
# only: the mechanical evaluator treats every non-search/intake label
# identically, and labels never authenticate provenance.
_EXTRACTION_ORIGIN = "extracted"

# Fact keys the verification policy reads per claim. Within-voice
# conflicts on any other key are still dropped, but only relevant keys
# can mark the evaluated claim materially contradictory.
_CLAIM_FACT_KEYS = {
    "high_rise": frozenset(("levels", "height_m", "building", "designation")),
    "commercial_property": frozenset(("use",)),
    "warehouse_industrial": frozenset(("use",)),
}

# Proposed design specifications are never current-building facts.
_PROPOSED_BLOCKED_FACTS = ("levels", "height_m", "building", "designation")


def _member_observations(
    members: list[int], items: list,
) -> list[CategoryObservation]:
    """Category observations from grouped members' extracted properties."""
    observations: list[CategoryObservation] = []
    for index in members:
        item = items[index]
        for prop in item.properties:
            if prop.category is None:
                continue
            observations.append(
                CategoryObservation(
                    value=prop.category.value,
                    origin=_EXTRACTION_ORIGIN,
                    source_url=item.result_url or "",
                )
            )
    return observations


def _voice_facts(item) -> tuple[dict[str, str], set[str]]:
    """Map one member's fact observations to defining facts.

    Only single-property members contribute: facts from multi-property
    or property-less results cannot be attributed to this candidate.
    Conflicting values for one key are dropped (never a favorable
    pick); ambiguous use-status fails closed to proposed. Proposed
    construction excludes all current-building facts.
    """
    if len(item.properties) != 1:
        return {}, set()
    values: dict[str, set[str]] = {}
    for observation in item.building_facts:
        values.setdefault(observation.fact, set()).add(observation.value)
    facts: dict[str, str] = {}
    conflicts: set[str] = set()
    for key in sorted(values):
        options = values[key]
        if len(options) > 1:
            if key == "use_status":
                facts[key] = "proposed"
                continue
            conflicts.add(key)
            continue
        facts[key] = next(iter(options))
    if facts.get("use_status") == "proposed":
        for key in _PROPOSED_BLOCKED_FACTS:
            facts.pop(key, None)
    return facts, conflicts


def _voice_attribution(item) -> str | None:
    """Map one member's attribution claims to a claimed publisher.

    Only single-property members contribute. Absent claims yield no
    publisher; incompatible claims within one member are omitted
    rather than resolved arbitrarily. Distinct spellings are never
    merged: the returned string is an unauthenticated claim for the
    policy's claimed-originator tier, not an identity.
    """
    if len(item.properties) != 1:
        return None
    claimed = {
        (observation.publisher or "").strip()
        for observation in item.source_attributions
    } - {""}
    if len(claimed) != 1:
        return None
    return next(iter(claimed))


def build_candidate_verification(
    candidate: RankedCandidate,
    members: list[int],
    items: list,
    *,
    basis: str,
    registry: SourceRegistry | None = None,
) -> CategoryVerification:
    """Evaluate one ranked candidate against the verification policy.

    Voices carry the member result URLs with adapter-collected
    provenance plus eligible defining facts and at most one claimed
    publisher mapped from the member's own extracted observations: no
    listing ids, no record types. The claim under test is the
    mechanically observed
    category when the grouped evidence agrees on one; when it
    contradicts itself, the first conflicting value (deterministic) so
    the contradiction itself is evaluated rather than misreported as
    absent evidence; else empty (no recognized claim). Identity stays
    unaligned.
    """
    active = PRODUCTION_REGISTRY if registry is None else registry
    mechanical = evaluate_category_evidence(_member_observations(members, items))
    claim = mechanical.observed or ""
    if not claim and mechanical.status is CategoryEvidenceStatus.CONTRADICTORY:
        claim = mechanical.conflicting_values[0]
    relevant = _CLAIM_FACT_KEYS.get(claim, frozenset())
    voices = []
    fact_conflict = False
    for index in members:
        facts, conflicts = _voice_facts(items[index])
        if conflicts & relevant:
            fact_conflict = True
        voices.append(
            VoiceInput(
                url=items[index].result_url or "",
                provenance="adapter_collected",
                publisher=_voice_attribution(items[index]),
                defining_facts=facts,
            )
        )
    disputes = len(candidate.identity_disputes)
    note = (
        "Phase 2A conservative rule: grouping basis "
        f"{basis!r} organizes evidence but does not verify that members "
        f"describe one physical building (observed identity disputes: {disputes})."
    )
    request = VerificationRequest(
        claim=claim,
        voices=voices,
        identity=IdentityAttestation(group_key=candidate.key, aligned=False, note=note),
        material_contradiction=(
            mechanical.status is CategoryEvidenceStatus.CONTRADICTORY or fact_conflict
        ),
        registry=active,
    )
    return verify_category(request)


def build_candidate_verifications(
    ranking: PropertyRanking,
    intel: ResearchIntelligence,
    registry: SourceRegistry | None = None,
) -> dict[str, CategoryVerification]:
    """Evaluate every ranked candidate (pure; ranking untouched).

    Group membership is recomputed from the supplied intelligence with
    the same grouping rules ranking used. Candidates without a matching
    group evaluate with no voices. Deterministic.
    """
    items = intel.results
    groups = {group.key: group for group in group_by_property(items)}
    verdicts: dict[str, CategoryVerification] = {}
    for candidate in ranking.candidates:
        group = groups.get(candidate.key)
        members = list(group.members) if group is not None else []
        basis = group.basis if group is not None else "unknown"
        verdicts[candidate.key] = build_candidate_verification(
            candidate, members, items, basis=basis, registry=registry
        )
    return verdicts


__all__ = [
    "build_candidate_verification",
    "build_candidate_verifications",
]
