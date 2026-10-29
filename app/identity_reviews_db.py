"""Durable identity-review queue writes (IR-3A, IR-3C C1a).

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
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability.events import RetentionClass
from app.properties_db import get_connection

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
SOURCE_RESEARCH = "research"

# Literal research verdict types (pinned to the producing builders by
# tests/test_research_review_writer.py, which builds real verdicts).
RESEARCH_VERDICT_EVENTS = frozenset(
    {
        "gabriel.research.candidate_decision",
        "gabriel.dossier.verdict",
    }
)
RESEARCH_REASON_IDENTITY_DISPUTE = "IDENTITY_DISPUTE"

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


def _research_key_dims(details: dict[str, Any], run_id: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Canonical research dedup dimensions plus the validated identity payload.

    Returns ``(dims, candidate_key, disputes)``. The dims key on the
    full candidate group key, the grouping basis, and the sorted
    disputed FIELD names -- never the disputed values, so evolving
    observations of one identity question stay in one case. Bare
    ``ungrouped:`` ordinals repeat every run, so those candidates are
    additionally scoped to their originating run.
    """
    ref = details.get("candidate_ref")
    if not isinstance(ref, dict):
        raise IdentityReviewError("research verdict missing candidate reference")
    key = ref.get("group_key")
    if not isinstance(key, str) or not key.strip():
        raise IdentityReviewError("research verdict missing candidate identity")
    basis = ref.get("basis")
    if basis is None or (isinstance(basis, str) and not basis.strip()):
        basis = "unknown"
    if not isinstance(basis, str):
        raise IdentityReviewError("research verdict has an unusable grouping basis")
    disputes = details.get("identity_disputes")
    if not isinstance(disputes, dict) or not disputes:
        raise IdentityReviewError("research verdict missing identity disputes")
    fields: list[str] = []
    for field, variants in disputes.items():
        if not isinstance(variants, list):
            raise IdentityReviewError("research verdict has a malformed identity dispute")
        if not any(str(variant).strip() for variant in variants):
            raise IdentityReviewError("research verdict dispute has no observed variants")
        fields.append(field)
    dims: dict[str, Any] = {
        "candidate_key": key,
        "basis": basis,
        "disputed_fields": sorted(fields),
    }
    if key.startswith("ungrouped:"):
        dims["run_id"] = run_id
    return dims, key, dict(disputes)


def review_context_from_event(event: Any) -> dict[str, Any] | None:
    """Translate one permanent decision event into a queue-write context.

    Returns None for events that are not PENDING-eligible reviews
    (creations, enrichments, duplicates, rejections, failures, run
    summaries, verdicts without identity disputes). Raises
    IdentityReviewError when the event is review-shaped but its
    required identity data is missing or contradictory -- the caller
    must fail closed.
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
    elif event_type in RESEARCH_VERDICT_EVENTS:
        # Eligibility depends on a nonempty disputes object, never on a
        # reason code: clean and screened verdicts carry no disputes.
        disputes = details.get("identity_disputes")
        if not isinstance(disputes, dict) or not disputes:
            return None
        source = SOURCE_RESEARCH
        reason = RESEARCH_REASON_IDENTITY_DISPUTE
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
    candidate_key: str | None = None
    identity_disputes: dict[str, Any] | None = None
    if source == SOURCE_RESEARCH:
        # Research candidates are never operational properties: no
        # property association may be invented or inherited here.
        if getattr(event, "property_id", None) is not None:
            raise IdentityReviewError("research review must not claim an operational property")
        dims, candidate_key, identity_disputes = _research_key_dims(details, run_id)
        pids: list[str] = []
        provider = None
        listing_url = None
    else:
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
        "candidate_key": candidate_key,
        "identity_disputes": identity_disputes,
        "decision_event_id": event_id,
        "run_id": run_id,
    }


_UPSERT_SQL = (
    "INSERT INTO identity_reviews (review_id, dedup_key, source, reason_code, "
    "observed_identity, provider, external_id, listing_url, conflicting_property_ids, "
    "candidate_key, identity_disputes, "
    "decision_event_id, run_id, occurrence_count, last_event_id) "
    "VALUES (%(review_id)s, %(dedup_key)s, %(source)s, %(reason_code)s, "
    "%(observed_identity)s, %(provider)s, %(external_id)s, %(listing_url)s, "
    "CAST(%(conflicting_property_ids)s AS uuid[]), %(candidate_key)s, %(identity_disputes)s, "
    "%(decision_event_id)s, %(run_id)s, "
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
    the event is not a queue-eligible review.
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
        "candidate_key": ctx["candidate_key"],
        "identity_disputes": (
            json.dumps(ctx["identity_disputes"], sort_keys=True)
            if ctx["identity_disputes"] is not None
            else None
        ),
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


# ---------------------------------------------------------------------------
# Read-only inspection (IR-3B). SELECT-only helpers; they never insert,
# update, or delete rows. Error handling mirrors list_properties.
# ---------------------------------------------------------------------------

REVIEW_LIST_DEFAULT_LIMIT = 50
REVIEW_LIST_MAX_LIMIT = 200
REVIEW_OCCURRENCE_LIMIT = 200

_REVIEW_COLUMNS = [
    "review_id",
    "dedup_key",
    "source",
    "reason_code",
    "observed_identity",
    "provider",
    "external_id",
    "listing_url",
    "conflicting_property_ids",
    "candidate_key",
    "identity_disputes",
    "decision_event_id",
    "run_id",
    "status",
    "occurrence_count",
    "last_event_id",
    "supersedes_id",
    "created_at",
    "updated_at",
]

_REVIEW_FILTER_COLUMNS = {
    "status": "status",
    "source": "source",
    "reason_code": "reason_code",
}


def _review_filter_clauses(
    filters: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build an exact-match WHERE fragment from validated filter values.

    Only allowlisted columns can be filtered and every value travels as
    a bound parameter, so filter input can never alter query structure.
    Unknown values simply match nothing.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for name, column in _REVIEW_FILTER_COLUMNS.items():
        value = filters.get(name)
        if value is None:
            continue
        clauses.append(f"{column} = %(filter_{name})s")
        params[f"filter_{name}"] = value
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def list_identity_reviews(
    *,
    status: str | None = None,
    source: str | None = None,
    reason_code: str | None = None,
    limit: int = REVIEW_LIST_DEFAULT_LIMIT,
    offset: int = 0,
    config: DatabaseConfig | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return one page of review cases plus the total matching count.

    Stable newest-activity-first ordering. The total uses the same
    filters as the page query.
    """
    limit = max(1, min(limit, REVIEW_LIST_MAX_LIMIT))
    offset = max(0, offset)
    where, params = _review_filter_clauses(
        {"status": status, "source": source, "reason_code": reason_code}
    )
    columns = ", ".join(_REVIEW_COLUMNS)
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {columns} FROM identity_reviews {where} "
                    "ORDER BY updated_at DESC, review_id DESC "
                    "LIMIT %(limit)s OFFSET %(offset)s",
                    {**params, "limit": limit, "offset": offset},
                )
                rows = cur.fetchall()
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM identity_reviews {where}",
                    dict(params),
                )
                total_row = cur.fetchone()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    total = total_row["n"] if isinstance(total_row, dict) else total_row[0]
    return [dict(row) for row in rows], int(total)


