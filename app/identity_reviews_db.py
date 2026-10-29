"""Durable identity-review queue writes (IR-3A).

Builds exact-match review cases from permanent Gabriel decision events
and records one occurrence row per counted encounter. The caller owns
the PostgreSQL transaction: event, review, and occurrence rows commit
together, so a decision is never durable without its queue records.

Review identity is exact-match only -- normalized names and addresses
plus verbatim URLs, external ids, and the sorted conflicting-property
set. A shared property id alone never merges two cases.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability.events import RetentionClass

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()

INTAKE_DECISION_EVENT = "gabriel.intake.decision"
DISCOVERY_DECISION_EVENT = "gabriel.discovery.decision"

INTAKE_REVIEW_OUTCOME = "needs_identity_review"
MUTATION_NOT_ATTEMPTED = "NOT_ATTEMPTED"

# Literal reason codes (pinned to the producing branches by
# tests/test_identity_reviews.py::test_review_reason_constants_match_production_branches).
INTAKE_REVIEW_REASONS = frozenset(
    {
        "URL_MULTI_ASSOC",
        "URL_CONFLICT",
        "AMBIGUOUS_ADDRESS",
        "NAME_CONFLICT",
        "URL_ADDRESS_DISAGREEMENT",
    }
)
DISCOVERY_REVIEW_REASONS = frozenset(
    {
        "RECORDED_MULTI_MATCH",
        "RECORDED_DIFFERENT_PROPERTY",
        "RECORDED_ADDRESS_CONFLICT",
        "AMBIGUOUS_ADDRESS",
    }
)

SOURCE_INTAKE = "intake"
SOURCE_DISCOVERY = "discovery"

_DEDUP_VERSION = 1


class IdentityReviewError(ValueError):
    """A review-shaped event lacks the identity data its queue row requires."""


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise IdentityReviewError(f"review event missing {field}")
    return value


def _conflicting_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise IdentityReviewError("operational review requires a conflicting property id")
    ids: list[str] = []
    for item in raw:
        try:
            ids.append(str(UUID(str(item))))
        except (ValueError, AttributeError) as exc:
            raise IdentityReviewError("conflicting property id is not a UUID") from exc
    return sorted(set(ids))


def _intake_key_dims(observed: dict[str, Any], inferred: Any) -> dict[str, Any]:
    if not isinstance(inferred, dict):
        raise IdentityReviewError("review event missing inferred identity")
    dims = {
        "name": inferred.get("name"),
        "addr": [
            inferred.get("street_address"),
            inferred.get("city"),
            inferred.get("state"),
            inferred.get("zip_code"),
        ],
        "url": observed.get("listing_url"),
    }
    if not isinstance(dims["name"], str) or not all(isinstance(part, str) for part in dims["addr"]):
        raise IdentityReviewError("review event missing inferred identity")
    if dims["url"] is not None and not isinstance(dims["url"], str):
        raise IdentityReviewError("review event has an unusable listing URL")
    return dims


def _discovery_key_dims(observed: dict[str, Any], inferred: Any) -> dict[str, Any]:
    if not isinstance(inferred, dict) or not isinstance(inferred.get("normalized_address"), dict):
        raise IdentityReviewError("review event missing inferred identity")
    addr = inferred["normalized_address"]
    dims = {
        "src": inferred.get("canonical_source") or observed.get("source"),
        "ext": observed.get("external_id"),
        "name": inferred.get("normalized_name"),
        "addr": [addr.get("street_address"), addr.get("city"), addr.get("state"), addr.get("zip_code")],
        "url": observed.get("element_url"),
    }
    if not isinstance(dims["src"], str) or not dims["src"]:
        raise IdentityReviewError("review event missing source identity")
    if not isinstance(dims["name"], str) or not all(isinstance(part, str) for part in dims["addr"]):
        raise IdentityReviewError("review event missing inferred identity")
    for field in ("ext", "url"):
        if dims[field] is not None and not isinstance(dims[field], str):
            raise IdentityReviewError(f"review event has an unusable {field}")
    return dims


def review_context_from_event(event: Any) -> dict[str, Any] | None:
    """Translate one permanent decision event into a queue-write context.

    Returns None for events that are not PENDING-eligible operational
    reviews (creations, enrichments, duplicates, rejections, failures,
    run summaries, research verdicts). Raises IdentityReviewError when
    the event is review-shaped but its required identity data is
    missing or contradictory -- the caller must fail closed.
    """
    details = getattr(event, "details", None)
    if not isinstance(details, dict):
        return None
    event_type = getattr(event, "event_type", None)
    outcome = details.get("outcome")
    reason = details.get("reason_code")
    mutation = details.get("mutation_status")
    if event_type == INTAKE_DECISION_EVENT:
        if outcome != INTAKE_REVIEW_OUTCOME or reason not in INTAKE_REVIEW_REASONS:
            if outcome == INTAKE_REVIEW_OUTCOME:
                raise IdentityReviewError(f"unsupported intake review reason: {reason!r}")
            return None
        source = SOURCE_INTAKE
    elif event_type == DISCOVERY_DECISION_EVENT:
        if reason not in DISCOVERY_REVIEW_REASONS:
            return None
        if not (isinstance(outcome, str) and outcome.startswith("skipped_")):
            raise IdentityReviewError(f"unsupported discovery review outcome: {outcome!r}")
        source = SOURCE_DISCOVERY
    else:
        return None
    if mutation != MUTATION_NOT_ATTEMPTED:
        raise IdentityReviewError("review outcome must not claim a property mutation")
    if getattr(event, "retention_class", None) != RetentionClass.PERMANENT:
        raise IdentityReviewError("review linkage requires a permanent decision event")
    run_id = _require_text(getattr(event, "run_id", None), "run_id")
    event_id = _require_text(getattr(event, "event_id", None), "event_id")
    observed = details.get("observed")
    if not isinstance(observed, dict):
        raise IdentityReviewError("review event missing observed identity")
    pids = _conflicting_ids(details.get("conflicting_property_ids"))
    if source == SOURCE_INTAKE:
        provider = observed.get("source_name")
        listing_url = observed.get("listing_url")
        dims = _intake_key_dims(observed, details.get("inferred"))
    else:
        provider = observed.get("source")
        listing_url = observed.get("element_url")
        dims = _discovery_key_dims(observed, details.get("inferred"))
    if provider is not None and not isinstance(provider, str):
        raise IdentityReviewError("review event has an unusable provider")
    dedup_key = json.dumps(
        {"v": _DEDUP_VERSION, "source": source, "reason": reason, "pids": pids, "q": dims},
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "source": source,
        "reason_code": reason,
        "dedup_key": dedup_key,
        "observed_identity": dict(observed),
        "provider": provider,
        "external_id": observed.get("external_id"),
        "listing_url": listing_url,
        "conflicting_property_ids": pids,
        "decision_event_id": event_id,
        "run_id": run_id,
    }


_UPSERT_SQL = (
    "INSERT INTO identity_reviews (review_id, dedup_key, source, reason_code, "
    "observed_identity, provider, external_id, listing_url, conflicting_property_ids, "
    "decision_event_id, run_id, occurrence_count, last_event_id) "
    "VALUES (%(review_id)s, %(dedup_key)s, %(source)s, %(reason_code)s, "
    "%(observed_identity)s, %(provider)s, %(external_id)s, %(listing_url)s, "
    "CAST(%(conflicting_property_ids)s AS uuid[]), %(decision_event_id)s, %(run_id)s, "
    "1, %(decision_event_id)s) "
    "ON CONFLICT (dedup_key) WHERE status = 'PENDING' DO UPDATE SET "
    "occurrence_count = identity_reviews.occurrence_count + 1, "
    "last_event_id = EXCLUDED.last_event_id, updated_at = EXCLUDED.updated_at "
    "RETURNING review_id"
)

_OCCURRENCE_SQL = (
    "INSERT INTO identity_review_occurrences (occurrence_id, review_id, event_id) "
    "VALUES (%(occurrence_id)s, %(review_id)s, %(event_id)s)"
)


def record_review_occurrence(conn: Any, event: Any) -> str | None:
    """Insert or join the pending case for one decision event, atomically.

    Runs on the caller's open connection without committing: the review
    upsert and its occurrence row commit or roll back with the
    surrounding event transaction. Returns the case id, or None when
    the event is not a queue-eligible operational review.
    """
    ctx = review_context_from_event(event)
    if ctx is None:
        return None
    params = {
        "review_id": str(uuid4()),
        "dedup_key": ctx["dedup_key"],
        "source": ctx["source"],
        "reason_code": ctx["reason_code"],
        "observed_identity": json.dumps(ctx["observed_identity"], sort_keys=True),
        "provider": ctx["provider"],
        "external_id": ctx["external_id"],
        "listing_url": ctx["listing_url"],
        "conflicting_property_ids": list(ctx["conflicting_property_ids"]),
        "decision_event_id": ctx["decision_event_id"],
        "run_id": ctx["run_id"],
    }
    try:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, params)
            row = cur.fetchone()
            if row is None:
                raise IdentityReviewError("review upsert returned no case")
            review_id = row["review_id"] if isinstance(row, dict) else row[0]
            cur.execute(
                _OCCURRENCE_SQL,
                {
                    "occurrence_id": str(uuid4()),
                    "review_id": str(review_id),
                    "event_id": ctx["decision_event_id"],
                },
            )
    except IdentityReviewError:
        raise
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except _DRIVER_ERRORS as exc:
        # Mirror insert_event_in_transaction: driver failures become the
        # controlled outage signal so service/route handling returns 503.
        # Validation and programming errors propagate unchanged.
        raise DatabaseUnavailableError("review_insert_failed") from exc
    return str(review_id)
