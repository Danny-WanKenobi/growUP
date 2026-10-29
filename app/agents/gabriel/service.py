"""Gabriel discovery orchestration: one bounded run, start to finish.

A run researches one provider, normalizes candidates through the
MVP-002 validation gate, skips duplicates and unusable leads, and
persists only new, valid, sourced records. Gabriel never updates
existing rows, so verified values cannot be overwritten by discovery.
Provider failures abort the run with nothing fabricated.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
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
from app.agents.gabriel.observe import new_run_id
from app.agents.gabriel.schemas import (
    DISCOVERY_CATEGORIES,
    DiscoveryRunRequest,
    DiscoveryRunResult,
    SkippedCandidate,
)
from app.agents.gabriel.sources.base import (
    BoundingBox,
    RawListing,
    SourceError,
    UnknownSourceError,
    get_source,
)
from app.config import get_database_config
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity_reviews_db import record_review_occurrence
from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity
from app.observability.store import insert_event_in_transaction
from app.properties import PropertyCreate
from app.properties_db import (
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

DECISION_EVENT_TYPE = "gabriel.discovery.decision"
RUN_EVENT_TYPE = "gabriel.discovery.run"

MUTATION_NOT_ATTEMPTED = "NOT_ATTEMPTED"
MUTATION_COMMITTED = "COMMITTED"
MUTATION_ROLLED_BACK = "ROLLED_BACK"

REASON_NEW_IDENTITY = "NEW_IDENTITY"
REASON_INSUFFICIENT_LOCATION = "INSUFFICIENT_LOCATION"
REASON_NORMALIZE_ERROR = "NORMALIZE_ERROR"
REASON_VALIDATION_FAILED = "VALIDATION_FAILED"
REASON_IN_RUN_DUPLICATE = "IN_RUN_DUPLICATE"
REASON_ALREADY_RECORDED = "ALREADY_RECORDED"
REASON_ADDRESS_WITHHELD = "ADDRESS_WITHHELD"
REASON_ADDRESS_MATCHED_STORED = "ADDRESS_MATCHED_STORED"
REASON_AMBIGUOUS_ADDRESS = "AMBIGUOUS_ADDRESS"
REASON_RECORDED_MULTI_MATCH = "RECORDED_MULTI_MATCH"
REASON_RECORDED_DIFFERENT_PROPERTY = "RECORDED_DIFFERENT_PROPERTY"
REASON_RECORDED_ADDRESS_CONFLICT = "RECORDED_ADDRESS_CONFLICT"
REASON_REGISTRATION_RACE_LOST = "REGISTRATION_RACE_LOST"
REASON_DATABASE_FAILURE = "DATABASE_FAILURE"
REASON_SOURCE_FAILURE = "SOURCE_FAILURE"
REASON_UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
REASON_RUN_FINISHED = "RUN_FINISHED"

REVIEW_CODES = frozenset(
    {
        REASON_RECORDED_MULTI_MATCH,
        REASON_RECORDED_DIFFERENT_PROPERTY,
        REASON_RECORDED_ADDRESS_CONFLICT,
        REASON_AMBIGUOUS_ADDRESS,
    }
)


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


def _capped(value: Any, limit: int) -> str | None:
    """Verbatim provider string within a bound (None when absent or junk)."""
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _capped_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    return None


@dataclass
class _RunContext:
    """Mutable per-run decision state (never persisted directly)."""

    run_id: str
    started: float
    result: DiscoveryRunResult | None = None
    review_holds: int = 0
    warnings: int = 0


@dataclass(frozen=True)
class _RecordedMeta:
    """Identity references behind one recorded-source outcome (provenance only)."""

    reason_code: str
    considered: tuple[str, ...] = ()
    conflicting: tuple[str, ...] = ()
    selected_pid: str | None = None
    observation_id: str | None = None


def _observed_from_item(item: RawListing) -> dict[str, Any]:
    """Explicitly supplied provider fields, verbatim within bounds."""
    return {
        "source": _capped(item.source, 100),
        "external_id": _capped(item.external_id, 200),
        "element_url": _capped(item.element_url, 500),
        "name": _capped(item.name, 300),
        "street_address": _capped(item.street_address, 500),
        "city": _capped(item.city, 200),
        "state": _capped(item.state, 100),
        "zip_code": _capped(item.zip_code, 20),
        "latitude": _capped_float(item.latitude),
        "longitude": _capped_float(item.longitude),
        "category_hint": _capped(item.category_hint, 100),
        "retrieved_at": _capped(item.retrieved_at, 64),
    }


def _inferred_for(
    item: RawListing, payload: dict[str, Any] | None, dedup_key_value: str | None
) -> dict[str, Any]:
    """Derived identity inputs exactly as the pipeline observes them.

    Canonical source mapping, normalized matching inputs, the operative
    in-run dedup key, and whether the display name was synthesized are
    derivations -- never directly observed values.
    """
    tracked = _tracked_source(item)
    derived = payload is not None and not item.name and bool(payload.get("name"))
    return {
        "canonical_source": tracked[0] if tracked is not None else None,
        "normalized_name": normalize_text(item.name),
        "normalized_address": {
            "street_address": normalize_text(item.street_address),
            "city": normalize_text(item.city),
            "state": normalize_text(item.state),
            "zip_code": normalize_text(item.zip_code),
        },
        "dedup_key": dedup_key_value[:1000] if isinstance(dedup_key_value, str) else None,
        "display_name_derived": bool(derived),
    }


def _build_candidate_event(
    *,
    run_id: str,
    index: int,
    started: float,
    item: RawListing,
    payload: dict[str, Any] | None,
    dedup_key_value: str | None,
    outcome: str,
    reason_code: str,
    message: str,
    mutation_status: str,
    considered: tuple[str, ...] = (),
    conflicting: tuple[str, ...] = (),
    result_property_id: str = "",
    column_pid: str | None = None,
    observation_id: str | None = None,
    duplicate_of_index: int | None = None,
    severity: Severity = Severity.INFO,
) -> AgentEvent:
    """Build (validating, no I/O) one permanent candidate decision event."""
    try:
        known_property: UUID | None = UUID(column_pid) if column_pid else None
    except (ValueError, AttributeError):
        known_property = None
    refs: list[dict[str, str]] = []
    tracked = _tracked_source(item)
    if tracked is not None:
        refs.append({"kind": "source_observation", "ref": f"{tracked[0]}:{tracked[1]}"[:200]})
    element_url = _capped(item.element_url, 500)
    if element_url is not None:
        refs.append({"kind": "source_url", "ref": element_url})
    return AgentEvent(
        agent=AgentId.GABRIEL,
        run_id=run_id,
        event_type=DECISION_EVENT_TYPE,
        summary=f"discovery decision: {outcome} ({reason_code})",
        severity=severity,
        retention_class=RetentionClass.PERMANENT,
        details={
            "outcome": outcome,
            "reason_code": reason_code,
            "message": message,
            "mutation_status": mutation_status,
            "observed": _observed_from_item(item),
            "inferred": _inferred_for(item, payload, dedup_key_value),
            "considered_property_ids": list(considered),
            "conflicting_property_ids": list(conflicting),
            "result": {
                "property_id": result_property_id,
                "observation_id": observation_id,
                "evidence_id": None,
                "distress_ids": [],
                "filled_fields": [],
                "candidate_index": index,
                "duplicate_of_index": duplicate_of_index,
            },
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        property_id=known_property,
        evidence_refs=refs,
    )


def _build_run_event(
    *,
    run_id: str,
    started: float,
    outcome: str,
    reason_code: str,
    message: str,
    source: str,
    categories: list[str],
    limit: int,
    bbox: str,
    returned: int,
    created: int,
    skipped_duplicate: int,
    skipped_unusable: int,
    review_holds: int,
    warnings: int,
    errors: int,
    failed_candidate: dict[str, Any] | None,
    severity: Severity = Severity.INFO,
) -> AgentEvent:
    """Build (validating, no I/O) one permanent run-summary event."""
    return AgentEvent(
        agent=AgentId.GABRIEL,
        run_id=run_id,
        event_type=RUN_EVENT_TYPE,
        summary=f"discovery run: {outcome} ({reason_code})",
        severity=severity,
        retention_class=RetentionClass.PERMANENT,
        details={
            "outcome": outcome,
            "reason_code": reason_code,
            "message": message,
            "source": source,
            "categories": list(categories),
            "limit": limit,
            "bbox": bbox,
            "counts": {
                "returned": returned,
                "created": created,
                "skipped_duplicate": skipped_duplicate,
                "skipped_unusable": skipped_unusable,
                "review_holds": review_holds,
            },
            "warnings": warnings,
            "errors": errors,
            "failed_candidate": failed_candidate,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )


def _persist_decision_dedicated(event: AgentEvent) -> None:
    """Persist one decision event in its own transaction (commits once).

    Used when no business transaction is open. Raises
    :class:`DatabaseUnavailableError` when persistence fails.
    """
    with get_connection() as conn:
        insert_event_in_transaction(conn, event)
        record_review_occurrence(conn, event)
        conn.commit()


def _best_effort_decision(event: AgentEvent) -> bool:
    """Attempt one run-event write without masking the real outcome."""
    try:
        _persist_decision_dedicated(event)
    except Exception as exc:
        logger.warning(
            "gabriel discovery event not persisted: %s (%s)", event.event_id, type(exc).__name__
        )
        return False
    return True


def _create_with_observation(
    payload: dict[str, Any],
    canonical: str,
    external_id: str,
    *,
    make_audit: Callable[[dict[str, Any], dict[str, Any]], AgentEvent] | None = None,
) -> dict[str, Any] | None:
    """Insert a property and claim its source link in one transaction.

    Returns the committed row, or None when a competing transaction
    claimed the source first (everything rolled back; the caller must
    re-read the winner instead of retrying). When ``make_audit`` is
    provided, the decision event it builds from the committed row and
    link commits atomically with them; audit failure rolls back the
    entire candidate transaction.
    """
    with get_connection() as conn:
        try:
            row = create_property_in_transaction(conn, payload)
            link = record_observation_in_transaction(conn, canonical, external_id, str(row["id"]))
            if link is None:
                conn.rollback()
                return None
            if make_audit is not None:
                insert_event_in_transaction(conn, make_audit(row, link))
        except ValueError:
            _rollback_quietly(conn)
            raise
        except (DatabaseNotConfiguredError, DatabaseUnavailableError):
            _rollback_quietly(conn)
            raise
        except Exception as exc:
            _rollback_quietly(conn)
            raise DatabaseUnavailableError("database_error") from exc
        conn.commit()
        return row


def _create_without_observation(
    payload: dict[str, Any],
    *,
    make_audit: Callable[[dict[str, Any], None], AgentEvent] | None = None,
) -> dict[str, Any]:
    """Insert one untracked property, atomically with its decision event.

    Mirrors the tracked creation transaction without the observation
    claim. Any failure before commit rolls back business and audit
    writes together.
    """
    with get_connection() as conn:
        try:
            row = create_property_in_transaction(conn, payload)
            if make_audit is not None:
                insert_event_in_transaction(conn, make_audit(row, None))
        except ValueError:
            _rollback_quietly(conn)
            raise
        except (DatabaseNotConfiguredError, DatabaseUnavailableError):
            _rollback_quietly(conn)
            raise
        except Exception as exc:
            _rollback_quietly(conn)
            raise DatabaseUnavailableError("database_error") from exc
        conn.commit()
        return row


def _resolve_recorded_source(
    item: RawListing,
    validated: dict[str, Any],
    address_match: AddressMatch,
    recorded_pid: str,
    observation_id: str | None = None,
) -> tuple[str, _RecordedMeta]:
    """Decide a previously recorded source object (no writes).

    Returns the response reason plus provenance metadata. Never selects
    a different property: conflicts and ambiguities are reported for
    review; consistent re-observations cite the recorded link. Incoming
    addresses are never populated onto the property.
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
            candidates = tuple(address_match.candidate_ids)
            meta = _RecordedMeta(
                REASON_RECORDED_MULTI_MATCH,
                considered=(recorded_pid, *candidates),
                conflicting=(recorded_pid, *candidates),
                observation_id=observation_id,
            )
        elif (
            address_match.status is AddressMatchStatus.UNIQUE
            and str(address_match.property_id) != recorded_pid
        ):
            matched_pid = str(address_match.property_id)
            reason = (
                f"source previously recorded as property {recorded_pid}; "
                f"address matches different property {matched_pid}; "
                "held for review"
            )
            meta = _RecordedMeta(
                REASON_RECORDED_DIFFERENT_PROPERTY,
                considered=(recorded_pid, matched_pid),
                conflicting=(recorded_pid, matched_pid),
                observation_id=observation_id,
            )
        elif stored_street and stored_street != incoming_street:
            reason = (
                f"source previously recorded as property {recorded_pid}; "
                "recorded address conflicts with incoming address; held for review"
            )
            meta = _RecordedMeta(
                REASON_RECORDED_ADDRESS_CONFLICT,
                considered=(recorded_pid,),
                conflicting=(recorded_pid,),
                selected_pid=recorded_pid,
                observation_id=observation_id,
            )
        elif not stored_street:
            reason = (
                f"source already recorded as property {recorded_pid}; "
                "incoming address not populated without verification"
            )
            meta = _RecordedMeta(
                REASON_ADDRESS_WITHHELD,
                considered=(recorded_pid,),
                selected_pid=recorded_pid,
                observation_id=observation_id,
            )
        else:
            reason = f"source already recorded as property {recorded_pid}"
            meta = _RecordedMeta(
                REASON_ALREADY_RECORDED,
                considered=(recorded_pid,),
                selected_pid=recorded_pid,
                observation_id=observation_id,
            )
    else:
        reason = f"source already recorded as property {recorded_pid}"
        meta = _RecordedMeta(
            REASON_ALREADY_RECORDED,
            considered=(recorded_pid,),
            selected_pid=recorded_pid,
            observation_id=observation_id,
        )
    return reason, meta


