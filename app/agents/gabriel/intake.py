"""Gabriel commercial listing intake (MVP-004A).

An intake mechanism -- not autonomous web search. It accepts structured
listing information supplied through authorized means (manual research,
user-supplied details, permitted datasets, future licensed providers),
validates it, maps attributable facts onto the MVP-002 property record,
and preserves the rest as linked listing evidence.

Mapping discipline (never confused):
- Advertised building area -> building_area_sqft_estimated.
- Explicit listing/vacancy durations -> estimated month fields.
- Asking price is NOT mapped to property_value_estimated: an advertised
  price is not necessarily a market-value estimate. It is preserved as
  an exact decimal string in listing_evidence.payload.
- Advertised available area is NOT mapped to vacant_area_sqft_estimated:
  available space may still be occupied. It is preserved in
  listing_evidence.payload; vacancy requires explicit vacancy evidence.
- Asking rent, advertised owner, listing type/date/history ->
  listing_evidence payload (asking rent is not rental income; an
  advertised owner is not ownership).
- Verified fields are never populated by intake; distress observations
  enter as preliminary only (Raphael verifies later).

The server never fetches submitted URLs (no SSRF surface).
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.agents.gabriel.dedup import find_existing_id, normalize_text
from app.agents.gabriel.observe import new_run_id, note_activity, note_warning
from app.config import get_database_config
from app.db import DatabaseNotConfiguredError
from app.listings_db import (
    create_distress_observations,
    create_listing_evidence,
    get_listing_evidence,
)
from app.properties import (
    ESTIMATED_FIELDS,
    PropertyCreate,
    check_source_text,
    derive_data_status,
)
from app.properties_db import create_property, enrich_property_estimates, get_property

logger = logging.getLogger(__name__)

SECURITY_LIMITATION = (
    "POST /discovery/intake, POST /discovery/runs, POST /properties, "
    "GET /properties, and GET /properties/{property_id} require the "
    "X-Intake-Key header matching the INTAKE_API_KEY environment variable "
    "(constant-time comparison; missing configuration fails closed with "
    "503). POST /discovery/research-runs and POST /discovery/investigations "
    "require the X-Research-Key header instead. Remaining limitations: a "
    "single shared secret with no rotation, per-client scoping, or audit "
    "trail. Some read-only and computational endpoints remain "
    "unauthenticated by design. Operate keyed endpoints over trusted "
    "networks and rotate a key if it may be exposed."
)

MAX_DISTRESS_OBSERVATIONS = 10
MAX_PRICE_REDUCTIONS = 10
MIN_SOURCE_NAME_LENGTH = 3
MIN_DISTRESS_DESCRIPTION_LENGTH = 10
FUTURE_SKEW_TOLERANCE = timedelta(hours=1)


class IntakeError(ValueError):
    """Intake input failed semantic validation (maps to HTTP 422)."""


class ListingType(str, Enum):
    SALE = "sale"
    LEASE = "lease"
    AUCTION = "auction"
    UNKNOWN = "unknown"


class DistressIndicator(str, Enum):
    PRICE_REDUCTION = "price_reduction"
    EXTENDED_LISTING = "extended_listing"
    ADVERTISED_VACANCY = "advertised_vacancy"
    TAX_DELINQUENCY = "tax_delinquency"
    FORECLOSURE_PROCEEDING = "foreclosure_proceeding"
    REPEATED_LISTING = "repeated_listing"
    OTHER = "other"


class DistressStatus(str, Enum):
    PRELIMINARY = "preliminary"
    VERIFIED = "verified"


class IntakeOutcome(str, Enum):
    CREATED = "created"
    ENRICHED = "enriched"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    NEEDS_IDENTITY_REVIEW = "needs_identity_review"


def _to_decimal(value: object) -> Decimal | None:
    """Float-safe money conversion (same rule as the property model)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, bool):
        raise ValueError("monetary value must be numeric, not boolean")
    return Decimal(str(value).strip())


def _checked_timestamp(value: datetime) -> datetime:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if moment > datetime.now(timezone.utc) + FUTURE_SKEW_TOLERANCE:
        raise ValueError("observation timestamp must not be in the future")
    return moment


class DistressObservationInput(BaseModel):
    """One preliminary distress observation (intake cannot verify)."""

    indicator_type: DistressIndicator
    description: str = Field(min_length=MIN_DISTRESS_DESCRIPTION_LENGTH, max_length=2000)
    source_name: str | None = Field(default=None, max_length=200)
    source_url: HttpUrl | None = None
    observed_at: datetime | None = None
    status: DistressStatus = DistressStatus.PRELIMINARY

    @field_validator("status")
    @classmethod
    def _reject_verified(cls, value: DistressStatus) -> DistressStatus:
        if value is not DistressStatus.PRELIMINARY:
            raise ValueError("intake observations enter as preliminary only; Raphael verifies")
        return value

    @field_validator("source_name")
    @classmethod
    def _check_source_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return check_source_text(value, field_name="observation source", min_length=MIN_SOURCE_NAME_LENGTH)

    @field_validator("observed_at")
    @classmethod
    def _check_observed_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _checked_timestamp(value)


