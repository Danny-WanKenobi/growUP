"""Investigative research coordinator: planner (5A) and executor (5B).

The planner turns ranked intelligence into targeted follow-up research:
which properties deserve investigation, what questions matter, and which
Brave-compatible queries could answer them. Pure and deterministic: no
network calls, no database writes, no AI calls.

The executor runs approved plans through the existing Brave provider,
budget, and authentication infrastructure. Planning never triggers
execution; execution never invents queries -- every executed query is
regenerated server-side from the submitted intelligence (deterministic
reconstruction), so forged plans cannot smuggle arbitrary queries past
validation.

One coherent architecture: planner output feeds the executor, and both
feed the dossier builder (dossier.py). No second ranking engine, no
second Brave integration, no recursive auto-research.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.agents.gabriel.extractor import (
    ResearchIntelligence,
    ResultIntelligence,
    assembled_identity,
    extract_research_intelligence,
    group_by_property,
)
from app.agents.gabriel.observe import (
    new_run_id,
    note_activity,
    note_diagnostic,
    note_warning,
)
from app.agents.gabriel.ranking import (
    CountyEligibility,
    PropertyRanking,
    QCStatus,
    RankedCandidate,
    rank_intelligence,
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

INVESTIGATION_VERSION = "004B-5A.0"

# Absolute caps (configurations outside these are rejected).
MAX_PROPERTIES_CAP = 10
MAX_PER_PROPERTY_CAP = 4
MAX_TOTAL_QUERIES_CAP = 12
MAX_DEPTH_CAP = 3
MAX_ITERATIONS_CAP = 3

# Conservative defaults.
DEFAULT_MAX_PROPERTIES = 3
DEFAULT_MAX_PER_PROPERTY = 2
DEFAULT_MAX_TOTAL_QUERIES = 6
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_ITERATIONS = 1

# Endpoint input bound (planning is pure but bodies stay finite).
MAX_PLAN_RESULTS = 200

# Query safety.
MAX_IDENTITY_CHARS = 80
MAX_QUERY_CHARS = 200


class InvestigationConfig(BaseModel):
    """Bounded controls for plan generation."""

    max_properties: int = Field(default=DEFAULT_MAX_PROPERTIES, ge=1, le=MAX_PROPERTIES_CAP)
    max_queries_per_property: int = Field(
        default=DEFAULT_MAX_PER_PROPERTY, ge=0, le=MAX_PER_PROPERTY_CAP
    )
    max_total_queries: int = Field(
        default=DEFAULT_MAX_TOTAL_QUERIES, ge=0, le=MAX_TOTAL_QUERIES_CAP
    )
    max_depth: int = Field(default=DEFAULT_MAX_DEPTH, ge=0, le=MAX_DEPTH_CAP)
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1, le=MAX_ITERATIONS_CAP)


class QuestionTopic(str, Enum):
    IDENTITY = "identity"
    GEOGRAPHY = "geography"
    TRANSACTION = "transaction"
    UNDERUSE = "underuse"
    DISTRESS = "distress"
    BUILDING = "building"
    EVIDENCE = "evidence"


class ResearchQuestion(BaseModel):
    """One unanswered question; generating it answers nothing."""

    id: str
    topic: QuestionTopic
    slug: str = ""
    question: str
    why_material: str
    sought: str
    owner: str = "Gabriel"  # Gabriel, Raphael, Onesimus, or Priscilla


class PlannedQuery(BaseModel):
    """One Brave-compatible follow-up query (planned, not executed)."""

    id: str
    query: str
    candidate_key: str
    question_ids: list[str] = []
    template: str = ""
    explanation: str = ""
    sought: str = ""
    depth: int = 1


class PropertyInvestigation(BaseModel):
    candidate_key: str
    name: str | None = None
    city: str | None = None
    identity_disputes: dict[str, list[str]] = Field(default_factory=dict)
    county: CountyEligibility = CountyEligibility.COUNTY_UNKNOWN
    qc_status: QCStatus = QCStatus.RESEARCH_REQUIRED
    opportunity_score: float | None = None
    value_rank: int | None = None
    research_priority: int = 0
    priority_reason: str = ""
    questions: list[ResearchQuestion] = []
    queries: list[PlannedQuery] = []
    evidence_summary: list[str] = []
    missing: list[str] = []
    contradictions: list[str] = []
    agent_owners: list[str] = []


class UnplannedCandidate(BaseModel):
    candidate_key: str
    reason: str


class InvestigationPlan(BaseModel):
    plan_version: str = INVESTIGATION_VERSION
    investigations: list[PropertyInvestigation] = []
    unplanned: list[UnplannedCandidate] = []
    total_queries: int = 0
    depth: int = 0
    iteration: int = 1
    run_id: str = ""
    correlation_id: str = ""


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


_WS_RE = re.compile(r"\s+")


def _sanitize_identity(value: str) -> str:
    """Strip query-breaking characters and cap length (never interprets)."""
    cleaned = value.replace('"', " ").replace("\n", " ").replace("\r", " ")
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned[:MAX_IDENTITY_CHARS]


def _geo_weight(county: CountyEligibility) -> float:
    if county is CountyEligibility.PRIMARY_AREA:
        return 0.10
    if county is CountyEligibility.SECONDARY_AREA:
        return 0.05
    return 0.0


# ---------------------------------------------------------------------------
# Question generation (deterministic rules over ranked candidates)
# ---------------------------------------------------------------------------

# Topic order also sets query priority: transaction value first, then the
# eligibility gate, distress, underuse, building, and evidence depth.
_TOPIC_ORDER = (
    QuestionTopic.TRANSACTION,
    QuestionTopic.GEOGRAPHY,
    QuestionTopic.DISTRESS,
    QuestionTopic.UNDERUSE,
    QuestionTopic.BUILDING,
    QuestionTopic.EVIDENCE,
    QuestionTopic.IDENTITY,
)


def _factor(candidate: RankedCandidate, name: str):
    for factor in candidate.opportunity.factors:
        if factor.factor == name:
            return factor
    return None


def _factor_unevaluated(candidate: RankedCandidate, name: str) -> bool:
    factor = _factor(candidate, name)
    return factor is None or factor.score is None


def _question(
    candidate_key: str, topic: QuestionTopic, slug: str, depth: int, iteration: int,
    question: str, why_material: str, sought: str, owner: str,
) -> ResearchQuestion:
    return ResearchQuestion(
        id=_stable_id(
            "giq", INVESTIGATION_VERSION, candidate_key, topic.value,
            slug, str(depth), str(iteration),
        ),
        topic=topic,
        slug=slug,
        question=question,
        why_material=why_material,
        sought=sought,
        owner=owner,
    )


def _build_questions(
    candidate: RankedCandidate,
    items: list[ResultIntelligence],
    members: list[int],
    needs_review: bool,
    name: str | None,
    address: str | None,
    depth: int,
    iteration: int,
) -> list[ResearchQuestion]:
    key = candidate.key
    questions: list[ResearchQuestion] = []

    def ask(topic, slug, question, why_material, sought, owner):
        questions.append(
            _question(key, topic, slug, depth, iteration, question, why_material, sought, owner)
        )

    # Identity: weak identity blocks safe targeted queries.
    if not name and not address:
        ask(
            QuestionTopic.IDENTITY, "address",
            "What is the property's complete street address?",
            "Without a name or address, follow-up searches cannot target this candidate safely.",
            "Property name and street address from listing or assessor sources.",
            "Raphael",
        )
    elif not address:
        ask(
            QuestionTopic.IDENTITY, "address",
            "What is the property's complete street address?",
            "A street address pins the building for county, ownership, and suitability checks.",
            "Street address, city, and ZIP code.",
            "Raphael",
        )
    if needs_review:
        ask(
            QuestionTopic.IDENTITY, "same-building",
            "Are these sources discussing the same building?",
            "Merging different buildings would corrupt every attributed fact.",
            "Confirmation that grouped sources share one street address.",
            "Gabriel",
        )
    if any(len(items[i].properties) > 1 for i in members):
        ask(
            QuestionTopic.IDENTITY, "portfolio",
            "Is this a single building or a multi-property portfolio?",
            "Portfolio-wide figures must not be attributed to one building.",
            "Per-building breakdown of prices, occupancy, and distress events.",
            "Raphael",
        )
    # Geography.
    if candidate.county is CountyEligibility.COUNTY_UNKNOWN:
        ask(
            QuestionTopic.GEOGRAPHY, "county",
            "Which county contains the property?",
            "County determines primary versus secondary research priority and queue eligibility.",
            "County name from parcel, appraisal-district, or listing evidence.",
            "Raphael",
        )
    # Transaction.
    if candidate.value_detail is None:
        ask(
            QuestionTopic.TRANSACTION, "asking-price",
            "What is the documented asking price, if the property is for sale?",
            "Asking price drives the value ranking and Priscilla's acquisition analysis.",
            "Current advertised asking price with source URL and date.",
            "Gabriel",
        )
    else:
        value_factor = _factor(candidate, "value")
        if value_factor is not None and value_factor.historical:
            ask(
                QuestionTopic.TRANSACTION, "current-status",
                "Is the reported transaction current, or historical?",
                "A historical price must never stand in for a current asking price.",
                "Current listing status and any updated asking price.",
                "Gabriel",
            )
    # Distress.
    if _factor_unevaluated(candidate, "distress"):
        ask(
            QuestionTopic.DISTRESS, "distress-status",
            "Is there documented foreclosure, receivership, bankruptcy, or loan default?",
            "Distress evidence materially raises conversion-opportunity priority.",
            "Named distress event with date, scope, and source.",
            "Gabriel",
        )
    else:
        distress_factor = _factor(candidate, "distress")
        if distress_factor is not None and distress_factor.historical:
            ask(
                QuestionTopic.DISTRESS, "distress-current",
                "Is the reported distress current or historical, and does it concern this property?",
                "Historical or owner-level distress must not be mistaken for current property distress.",
                "Current distress status scoped to this building.",
                "Raphael",
            )
    # Underuse.
    if _factor_unevaluated(candidate, "underuse"):
        ask(
            QuestionTopic.UNDERUSE, "occupancy",
            "What is the latest reported occupancy or vacancy?",
            "Underutilization is a core Grow Up signal even for healthy owners.",
            "Whole-building occupancy or vacancy percentage with reporting period.",
            "Raphael",
        )
    # Building.
    if _factor_unevaluated(candidate, "building"):
        ask(
            QuestionTopic.BUILDING, "type-size",
            "What is the building type and size?",
            "Type and scale determine whether Kratky conversion is plausible.",
            "Building type, floor count or area, and construction status.",
            "Raphael",
        )
    if any("proposed" in text for text in candidate.unresolved):
        ask(
            QuestionTopic.BUILDING, "exists",
            "Is the structure constructed and standing, or merely proposed?",
            "A proposed building offers no convertible space today.",
            "Evidence the structure exists and operates.",
            "Raphael",
        )
    # Evidence depth.
    contradictions = [t for t in candidate.unresolved if t.startswith("contradiction:")]
    if contradictions:
        ask(
            QuestionTopic.EVIDENCE, "reconcile",
            "How do the conflicting reports reconcile?",
            "Contradictory evidence cannot support a ranking until resolved.",
            "Reconciled figure or identification of the erroneous source.",
            "Raphael",
        )
    if len(candidate.sources) < 2:
        ask(
            QuestionTopic.EVIDENCE, "corroborate",
            "Do independent sources corroborate the key claims?",
            "Single-source claims await independent confirmation before trust.",
            "Second independent source for price, occupancy, or distress.",
            "Gabriel",
        )
    # Deterministic topic-major order.
    order = {topic: index for index, topic in enumerate(_TOPIC_ORDER)}
    questions.sort(key=lambda item: (order[item.topic], item.id))
    return questions


# ---------------------------------------------------------------------------
# Targeted query generation (templates over supported identity only)
# ---------------------------------------------------------------------------

# Template suffixes; the quoted identity phrase is prepended.
_QUERY_TEMPLATES = {
    "asking_price": ("asking price sale", QuestionTopic.TRANSACTION),
    "county": ("county Texas", QuestionTopic.GEOGRAPHY),
    "distress": ("foreclosure receivership bankruptcy", QuestionTopic.DISTRESS),
    "occupancy": ("occupancy vacancy", QuestionTopic.UNDERUSE),
    "size": ("building square footage", QuestionTopic.BUILDING),
    "listing": ("commercial real estate listing", QuestionTopic.EVIDENCE),
}

# Which templates each question slug may trigger (priority order preserved).
_SLUG_TEMPLATES = {
    ("transaction", "asking-price"): ("asking_price",),
    ("transaction", "current-status"): ("listing", "asking_price"),
    ("geography", "county"): ("county",),
    ("distress", "distress-status"): ("distress",),
    ("distress", "distress-current"): ("distress", "listing"),
    ("underuse", "occupancy"): ("occupancy",),
    ("building", "type-size"): ("size",),
    ("building", "exists"): ("listing",),
    ("evidence", "corroborate"): ("listing",),
    # Identity and contradiction questions get no query: the former lack a
    # safe identity anchor, the latter need reconciliation, not more hits.
}


def _render_query(
    template: str,
    name: str | None,
    address: str | None,
    city: str | None,
    disputes: dict[str, list[str]] | None = None,
) -> str | None:
    """Render one query from undisputed identity; None when too weak.

    Disputed components are never search anchors: a disputed name falls
    through to address-anchored forms, and a disputed city qualifier is
    dropped. Uncontested components proceed unchanged.
    """
    suffix, _ = _QUERY_TEMPLATES[template]
    disputed = disputes or {}
    clean_name = "" if "name" in disputed else (_sanitize_identity(name) if name else "")
    clean_address = (
        "" if "street_address" in disputed else (_sanitize_identity(address) if address else "")
    )
    clean_city = "" if "city" in disputed else (_sanitize_identity(city) if city else "")
    if clean_name and clean_city:
        query = f'"{clean_name}" "{clean_city}" {suffix}'
    elif clean_name:
        query = f'"{clean_name}" {suffix}'
    elif clean_address and clean_city:
        query = f'"{clean_address}, {clean_city}" {suffix}'
    elif clean_address:
        query = f'"{clean_address}" {suffix}'
    else:
        return None
    if len(query) > MAX_QUERY_CHARS:
        # Drop the city qualifier rather than emit an overlong query.
        phrase = clean_name or clean_address
        query = f'"{phrase}" {suffix}'
    return query[:MAX_QUERY_CHARS]


def _normalize_query(query: str) -> str:
    return _WS_RE.sub(" ", query).strip().lower()


def _build_queries(
    candidate_key: str,
    questions: list[ResearchQuestion],
    name: str | None,
    address: str | None,
    city: str | None,
    per_property_max: int,
    seen: set[str],
    depth: int,
    disputes: dict[str, list[str]] | None = None,
) -> list[PlannedQuery]:
    queries: list[PlannedQuery] = []
    for question in questions:
        if len(queries) >= per_property_max:
            break
        templates = _SLUG_TEMPLATES.get((question.topic.value, question.slug))
        if not templates:
            continue
        for template in templates:
            if len(queries) >= per_property_max:
                break
            rendered = _render_query(template, name, address, city, disputes)
            if rendered is None:
                continue
            normalized = _normalize_query(rendered)
            if normalized in seen:
                continue
            seen.add(normalized)
            queries.append(
                PlannedQuery(
                    id=_stable_id(
                        "gip", INVESTIGATION_VERSION, candidate_key, template, str(depth + 1)
                    ),
                    query=rendered,
                    candidate_key=candidate_key,
                    question_ids=[question.id],
                    template=template,
                    explanation=f"Targeted follow-up for: {question.question}",
                    sought=question.sought,
                    depth=depth + 1,
                )
            )
    return queries


# ---------------------------------------------------------------------------
# Plan assembly (pure)
# ---------------------------------------------------------------------------


def _candidate_identity(
    items: list[ResultIntelligence], members: list[int], group_key: str
) -> tuple[str | None, str | None, str | None]:
    """First supported name, street address, and city across eligible members."""
    identity = assembled_identity(items, members, group_key)
    return identity["name"], identity["street_address"], identity["city"]


def _priority(candidate: RankedCandidate) -> tuple[float, str]:
    score = candidate.opportunity.score if candidate.opportunity.sufficient else 0.0
    return (score or 0.0) + _geo_weight(candidate.county), candidate.key


def _evidence_summary(candidate: RankedCandidate) -> list[str]:
    summary: list[str] = []
    if candidate.value_detail is not None:
        summary.append(f"asking price {candidate.value_detail.amount} ({candidate.value_detail.url})")
    for factor in candidate.opportunity.factors:
        if factor.score is not None and factor.factor in ("distress", "underuse"):
            signals = "; ".join(ref.excerpt for ref in factor.evidence[:3])
            summary.append(f"{factor.factor}: {signals or 'signals present'}")
    summary.append(f"{len(candidate.sources)} source(s): {', '.join(sorted(candidate.sources)[:3])}")
    return summary


def _missing_list(candidate: RankedCandidate) -> list[str]:
    missing = list(candidate.unresolved)
    for factor in candidate.opportunity.factors:
        missing.extend(factor.unknowns)
    return missing


class InvestigationPlanRequest(BaseModel):
    """Intelligence to plan follow-ups for (planning executes nothing)."""

    intelligence: ResearchIntelligence
    ranking: PropertyRanking | None = None
    config: InvestigationConfig = InvestigationConfig()
    depth: int = Field(default=0, ge=0, le=MAX_DEPTH_CAP)
    iteration: int = Field(default=1, ge=1, le=MAX_ITERATIONS_CAP)
    correlation_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _bound_results(self):
        if len(self.intelligence.results) > MAX_PLAN_RESULTS:
            raise ValueError(f"too many results (max {MAX_PLAN_RESULTS})")
        return self


def plan_investigations(
    intel: ResearchIntelligence,
    ranking: PropertyRanking | None = None,
    config: InvestigationConfig | None = None,
    depth: int = 0,
    iteration: int = 1,
) -> InvestigationPlan:
    """Generate a bounded investigation plan (pure, deterministic).

    Ranking is recomputed when omitted. Candidates are prioritized by
    opportunity score plus a small geographic weight (primary first),
    then planned in that order within the configured caps. Identity too
    weak for a safe query yields questions only, never a blind search.
    """
    cfg = config or InvestigationConfig()
    assessed = ranking if ranking is not None else rank_intelligence(intel)
    plan = InvestigationPlan(depth=depth, iteration=iteration)
    if depth >= cfg.max_depth:
        plan.unplanned = [
            UnplannedCandidate(candidate_key=c.key, reason="max investigation depth reached")
            for c in assessed.candidates
        ]
        return plan
    if iteration > cfg.max_iterations:
        plan.unplanned = [
            UnplannedCandidate(candidate_key=c.key, reason="max follow-up iterations reached")
            for c in assessed.candidates
        ]
        return plan
    groups = {group.key: group for group in group_by_property(intel.results)}
    eligible = [
        c for c in assessed.candidates
        if c.qc_status not in (QCStatus.EXCLUDE, QCStatus.DEFERRED)
    ]
    for candidate in assessed.candidates:
        if candidate.qc_status is QCStatus.EXCLUDE:
            plan.unplanned.append(
                UnplannedCandidate(candidate_key=candidate.key, reason="excluded by quality control")
            )
        elif candidate.qc_status is QCStatus.DEFERRED:
            plan.unplanned.append(
                UnplannedCandidate(candidate_key=candidate.key, reason="deferred: outside operating area")
            )
    ordered = sorted(eligible, key=_priority, reverse=True)
    seen_queries: set[str] = set()
    for rank, candidate in enumerate(ordered, start=1):
        if len(plan.investigations) >= cfg.max_properties:
            plan.unplanned.append(
                UnplannedCandidate(candidate_key=candidate.key, reason="beyond max_properties limit")
            )
            continue
        group = groups.get(candidate.key)
        members = group.members if group is not None else []
        disputes = dict(candidate.identity_disputes)
        needs_review = bool(group is not None and group.needs_review) or bool(disputes)
        name, address, city = _candidate_identity(intel.results, members, candidate.key)
        questions = _build_questions(
            candidate, intel.results, members, needs_review,
            name or candidate.name, address, depth, iteration,
        )
        queries = _build_queries(
            candidate.key, questions, name or candidate.name, address,
            city or candidate.city, cfg.max_queries_per_property, seen_queries, depth,
            disputes,
        )
        # Enforce the plan-wide query cap by trimming the lowest-priority
        # candidate first (candidates are already priority-ordered).
        room = cfg.max_total_queries - plan.total_queries
        if room <= 0:
            queries = []
        else:
            queries = queries[:room]
        owners = sorted({question.owner for question in questions})
        plan.investigations.append(
            PropertyInvestigation(
                candidate_key=candidate.key,
                name=candidate.name,
                city=candidate.city,
                identity_disputes=disputes,
                county=candidate.county,
                qc_status=candidate.qc_status,
                opportunity_score=candidate.opportunity.score,
                value_rank=candidate.value_rank,
                research_priority=rank,
                priority_reason=(
                    f"opportunity score {candidate.opportunity.score} "
                    f"({'sufficient' if candidate.opportunity.sufficient else 'insufficient'}), "
                    f"{candidate.county.value} territory"
                ),
                questions=questions,
                queries=queries,
                evidence_summary=_evidence_summary(candidate),
                missing=_missing_list(candidate),
                contradictions=[t for t in candidate.unresolved if t.startswith("contradiction:")],
                agent_owners=owners,
            )
        )
        plan.total_queries += len(queries)
    return plan


# ---------------------------------------------------------------------------
# Executor (5B): bounded follow-up execution through existing infrastructure
# ---------------------------------------------------------------------------

MAX_INVESTIGATION_QUERIES = 3


class InvestigationError(ValueError):
    """Invalid investigation request (maps to HTTP 422)."""


class InvestigationRunRequest(BaseModel):
    """Follow-up research to execute (server regenerates every query)."""

    intelligence: ResearchIntelligence
    config: InvestigationConfig = InvestigationConfig()
    query_ids: list[str] | None = Field(default=None, max_length=25)
    timeout_seconds: float = Field(default=15.0, ge=5.0, le=60.0)
    correlation_id: str = Field(default="", max_length=128)


class ExecutedQuery(BaseModel):
    query_id: str
    query: str
    candidate_key: str
    template: str


class InvestigationQueryError(BaseModel):
    query_id: str
    error: str


class InvestigationRunResult(BaseModel):
    """Outcome of one bounded investigation execution."""

    executed: list[ExecutedQuery] = []
    request_count: int = 0
    budget_month: str = ""
    budget_ceiling: int = 0
    budget_used: int = 0
    budget_remaining: int = 0
    results: list[BraveSearchResult] = []
    errors: list[InvestigationQueryError] = []
    retrieved_at: str = ""
    intelligence: ResearchIntelligence | None = None
    run_id: str = ""
    correlation_id: str = ""


def _select_queries(
    plan: InvestigationPlan, query_ids: list[str] | None
) -> list[PlannedQuery]:
    planned = [query for inv in plan.investigations for query in inv.queries]
    if query_ids is not None:
        known = {query.id for query in planned}
        for wanted in query_ids:
            if wanted not in known:
                raise InvestigationError(f"unknown planned query: {wanted}")
        wanted_set = set(query_ids)
        planned = [query for query in planned if query.id in wanted_set]
    return planned[:MAX_INVESTIGATION_QUERIES]


def _investigation_error_code(exc: BraveError) -> str:
    if isinstance(exc, BraveTimeout):
        return "brave_timeout"
    if isinstance(exc, BraveRateLimited):
        return "brave_rate_limited"
    if isinstance(exc, BraveUnauthorized):
        return "brave_unauthorized"
    return str(exc) or "brave_error"


def _apply_investigation_budget(result: InvestigationRunResult, budget: dict) -> None:
    result.budget_month = str(budget["month"])
    result.budget_ceiling = budget["ceiling"]
    result.budget_used = budget["used"]
    result.budget_remaining = budget["remaining"]


def run_investigation(request: InvestigationRunRequest) -> InvestigationRunResult:
    """Execute a bounded, budgeted investigation run.

    The plan is regenerated server-side from the submitted intelligence,
    so only planner-derived queries can execute: forged query text has
    no path to the provider. One budget reservation immediately precedes
    each external call; attempts count even when they fail. No recursion:
    this run never plans follow-ups of its own results.
    """
    run_id = new_run_id()
    correlation_id = request.correlation_id or ""
    started = time.perf_counter()
    note_activity(
        run_id, "gabriel.investigation.execution_started",
        "investigation execution started",
        details={
            "intelligence_results": len(request.intelligence.results),
            "query_ids_requested": list(request.query_ids) if request.query_ids else None,
            "timeout_seconds": request.timeout_seconds,
        },
        correlation_id=correlation_id,
    )
    if not get_database_config().is_configured:
        raise DatabaseNotConfiguredError("database_not_configured")
    ranking = rank_intelligence(request.intelligence)
    plan = plan_investigations(request.intelligence, ranking, request.config)
    try:
        selected = _select_queries(plan, request.query_ids)
    except InvestigationError:
        note_warning(
            run_id, "gabriel.investigation.selection_failed",
            "investigation rejected: unknown or unexecutable planned queries",
            correlation_id=correlation_id,
        )
        raise
    note_activity(
        run_id, "gabriel.investigation.plan_resolved",
        f"investigation plan resolved: {len(plan.investigations)} properties, "
        f"{len(selected)} queries approved for execution",
        details={
            "properties": len(plan.investigations),
            "questions": sum(len(inv.questions) for inv in plan.investigations),
            "queries_planned": plan.total_queries,
            "queries_approved": [
                {
                    "query_id": item.id,
                    "candidate_key": item.candidate_key,
                    "template": item.template,
                }
                for item in selected
            ],
            "unplanned": len(plan.unplanned),
        },
        correlation_id=correlation_id,
    )
    if not selected:
        raise InvestigationError("no executable queries in plan")
    get_brave_api_key()  # fail fast before spending budget
    get_monthly_ceiling()  # fail fast on misconfigured ceiling
    result = InvestigationRunResult(
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
            if not result.request_count:
                budget = read_budget()
            _apply_investigation_budget(result, budget)
            note_warning(
                run_id, "gabriel.budget.exhausted",
                f"investigation stopped: monthly Brave budget exhausted at "
                f"{budget['used']}/{budget['ceiling']}",
                details={
                    "query_id": item.id,
                    "budget_used": budget["used"],
                    "budget_ceiling": budget["ceiling"],
                    "skipped": len(remaining),
                },
                correlation_id=correlation_id,
            )
            result.errors.append(
                InvestigationQueryError(query_id=item.id, error="budget_exhausted")
            )
            for skipped in remaining:
                result.errors.append(
                    InvestigationQueryError(query_id=skipped.id, error="not_attempted")
                )
            break
        _apply_investigation_budget(result, budget)
        result.request_count += 1
        result.executed.append(
            ExecutedQuery(
                query_id=item.id,
                query=item.query,
                candidate_key=item.candidate_key,
                template=item.template,
            )
        )
        note_diagnostic(
            run_id, "gabriel.brave.request_attempted",
            f"Brave investigation query attempted for {item.id}",
            details={"query_id": item.id, "template": item.template},
            correlation_id=correlation_id,
        )
        call_started = time.perf_counter()
        try:
            found = execute_query(item.query, item.id, request.timeout_seconds)
        except (BraveRateLimited, BraveUnauthorized) as exc:
            logger.warning("investigation aborting: %s", type(exc).__name__)
            code = _investigation_error_code(exc)
            note_warning(
                run_id, "gabriel.investigation.interrupted",
                f"investigation interrupted by provider {code}; remaining queries skipped",
                details={
                    "query_id": item.id,
                    "error": code,
                    "duration_ms": round((time.perf_counter() - call_started) * 1000, 1),
                    "skipped": len(remaining),
                },
                correlation_id=correlation_id,
            )
            result.errors.append(
                InvestigationQueryError(query_id=item.id, error=code)
            )
            for skipped in remaining:
                result.errors.append(
                    InvestigationQueryError(query_id=skipped.id, error="not_attempted")
                )
            break
        except BraveError as exc:
            logger.warning("investigation query failed: %s", type(exc).__name__)
            code = _investigation_error_code(exc)
            note_diagnostic(
                run_id, "gabriel.brave.request_failed",
                f"Brave investigation query for {item.id} failed ({code}); run continues",
                details={
                    "query_id": item.id,
                    "error": code,
                    "duration_ms": round((time.perf_counter() - call_started) * 1000, 1),
                },
                correlation_id=correlation_id,
            )
            result.errors.append(
                InvestigationQueryError(query_id=item.id, error=code)
            )
            continue
        note_diagnostic(
            run_id, "gabriel.brave.request_completed",
            f"Brave investigation query for {item.id} returned {len(found)} result(s)",
            details={
                "query_id": item.id,
                "result_count": len(found),
                "duration_ms": round((time.perf_counter() - call_started) * 1000, 1),
            },
            correlation_id=correlation_id,
        )
        result.results.extend(found)
    # Pure local extraction over already-retrieved results: no extra Brave
    # calls, no property writes, no intake submissions.
    result.intelligence = extract_research_intelligence(result.results)
    unresolved = sum(len(inv.questions) for inv in plan.investigations)
    note_activity(
        run_id, "gabriel.investigation.completed",
        f"investigation completed: {result.request_count} request(s), "
        f"{len(result.results)} result(s), {unresolved} question(s) still open",
        details={
            "request_count": result.request_count,
            "result_count": len(result.results),
            "error_count": len(result.errors),
            "questions_open": unresolved,
            "budget_used": result.budget_used,
            "budget_remaining": result.budget_remaining,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
        correlation_id=correlation_id,
    )
    return result
