"""Explainable category-verification policy (isolated, pure, unwired).

Decides whether supplied property-category evidence satisfies the
AUTHORITY or CREDIBLE_CORROBORATION pathway and explains why the claim
is VERIFIED or NOT VERIFIED. Deterministic; no I/O; no network.

TRUST BOUNDARY: this module evaluates supplied facts and metadata. It
does not retrieve, authenticate, or independently verify source
artifacts, and a successful evaluation here is NOT a trusted production
verification. Trusted verification additionally requires evidence built
by executed source adapters within authorized wiring -- which does not
exist yet. There are no production callers; tests only.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.agents.gabriel.attribution import normalize_category_value
from app.agents.gabriel.source_identity import canonical_hostname
from app.agents.gabriel.source_registry import SourceClass, SourceRegistry
from app.agents.gabriel.sources.overpass import (
    _HIGH_RISE_HEIGHT_M,
    _HIGH_RISE_LEVELS,
)

_PUBLISHER_UNKNOWN = "unknown-publisher"


class VerificationOutcome(str, Enum):
    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"


class VerificationPathway(str, Enum):
    AUTHORITY = "authority"
    CREDIBLE_CORROBORATION = "credible_corroboration"


class VerificationFailureReason(str, Enum):
    NO_RECOGNIZED_EVIDENCE = "no_recognized_evidence"
    SINGLE_VOICE_ONLY = "single_voice_only"
    UNVETTED_SOURCES_ONLY = "unvetted_sources_only"
    SECONDARY_ONLY = "secondary_only"
    HOSTLESS_EVIDENCE = "hostless_evidence"
    CLIENT_ASSERTION_ONLY = "client_assertion_only"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    INDEPENDENCE_UNCERTAIN = "independence_uncertain"
    MATERIAL_CONTRADICTION = "material_contradiction"
    MISSING_DEFINING_FACTS = "missing_defining_facts"
    AUTHORITY_INSUFFICIENT = "authority_insufficient"
    REGISTRY_UNCONFIGURED = "registry_unconfigured"


class VoiceInput(BaseModel):
    """One supplied evidence voice. Fields are claims, not proof."""

    url: str = ""
    # Originator attribution modeled as supplied evidence (e.g. adapter-
    # extracted "listed by" data in the future). Documented as
    # unauthenticated: the isolated policy evaluates it, never verifies it.
    publisher: str | None = None
    originating_id: str | None = None  # listing/advertisement identifier
    listing_ref: str | None = None
    record_type: str | None = None  # authority path: kind of official record
    provenance: Literal["adapter_collected", "caller_submitted"] = "caller_submitted"
    defining_facts: dict[str, str] = Field(default_factory=dict)


class IdentityAttestation(BaseModel):
    """Caller-supplied identity assessment (evaluated, never assumed)."""

    group_key: str = ""
    aligned: bool = False
    note: str = ""


class VerificationRequest(BaseModel):
    """Complete policy input. Not proof of authenticity."""

    claim: str = ""
    voices: list[VoiceInput] = Field(default_factory=list)
    identity: IdentityAttestation = Field(default_factory=IdentityAttestation)
    material_contradiction: bool = False
    contradiction_uncertain: bool = False
    contradiction_note: str = ""
    registry: SourceRegistry = Field(default_factory=SourceRegistry)


class VerificationVoice(BaseModel):
    """One input voice with resolved host and class (audit trail)."""

    url: str = ""
    host: str = ""
    publisher: str | None = None
    source_class: SourceClass = SourceClass.UNVETTED
    listing_ref: str | None = None
    originating_id: str | None = None


class CategoryVerification(BaseModel):
    """Policy verdict with deterministic explanation."""

    outcome: VerificationOutcome
    pathway: VerificationPathway | None = None
    reasons: list[VerificationFailureReason] = Field(default_factory=list)
    rationale: str = ""
    limitations: list[str] = Field(default_factory=list)
    voices: list[VerificationVoice] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistent(self) -> CategoryVerification:
        if self.outcome is VerificationOutcome.VERIFIED:
            if self.pathway is None:
                raise ValueError("verified outcome requires a pathway")
            if self.reasons:
                raise ValueError("verified outcome must not carry failure reasons")
            if not self.rationale:
                raise ValueError("verified outcome requires a rationale")
            if not self.voices:
                raise ValueError("verified outcome requires qualifying voices")
            if self.pathway is VerificationPathway.CREDIBLE_CORROBORATION and len(self.voices) < 2:
                raise ValueError("corroboration pathway requires two voices")
        else:
            if self.pathway is not None:
                raise ValueError("unverified outcome must not carry a pathway")
            if not self.reasons:
                raise ValueError("unverified outcome requires a failure reason")
            if not self.rationale:
                raise ValueError("unverified outcome requires a rationale")
        return self


def _facts_support(claim: str, facts: dict[str, str]) -> tuple[bool, bool]:
    """Whether supplied facts establish the claim; second flag marks unknown use-status.

    High-rise thresholds reuse the established OSM classification rules;
    nothing here invents a threshold. Malformed numerics fail closed.
    """
    if claim == "high_rise":
        try:
            levels = int(str(facts.get("levels", "")).strip())
        except ValueError:
            levels = 0
        try:
            height = float(str(facts.get("height_m", "")).strip())
        except ValueError:
            height = 0.0
        building = str(facts.get("building", "")).strip().lower()
        designation = str(facts.get("designation", "")).strip().lower().replace("-", "_")
        supported = (
            levels >= _HIGH_RISE_LEVELS
            or height >= _HIGH_RISE_HEIGHT_M
            or building == "skyscraper"
            or designation in ("high_rise", "skyscraper")
        )
        return supported, False
    if claim in ("commercial_property", "warehouse_industrial"):
        use = str(facts.get("use", "")).strip()
        if not use:
            return False, False  # zoning or bare assertions alone are insufficient
        status = str(facts.get("use_status", "")).strip().lower()
        if status == "proposed":
            return False, False
        return True, status != "current"
    return False, False


def _resolve_voices(request: VerificationRequest) -> list[VerificationVoice]:
    resolved = []
    for voice in request.voices:
        host = canonical_hostname(voice.url) if voice.url else ""
        resolved.append(
            VerificationVoice(
                url=voice.url,
                host=host,
                publisher=voice.publisher,
                source_class=request.registry.source_class_of(voice.url),
                listing_ref=voice.listing_ref,
                originating_id=voice.originating_id,
            )
        )
    resolved.sort(key=lambda v: (v.host, v.url, v.publisher or ""))
    return resolved


def _record_type_ok(voice: VoiceInput, registry: SourceRegistry) -> bool:
    """Whether the voice's record type satisfies its registry entry, if any."""
    entry = registry.lookup(voice.url)
    record_types = entry.record_types if entry is not None else []
    if not record_types:
        return True
    return (voice.record_type or "") in record_types