class ListingIntakeRequest(BaseModel):
    """Request body for POST /discovery/intake (bounded, attributable)."""

    model_config = {"populate_by_name": True}

    name: str | None = Field(default=None, min_length=1, max_length=300)
    street_address: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, max_length=100)
    zip_code: str | None = Field(default=None, max_length=20)
    category: str = Field(default="other", min_length=1, max_length=100)
    source_name: str = Field(max_length=200)
    listing_url: HttpUrl
    observed_at: datetime
    asking_price: Decimal | None = Field(default=None, ge=0)
    advertised_building_area_sqft: float | None = Field(default=None, ge=0)
    advertised_available_area_sqft: float | None = Field(default=None, ge=0)
    months_listed_for_sale: int | None = Field(default=None, ge=0)
    months_vacant: int | None = Field(default=None, ge=0)
    occupancy_info: str | None = Field(default=None, max_length=2000)
    owner_advertised: str | None = Field(default=None, max_length=300)
    asking_rent_monthly: Decimal | None = Field(default=None, ge=0)
    listing_type: ListingType = ListingType.UNKNOWN
    listing_date: date | None = None
    listing_notes: str | None = Field(default=None, max_length=2000)
    price_reductions: list[str] = Field(default_factory=list, max_length=MAX_PRICE_REDUCTIONS)
    distress_observations: list[DistressObservationInput] = Field(
        default_factory=list, max_length=MAX_DISTRESS_OBSERVATIONS
    )
    correlation_id: str = Field(default="", max_length=128)

    @field_validator("asking_price", "asking_rent_monthly", mode="before")
    @classmethod
    def _coerce_money(cls, value: object) -> Decimal | None:
        return _to_decimal(value)

    @field_validator("source_name")
    @classmethod
    def _check_source_name(cls, value: str) -> str:
        return check_source_text(value, field_name="listing source", min_length=MIN_SOURCE_NAME_LENGTH)

    @field_validator("observed_at")
    @classmethod
    def _check_observed_at(cls, value: datetime) -> datetime:
        return _checked_timestamp(value)

    @field_validator("listing_date")
    @classmethod
    def _check_listing_date(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(timezone.utc).date():
            raise ValueError("listing date must not be in the future")
        return value

    @field_validator("price_reductions")
    @classmethod
    def _check_reductions(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value]
        for item in cleaned:
            if not item or len(item) > 500:
                raise ValueError("price reduction entries must be 1-500 characters")
        return cleaned

    @model_validator(mode="after")
    def _require_identity(self) -> ListingIntakeRequest:
        if not (self.name or "").strip() and not (self.street_address or "").strip():
            raise ValueError("listing must include a property name or street address")
        return self


class IntakeResult(BaseModel):
    """Outcome of one intake submission."""

    outcome: IntakeOutcome
    property_id: str
    filled_fields: list[str] = []
    listing_evidence_id: str | None = None
    distress_observation_ids: list[str] = []
    message: str
    run_id: str = ""
    correlation_id: str = ""


def _display_name(body: ListingIntakeRequest) -> str:
    if (body.name or "").strip():
        return body.name.strip()  # type: ignore[union-attr]
    parts = [body.street_address.strip()]  # type: ignore[union-attr]
    if (body.city or "").strip():
        parts.append(body.city.strip())  # type: ignore[union-attr]
    return ", ".join(parts)


def _provenance(body: ListingIntakeRequest) -> str:
    return (
        f"Commercial listing intake from {body.source_name} observed "
        f"{body.observed_at.isoformat()} ({body.listing_url}); "
        "submitted through authorized intake"
    )


def _field_reference(body: ListingIntakeRequest) -> str:
    return (
        f"Advertised in {body.source_name} listing observed "
        f"{body.observed_at.date().isoformat()}: {body.listing_url}"
    )


def _intake_notes(body: ListingIntakeRequest, advertised: list[str]) -> str:
    unknown = [
        label
        for label, present in (
            ("asking price", body.asking_price is not None),
            ("building area", body.advertised_building_area_sqft is not None),
            ("available area", body.advertised_available_area_sqft is not None),
            ("occupancy", bool((body.occupancy_info or "").strip())),
            ("listing duration", body.months_listed_for_sale is not None),
            ("vacancy duration", body.months_vacant is not None),
        )
        if not present
    ]
    lines = [
        f"Commercial {body.listing_type.value} listing from {body.source_name} "
        f"observed {body.observed_at.date().isoformat()} ({body.listing_url})."
    ]
    if advertised:
        lines.append(f"Advertised: {', '.join(advertised)}.")
    evidence_only = [
        label
        for label, present in (
            ("asking price", body.asking_price is not None),
            ("available area", body.advertised_available_area_sqft is not None),
        )
        if present
    ]
    if evidence_only:
        lines.append(f"Preserved as listing evidence (not estimates): {', '.join(evidence_only)}.")
    if unknown:
        lines.append(f"Unknown: {', '.join(unknown)}.")
    if body.listing_notes and body.listing_notes.strip():
        lines.append(f"Listing notes: {body.listing_notes.strip()}")
    return " ".join(lines)[:5000]


def build_property_payload(body: ListingIntakeRequest) -> dict[str, Any]:
    """Map intake fields onto a PropertyCreate-compatible payload.

    Only explicitly supplied, attributable values map to estimated
    fields; everything else stays null. Verified fields are never set.
    """
    payload: dict[str, Any] = {
        "name": _display_name(body),
        "street_address": (body.street_address or "").strip() or None,
        "city": (body.city or "").strip() or None,
        "state": (body.state or "").strip() or None,
        "zip_code": (body.zip_code or "").strip() or None,
        "category": body.category,
        "building_area_sqft_estimated": body.advertised_building_area_sqft,
        "occupancy": (body.occupancy_info or "").strip() or None,
        "months_on_market_estimated": body.months_listed_for_sale,
        "months_vacant_estimated": body.months_vacant,
        "provenance": _provenance(body),
        "notes": "",
    }
    advertised = [key for key in ESTIMATED_FIELDS if payload.get(key) is not None]
    payload["notes"] = _intake_notes(body, advertised)
    if advertised:
        payload["field_evidence"] = {key: _field_reference(body) for key in advertised}
    return payload


def build_evidence_payload(body: ListingIntakeRequest, mapped: list[str]) -> dict[str, Any]:
    """Bundle listing attributes with no safe property-column home."""
    return {
        "listing_type": body.listing_type.value,
        "listing_date": body.listing_date.isoformat() if body.listing_date else None,
        "asking_price": str(body.asking_price) if body.asking_price is not None else None,
        "advertised_available_area_sqft": body.advertised_available_area_sqft,
        "asking_rent_monthly": str(body.asking_rent_monthly) if body.asking_rent_monthly is not None else None,
        "owner_advertised": (body.owner_advertised or "").strip() or None,
        "price_reductions": list(body.price_reductions),
        "listing_notes": (body.listing_notes or "").strip() or None,
        "mapped_estimates": mapped,
    }


def build_distress_rows(body: ListingIntakeRequest) -> list[dict[str, Any]]:
    """Resolve per-observation attribution, defaulting to the listing source."""
    rows: list[dict[str, Any]] = []
    for obs in body.distress_observations:
        rows.append(
            {
                "indicator_type": obs.indicator_type.value,
                "source_name": obs.source_name or body.source_name,
                "source_url": str(obs.source_url or body.listing_url),
                "observed_at": obs.observed_at or body.observed_at,
                "description": obs.description.strip(),
                "status": DistressStatus.PRELIMINARY.value,
            }
        )
    return rows


def _identity_ambiguous(body: ListingIntakeRequest, existing: dict[str, Any]) -> bool:
    """True when the address matches but the building names conflict.

    Conservative collision guard: an address match alone does not prove
    the same physical building. Both names must be present and, after the
    shared normalization (case/punctuation/whitespace-insensitive),
    different. No fuzzy matching; no unit, parcel, coordinate, or
    ownership inference. The incoming side uses the explicitly supplied
    name only -- a derived display name ("street, city") is not evidence.
    """
    incoming = normalize_text(body.name) if (body.name or "").strip() else ""
    stored = normalize_text(existing.get("name"))
    return bool(incoming) and bool(stored) and incoming != stored


def process_intake(body: ListingIntakeRequest) -> IntakeResult:
    """Validate, dedup, and persist one listing (instrumented, nonfatal).

    Emits one accepted (activity) or rejected (warning) event per call.
    The event carries field names, counts, and the listing URL only --
    never the complete submission, credentials, or personal details.
    """
    run_id = new_run_id()
    correlation_id = body.correlation_id or ""
    started = time.perf_counter()
    try:
        result = _process_intake(body)
    except IntakeError as exc:
        note_warning(
            run_id, "gabriel.intake.rejected",
            f"listing rejected: {exc}"[:300],
            details={
                "reason": str(exc)[:300],
                "source_name": body.source_name,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
            correlation_id=correlation_id,
        )
        raise
    result.run_id = run_id
    result.correlation_id = correlation_id
    try:
        known_property: UUID | None = UUID(str(result.property_id))
    except ValueError:
        known_property = None
    note_activity(
        run_id, "gabriel.intake.accepted",
        f"listing accepted: {result.outcome.value} property {result.property_id}",
        details={
            "outcome": result.outcome.value,
            "filled_fields": list(result.filled_fields),
            "listing_evidence_id": result.listing_evidence_id,
            "distress_observations": len(result.distress_observation_ids),
            "category": body.category,
            "listing_type": body.listing_type.value,
            "source_name": body.source_name,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        correlation_id=correlation_id,
        property_id=known_property,
        evidence_refs=[{"kind": "listing_url", "ref": str(body.listing_url)[:500]}],
    )
    return result


def _process_intake(body: ListingIntakeRequest) -> IntakeResult:
    """Validate, dedup, and persist one listing (create, enrich, or skip)."""
    if not get_database_config().is_configured:
        raise DatabaseNotConfiguredError("database_not_configured")
    try:
        validated = PropertyCreate.model_validate(build_property_payload(body))
    except Exception as exc:
        raise IntakeError(f"listing failed operational validation: {exc}") from exc
    data = validated.model_dump()
    mapped = [key for key in ESTIMATED_FIELDS if data.get(key) is not None]
    evidence_payload = build_evidence_payload(body, mapped)
    distress_rows = build_distress_rows(body)
    source_url = str(body.listing_url)

    existing_id = find_existing_id(data)
    if existing_id is None:
        row = create_property(data)
        property_id = str(row["id"])
        evidence = create_listing_evidence(
            property_id=property_id,
            source_name=body.source_name,
            source_url=source_url,
            observed_at=body.observed_at,
            listing_type=body.listing_type.value,
            payload=evidence_payload,
        )
        distress_ids = create_distress_observations(property_id, distress_rows)
        return IntakeResult(
            outcome=IntakeOutcome.CREATED,
            property_id=property_id,
            filled_fields=mapped,
            listing_evidence_id=str(evidence["id"]),
            distress_observation_ids=distress_ids,
            message="listing accepted; new operational record created",
        )

    # Duplicate address: enrich NULL estimated fields only, never overwrite.
    existing = get_property(UUID(str(existing_id)))
    if existing is None:  # pragma: no cover - raced deletion; fail closed
        raise IntakeError("duplicate target disappeared; resubmit the listing")
    if _identity_ambiguous(body, existing):
        # Same address but a materially different building name: the two
        # listings may describe different buildings. Preserve everything
        # unchanged -- no enrichment, no evidence, no second record -- and
        # require human identity review.
        return IntakeResult(
            outcome=IntakeOutcome.NEEDS_IDENTITY_REVIEW,
            property_id=str(existing_id),
            message=(
                "same address but a materially different building name; "
                "identity is ambiguous, existing record preserved unchanged, "
                "no evidence attached; human identity review required"
            ),
        )
    fills = {
        key: data[key] for key in ESTIMATED_FIELDS
        if data.get(key) is not None and existing.get(key) is None
    }
    message = "duplicate address; existing record preserved"
    if fills:
        merged = {**existing, **fills}
        try:
            PropertyCreate.model_validate(merged)
        except Exception:
            fills = {}
            message = (
                "duplicate address; conflicting values skipped to protect "
                "the existing record"
            )
    already_recorded = get_listing_evidence(str(existing_id), source_url) is not None
    if not fills and already_recorded:
        return IntakeResult(
            outcome=IntakeOutcome.SKIPPED_DUPLICATE,
            property_id=str(existing_id),
            message="duplicate listing already recorded; nothing changed",
        )
    new_refs = {key: _field_reference(body) for key in fills}
    stored_evidence = existing.get("field_evidence")
    if not isinstance(stored_evidence, dict):
        stored_evidence = {}
    merged_evidence = {
        **stored_evidence,
        **{k: v for k, v in new_refs.items() if k not in stored_evidence},
    }
    merged_status = derive_data_status({**existing, **fills}).value
    filled = enrich_property_estimates(UUID(str(existing_id)), fills, merged_evidence or None, merged_status)
    evidence_id: str | None = None
    distress_ids: list[str] = []
    if not already_recorded:
        evidence = create_listing_evidence(
            property_id=str(existing_id),
            source_name=body.source_name,
            source_url=source_url,
            observed_at=body.observed_at,
            listing_type=body.listing_type.value,
            payload=evidence_payload,
        )
        evidence_id = str(evidence["id"])
        distress_ids = create_distress_observations(str(existing_id), distress_rows)
    return IntakeResult(
        outcome=IntakeOutcome.ENRICHED,
        property_id=str(existing_id),
        filled_fields=filled,
        listing_evidence_id=evidence_id,
        distress_observation_ids=distress_ids,
        message=message if not fills else "existing record enriched with new attributable values",
    )