def get_identity_review(
    review_id: Any,
    config: DatabaseConfig | None = None,
) -> dict[str, Any] | None:
    """Return one review case with its occurrence history and provenance.

    Occurrences arrive oldest-first, bounded to REVIEW_OCCURRENCE_LIMIT
    with an explicit truncation flag; occurrence_count stays the
    authority. A missing decision event is a state the queue writer
    cannot produce, so the call fails closed instead of serving a
    case without provenance.
    """
    columns = ", ".join(_REVIEW_COLUMNS)
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {columns} FROM identity_reviews WHERE review_id = %(id)s",
                    {"id": str(review_id)},
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute(
                    "SELECT occurrence_id, event_id, created_at "
                    "FROM identity_review_occurrences WHERE review_id = %(id)s "
                    "ORDER BY created_at ASC, occurrence_id ASC "
                    "LIMIT %(limit)s",
                    {"id": str(review_id), "limit": REVIEW_OCCURRENCE_LIMIT + 1},
                )
                occurrence_rows = [dict(item) for item in cur.fetchall()]
                cur.execute(
                    "SELECT event_id, event_type, details->>'outcome' AS outcome, "
                    "details->>'reason_code' AS reason_code, occurred_at "
                    "FROM agent_events WHERE event_id = %(event_id)s",
                    {"event_id": row["decision_event_id"]},
                )
                event_row = cur.fetchone()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    if event_row is None:
        raise DatabaseUnavailableError("review_provenance_missing")
    detail = dict(row)
    detail["occurrences"] = occurrence_rows[:REVIEW_OCCURRENCE_LIMIT]
    detail["occurrences_truncated"] = len(occurrence_rows) > REVIEW_OCCURRENCE_LIMIT
    detail["decision_event"] = dict(event_row)
    return detail


