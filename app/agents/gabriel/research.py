"""Bounded Brave research execution (MVP-004B-2).

Runs planner assignments -- never arbitrary caller queries -- against
Brave Search, at most 3 external requests per run. Exactly one request
of budget is reserved immediately before each external call, so skipped
assignments (after an abort or exhaustion) consume nothing. Attempts
count even when they fail. Results are normalized and attributable;
nothing is written to operational property records in this phase.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.agents.gabriel.extractor import ResearchIntelligence, extract_research_intelligence
from app.agents.gabriel.observe import (
    new_run_id,
    note_activity,
    note_decision,
    note_diagnostic,
    note_warning,
)
from app.agents.gabriel.ranking import PropertyRanking, rank_intelligence
from app.agents.gabriel.planner import (
    PLANNED_CATEGORIES,
    PLANNER_VERSION,
    SearchAssignment,
)
from app.agents.gabriel.sources.brave import (
    BraveError,
    BraveRateLimited,
    BraveSearchResult,
    BraveTimeout,
    BraveUnauthorized,
    execute_query,
    get_brave_api_key,
)
from app.config import get_database_config
from app.db import DatabaseNotConfiguredError
from app.research_budget_db import (
    BudgetExhausted,
    get_monthly_ceiling,
    read_budget,
    reserve_requests,
)

logger = logging.getLogger(__name__)

MAX_PER_RUN = 3


class ResearchError(ValueError):
    """Invalid research selection (maps to HTTP 422)."""


class ResearchRunRequest(BaseModel):
    """Assignments to research (planner output only, never raw queries)."""

    assignments: list[SearchAssignment] = Field(min_length=1, max_length=25)
    timeout_seconds: float = Field(default=15.0, ge=5.0, le=60.0)
    include_intelligence: bool = True
    include_ranking: bool = False
    correlation_id: str = Field(default="", max_length=128)
    rotation: int = Field(
        default=0,
        ge=0,
        description="Per-category sweep offset; later explicit runs pass 1, 2, ... "
        "to reach counties beyond the first assignment per category.",
    )


class ExecutedAssignment(BaseModel):
    assignment_id: str
    query: str
    category: str
    strategy: str


class ResearchAssignmentError(BaseModel):
    assignment_id: str
    error: str


class ResearchRunResult(BaseModel):
    """Outcome of one bounded research run."""

    executed: list[ExecutedAssignment] = []
    request_count: int = 0
    budget_month: str = ""
    budget_ceiling: int = 0
    budget_used: int = 0
    budget_remaining: int = 0
    results: list[BraveSearchResult] = []
    errors: list[ResearchAssignmentError] = []
    retrieved_at: str = ""
    intelligence: ResearchIntelligence | None = None
    ranking: PropertyRanking | None = None
    run_id: str = ""
    correlation_id: str = ""


def select_assignments(
    assignments: list[SearchAssignment], rotation: int = 0
) -> list[SearchAssignment]:
    """Select one assignment per primary category, priority order.

    Rotation 0 (the default) keeps the legacy behavior: the first
    assignment per category. A later explicitly requested run passes a
    higher rotation to execute the next assignment per category instead
    (modulo each category's group size), sweeping counties and strategies
    across runs without widening any single run. Rejects non-planner
    input (wrong planner version) and caps the selection at MAX_PER_RUN.
    Deterministic for identical inputs.
    """
    if rotation < 0:
        raise ResearchError("rotation must be non-negative")
    for assignment in assignments:
        if assignment.planner_version != PLANNER_VERSION:
            raise ResearchError(f"unsupported planner version: {assignment.planner_version}")
    planned = set(PLANNED_CATEGORIES)
    grouped: dict[object, list[SearchAssignment]] = {}
    for assignment in sorted(assignments, key=lambda item: item.priority):
        if assignment.category in planned:
            grouped.setdefault(assignment.category, []).append(assignment)
    selected = sorted(
        (group[rotation % len(group)] for group in grouped.values()),
        key=lambda item: item.priority,
    )
    if not selected:
        raise ResearchError("no primary-category assignments to research")
    return selected[:MAX_PER_RUN]


def _error_code(exc: BraveError) -> str:
    if isinstance(exc, BraveTimeout):
        return "brave_timeout"
    if isinstance(exc, BraveRateLimited):
        return "brave_rate_limited"
    if isinstance(exc, BraveUnauthorized):
        return "brave_unauthorized"
    return str(exc) or "brave_error"


def _apply_budget(result: ResearchRunResult, budget: dict) -> None:
    result.budget_month = str(budget["month"])
    result.budget_ceiling = budget["ceiling"]
    result.budget_used = budget["used"]
    result.budget_remaining = budget["remaining"]


def _summarize_intelligence(intel: ResearchIntelligence) -> dict:
    """Counts-only extraction summary (no payload duplication)."""
    classifications = Counter(item.classification.value for item in intel.results)
    return {
        "results": len(intel.results),
        "classifications": dict(classifications),
        "property_identities": sum(len(item.properties) for item in intel.results),
        "money_mentions": sum(len(item.money) for item in intel.results),
        "occupancy_observations": sum(len(item.occupancy) for item in intel.results),
        "distress_signals": sum(len(item.distress) for item in intel.results),
        "underuse_signals": sum(len(item.underuse) for item in intel.results),
        "multi_property_results": sum(1 for item in intel.results if len(item.properties) > 1),
    }


def _summarize_ranking(ranking: PropertyRanking) -> dict:
    """Counts-only ranking summary (scores are heuristics, never verdicts)."""
    qc = Counter(candidate.qc_status.value for candidate in ranking.candidates)
    return {
        "candidates": len(ranking.candidates),
        "qc_status": dict(qc),
        "value_ranked": len(ranking.value_order),
        "unpriced": len(ranking.unpriced),
        "opportunity_ranked": len(ranking.opportunity_order),
        "insufficient_data": len(ranking.insufficient_data),
        "screened_leads": len(ranking.screened_leads),
    }


def run_research(request: ResearchRunRequest) -> ResearchRunResult:
    """Execute one bounded, budgeted research run (instrumented, nonfatal)."""
    run_id = new_run_id()
    correlation_id = request.correlation_id or ""
    started = time.perf_counter()
    note_activity(
        run_id, "gabriel.research.started",
        f"research run started: {len(request.assignments)} assignment(s), rotation {request.rotation}",
        details={
            "assignments_received": len(request.assignments),
            "rotation": request.rotation,
            "timeout_seconds": request.timeout_seconds,
            "include_intelligence": request.include_intelligence,
            "include_ranking": request.include_ranking,
        },
        correlation_id=correlation_id,
    )
    if not get_database_config().is_configured:
        raise DatabaseNotConfiguredError("database_not_configured")
    try:
        selected = select_assignments(request.assignments, request.rotation)
    except ResearchError:
        note_warning(
            run_id, "gabriel.research.selection_failed",
            "research run rejected: no selectable planner assignments",
            correlation_id=correlation_id,
        )
        raise
    note_activity(
        run_id, "gabriel.research.assignments_selected",
        f"research run selected {len(selected)} assignment(s)",
        details={
            "selected": [
                {
                    "assignment_id": item.id,
                    "county": item.county or "unknown",
                    "category": item.category.value,
                    "strategy": item.strategy.value,
                }
                for item in selected
            ],
        },
        correlation_id=correlation_id,
    )
    get_brave_api_key()  # fail fast before spending budget
    get_monthly_ceiling()  # fail fast on misconfigured ceiling
    result = ResearchRunResult(
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        run_id=run_id,
        correlation_id=correlation_id,
    )
    remaining = list(selected)
    while remaining:
        item = remaining.pop(0)
        try:
            budget = reserve_requests(1)
        except BudgetExhausted:
            # Stop immediately; skipped assignments consume nothing.
            if not result.request_count:
                budget = read_budget()
            _apply_budget(result, budget)
            note_warning(
                run_id, "gabriel.budget.exhausted",
                f"research run stopped: monthly Brave budget exhausted at {budget['used']}/{budget['ceiling']}",
                details={
                    "assignment_id": item.id,
                    "budget_month": str(budget["month"]),
                    "budget_used": budget["used"],
                    "budget_ceiling": budget["ceiling"],
                    "skipped": len(remaining),
                },
                correlation_id=correlation_id,
            )
            result.errors.append(
                ResearchAssignmentError(assignment_id=item.id, error="budget_exhausted")
            )
            for skipped in remaining:
                result.errors.append(
                    ResearchAssignmentError(assignment_id=skipped.id, error="not_attempted")
                )
            break
        _apply_budget(result, budget)
        result.request_count += 1
        result.executed.append(
            ExecutedAssignment(
                assignment_id=item.id,
                query=item.query,
                category=item.category.value,
                strategy=item.strategy.value,
            )
        )
        note_diagnostic(
            run_id, "gabriel.brave.request_attempted",
            f"Brave request attempted for {item.id}",
            details={
                "assignment_id": item.id,
                "county": item.county or "unknown",
                "category": item.category.value,
                "timeout_seconds": request.timeout_seconds,
            },
            correlation_id=correlation_id,
        )
        call_started = time.perf_counter()
        try:
            found = execute_query(item.query, item.id, request.timeout_seconds)
        except (BraveRateLimited, BraveUnauthorized) as exc:
            # Back off / abort: never hammer a failing provider.
            logger.warning("brave research aborting: %s", type(exc).__name__)
            code = _error_code(exc)
            note_warning(
                run_id, "gabriel.brave.request_failed",
                f"Brave request for {item.id} failed ({code}); run aborted",
                details={
                    "assignment_id": item.id,
                    "error": code,
                    "duration_ms": round((time.perf_counter() - call_started) * 1000, 1),
                    "skipped": len(remaining),
                },
                correlation_id=correlation_id,
            )
            result.errors.append(ResearchAssignmentError(assignment_id=item.id, error=code))
            for skipped in remaining:
                result.errors.append(
                    ResearchAssignmentError(assignment_id=skipped.id, error="not_attempted")
                )
            break
        except BraveError as exc:
            logger.warning("brave query failed: %s", type(exc).__name__)
            code = _error_code(exc)
            note_diagnostic(
                run_id, "gabriel.brave.request_failed",
                f"Brave request for {item.id} failed ({code}); run continues",
                details={
                    "assignment_id": item.id,
                    "error": code,
                    "duration_ms": round((time.perf_counter() - call_started) * 1000, 1),
                },
                correlation_id=correlation_id,
            )
            result.errors.append(ResearchAssignmentError(assignment_id=item.id, error=code))
            continue
        note_diagnostic(
            run_id, "gabriel.brave.request_completed",
            f"Brave request for {item.id} returned {len(found)} result(s)",
            details={
                "assignment_id": item.id,
                "result_count": len(found),
                "duration_ms": round((time.perf_counter() - call_started) * 1000, 1),
            },
            correlation_id=correlation_id,
        )
        result.results.extend(found)
    note_activity(
        run_id, "gabriel.research.results_received",
        f"research run received {len(result.results)} result(s) from {result.request_count} request(s)",
        details={
            "request_count": result.request_count,
            "result_count": len(result.results),
            "domains": sorted({item.domain for item in result.results if item.domain}),
            "error_count": len(result.errors),
        },
        correlation_id=correlation_id,
    )
    if request.include_intelligence:
        # Pure local extraction over already-retrieved results: no extra
        # Brave calls, no property writes, no intake submissions.
        result.intelligence = extract_research_intelligence(result.results)
        note_activity(
            run_id, "gabriel.extraction.completed",
            "research extraction completed (reported_unverified, never verified)",
            details=_summarize_intelligence(result.intelligence),
            correlation_id=correlation_id,
        )
    if request.include_ranking:
        if result.intelligence is None:
            raise ResearchError("ranking requires include_intelligence")
        result.ranking = rank_intelligence(result.intelligence)
        note_activity(
            run_id, "gabriel.ranking.completed",
            "research ranking completed (heuristic prioritization, not investment advice)",
            details=_summarize_ranking(result.ranking),
            correlation_id=correlation_id,
        )
        for candidate in result.ranking.candidates:
            for exclusion in candidate.exclusions:
                note_decision(
                    run_id, "gabriel.ranking.excluded",
                    f"candidate {candidate.key} excluded: {exclusion.rule}",
                    details={
                        "candidate_key": candidate.key,
                        "rule": exclusion.rule,
                        "evidence": exclusion.evidence[:300],
                        "date": exclusion.date,
                    },
                    correlation_id=correlation_id,
                    evidence_refs=[{"kind": "source_url", "ref": exclusion.source_url}],
                )
    note_activity(
        run_id, "gabriel.research.completed",
        f"research run completed: {result.request_count} request(s), {len(result.results)} result(s)",
        details={
            "request_count": result.request_count,
            "result_count": len(result.results),
            "error_count": len(result.errors),
            "budget_used": result.budget_used,
            "budget_remaining": result.budget_remaining,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        correlation_id=correlation_id,
    )
    return result
