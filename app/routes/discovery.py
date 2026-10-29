"""Gabriel discovery endpoints (minimal, bounded, testable)."""

from __future__ import annotations

import logging
import time

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
from app.agents.gabriel.investigation import (
    InvestigationError,
    InvestigationPlan,
    InvestigationPlanRequest,
    InvestigationRunRequest,
    InvestigationRunResult,
    plan_investigations,
    run_investigation,
)
from app.agents.gabriel.observe import new_run_id, note_activity
from app.agents.gabriel.dossier import DossierRequest, DossierSet, build_dossiers
from app.agents.gabriel.provenance import build_dossier_verdicts, persist_verdicts
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
from app.observability import DurabilityError

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
def start_discovery_run(
    body: DiscoveryRunRequest,
    _: None = Depends(require_intake_key),
) -> DiscoveryRunResult:
    """Run one bounded discovery pass and persist new candidates.

    Requires the X-Intake-Key header. Unknown source names are
    rejected; provider failures return 502 with nothing fabricated
    or persisted.
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
    total: int = 30,
    per_category: int = 10,
    downtown: bool = False,
    secondary_counties: bool = True,
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
            include_secondary_counties=secondary_counties,
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
    except DurabilityError as exc:
        logger.warning("research run verdict persistence failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc


@router.post("/discovery/investigation-plans", response_model=InvestigationPlan)
def create_investigation_plan(body: InvestigationPlanRequest) -> InvestigationPlan:
    """Plan targeted follow-up research (pure; executes nothing).

    Deterministic transform of submitted intelligence: no provider calls,
    no budget consumption, no database access, no property changes.
    """
    run_id = new_run_id()
    correlation_id = body.correlation_id or ""
    started = time.perf_counter()
    try:
        plan = plan_investigations(
            body.intelligence,
            body.ranking,
            body.config,
            body.depth,
            body.iteration,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_plan_request"
        ) from exc
    plan.run_id = run_id
    plan.correlation_id = correlation_id
    note_activity(
        run_id, "gabriel.investigation.plan_created",
        f"investigation plan created: {len(plan.investigations)} properties, "
        f"{plan.total_queries} queries",
        details={
            "properties": len(plan.investigations),
            "questions": sum(len(inv.questions) for inv in plan.investigations),
            "total_queries": plan.total_queries,
            "unplanned": len(plan.unplanned),
            "depth": plan.depth,
            "iteration": plan.iteration,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        correlation_id=correlation_id,
    )
    return plan


@router.post("/discovery/investigations", response_model=InvestigationRunResult)
def start_investigation(
    body: InvestigationRunRequest,
    _: None = Depends(require_research_key),
) -> InvestigationRunResult:
    """Execute a bounded investigation run over a regenerated plan.

    Every query is regenerated server-side from the submitted
    intelligence; at most 3 external requests, budgeted in PostgreSQL
    before any call. Writes no operational property records.
    """
    try:
        return run_investigation(body)
    except InvestigationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_investigation"
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
        logger.warning("investigation database failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc


@router.post("/discovery/dossiers", response_model=DossierSet)
def create_dossiers(body: DossierRequest) -> DossierSet:
    """Consolidate multi-round intelligence into property dossiers.

    Ranking reassessment reuses the existing ranking engine. Each
    final dossier disposition persists as a permanent verdict event;
    no provider calls, no budget consumption, no property changes.
    """
    run_id = new_run_id()
    correlation_id = body.correlation_id or ""
    started = time.perf_counter()
    total_results = sum(len(item.results) for item in body.rounds)
    note_activity(
        run_id, "gabriel.dossier.started",
        f"dossier construction started: {len(body.rounds)} round(s), {total_results} result(s)",
        details={"rounds": len(body.rounds), "total_results": total_results},
        correlation_id=correlation_id,
    )
    try:
        dossier_set = build_dossiers(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_dossier_request"
        ) from exc
    dossier_set.run_id = run_id
    dossier_set.correlation_id = correlation_id
    items = [item for round_intel in body.rounds for item in round_intel.results]
    try:
        verdicts = build_dossier_verdicts(
            run_id=run_id, correlation_id=correlation_id,
            dossier_set=dossier_set, items=items, started=started,
        )
        outcome = persist_verdicts(verdicts)
    except DurabilityError as exc:
        logger.warning("dossier verdict persistence failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc
    dossier_set.provenance_pending = outcome.pending
    qc_histogram: dict[str, int] = {}
    for dossier in dossier_set.dossiers:
        qc_histogram[dossier.qc_status.value] = qc_histogram.get(dossier.qc_status.value, 0) + 1
        if dossier.qc_status.value != "exclude":
            note_activity(
                run_id, "gabriel.dossier.handoff_ready",
                f"dossier {dossier.key} handoff ready, not sent "
                f"({dossier.qc_status.value})",
                details={
                    "dossier_key": dossier.key,
                    "qc_status": dossier.qc_status.value,
                    "agents": [handoff.agent for handoff in dossier.handoffs],
                    "remaining_questions": len(dossier.remaining_questions),
                    "conflicts": len(dossier.conflicts),
                },
                correlation_id=correlation_id,
            )
    note_activity(
        run_id, "gabriel.dossier.completed",
        f"dossier construction completed: {len(dossier_set.dossiers)} dossier(s)",
        details={
            "dossiers": len(dossier_set.dossiers),
            "qc_status": qc_histogram,
            "conflicts": sum(len(dossier.conflicts) for dossier in dossier_set.dossiers),
            "comparisons": len(dossier_set.comparison),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        correlation_id=correlation_id,
    )
    return dossier_set
