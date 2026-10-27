"""Gabriel discovery endpoints (minimal, bounded, testable)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError

from app.agents.gabriel.schemas import (
    DiscoveryRunRequest,
    DiscoveryRunResult,
    DiscoverySourceInfo,
)
from app.agents.gabriel.intake import IntakeError, IntakeResult, ListingIntakeRequest, process_intake
from app.agents.gabriel.planner import (
    PlannerConfig,
    SearchAssignment,
    plan_initial_searches,
)
from app.agents.gabriel.research import ResearchRunRequest, ResearchRunResult, ResearchError, run_research
from app.agents.gabriel.sources.brave import BraveConfigError
from app.auth import require_intake_key, require_research_key
from app.research_budget_db import BudgetConfigError, BudgetExhausted
from app.agents.gabriel.service import run_discovery
from app.agents.gabriel.sources.base import (
    SOURCES,
    SourceError,
    SourceTimeout,
    UnknownSourceError,
)
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["discovery"])


@router.get("/discovery/sources", response_model=list[DiscoverySourceInfo])
def list_sources() -> list:
    """List registered discovery providers and their requirements."""
    return [
        {
            "name": source.name,
            "description": source.description,
            "docs_url": source.docs_url,
            "requires_credentials": source.requires_credentials,
        }
        for source in SOURCES.values()
    ]


@router.post("/discovery/runs", response_model=DiscoveryRunResult)
def start_discovery_run(body: DiscoveryRunRequest) -> DiscoveryRunResult:
    """Run one bounded discovery pass and persist new candidates.

    Unknown source names are rejected; provider failures return 502
    with nothing fabricated or persisted.
    """
    try:
        return run_discovery(body)
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_configured"
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("discovery run database failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc
    except UnknownSourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unknown_source"
        ) from exc
    except (SourceTimeout, SourceError) as exc:
        logger.warning("discovery run source failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="source_unavailable"
        ) from exc

@router.post("/discovery/intake", response_model=IntakeResult, status_code=status.HTTP_201_CREATED)
def submit_listing(
    body: ListingIntakeRequest,
    response: Response,
    _: None = Depends(require_intake_key),
) -> IntakeResult:
    """Accept one attributable commercial listing (intake, not web search).

    Requires the X-Intake-Key header (see intake.SECURITY_LIMITATION).
    Submitted URLs are stored, never fetched.
    """
    try:
        result = process_intake(body)
    except IntakeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_listing"
        ) from exc
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_configured"
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("listing intake database failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc
    if result.outcome != "created":
        response.status_code = status.HTTP_200_OK
    return result

@router.get("/discovery/search-plan", response_model=list[SearchAssignment])
def preview_search_plan(
    total: int = 12,
    per_category: int = 4,
    downtown: bool = True,
) -> list:
    """Preview Gabriel research assignments (read-only, no execution).

    Pure planning: no database access, no network calls, no property
    changes. Invalid budgets return 422.
    """
    try:
        config = PlannerConfig(
            total_max_queries=total,
            per_category_max=per_category,
            downtown_refinement=downtown,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_plan_config"
        ) from exc
    return plan_initial_searches(config)

@router.post("/discovery/research-runs", response_model=ResearchRunResult)
def start_research_run(
    body: ResearchRunRequest,
    _: None = Depends(require_research_key),
) -> ResearchRunResult:
    """Execute a bounded Brave research run over planner assignments.

    At most 3 external requests, budgeted in PostgreSQL before any call.
    Writes no operational property records.
    """
    try:
        return run_research(body)
    except ResearchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_research_request"
        ) from exc
    except (BraveConfigError, BudgetConfigError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="research_not_configured"
        ) from exc
    except BudgetExhausted as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="budget_exhausted"
        ) from exc
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_configured"
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("research run database failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc
