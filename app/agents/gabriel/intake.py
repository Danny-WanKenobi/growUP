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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.agents.gabriel.dedup import (
    AddressMatchStatus,
    intake_address_lock_key,
    intake_url_lock_key,
    match_existing_address,
    normalize_text,
)
from app.agents.gabriel.observe import new_run_id, note_activity, note_warning
from app.config import get_database_config
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError, acquire_intake_locks
from app.identity_reviews_db import record_review_occurrence
from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity
from app.observability.store import insert_event_in_transaction
from app.listings_db import (
    create_distress_observations_in_transaction,
    create_listing_evidence_in_transaction,
    find_evidence_by_url,
    get_listing_evidence,
)
from app.properties import (
    ESTIMATED_FIELDS,
    PropertyCreate,
    check_source_text,
    derive_data_status,
)
from app.properties_db import (
    create_property_in_transaction,
    enrich_property_estimates_in_transaction,
    get_connection,
    get_property,
)

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

    def __init__(self, message: str = "", *, reason_code: str = "VALIDATION_FAILED") -> None:
        super().__init__(message)
        self.reason_code = reason_code


DECISION_EVENT_TYPE = "gabriel.intake.decision"

OUTCOME_REJECTED = "rejected"
OUTCOME_ATTEMPT_FAILED = "attempt_failed"

MUTATION_NOT_ATTEMPTED = "NOT_ATTEMPTED"
MUTATION_COMMITTED = "COMMITTED"
MUTATION_ROLLED_BACK = "ROLLED_BACK"

REASON_NEW_IDENTITY = "NEW_IDENTITY"
REASON_NULL_FILL = "NULL_FILL"
REASON_EVIDENCE_ONLY = "EVIDENCE_ONLY"
REASON_MERGE_VALIDATION_CONFLICT = "MERGE_VALIDATION_CONFLICT"
REASON_DUPLICATE_URL = "DUPLICATE_URL"
REASON_DUPLICATE_ADDRESS_RECORDED = "DUPLICATE_ADDRESS_RECORDED"
REASON_AMBIGUOUS_ADDRESS = "AMBIGUOUS_ADDRESS"
REASON_NAME_CONFLICT = "NAME_CONFLICT"
REASON_URL_CONFLICT = "URL_CONFLICT"
REASON_URL_MULTI_ASSOC = "URL_MULTI_ASSOC"
REASON_URL_ADDRESS_DISAGREEMENT = "URL_ADDRESS_DISAGREEMENT"
REASON_VALIDATION_FAILED = "VALIDATION_FAILED"
REASON_RACED_STATE = "RACED_STATE"
REASON_DATABASE_FAILURE = "DATABASE_FAILURE"


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
    reason_code: str = ""
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


def _location_conflicts(body: ListingIntakeRequest, existing: dict[str, Any]) -> bool:
    """True when a present location component contradicts the stored one.

    Only compares city, state, and ZIP when both sides supply them;
    missing information is never a contradiction. Exact normalized
    comparison only -- no fuzzy matching, no invented components.
    """
    for field in ("city", "state", "zip_code"):
        incoming = normalize_text(getattr(body, field))
        stored = normalize_text(existing.get(field))
        if incoming and stored and incoming != stored:
            return True
    return False


def _street_conflicts(data: dict[str, Any], existing: dict[str, Any]) -> bool:
    """True when present street addresses contradict each other.

    Missing information on either side is never a contradiction.
    Exact normalized comparison only.
    """
    incoming = normalize_text(data.get("street_address"))
    stored = normalize_text(existing.get("street_address"))
    return bool(incoming) and bool(stored) and incoming != stored


@dataclass(frozen=True)
class _DecisionMeta:
    """Identity references behind one intake outcome (provenance only)."""

    reason_code: str
    considered: tuple[str, ...] = ()
    conflicting: tuple[str, ...] = ()


