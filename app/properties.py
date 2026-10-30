"""Shared property record for the seven-agent pipeline (MVP-002).

The operational record explicitly separates ESTIMATED values from VERIFIED
values within one persistent record. Numeric facts come in estimated/verified
pairs; unknown values stay NULL, never zero. Hypothetical values are not
permitted in operational records.

Integrity rules (enforced here, at the API boundary):
- Estimates must be sourced: any populated estimated field requires
  traceable record-level ``provenance``.
- Verified fields require supporting per-field evidence references in
  ``field_evidence``.
- ``data_status`` (verified / estimated / mixed / unknown) is derived
  server-side from the populated estimated/verified fields. A client
  label is never trusted, and ``hypothetical`` is rejected outright.
- Vacant area must not exceed building area within the same evidence
  class (cross-class pairs are not comparable and not checked).

Known limitation: a client-provided "verified" label or evidence
reference does not independently establish truth. MVP-002 enforces
traceability structurally (required, substantive, non-placeholder
sources) but cannot confirm cited facts against external sources; a
review workflow and trusted-source registry are future work.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


class PropertyCategory(str, Enum):
    """Accepted property categories. Note: ``empty_lot`` is accepted by
    the data model but remains outside Gabriel's initial discovery scope."""

    HIGH_RISE = "high_rise"
    COMMERCIAL_PROPERTY = "commercial_property"
    WAREHOUSE_INDUSTRIAL = "warehouse_industrial"
    EMPTY_LOT = "empty_lot"
    OTHER = "other"


class DataStatus(str, Enum):
    """Record-level evidence class, always derived server-side."""

    VERIFIED = "verified"
    ESTIMATED = "estimated"
    MIXED = "mixed"
    UNKNOWN = "unknown"


ESTIMATED_FIELDS = (
    "building_area_sqft_estimated",
    "vacant_area_sqft_estimated",
    "gross_monthly_rent_income_estimated",
    "owner_monthly_costs_estimated",
    "property_value_estimated",
    "liens_total_estimated",
    "months_on_market_estimated",
    "months_vacant_estimated",
)

VERIFIED_FIELDS = (
    "building_area_sqft_verified",
    "vacant_area_sqft_verified",
    "gross_monthly_rent_income_verified",
    "owner_monthly_costs_verified",
    "property_value_verified",
    "liens_total_verified",
    "months_on_market_verified",
    "months_vacant_verified",
)

EVIDENCED_FIELDS = ESTIMATED_FIELDS + VERIFIED_FIELDS

MONEY_FIELDS = (
    "gross_monthly_rent_income_estimated",
    "gross_monthly_rent_income_verified",
    "owner_monthly_costs_estimated",
    "owner_monthly_costs_verified",
    "property_value_estimated",
    "property_value_verified",
    "liens_total_estimated",
    "liens_total_verified",
)

_CATEGORY_ALIASES = {
    "highrise": PropertyCategory.HIGH_RISE,
    "warehouse": PropertyCategory.WAREHOUSE_INDUSTRIAL,
    "industrial": PropertyCategory.WAREHOUSE_INDUSTRIAL,
    # Retired canonical category (B5-2A.2b-0): historical input only.
    # Never emitted by canonical search plans or new persistence.
    "downtown_commercial": PropertyCategory.COMMERCIAL_PROPERTY,
}

MIN_PROVENANCE_LENGTH = 20

# Exact (case-insensitive) provenance values that carry no traceable evidence.
_PLACEHOLDER_PROVENANCE = frozenset(
    {
        "unknown",
        "n/a",
        "na",
        "tbd",
        "t.b.d.",
        "none",
        "no provenance",
        "test",
        "testing",
        "example",
        "sample",
        "placeholder",
        "synthetic",
        "fictional",
        "dummy",
        "xxx",
        "-",
        ".",
    }
)

# Substrings marking a source as unverified by definition.
_UNVERIFIED_SOURCE_MARKERS = (
    "synthetic",
    "hypothetical",
    "fictional",
    "invented",
    "placeholder",
)