def _origin_key(voice: VoiceInput, resolved: VerificationVoice, registry: SourceRegistry) -> tuple[str, str]:
    """Distinguishability basis: registry publisher, modeled originator, else unknown.

    Listing ids identify advertisements; they never establish publisher
    independence (shared ids are collapsed before this runs). A modeled
    per-voice originator counts as supplied evidence only -- the isolated
    policy evaluates it and documents it as unauthenticated. Unknown
    publishers keep the listing id in the key so distinct advertisements
    report uncertain independence instead of a confirmed single voice.
    """
    publisher = registry.publisher_of(voice.url)
    if publisher:
        return ("publisher", publisher)
    claimed = (voice.publisher or "").strip()
    if claimed:
        return ("originator", claimed)
    listing_id = (voice.originating_id or "").strip()
    if listing_id:
        return ("unknown", f"{resolved.host}#{listing_id}")
    return ("unknown", resolved.host)


def _effective_indices(
    eligible: list[tuple[VoiceInput, VerificationVoice]],
    registry: SourceRegistry,
) -> tuple[list[int], bool]:
    """Collapse eligible voices to effective verification voices.

    Only known copies of one originating advertisement merge (shared
    listing id, first occurrence kept). Shared hostnames never collapse
    voices by themselves. Returns the surviving indices plus whether
    merged copies carried conflicting registry-publisher metadata --
    reported as a limitation, never counted as independence.
    Deterministic.
    """
    seen_ids: dict[str, int] = {}
    final: list[int] = []
    conflict = False
    for index in range(len(eligible)):
        listing_id = (eligible[index][0].originating_id or "").strip()
        if listing_id:
            if listing_id in seen_ids:
                kept_pub = registry.publisher_of(eligible[seen_ids[listing_id]][0].url)
                drop_pub = registry.publisher_of(eligible[index][0].url)
                if kept_pub and drop_pub and kept_pub != drop_pub:
                    conflict = True
                continue
            seen_ids[listing_id] = index
        final.append(index)
    return final, conflict