def _resolve_url_association(
    body: ListingIntakeRequest, data: dict[str, Any], source_url: str
) -> tuple[IntakeResult | None, _DecisionMeta]:
    """Check global URL associations for a listing with no address match.

    Returns a duplicate or review outcome with zero writes, or None
    when no previous association exists and intake may proceed. A URL
    alone never proves property identity: a single association without
    contradicting identity information is reported as a duplicate of
    the recorded listing (nothing changed); anything conflicting or
    ambiguous requires human identity review.
    """
    associated = find_evidence_by_url(source_url)
    if len(associated) > 1:
        pids = tuple(str(link["property_id"]) for link in associated)
        return (
            IntakeResult(
                outcome=IntakeOutcome.NEEDS_IDENTITY_REVIEW,
                property_id="",
                message=(
                    "listing URL already associated with multiple properties; "
                    "identity is ambiguous, nothing was changed, no evidence "
                    "attached; human identity review required"
                ),
                reason_code=REASON_URL_MULTI_ASSOC,
            ),
            _DecisionMeta(REASON_URL_MULTI_ASSOC, considered=pids, conflicting=pids),
        )
    if not associated:
        return None, _DecisionMeta("")
    property_id = str(associated[0]["property_id"])
    existing = get_property(UUID(property_id))
    if existing is None:  # pragma: no cover - raced deletion; fail closed
        raise IntakeError(
            "associated property disappeared; resubmit the listing",
            reason_code=REASON_RACED_STATE,
        )
    if (
        _identity_ambiguous(body, existing)
        or _location_conflicts(body, existing)
        or _street_conflicts(data, existing)
    ):
        return (
            IntakeResult(
                outcome=IntakeOutcome.NEEDS_IDENTITY_REVIEW,
                property_id="",
                message=(
                    "listing URL already recorded for a conflicting property "
                    "identity; nothing was changed, no evidence attached; "
                    "human identity review required"
                ),
                reason_code=REASON_URL_CONFLICT,
            ),
            _DecisionMeta(REASON_URL_CONFLICT, considered=(property_id,), conflicting=(property_id,)),
        )
    return (
        IntakeResult(
            outcome=IntakeOutcome.SKIPPED_DUPLICATE,
            property_id=property_id,
            message="duplicate listing URL already recorded; nothing changed",
            reason_code=REASON_DUPLICATE_URL,
        ),
        _DecisionMeta(REASON_DUPLICATE_URL, considered=(property_id,)),
    )


@dataclass
class _DecisionContext:
    """Per-request decision state shared by the locked intake path."""

    body: ListingIntakeRequest
    conn: Any
    run_id: str
    started: float
    source_url: str
    writes_attempted: bool = False


def _observed_identity(body: ListingIntakeRequest, source_url: str) -> dict[str, Any]:
    """Explicitly supplied identity fields, verbatim (None when absent).

    No stripping or case folding: whitespace is preserved exactly as
    submitted so the record shows what Gabriel observed. Normalized
    forms belong in the inferred section only.
    """
    return {
        "source_name": body.source_name,
        "external_id": None,
        "name": body.name if body.name else None,
        "street_address": body.street_address if body.street_address else None,
        "city": body.city if body.city else None,
        "state": body.state if body.state else None,
        "zip_code": body.zip_code if body.zip_code else None,
        "listing_url": source_url,
        "observed_at": body.observed_at.isoformat(),
    }


def _inferred_identity(body: ListingIntakeRequest) -> dict[str, Any]:
    """Normalized identity fields exactly as matching observes them.

    Empty string mirrors ``normalize_text`` on absent input; these are
    derived matching inputs, never directly observed values.
    """
    explicit_name = body.name if (body.name or "").strip() else ""
    return {
        "name": normalize_text(explicit_name),
        "street_address": normalize_text(body.street_address),
        "city": normalize_text(body.city),
        "state": normalize_text(body.state),
        "zip_code": normalize_text(body.zip_code),
    }


def _decision_event(
    ctx: _DecisionContext,
    outcome: str,
    reason_code: str,
    message: str,
    mutation_status: str,
    considered: tuple[str, ...],
    conflicting: tuple[str, ...],
    result: IntakeResult | None,
    severity: Severity = Severity.INFO,
) -> AgentEvent:
    """Build (validating, no I/O) one permanent intake decision event."""
    body = ctx.body
    try:
        known_property: UUID | None = UUID(str(result.property_id)) if result is not None else None
    except (ValueError, AttributeError):
        known_property = None
    return AgentEvent(
        agent=AgentId.GABRIEL,
        run_id=ctx.run_id,
        event_type=DECISION_EVENT_TYPE,
        summary=f"intake decision: {outcome} ({reason_code})",
        severity=severity,
        retention_class=RetentionClass.PERMANENT,
        details={
            "outcome": outcome,
            "reason_code": reason_code,
            "message": message,
            "mutation_status": mutation_status,
            "observed": _observed_identity(body, ctx.source_url),
            "inferred": _inferred_identity(body),
            "considered_property_ids": list(considered),
            "conflicting_property_ids": list(conflicting),
            "result": {
                "property_id": result.property_id if result is not None else "",
                "evidence_id": result.listing_evidence_id if result is not None else None,
                "distress_ids": list(result.distress_observation_ids) if result is not None else [],
                "filled_fields": list(result.filled_fields) if result is not None else [],
            },
            "duration_ms": round((time.perf_counter() - ctx.started) * 1000, 1),
        },
        correlation_id=body.correlation_id or "",
        property_id=known_property,
        evidence_refs=[{"kind": "listing_url", "ref": ctx.source_url[:500]}],
    )


