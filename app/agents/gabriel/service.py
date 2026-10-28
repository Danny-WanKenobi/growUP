"""Gabriel discovery orchestration: one bounded run, start to finish.

A run researches one provider, normalizes candidates through the
MVP-002 validation gate, skips duplicates and unusable leads, and
persists only new, valid, sourced records. Gabriel never updates
existing rows, so verified values cannot be overwritten by discovery.
Provider failures abort the run with nothing fabricated.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import ValidationError

from app.agents.gabriel.dedup import AddressMatchStatus, dedup_key, match_existing_address
from app.agents.gabriel.normalize import normalize_listing
from app.agents.gabriel.schemas import (
    DISCOVERY_CATEGORIES,
    DiscoveryRunRequest,
    DiscoveryRunResult,
    SkippedCandidate,
)
from app.agents.gabriel.sources.base import BoundingBox, get_source
from app.config import get_database_config
from app.db import DatabaseNotConfiguredError
from app.properties import PropertyCreate
from app.properties_db import create_property

logger = logging.getLogger(__name__)


def run_discovery(request: DiscoveryRunRequest) -> DiscoveryRunResult:
    """Execute one bounded discovery run and persist new records."""
    if not get_database_config().is_configured:
        # Fail fast before spending an external call.
        raise DatabaseNotConfiguredError("database_not_configured")
    source = get_source(request.source)
    categories = list(request.categories) if request.categories else list(DISCOVERY_CATEGORIES)
    bbox = BoundingBox(
        south=request.bbox.south,
        west=request.bbox.west,
        north=request.bbox.north,
        east=request.bbox.east,
    )
    bbox_label = f"bbox({bbox.south},{bbox.west},{bbox.north},{bbox.east})"
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    listings = source.search(bbox, categories, request.limit, request.timeout_seconds)

    result = DiscoveryRunResult(
        source=source.name,
        retrieved_at=retrieved_at,
        categories=categories,
        limit=request.limit,
    )
    result.counts.returned = len(listings)
    seen: set[str] = set()
    for item in listings:
        try:
            payload = normalize_listing(item, bbox_label)
        except Exception as exc:
            logger.warning("gabriel normalize failed: %s", type(exc).__name__)
            payload = None
        if payload is None:
            result.counts.skipped_unusable += 1
            result.skipped.append(
                SkippedCandidate(
                    external_id=item.external_id,
                    reason="insufficient location information (no name, address, or coordinates)",
                    name=item.name,
                    street_address=item.street_address,
                )
            )
            continue
        try:
            validated = PropertyCreate.model_validate(payload)
        except ValidationError as exc:
            logger.warning("gabriel candidate failed validation: %s", type(exc).__name__)
            result.counts.skipped_unusable += 1
            result.skipped.append(
                SkippedCandidate(
                    external_id=item.external_id,
                    reason="failed operational validation gate",
                    name=item.name,
                    street_address=item.street_address,
                )
            )
            continue
        key = dedup_key(validated.model_dump())
        if key in seen:
            result.counts.skipped_duplicate += 1
            result.skipped.append(
                SkippedCandidate(
                    external_id=item.external_id,
                    reason="duplicate within this run",
                    name=item.name,
                    street_address=item.street_address,
                )
            )
            continue
        seen.add(key)
        address_match = match_existing_address(validated.model_dump())
        if address_match.status is AddressMatchStatus.AMBIGUOUS:
            # Several stored properties share this address: report the
            # ambiguity accurately instead of claiming one of them.
            # Write-free, as always: nothing is created or updated.
            result.counts.skipped_duplicate += 1
            result.skipped.append(
                SkippedCandidate(
                    external_id=item.external_id,
                    reason="address matches multiple stored properties; identity ambiguous, held for review",
                    name=item.name,
                    street_address=item.street_address,
                )
            )
            continue
        if address_match.status is AddressMatchStatus.UNIQUE:
            # Never update: existing (possibly verified) values stand.
            result.counts.skipped_duplicate += 1
            result.skipped.append(
                SkippedCandidate(
                    external_id=item.external_id,
                    reason=f"address already stored as property {address_match.property_id}",
                    name=item.name,
                    street_address=item.street_address,
                )
            )
            continue
        row = create_property(validated.model_dump())
        result.counts.created += 1
        result.created_ids.append(str(row["id"]))
    return result
