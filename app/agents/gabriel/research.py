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
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.agents.gabriel.extractor import ResearchIntelligence, extract_research_intelligence
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


def select_assignments(assignments: list[SearchAssignment]) -> list[SearchAssignment]:
    """Select the first assignment per primary category, priority order.

    Rejects non-planner input (wrong planner version) and caps the
    selection at MAX_PER_RUN. Deterministic for identical inputs.
    """
    for assignment in assignments:
        if assignment.planner_version != PLANNER_VERSION:
            raise ResearchError(f"unsupported planner version: {assignment.planner_version}")
    planned = set(PLANNED_CATEGORIES)
    first_per_category: dict[object, SearchAssignment] = {}
    for assignment in sorted(assignments, key=lambda item: item.priority):
        if assignment.category in planned and assignment.category not in first_per_category:
            first_per_category[assignment.category] = assignment
    selected = sorted(first_per_category.values(), key=lambda item: item.priority)
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


def run_research(request: ResearchRunRequest) -> ResearchRunResult:
    """Execute one bounded, budgeted research run."""
    if not get_database_config().is_configured:
        raise DatabaseNotConfiguredError("database_not_configured")
    selected = select_assignments(request.assignments)
    get_brave_api_key()  # fail fast before spending budget
    get_monthly_ceiling()  # fail fast on misconfigured ceiling
    result = ResearchRunResult(
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
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
        try:
            found = execute_query(item.query, item.id, request.timeout_seconds)
        except (BraveRateLimited, BraveUnauthorized) as exc:
            # Back off / abort: never hammer a failing provider.
            logger.warning("brave research aborting: %s", type(exc).__name__)
            result.errors.append(ResearchAssignmentError(assignment_id=item.id, error=_error_code(exc)))
            for skipped in remaining:
                result.errors.append(
                    ResearchAssignmentError(assignment_id=skipped.id, error="not_attempted")
                )
            break
        except BraveError as exc:
            logger.warning("brave query failed: %s", type(exc).__name__)
            result.errors.append(ResearchAssignmentError(assignment_id=item.id, error=_error_code(exc)))
            continue
        result.results.extend(found)
    if request.include_intelligence:
        # Pure local extraction over already-retrieved results: no extra
        # Brave calls, no property writes, no intake submissions.
        result.intelligence = extract_research_intelligence(result.results)
    if request.include_ranking:
        if result.intelligence is None:
            raise ResearchError("ranking requires include_intelligence")
        result.ranking = rank_intelligence(result.intelligence)
    return result