def _persist_decision_dedicated(event: AgentEvent) -> None:
    """Persist one decision event in its own transaction (commits once).

    Used only when no business transaction is open. Raises
    :class:`DatabaseUnavailableError` when persistence fails.
    """
    with get_connection() as conn:
        insert_event_in_transaction(conn, event)
        record_review_occurrence(conn, event)
        conn.commit()


def _best_effort_decision(event: AgentEvent) -> bool:
    """Attempt one failure-event write without masking the real outcome."""
    try:
        _persist_decision_dedicated(event)
    except Exception as exc:
        logger.warning(
            "gabriel decision event not persisted: %s (%s)", event.event_id, type(exc).__name__
        )
        return False
    return True


def _finish_decision(
    ctx: _DecisionContext,
    *,
    outcome: IntakeOutcome,
    reason_code: str,
    property_id: str,
    message: str,
    commit: bool,
    considered: tuple[str, ...] = (),
    conflicting: tuple[str, ...] = (),
    filled: list[str] | None = None,
    evidence_id: str | None = None,
    distress_ids: list[str] | None = None,
) -> tuple[IntakeResult, AgentEvent | None]:
    """Build one outcome plus its permanent decision event.

    Mutating outcomes insert the event on the locked connection and
    commit once here. Read-only outcomes return the event for a
    dedicated post-transaction write, leaving the locked transaction
    free of business writes.
    """
    result = IntakeResult(
        outcome=outcome,
        property_id=property_id,
        filled_fields=list(filled or []),
        listing_evidence_id=evidence_id,
        distress_observation_ids=list(distress_ids or []),
        message=message,
        reason_code=reason_code,
    )
    event = _decision_event(
        ctx,
        outcome.value,
        reason_code,
        message,
        MUTATION_COMMITTED if commit else MUTATION_NOT_ATTEMPTED,
        considered,
        conflicting,
        result,
    )
    if commit:
        insert_event_in_transaction(ctx.conn, event)
        ctx.conn.commit()
        return result, None
    return result, event


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
        result, pending = _process_intake(body, started, run_id)
    except IntakeError as exc:
        ctx = _DecisionContext(body=body, conn=None, run_id=run_id, started=started, source_url=str(body.listing_url))
        _best_effort_decision(
            _decision_event(
                ctx,
                OUTCOME_REJECTED,
                exc.reason_code,
                str(exc)[:500],
                MUTATION_NOT_ATTEMPTED,
                (),
                (),
                None,
                Severity.WARNING,
            )
        )
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
    if pending is not None:
        try:
            _persist_decision_dedicated(pending)
        except (DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
            ctx = _DecisionContext(
                body=body, conn=None, run_id=run_id, started=started, source_url=str(body.listing_url)
            )
            _best_effort_decision(
                _decision_event(
                    ctx,
                    OUTCOME_ATTEMPT_FAILED,
                    REASON_DATABASE_FAILURE,
                    "permanent decision event could not be persisted; decision not durably recorded",
                    MUTATION_NOT_ATTEMPTED,
                    (),
                    (),
                    None,
                    Severity.ERROR,
                )
            )
            raise DatabaseUnavailableError("decision_event_failed") from exc
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


def _rollback_quietly(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _intake_lock_keys(data: dict[str, Any], source_url: str) -> list[str]:
    """Coordination keys for one intake operation.

    Every usable listing URL contributes its exact-URL key; a usable
    street address additionally contributes its normalized-address key.
    Keys coordinate concurrent operations sharing a URL or an address;
    they never prove physical identity.
    """
    keys = []
    if source_url:
        keys.append(intake_url_lock_key(source_url))
    if normalize_text(data.get("street_address")):
        keys.append(
            intake_address_lock_key(
                data.get("street_address"),
                data.get("city"),
                data.get("state"),
                data.get("zip_code"),
            )
        )
    return keys


def _process_intake(
    body: ListingIntakeRequest, started: float, run_id: str
) -> tuple[IntakeResult, AgentEvent | None]:
    """Validate one listing, then decide and persist inside a locked transaction.

    Returns the outcome plus a pending decision event when the locked
    transaction made no business writes (the caller persists it on a
    dedicated transaction). Mutating outcomes commit their event on the
    locked connection and return no pending event.
    """
    if not get_database_config().is_configured:
        raise DatabaseNotConfiguredError("database_not_configured")
    try:
        validated = PropertyCreate.model_validate(build_property_payload(body))
    except Exception as exc:
        raise IntakeError(
            f"listing failed operational validation: {exc}",
            reason_code=REASON_VALIDATION_FAILED,
        ) from exc
    data = validated.model_dump()
    mapped = [key for key in ESTIMATED_FIELDS if data.get(key) is not None]
    evidence_payload = build_evidence_payload(body, mapped)
    distress_rows = build_distress_rows(body)
    source_url = str(body.listing_url)
    keys = _intake_lock_keys(data, source_url)
    ctx = _DecisionContext(body=body, conn=None, run_id=run_id, started=started, source_url=source_url)
    try:
        with get_connection() as conn:
            ctx.conn = conn
            try:
                with conn.cursor() as cur:
                    acquire_intake_locks(cur, keys)
                return _process_intake_locked(ctx, data, mapped, evidence_payload, distress_rows)
            except (IntakeError, ValueError, DatabaseNotConfiguredError, DatabaseUnavailableError):
                _rollback_quietly(conn)
                raise
            except Exception as exc:
                _rollback_quietly(conn)
                raise DatabaseUnavailableError("database_error") from exc
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
        _best_effort_decision(
            _decision_event(
                ctx,
                OUTCOME_ATTEMPT_FAILED,
                REASON_DATABASE_FAILURE,
                f"intake database failure: {type(exc).__name__}",
                MUTATION_ROLLED_BACK if ctx.writes_attempted else MUTATION_NOT_ATTEMPTED,
                (),
                (),
                None,
                Severity.ERROR,
            )
        )
        raise


def _process_intake_locked(
    ctx: _DecisionContext,
    data: dict[str, Any],
    mapped: list[str],
    evidence_payload: dict[str, Any],
    distress_rows: list[dict[str, Any]],
) -> tuple[IntakeResult, AgentEvent | None]:
    """Decide and persist one listing inside a locked transaction.

    Runs after advisory locks are held, so every identity check below
    observes post-lock state and no same-key operation can commit
    concurrently. Write paths commit exactly once (business rows plus
    the decision event together); read-only outcomes return their
    decision event for a dedicated post-transaction write.
    """
    body = ctx.body
    conn = ctx.conn
    source_url = ctx.source_url
    address_match = match_existing_address(data)
    if address_match.status is AddressMatchStatus.AMBIGUOUS:
        # Several stored properties share this normalized address: select
        # none of them. No enrichment, no evidence, no new record -- the
        # ambiguity itself is the finding, for human identity review.
        candidates = tuple(address_match.candidate_ids)
        return _finish_decision(
            ctx,
            outcome=IntakeOutcome.NEEDS_IDENTITY_REVIEW,
            reason_code=REASON_AMBIGUOUS_ADDRESS,
            property_id="",
            message=(
                "address matches multiple stored properties; identity is "
                "ambiguous, nothing was changed, no evidence attached; "
                "human identity review required"
            ),
            commit=False,
            considered=candidates,
            conflicting=candidates,
        )
    if address_match.status is AddressMatchStatus.NONE:
        if source_url:
            # No address match: a repeated listing URL must not mint
            # another property, whether addressed or addressless.
            # Missing/unusable URLs keep existing behavior (the request
            # model requires a valid URL).
            duplicate, meta = _resolve_url_association(body, data, source_url)
            if duplicate is not None:
                event = _decision_event(
                    ctx,
                    duplicate.outcome.value,
                    meta.reason_code,
                    duplicate.message,
                    MUTATION_NOT_ATTEMPTED,
                    meta.considered,
                    meta.conflicting,
                    duplicate,
                )
                return duplicate, event
        ctx.writes_attempted = True
        row = create_property_in_transaction(conn, data)
        property_id = str(row["id"])
        evidence = create_listing_evidence_in_transaction(
            conn,
            property_id=property_id,
            source_name=body.source_name,
            source_url=source_url,
            observed_at=body.observed_at,
            listing_type=body.listing_type.value,
            payload=evidence_payload,
        )
        distress_ids = create_distress_observations_in_transaction(conn, property_id, distress_rows)
        return _finish_decision(
            ctx,
            outcome=IntakeOutcome.CREATED,
            reason_code=REASON_NEW_IDENTITY,
            property_id=property_id,
            message="listing accepted; new operational record created",
            commit=True,
            filled=mapped,
            evidence_id=str(evidence["id"]),
            distress_ids=distress_ids,
        )

    # Duplicate address: enrich NULL estimated fields only, never overwrite.
    existing_id = address_match.property_id
    if existing_id is None:  # pragma: no cover - UNIQUE always carries the id
        raise IntakeError(
            "address match lost its identity; resubmit the listing",
            reason_code=REASON_RACED_STATE,
        )
    existing = get_property(UUID(str(existing_id)))
    if existing is None:  # pragma: no cover - raced deletion; fail closed
        raise IntakeError(
            "duplicate target disappeared; resubmit the listing",
            reason_code=REASON_RACED_STATE,
        )
    if _identity_ambiguous(body, existing):
        # Same address but a materially different building name: the two
        # listings may describe different buildings. Preserve everything
        # unchanged -- no enrichment, no evidence, no second record -- and
        # require human identity review.
        return _finish_decision(
            ctx,
            outcome=IntakeOutcome.NEEDS_IDENTITY_REVIEW,
            reason_code=REASON_NAME_CONFLICT,
            property_id=str(existing_id),
            message=(
                "same address but a materially different building name; "
                "identity is ambiguous, existing record preserved unchanged, "
                "no evidence attached; human identity review required"
            ),
            commit=False,
            considered=(str(existing_id),),
            conflicting=(str(existing_id),),
        )
    url_associated = find_evidence_by_url(source_url)
    foreign = sorted({str(link["property_id"]) for link in url_associated if str(link["property_id"]) != str(existing_id)})
    if foreign:
        # This listing URL already evidences a different property while
        # the address matches this one: the signals disagree. Review
        # with zero writes; select neither property.
        parties = (str(existing_id), *foreign)
        return _finish_decision(
            ctx,
            outcome=IntakeOutcome.NEEDS_IDENTITY_REVIEW,
            reason_code=REASON_URL_ADDRESS_DISAGREEMENT,
            property_id="",
            message=(
                "listing URL already recorded for a different property; "
                "nothing was changed, no evidence attached; "
                "human identity review required"
            ),
            commit=False,
            considered=parties,
            conflicting=parties,
        )
    fills = {
        key: data[key] for key in ESTIMATED_FIELDS
        if data.get(key) is not None and existing.get(key) is None
    }
    message = "duplicate address; existing record preserved"
    fills_dropped = False
    if fills:
        merged = {**existing, **fills}
        try:
            PropertyCreate.model_validate(merged)
        except Exception:
            fills = {}
            fills_dropped = True
            message = (
                "duplicate address; conflicting values skipped to protect "
                "the existing record"
            )
    already_recorded = get_listing_evidence(str(existing_id), source_url) is not None
    if not fills and already_recorded:
        return _finish_decision(
            ctx,
            outcome=IntakeOutcome.SKIPPED_DUPLICATE,
            reason_code=REASON_DUPLICATE_ADDRESS_RECORDED,
            property_id=str(existing_id),
            message="duplicate listing already recorded; nothing changed",
            commit=False,
            considered=(str(existing_id),),
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
    ctx.writes_attempted = True
    filled = enrich_property_estimates_in_transaction(conn, UUID(str(existing_id)), fills, merged_evidence or None, merged_status)
    evidence_id: str | None = None
    distress_ids: list[str] = []
    if not already_recorded:
        evidence = create_listing_evidence_in_transaction(
            conn,
            property_id=str(existing_id),
            source_name=body.source_name,
            source_url=source_url,
            observed_at=body.observed_at,
            listing_type=body.listing_type.value,
            payload=evidence_payload,
        )
        evidence_id = str(evidence["id"])
        distress_ids = create_distress_observations_in_transaction(conn, str(existing_id), distress_rows)
    if filled:
        reason_code = REASON_NULL_FILL
    elif fills_dropped:
        reason_code = REASON_MERGE_VALIDATION_CONFLICT
    else:
        reason_code = REASON_EVIDENCE_ONLY
    return _finish_decision(
        ctx,
        outcome=IntakeOutcome.ENRICHED,
        reason_code=reason_code,
        property_id=str(existing_id),
        message=message if not fills else "existing record enriched with new attributable values",
        commit=True,
        considered=(str(existing_id),),
        filled=filled,
        evidence_id=evidence_id,
        distress_ids=distress_ids,
    )