def _record_skip(
    ctx: _RunContext,
    item: RawListing,
    index: int,
    candidate_started: float,
    *,
    unusable: bool,
    reason_code: str,
    message: str,
    event_message: str | None = None,
    payload: dict[str, Any] | None = None,
    dedup_key_value: str | None = None,
    considered: tuple[str, ...] = (),
    conflicting: tuple[str, ...] = (),
    selected_pid: str | None = None,
    observation_id: str | None = None,
    duplicate_of_index: int | None = None,
) -> None:
    """Append one skip to the response and persist its decision event.

    Single choke point for every read-only candidate outcome: the
    response entry and the permanent event are always recorded together.
    Event persistence failure raises, aborting the run rather than
    returning an unrecorded decision.
    """
    assert ctx.result is not None
    ctx.result.skipped.append(
        SkippedCandidate(
            external_id=item.external_id,
            reason=message,
            name=item.name,
            street_address=item.street_address,
            reason_code=reason_code,
        )
    )
    if unusable:
        ctx.result.counts.skipped_unusable += 1
    else:
        ctx.result.counts.skipped_duplicate += 1
    if reason_code in REVIEW_CODES:
        ctx.review_holds += 1
    event = _build_candidate_event(
        run_id=ctx.run_id,
        index=index,
        started=candidate_started,
        item=item,
        payload=payload,
        dedup_key_value=dedup_key_value,
        outcome="skipped_unusable" if unusable else "skipped_duplicate",
        reason_code=reason_code,
        message=event_message if event_message is not None else message,
        mutation_status=MUTATION_NOT_ATTEMPTED,
        considered=considered,
        conflicting=conflicting,
        column_pid=selected_pid,
        observation_id=observation_id,
        duplicate_of_index=duplicate_of_index,
    )
    _persist_decision_dedicated(event)