_SUMMARY_SQL = (
    "WITH cases AS ("
    " SELECT review_id, status, source, occurrence_count"
    " FROM identity_reviews"
    " WHERE (%(source)s IS NULL OR source = %(source)s)"
    " AND (%(run_id)s IS NULL OR run_id = %(run_id)s)"
    " AND created_at >= %(since)s AND created_at < %(until)s"
    "), occ AS ("
    " SELECT o.review_id, COUNT(*) AS n"
    " FROM identity_review_occurrences o JOIN cases c USING (review_id)"
    " GROUP BY o.review_id"
    "), dec AS ("
    " SELECT d.review_id, d.outcome, d.decided_at"
    " FROM identity_review_decisions d JOIN cases c ON c.review_id = d.review_id"
    ") SELECT json_build_object("
    " 'total_cases', (SELECT COUNT(*) FROM cases),"
    " 'pending_cases', (SELECT COUNT(*) FROM cases WHERE status = 'PENDING'),"
    " 'resolved_cases', (SELECT COUNT(*) FROM cases WHERE status = 'RESOLVED'),"
    " 'total_occurrences', (SELECT COALESCE(SUM(n), 0) FROM occ),"
    " 'unresolved_occurrences', (SELECT COALESCE(SUM(o.n), 0) FROM occ o"
    " JOIN cases c USING (review_id) WHERE c.status = 'PENDING'),"
    " 'total_decisions', (SELECT COUNT(*) FROM dec),"
    " 'same_property', (SELECT COUNT(*) FROM dec WHERE outcome = 'SAME_PROPERTY'),"
    " 'distinct_properties', (SELECT COUNT(*) FROM dec WHERE outcome = 'DISTINCT_PROPERTIES'),"
    " 'by_source', (SELECT COALESCE(json_object_agg(source, n), '{}')"
    " FROM (SELECT source, COUNT(*) AS n FROM cases GROUP BY source) s),"
    " 'bucket_one', (SELECT COUNT(*) FROM cases c LEFT JOIN occ o USING (review_id)"
    " WHERE COALESCE(o.n, 0) = 1),"
    " 'bucket_two_three', (SELECT COUNT(*) FROM cases c LEFT JOIN occ o USING (review_id)"
    " WHERE COALESCE(o.n, 0) BETWEEN 2 AND 3),"
    " 'bucket_four_plus', (SELECT COUNT(*) FROM cases c LEFT JOIN occ o USING (review_id)"
    " WHERE COALESCE(o.n, 0) >= 4),"
    " 'by_day', (SELECT COALESCE(json_agg(row ORDER BY day), '[]')"
    " FROM (SELECT DATE(decided_at AT TIME ZONE 'UTC')::text AS day, COUNT(*) AS count"
    " FROM dec WHERE decided_at >= %(since)s AND decided_at < %(until)s"
    " GROUP BY 1) row),"
    " 'resolved_without_decision', (SELECT COUNT(*) FROM cases c"
    " WHERE c.status = 'RESOLVED'"
    " AND NOT EXISTS (SELECT 1 FROM dec WHERE dec.review_id = c.review_id)),"
    " 'pending_with_decision', (SELECT COUNT(*) FROM cases c"
    " WHERE c.status = 'PENDING'"
    " AND EXISTS (SELECT 1 FROM dec WHERE dec.review_id = c.review_id)),"
    " 'counter_mismatch', (SELECT COUNT(*) FROM cases c LEFT JOIN occ o USING (review_id)"
    " WHERE c.occurrence_count != COALESCE(o.n, 0))"
    ") AS summary"
)

_SUMMARY_SOURCES = ("intake", "discovery", "research")


def get_review_summary(
    *,
    source: str | None = None,
    run_id: str | None = None,
    since: datetime,
    until: datetime,
    config: DatabaseConfig | None = None,
) -> dict[str, Any]:
    """Aggregate one consistent snapshot of the review queue (IR-4C).

    A single SELECT computes every count from the same snapshot:
    case totals by status, occurrence-row totals (rows are the
    authority, never the stored counters), decision totals by
    outcome, per-source and per-bucket breakdowns, and the UTC
    daily resolution series inside the effective window. Empty
    sets report zeros. Any integrity mismatch (RESOLVED without
    a decision, PENDING with one, or a stored counter disagreeing
    with its rows) fails closed with DatabaseUnavailableError so
    callers serve the generic outage response instead of a
    corrupt summary. SELECT-only; nothing is written.
    """
    params = {"source": source, "run_id": run_id, "since": since, "until": until}
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(_SUMMARY_SQL, params)
                row = cur.fetchone()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    if row is None:
        raise DatabaseUnavailableError("review_summary_missing")
    payload = dict(row["summary"])
    if (
        payload.get("resolved_without_decision")
        or payload.get("pending_with_decision")
        or payload.get("counter_mismatch")
    ):
        raise DatabaseUnavailableError("review_summary_integrity")
    by_source = dict(payload.get("by_source") or {})
    buckets = {
        "one": int(payload.get("bucket_one") or 0),
        "two_to_three": int(payload.get("bucket_two_three") or 0),
        "four_or_more": int(payload.get("bucket_four_plus") or 0),
    }
    return {
        "cases": {
            "total": int(payload.get("total_cases") or 0),
            "pending": int(payload.get("pending_cases") or 0),
            "resolved": int(payload.get("resolved_cases") or 0),
        },
        "occurrences": {
            "total": int(payload.get("total_occurrences") or 0),
            "unresolved": int(payload.get("unresolved_occurrences") or 0),
        },
        "decisions": {
            "total": int(payload.get("total_decisions") or 0),
            "by_outcome": {
                "SAME_PROPERTY": int(payload.get("same_property") or 0),
                "DISTINCT_PROPERTIES": int(payload.get("distinct_properties") or 0),
            },
        },
        "cases_by_source": {
            name: int(by_source.get(name) or 0) for name in _SUMMARY_SOURCES
        },
        "repeated_case_count": buckets["two_to_three"] + buckets["four_or_more"],
        "occurrence_buckets": buckets,
        "resolutions_by_day": [
            {"day": item["day"], "count": int(item["count"])}
            for item in (payload.get("by_day") or [])
        ],
    }
