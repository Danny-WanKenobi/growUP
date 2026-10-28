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
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.agents.gabriel.dedup import (
    AddressMatch,
    AddressMatchStatus,
    dedup_key,
    match_existing_address,
    normalize_text,
)
from app.agents.gabriel.normalize import normalize_listing
from app.agents.gabriel.schemas import (
    DISCOVERY_CATEGORIES,
    DiscoveryRunRequest,
    DiscoveryRunResult,
    SkippedCandidate,
)
from app.agents.gabriel.sources.base import BoundingBox, RawListing, get_source
from app.config import get_database_config
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties import PropertyCreate
from app.properties_db import (
    create_property,
    create_property_in_transaction,
    get_connection,
    get_property,
)
from app.source_observations_db import (
    canonical_source,
    find_observation,
    record_observation_in_transaction,
)

logger = logging.getLogger(__name__)


def _tracked_source(item: RawListing) -> tuple[str, str] | None:
    """Canonical (source, external_id) for observation tracking, if mappable.

    Unknown provider namespaces and blank external ids are untracked:
    existing behavior is preserved for them (no observation recorded).
    """
    external_id = item.external_id
    if not isinstance(external_id, str) or not external_id.strip():
        return None
    canonical = canonical_source(item.source)
    if canonical is None:
        return None
    return (canonical, external_id)


def _rollback_quietly(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _create_with_observation(
    payload: dict[str, Any], canonical: str, external_id: str
) -> dict[str, Any] | None:
    """Insert a property and claim its source link in one transaction.

    Returns the committed row, or None when a competing transaction
    claimed the source first (everything rolled back; the caller must
    re-read the winner instead of retrying).
    """
    with get_connection() as conn:
        try:
            row = create_property_in_transaction(conn, payload)
            link = record_observation_in_transaction(conn, canonical, external_id, str(row["id"]))
        except ValueError:
            _rollback_quietly(conn)
            raise
        except (DatabaseNotConfiguredError, DatabaseUnavailableError):
            _rollback_quietly(conn)
            raise
        except Exception as exc:
            _rollback_quietly(conn)
            raise DatabaseUnavailableError("database_error") from exc
        if link is None:
            conn.rollback()
            return None
        conn.commit()
        return row


def _resolve_recorded_source(
    item: RawListing,
    validated: dict[str, Any],
    address_match: AddressMatch,
    recorded_pid: str,
) -> SkippedCandidate:
    """Build the skip for a previously recorded source object (no writes).

    Never selects a different property: conflicts and ambiguities are
    reported for review; consistent re-observations cite the recorded
    link. Incoming addresses are never populated onto the property.
    """
    existing = get_property(UUID(recorded_pid))
    stored_street = normalize_text(existing.get("street_address")) if existing else ""
    incoming_street = normalize_text(validated.get("street_address"))
    if incoming_street:
        if address_match.status is AddressMatchStatus.AMBIGUOUS:
            reason = (
                f"source previously recorded as property {recorded_pid}; "
                "address now matches multiple properties; held for review"
            )
        elif (
            address_match.status is AddressMatchStatus.UNIQUE
            and str(address_match.property_id) != recorded_pid
        ):
            reason = (
                f"source previously recorded as property {recorded_pid}; "
                f"address matches different property {address_match.property_id}; "
                "held for review"
            )
        elif stored_street and stored_street != incoming_street:
            reason = (
                f"source previously recorded as property {recorded_pid}; "
                "recorded address conflicts with incoming address; held for review"
            )
        elif not stored_street:
            reason = (
                f"source already recorded as property {recorded_pid}; "
                "incoming address not populated without verification"
            )
        else:
            reason = f"source already recorded as property {recorded_pid}"
    else:
        reason = f"source already recorded as property {recorded_pid}"
    return SkippedCandidate(
        external_id=item.external_id,
        reason=reason,
        name=item.name,
        street_address=item.street_address,
    )


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
        key = dedup_key(
            validated.model_dump(),
            source=item.source,
            external_id=item.external_id,
            latitude=item.latitude,
            longitude=item.longitude,
        )
        if key is not None and key in seen:
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
        if key is not None:
            seen.add(key)
        address_match = match_existing_address(validated.model_dump())
        tracked = _tracked_source(item)
        recorded_pid: str | None = None
        if tracked is not None:
            observation = find_observation(*tracked)
            if observation is not None:
                recorded_pid = str(observation["property_id"])
        if recorded_pid is not None:
            result.counts.skipped_duplicate += 1
            result.skipped.append(
                _resolve_recorded_source(item, validated.model_dump(), address_match, recorded_pid)
            )
            continue
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
            # No observation link is recorded on an address match alone.
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
        if tracked is None:
            row = create_property(validated.model_dump())
            result.counts.created += 1
            result.created_ids.append(str(row["id"]))
            continue
        row = _create_with_observation(validated.model_dump(), *tracked)
        if row is None:
            # Lost the registration race: re-read the winner (committed
            # before our conflict resolved) instead of retrying.
            winner = find_observation(*tracked)
            if winner is None:  # pragma: no cover - unreachable: conflict implies commit
                reason = "source registration raced with another run; held for review"
            else:
                reason = f"source already recorded as property {winner['property_id']}"
            result.counts.skipped_duplicate += 1
            result.skipped.append(
                SkippedCandidate(
                    external_id=item.external_id,
                    reason=reason,
                    name=item.name,
                    street_address=item.street_address,
                )
            )
            continue
        result.counts.created += 1
        result.created_ids.append(str(row["id"]))
    return result