def run_discovery(request: DiscoveryRunRequest) -> DiscoveryRunResult:
    """Execute one bounded discovery run and persist new records.

    Every successfully processed candidate records one permanent
    decision event; the run records one best-effort summary. Candidates
    stay transactionally independent: a failure aborts the run but
    never rolls back earlier committed candidates.
    """
    if not get_database_config().is_configured:
        # Fail fast before spending an external call.
        raise DatabaseNotConfiguredError("database_not_configured")
    run_id = new_run_id()
    started = time.perf_counter()
    ctx = _RunContext(run_id=run_id, started=started)
    categories = list(request.categories) if request.categories else list(DISCOVERY_CATEGORIES)
    bbox = BoundingBox(
        south=request.bbox.south,
        west=request.bbox.west,
        north=request.bbox.north,
        east=request.bbox.east,
    )
    bbox_label = f"bbox({bbox.south},{bbox.west},{bbox.north},{bbox.east})"
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    resolved_source: str | None = None
    failed_index: int | None = None
    failed_external: str | None = None

    def _abort(reason_code: str, severity: Severity) -> None:
        failed = (
            {"index": failed_index, "external_id": failed_external}
            if failed_index is not None
            else None
        )
        counts = ctx.result.counts if ctx.result is not None else None
        _best_effort_decision(
            _build_run_event(
                run_id=run_id,
                started=started,
                outcome="run_aborted",
                reason_code=reason_code,
                message=f"discovery run aborted ({reason_code})",
                source=resolved_source or request.source,
                categories=categories,
                limit=request.limit,
                bbox=bbox_label,
                returned=counts.returned if counts is not None else 0,
                created=counts.created if counts is not None else 0,
                skipped_duplicate=counts.skipped_duplicate if counts is not None else 0,
                skipped_unusable=counts.skipped_unusable if counts is not None else 0,
                review_holds=ctx.review_holds,
                warnings=ctx.warnings,
                errors=1,
                failed_candidate=failed,
                severity=severity,
            )
        )

    try:
        source = get_source(request.source)
        resolved_source = source.name
        listings = source.search(bbox, categories, request.limit, request.timeout_seconds)

        result = DiscoveryRunResult(
            source=source.name,
            retrieved_at=retrieved_at,
            categories=categories,
            limit=request.limit,
            run_id=run_id,
        )
        ctx.result = result
        result.counts.returned = len(listings)
        seen: dict[str, int] = {}
        for index, item in enumerate(listings):
            failed_index = index
            failed_external = item.external_id if isinstance(item.external_id, str) else None
            candidate_started = time.perf_counter()
            normalize_failed = False
            try:
                payload = normalize_listing(item, bbox_label)
            except Exception as exc:
                logger.warning("gabriel normalize failed: %s", type(exc).__name__)
                ctx.warnings += 1
                payload = None
                normalize_failed = True
            if payload is None:
                _record_skip(
                    ctx, item, index, candidate_started,
                    unusable=True,
                    reason_code=REASON_NORMALIZE_ERROR if normalize_failed else REASON_INSUFFICIENT_LOCATION,
                    message="insufficient location information (no name, address, or coordinates)",
                )
                continue
            try:
                validated = PropertyCreate.model_validate(payload)
            except ValidationError as exc:
                logger.warning("gabriel candidate failed validation: %s", type(exc).__name__)
                ctx.warnings += 1
                _record_skip(
                    ctx, item, index, candidate_started,
                    unusable=True,
                    reason_code=REASON_VALIDATION_FAILED,
                    message="failed operational validation gate",
                    payload=payload,
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
                _record_skip(
                    ctx, item, index, candidate_started,
                    unusable=False,
                    reason_code=REASON_IN_RUN_DUPLICATE,
                    message="duplicate within this run",
                    payload=payload,
                    dedup_key_value=key,
                    duplicate_of_index=seen[key],
                )
                continue
            if key is not None:
                seen[key] = index
            validated_dump = validated.model_dump()
            address_match = match_existing_address(validated_dump)
            tracked = _tracked_source(item)
            recorded = find_observation(*tracked) if tracked is not None else None
            if recorded is not None:
                recorded_pid = str(recorded["property_id"])
                obs_id = recorded.get("id")
                reason, meta = _resolve_recorded_source(
                    item, validated_dump, address_match, recorded_pid,
                    str(obs_id) if obs_id is not None else None,
                )
                _record_skip(
                    ctx, item, index, candidate_started,
                    unusable=False,
                    reason_code=meta.reason_code,
                    message=reason,
                    payload=payload,
                    dedup_key_value=key,
                    considered=meta.considered,
                    conflicting=meta.conflicting,
                    selected_pid=meta.selected_pid,
                    observation_id=meta.observation_id,
                )
                continue
            if address_match.status is AddressMatchStatus.AMBIGUOUS:
                # Several stored properties share this address: report the
                # ambiguity accurately instead of claiming one of them.
                # Write-free, as always: nothing is created or updated.
                candidates = tuple(address_match.candidate_ids)
                _record_skip(
                    ctx, item, index, candidate_started,
                    unusable=False,
                    reason_code=REASON_AMBIGUOUS_ADDRESS,
                    message="address matches multiple stored properties; identity ambiguous, held for review",
                    payload=payload,
                    dedup_key_value=key,
                    considered=candidates,
                    conflicting=candidates,
                )
                continue
            if address_match.status is AddressMatchStatus.UNIQUE:
                # Never update: existing (possibly verified) values stand.
                # No observation link is recorded on an address match alone.
                matched_pid = str(address_match.property_id)
                _record_skip(
                    ctx, item, index, candidate_started,
                    unusable=False,
                    reason_code=REASON_ADDRESS_MATCHED_STORED,
                    message=f"address already stored as property {address_match.property_id}",
                    payload=payload,
                    dedup_key_value=key,
                    considered=(matched_pid,),
                    selected_pid=matched_pid,
                )
                continue
            if tracked is None:
                def _audit_untracked(row: dict[str, Any], link: None) -> AgentEvent:
                    # Invoked synchronously within this iteration; loop
                    # variables below still hold this candidate's values.
                    return _build_candidate_event(
                        run_id=run_id,
                        index=index,
                        started=candidate_started,
                        item=item,
                        payload=payload,
                        dedup_key_value=key,
                        outcome="created",
                        reason_code=REASON_NEW_IDENTITY,
                        message=f"created property {row['id']} from untracked {item.source} observation",
                        mutation_status=MUTATION_COMMITTED,
                        result_property_id=str(row["id"]),
                        column_pid=str(row["id"]),
                    )

                row = _create_without_observation(payload, make_audit=_audit_untracked)
                result.counts.created += 1
                result.created_ids.append(str(row["id"]))
                continue
            canonical, external_id = tracked

            def _audit_tracked(row: dict[str, Any], link: dict[str, Any]) -> AgentEvent:
                # Invoked synchronously within this iteration; loop
                # variables below still hold this candidate's values.
                return _build_candidate_event(
                    run_id=run_id,
                    index=index,
                    started=candidate_started,
                    item=item,
                    payload=payload,
                    dedup_key_value=key,
                    outcome="created",
                    reason_code=REASON_NEW_IDENTITY,
                    message=f"created property {row['id']} from {canonical} observation {external_id}",
                    mutation_status=MUTATION_COMMITTED,
                    result_property_id=str(row["id"]),
                    column_pid=str(row["id"]),
                    observation_id=str(link["id"]),
                )

            row = _create_with_observation(payload, canonical, external_id, make_audit=_audit_tracked)
            if row is None:
                # Lost the registration race: re-read the winner (committed
                # before our conflict resolved) instead of retrying.
                winner = find_observation(*tracked)
                if winner is None:  # pragma: no cover - unreachable: conflict implies commit
                    reason = "source registration raced with another run; held for review"
                    considered: tuple[str, ...] = ()
                    selected: str | None = None
                else:
                    winner_pid = str(winner["property_id"])
                    reason = f"source already recorded as property {winner_pid}"
                    considered = (winner_pid,)
                    selected = winner_pid
                _record_skip(
                    ctx, item, index, candidate_started,
                    unusable=False,
                    reason_code=REASON_REGISTRATION_RACE_LOST,
                    message=reason,
                    event_message=f"{reason}; speculative registration work rolled back",
                    payload=payload,
                    dedup_key_value=key,
                    considered=considered,
                    selected_pid=selected,
                )
                continue
            result.counts.created += 1
            result.created_ids.append(str(row["id"]))
    except UnknownSourceError:
        _abort(REASON_UNKNOWN_SOURCE, Severity.WARNING)
        raise
    except SourceError:
        _abort(REASON_SOURCE_FAILURE, Severity.ERROR)
        raise
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        _abort(REASON_DATABASE_FAILURE, Severity.ERROR)
        raise
    except ValueError:
        _abort(REASON_VALIDATION_FAILED, Severity.ERROR)
        raise
    except Exception:
        _abort(REASON_DATABASE_FAILURE, Severity.ERROR)
        raise
    assert ctx.result is not None
    completed = ctx.result
    summary = _build_run_event(
        run_id=run_id,
        started=started,
        outcome="run_completed",
        reason_code=REASON_RUN_FINISHED,
        message=(
            f"discovery run completed: {completed.counts.created} created, "
            f"{completed.counts.skipped_duplicate + completed.counts.skipped_unusable} skipped"
        ),
        source=source.name,
        categories=categories,
        limit=request.limit,
        bbox=bbox_label,
        returned=completed.counts.returned,
        created=completed.counts.created,
        skipped_duplicate=completed.counts.skipped_duplicate,
        skipped_unusable=completed.counts.skipped_unusable,
        review_holds=ctx.review_holds,
        warnings=ctx.warnings,
        errors=0,
        failed_candidate=None,
    )
    try:
        _persist_decision_dedicated(summary)
    except Exception as exc:
        logger.warning(
            "gabriel discovery run summary NOT durable: run_id=%s source=%s created=%d skipped=%d (%s)",
            run_id,
            source.name,
            completed.counts.created,
            completed.counts.skipped_duplicate + completed.counts.skipped_unusable,
            type(exc).__name__,
        )
    return completed