def _normalize_category(value: object) -> PropertyCategory:
    """Map common category spellings to one canonical value."""
    if isinstance(value, PropertyCategory):
        return value
    text = str(value).strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    while "__" in text:
        text = text.replace("__", "_")
    if text in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[text]
    return PropertyCategory(text)


def _to_decimal(value: object) -> Decimal | None:
    """Convert money input to Decimal without floating-point artifacts.

    Floats pass through ``str()`` so 0.1 stays Decimal("0.1") instead of
    picking up binary representation error. Prefer JSON strings upstream.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, bool):
        raise ValueError("monetary value must be numeric, not boolean")
    return Decimal(str(value).strip())


def check_source_text(value: object, *, field_name: str, min_length: int) -> str:
    """Validate a source/evidence string. Returns the stripped value.

    Raises ValueError when the text is missing, too short, a known
    placeholder, or explicitly unverified (synthetic, hypothetical, ...).
    """
    text = str(value).strip() if value is not None else ""
    if len(text) < min_length:
        raise ValueError(
            f"{field_name} must name a traceable source (at least {min_length} characters)"
        )
    lowered = text.lower()
    if lowered in _PLACEHOLDER_PROVENANCE:
        raise ValueError(f"{field_name} must name a traceable source, not a placeholder")
    for marker in _UNVERIFIED_SOURCE_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"{field_name} marked {marker!r} is not acceptable evidence for "
                "an operational record"
            )
    return text


def derive_data_status(values: Mapping[str, Any]) -> DataStatus:
    """Derive the record status from populated estimated/verified fields."""
    has_estimated = any(values.get(name) is not None for name in ESTIMATED_FIELDS)
    has_verified = any(values.get(name) is not None for name in VERIFIED_FIELDS)
    if has_estimated and has_verified:
        return DataStatus.MIXED
    if has_verified:
        return DataStatus.VERIFIED
    if has_estimated:
        return DataStatus.ESTIMATED
    return DataStatus.UNKNOWN


class PropertyBase(BaseModel):
    """Common operational property fields. Unknown information stays null."""

    model_config = {"populate_by_name": True}

    name: str = Field(min_length=1, max_length=300)
    street_address: str | None = Field(
        default=None,
        max_length=500,
        validation_alias=AliasChoices("street_address", "address", "street"),
    )
    city: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, max_length=100)
    zip_code: str | None = Field(
        default=None,
        max_length=20,
        validation_alias=AliasChoices("zip_code", "zip", "zipcode", "postal_code"),
    )
    category: PropertyCategory
    building_area_sqft_estimated: float | None = Field(default=None, ge=0)
    building_area_sqft_verified: float | None = Field(default=None, ge=0)
    vacant_area_sqft_estimated: float | None = Field(default=None, ge=0)
    vacant_area_sqft_verified: float | None = Field(default=None, ge=0)
    occupancy: str | None = Field(
        default=None,
        max_length=2000,
        validation_alias=AliasChoices("occupancy", "occupancy_info", "existing_occupancy"),
    )
    gross_monthly_rent_income_estimated: Decimal | None = Field(default=None, ge=0)
    gross_monthly_rent_income_verified: Decimal | None = Field(default=None, ge=0)
    owner: str | None = Field(default=None, max_length=300)
    owner_monthly_costs_estimated: Decimal | None = Field(default=None, ge=0)
    owner_monthly_costs_verified: Decimal | None = Field(default=None, ge=0)
    property_value_estimated: Decimal | None = Field(default=None, ge=0)
    property_value_verified: Decimal | None = Field(default=None, ge=0)
    liens_total_estimated: Decimal | None = Field(default=None, ge=0)
    liens_total_verified: Decimal | None = Field(default=None, ge=0)
    # Distress indicators (nullable whole months; NULL means unknown).
    # months_on_market (listed for sale) and months_vacant (space empty)
    # are separate measurements and are never treated as equivalent.
    months_on_market_estimated: int | None = Field(default=None, ge=0)
    months_on_market_verified: int | None = Field(default=None, ge=0)
    months_vacant_estimated: int | None = Field(default=None, ge=0)
    months_vacant_verified: int | None = Field(default=None, ge=0)
    provenance: str | None = Field(
        default=None,
        max_length=500,
        validation_alias=AliasChoices("provenance", "data_source", "source"),
    )
    field_evidence: dict[str, str] | None = Field(
        default=None,
        validation_alias=AliasChoices("field_evidence", "evidence"),
    )
    data_status: DataStatus = Field(
        default=DataStatus.UNKNOWN,
        validation_alias=AliasChoices(
            "data_status", "status", "confidence", "data_confidence"
        ),
    )
    notes: str | None = Field(
        default=None,
        max_length=5000,
        validation_alias=AliasChoices("notes", "additional_notes", "note"),
    )

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, value: object) -> PropertyCategory:
        return _normalize_category(value)

    @field_validator(*MONEY_FIELDS, mode="before")
    @classmethod
    def _coerce_money(cls, value: object) -> Decimal | None:
        return _to_decimal(value)

    @field_validator("provenance")
    @classmethod
    def _check_provenance(cls, value: object) -> str | None:
        if value is None:
            return None
        return check_source_text(value, field_name="provenance", min_length=MIN_PROVENANCE_LENGTH)

    @field_validator("field_evidence")
    @classmethod
    def _check_field_evidence(cls, value: object) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("field_evidence must map field names to evidence references")
        cleaned: dict[str, str] = {}
        for key, ref in value.items():
            name = str(key).strip()
            if name not in EVIDENCED_FIELDS:
                raise ValueError(
                    f"field_evidence key {name!r} is not an estimated/verified field"
                )
            cleaned[name] = check_source_text(ref, field_name=f"evidence for {name}", min_length=3)
        return cleaned

    @field_validator("data_status", mode="before")
    @classmethod
    def _screen_status(cls, value: object) -> DataStatus:
        # The client's label is screened, never trusted: hypothetical is
        # rejected outright and anything else is overwritten by derivation.
        if value is None:
            return DataStatus.UNKNOWN
        if isinstance(value, DataStatus):
            return value
        text = str(value).strip().lower()
        if text == "hypothetical":
            raise ValueError("hypothetical values are not permitted in operational records")
        return DataStatus(text)

    @model_validator(mode="after")
    def _enforce_evidence_rules(self) -> PropertyBase:
        for suffix in ("estimated", "verified"):
            building = getattr(self, f"building_area_sqft_{suffix}")
            vacant = getattr(self, f"vacant_area_sqft_{suffix}")
            if building is not None and vacant is not None and vacant > building:
                raise ValueError(
                    f"vacant_area_sqft_{suffix} cannot exceed building_area_sqft_{suffix}"
                )
        evidence = self.field_evidence or {}
        for name in VERIFIED_FIELDS:
            if getattr(self, name) is not None and not evidence.get(name):
                raise ValueError(
                    f"{name} is populated but has no supporting reference in field_evidence"
                )
        populated = [n for n in EVIDENCED_FIELDS if getattr(self, n) is not None]
        if populated and self.provenance is None:
            raise ValueError(
                "provenance is required when estimated or verified values are present"
            )
        self.data_status = derive_data_status({name: getattr(self, name) for name in EVIDENCED_FIELDS})
        return self


class PropertyCreate(PropertyBase):
    """Request body for POST /properties (server assigns id/timestamps/status)."""


class Property(PropertyBase):
    """Stored operational property record returned by the API."""

    id: UUID
    created_at: datetime
    updated_at: datetime
    # Downtown priority attribute (B5-2A.2b-0): False means downtown
    # priority is NOT established (non-downtown or uncertain). True
    # requires credible property-specific evidence explicitly locating
    # the property downtown. Read-model only: callers cannot set it via
    # PropertyCreate, and positive determination is a deferred step.
    is_downtown: bool = False