def verify_category(request: VerificationRequest) -> CategoryVerification:
    """Evaluate supplied evidence against both pathways (pure, deterministic)."""
    claim = normalize_category_value(request.claim)
    resolved = _resolve_voices(request)
    # Sorted pairs keep input voices aligned with their resolved records
    # while making every downstream decision input-order independent.
    paired = sorted(
        zip(request.voices, resolved),
        key=lambda pair: (pair[1].host, pair[1].url, pair[1].publisher or ""),
    )
    resolved = [shown for _, shown in paired]
    if claim is None:
        return CategoryVerification(
            outcome=VerificationOutcome.NOT_VERIFIED,
            reasons=[VerificationFailureReason.NO_RECOGNIZED_EVIDENCE],
            rationale=(
                f"Not verified: {request.claim!r} is not a recognized category; "
                "no evidence was evaluated. This does not mean the claim is false."
            ),
            voices=resolved,
        )
    if not request.voices:
        return CategoryVerification(
            outcome=VerificationOutcome.NOT_VERIFIED,
            reasons=[VerificationFailureReason.NO_RECOGNIZED_EVIDENCE],
            rationale=(
                f"Not verified {claim}: no evidence voices were supplied. "
                "This does not mean the claim is false."
            ),
            voices=resolved,
        )

    identity_ok = bool(request.identity.aligned and request.identity.group_key)
    contradiction = bool(request.material_contradiction or request.contradiction_uncertain)
    adapter = [v for v in request.voices if v.provenance == "adapter_collected"]

    def facts_ok(voice: VoiceInput) -> bool:
        supported, _ = _facts_support(claim, voice.defining_facts)
        return supported

    # Authority pathway: one qualifying authoritative voice suffices.
    authority_voices: list[tuple[VoiceInput, VerificationVoice]] = []
    for voice, shown in paired:
        if voice.provenance != "adapter_collected":
            continue
        if shown.source_class is not SourceClass.AUTHORITATIVE:
            continue
        if not shown.host or not facts_ok(voice):
            continue
        if not _record_type_ok(voice, request.registry):
            continue
        authority_voices.append((voice, shown))
    if authority_voices and identity_ok and not contradiction:
        shown = [shown for _, shown in authority_voices]
        hosts = sorted({v.host for v in shown})
        return CategoryVerification(
            outcome=VerificationOutcome.VERIFIED,
            pathway=VerificationPathway.AUTHORITY,
            rationale=(
                f"Verified {claim} via one authoritative record "
                f"({', '.join(hosts)}) with category-defining facts, aligned "
                "property identity, and no material contradiction."
            ),
            limitations=[
                "Authoritative record establishes this category claim only, "
                "not every fact about the property."
            ],
            voices=shown,
        )

    # Credible-corroboration pathway.
    eligible: list[tuple[VoiceInput, VerificationVoice]] = []
    for voice, shown in paired:
        if voice.provenance != "adapter_collected":
            continue
        if not shown.host:
            continue
        if shown.source_class is SourceClass.UNVETTED:
            continue
        if not facts_ok(voice):
            continue
        if not _record_type_ok(voice, request.registry):
            continue
        eligible.append((voice, shown))

    reasons: list[VerificationFailureReason] = []
    if not identity_ok:
        reasons.append(VerificationFailureReason.IDENTITY_UNRESOLVED)
    if contradiction:
        reasons.append(VerificationFailureReason.MATERIAL_CONTRADICTION)
    if not adapter:
        reasons.append(VerificationFailureReason.CLIENT_ASSERTION_ONLY)
    if adapter and not any(canonical_hostname(v.url) for v in adapter):
        reasons.append(VerificationFailureReason.HOSTLESS_EVIDENCE)

    effective, id_conflict = _effective_indices(eligible, request.registry)
    conflict_note = (
        "Merged copies of one originating advertisement carried conflicting "
        "publisher metadata; counted once."
    )
    keys = [_origin_key(eligible[i][0], eligible[i][1], request.registry) for i in effective]
    distinct_keys = sorted(set(keys))
    namespaces = {kind for kind, _ in distinct_keys}
    vetted_present = any(
        eligible[i][1].source_class
        in (SourceClass.AUTHORITATIVE, SourceClass.VETTED_COMMERCIAL)
        for i in effective
    )
    if effective and not vetted_present and len(effective) >= 2:
        reasons.append(VerificationFailureReason.SECONDARY_ONLY)
    if not eligible and adapter:
        hosted = [
            (v, s)
            for v, s in paired
            if v.provenance == "adapter_collected" and s.host
        ]
        wrong_record = any(
            not _record_type_ok(v, request.registry)
            for v, s in hosted
            if s.source_class is not SourceClass.UNVETTED and facts_ok(v)
        )
        if wrong_record:
            reasons.append(VerificationFailureReason.AUTHORITY_INSUFFICIENT)
        elif any(
            s.source_class is not SourceClass.UNVETTED for _, s in hosted
        ):
            reasons.append(VerificationFailureReason.MISSING_DEFINING_FACTS)
        elif hosted:
            reasons.append(VerificationFailureReason.UNVETTED_SOURCES_ONLY)
    if not request.registry.entries and not any(
        s.source_class is not SourceClass.UNVETTED for s in resolved
    ):
        reasons.append(VerificationFailureReason.REGISTRY_UNCONFIGURED)
    if len(distinct_keys) < 2:
        if eligible:
            reasons.append(VerificationFailureReason.SINGLE_VOICE_ONLY)
    elif len(namespaces) > 1 or any(kind == "unknown" for kind, _ in distinct_keys):
        reasons.append(VerificationFailureReason.INDEPENDENCE_UNCERTAIN)
    if any(
        request.registry.publisher_of(v.url)
        and (v.publisher or "").strip()
        and request.registry.publisher_of(v.url) != (v.publisher or "").strip()
        for v, _ in eligible
    ):
        # Registry (curated) and modeled (supplied) originators disagree;
        # fail closed rather than picking a winner.
        reasons.append(VerificationFailureReason.INDEPENDENCE_UNCERTAIN)

    # Deduplicate while preserving canonical enum order.
    order = list(VerificationFailureReason)
    reasons = sorted(set(reasons), key=order.index)

    if not reasons and len(distinct_keys) >= 2 and vetted_present:
        qualifying = [eligible[i][1] for i in effective]
        hosts = sorted({v.host for v in qualifying})
        limitations = [
            "Originating-source distinguishability is reasonable, not absolute; "
            "paraphrased copying is not detectable from the supplied metadata."
        ]
        if any(s.source_class is SourceClass.SECONDARY for s in qualifying):
            limitations.append("One counted voice is a secondary source.")
        use_unknown = any(
            _facts_support(claim, eligible[i][0].defining_facts)[1] for i in effective
        )
        if use_unknown:
            limitations.append("Current versus proposed use was not established.")
        if id_conflict:
            limitations.append(conflict_note)
        return CategoryVerification(
            outcome=VerificationOutcome.VERIFIED,
            pathway=VerificationPathway.CREDIBLE_CORROBORATION,
            rationale=(
                f"Verified {claim} via {len(distinct_keys)} distinguishable "
                f"originating sources ({', '.join(hosts)}) with category-defining "
                "facts, aligned property identity, and no material contradiction."
            ),
            limitations=limitations,
            voices=qualifying,
        )

    # Authority pathway did not apply either; record why it could not.
    if not any(
        s.source_class is SourceClass.AUTHORITATIVE
        for v, s in paired
        if v.provenance == "adapter_collected"
    ):
        reasons.append(VerificationFailureReason.AUTHORITY_INSUFFICIENT)
    reasons = sorted(set(reasons), key=order.index)
    if not reasons:
        # Defensive: no pathway passed but nothing was recorded (unreachable
        # by construction; fail closed rather than emit a reasonless verdict).
        reasons.append(VerificationFailureReason.SINGLE_VOICE_ONLY)
    hosts = sorted({v.host for v in resolved if v.host}) or ["no usable hosts"]
    return CategoryVerification(
        outcome=VerificationOutcome.NOT_VERIFIED,
        reasons=reasons,
        rationale=(
            f"Not verified {claim}: {', '.join(r.value for r in reasons)}. "
            f"Available evidence references: {', '.join(hosts)}. "
            "This does not mean the claim is false."
        ),
        limitations=[conflict_note] if id_conflict else [],
        voices=resolved,
    )


__all__ = [
    "VerificationOutcome",
    "VerificationPathway",
    "VerificationFailureReason",
    "VoiceInput",
    "IdentityAttestation",
    "VerificationRequest",
    "VerificationVoice",
    "CategoryVerification",
    "verify_category",
]
