"""Gabriel discovery API models (request bounds and run results)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.properties import PropertyCategory

MAX_RUN_LIMIT = 25
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 60.0
MAX_BBOX_SPAN_DEGREES = 0.5

DISCOVERY_CATEGORIES = (
    PropertyCategory.HIGH_RISE.value,
    PropertyCategory.DOWNTOWN_COMMERCIAL.value,
    PropertyCategory.WAREHOUSE_INDUSTRIAL.value,
)


class BoundingBoxInput(BaseModel):
    """South/west/north/east bounds with span caps to bound query cost."""

    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)

    @field_validator("north")
    @classmethod
    def _check_lat_order(cls, value: float, info) -> float:  # type: ignore[no-untyped-def]
        south = (info.data or {}).get("south")
        if south is not None:
            if value <= south:
                raise ValueError("north must exceed south")
            if value - south > MAX_BBOX_SPAN_DEGREES:
                raise ValueError(f"latitude span exceeds {MAX_BBOX_SPAN_DEGREES} degrees")
        return value

    @field_validator("east")
    @classmethod
    def _check_lon_order(cls, value: float, info) -> float:  # type: ignore[no-untyped-def]
        west = (info.data or {}).get("west")
        if west is not None:
            if value <= west:
                raise ValueError("east must exceed west")
            if value - west > MAX_BBOX_SPAN_DEGREES:
                raise ValueError(f"longitude span exceeds {MAX_BBOX_SPAN_DEGREES} degrees")
        return value


class DiscoveryRunRequest(BaseModel):
    """Request body for POST /discovery/runs."""

    source: str = Field(default="overpass", min_length=1, max_length=100)
    bbox: BoundingBoxInput
    categories: list[str] | None = Field(default=None, max_length=10)
    limit: int = Field(default=10, ge=1, le=MAX_RUN_LIMIT)
    timeout_seconds: float = Field(default=15.0, ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [str(item).strip().lower() for item in value]
        allowed = {*DISCOVERY_CATEGORIES, "other"}
        for item in cleaned:
            if item not in allowed:
                raise ValueError(f"unknown discovery category: {item}")
        return cleaned


class SkippedCandidate(BaseModel):
    """A candidate Gabriel saw but did not persist, with the reason."""

    external_id: str
    reason: str
    name: str | None = None
    street_address: str | None = None
    reason_code: str = ""


class DiscoveryRunCounts(BaseModel):
    returned: int = 0
    created: int = 0
    skipped_duplicate: int = 0
    skipped_unusable: int = 0


class DiscoveryRunResult(BaseModel):
    """Outcome of one bounded discovery run (no background jobs)."""

    source: str
    retrieved_at: str
    categories: list[str]
    limit: int
    counts: DiscoveryRunCounts = DiscoveryRunCounts()
    created_ids: list[str] = []
    skipped: list[SkippedCandidate] = []
    run_id: str = ""


class DiscoverySourceInfo(BaseModel):
    """Metadata for one registered discovery provider."""

    name: str
    description: str
    docs_url: str
    requires_credentials: bool
