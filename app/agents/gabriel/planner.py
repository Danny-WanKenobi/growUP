"""Gabriel commercial listing search planner (MVP-004B-1, B5-1 twelve-county expansion).

Generates structured, deterministic research assignments for finding
commercial properties offered for sale. Listings first. Value driven.
Distress focused.

Coverage spans the twelve-county operating area in shared priority order
(Dallas first, then Tarrant, Collin, Denton, Ellis, Johnson, Kaufman,
Parker, Rockwall, Hunt, Wise, Navarro): counties process sequentially
with all three categories completing in one county before advancing, so
the default plan is exactly 36 baseline assignments
(3 categories x 12 counties) prioritizing properties advertised for
sale. Each assignment carries its county's five-city municipality
priority list for future batch scheduling; baseline queries stay
countywide. Downtown is an opt-in refinement of Dallas County slots
only, using the explicitly configured municipality -- county-based
downtown labels are never generated.

This phase plans only: it executes no searches, scrapes nothing, calls
no AI models, writes no database records, and makes no network requests.
Assignments are provider-independent and executable by a future adapter.

Determinism contract: the same inputs produce the same assignments in
the same order, with stable identifiers derived from content (never
timestamps or randomness).
"""

from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field

from app.agents.gabriel.geography import (
    PRIMARY_COUNTY_DISPLAY,
    ordered_cities,
    ordered_county_names,
)
from app.properties import PropertyCategory

PLANNER_VERSION = "004B-2.1"

# Absolute budget caps (configurations outside these are rejected).
MAX_TOTAL_QUERIES = 54  # bounded well within the 12 x 3 x 9 query space
MAX_PER_CATEGORY_QUERIES = 18
MAX_FOLLOWUP_QUERIES = 50
MAX_DEPTH_CAP = 5

# Conservative defaults for a future metered search API.
DEFAULT_TOTAL_QUERIES = 36
DEFAULT_PER_CATEGORY_QUERIES = 12
DEFAULT_FOLLOWUP_QUERIES = 6
DEFAULT_MAX_DEPTH = 2

PLANNED_CATEGORIES = (
    PropertyCategory.HIGH_RISE,
    PropertyCategory.COMMERCIAL_PROPERTY,
    PropertyCategory.WAREHOUSE_INDUSTRIAL,
)

_CATEGORY_PHRASES = {
    PropertyCategory.HIGH_RISE: "high-rise office building",
    PropertyCategory.COMMERCIAL_PROPERTY: "commercial office building",
    PropertyCategory.WAREHOUSE_INDUSTRIAL: "industrial warehouse property",
}


class SearchStrategy(str, Enum):
    """Supported sale-opportunity search strategies (fixed order)."""

    FOR_SALE = "for_sale"
    HIGH_VALUE = "high_value"
    DISTRESSED = "distressed"
    FORECLOSURE_REO = "foreclosure_reo"
    RECEIVERSHIP_BANKRUPTCY = "receivership_bankruptcy"
    AUCTION_LIQUIDATION = "auction_liquidation"
    VACANT_UNDERUTILIZED = "vacant_underutilized"
    PRICE_REDUCED = "price_reduced"
    LONG_LISTING = "long_listing"


STRATEGIES = tuple(SearchStrategy)

_QUERY_TEMPLATES = {
    SearchStrategy.FOR_SALE: "{cat} for sale in {market}",
    SearchStrategy.HIGH_VALUE: "high-value {cat} for sale in {market}",
    SearchStrategy.DISTRESSED: "distressed {cat} for sale in {market}",
    SearchStrategy.FORECLOSURE_REO: "foreclosure or bank-owned {cat} for sale in {market}",
    SearchStrategy.RECEIVERSHIP_BANKRUPTCY: "bankruptcy or receivership {cat} sale in {market}",
    SearchStrategy.AUCTION_LIQUIDATION: "{cat} auction or liquidation sale in {market}",
    SearchStrategy.VACANT_UNDERUTILIZED: "vacant {cat} for sale in {market}",
    SearchStrategy.PRICE_REDUCED: "price-reduced {cat} for sale in {market}",
    SearchStrategy.LONG_LISTING: "long-standing {cat} listing for sale in {market}",
}

_STRATEGY_INTENTS = {
    SearchStrategy.FOR_SALE: "Find commercial properties actively advertised for sale.",
    SearchStrategy.HIGH_VALUE: "Surface high-value sale listings; value claims await verification.",
    SearchStrategy.DISTRESSED: "Surface listings using distress language; query wording is not evidence of distress.",
    SearchStrategy.FORECLOSURE_REO: "Find lender-owned or foreclosure sales; confirm status from listing evidence, not the query.",
    SearchStrategy.RECEIVERSHIP_BANKRUPTCY: "Find court- or receiver-driven sales; confirm from evidence, not the query.",
    SearchStrategy.AUCTION_LIQUIDATION: "Find auction and liquidation sale opportunities.",
    SearchStrategy.VACANT_UNDERUTILIZED: "Find listings describing vacancy or underuse; confirm occupancy from evidence.",
    SearchStrategy.PRICE_REDUCED: "Find listings with advertised price reductions.",
    SearchStrategy.LONG_LISTING: "Find long-standing listings that may indicate negotiable sellers; listing age is not proof of distress.",
}


class FeedbackOutcome(str, Enum):
    """Structured research feedback outcomes."""

    NO_RESULTS = "no_results"
    DUPLICATES_HEAVY = "duplicates_heavy"
    PROMISING = "promising"
    PROVIDER_FAILURE = "provider_failure"
    MISSING_PRICING = "missing_pricing"
    MISSING_DISTRESS_EVIDENCE = "missing_distress_evidence"


class PlannerConfig(BaseModel):
    """Budget controls for plan generation (all bounded)."""

    total_max_queries: int = Field(default=DEFAULT_TOTAL_QUERIES, ge=1, le=MAX_TOTAL_QUERIES)
    per_category_max: int = Field(
        default=DEFAULT_PER_CATEGORY_QUERIES, ge=1, le=MAX_PER_CATEGORY_QUERIES
    )
    followup_max: int = Field(default=DEFAULT_FOLLOWUP_QUERIES, ge=0, le=MAX_FOLLOWUP_QUERIES)
    max_depth: int = Field(default=DEFAULT_MAX_DEPTH, ge=0, le=MAX_DEPTH_CAP)
    downtown_refinement: bool = False
    include_secondary_counties: bool = True
    city: str = Field(default="Dallas", min_length=1, max_length=200)
    state: str = Field(default="TX", min_length=1, max_length=100)


class SearchAssignment(BaseModel):
    """One deterministic research assignment (no execution)."""

    id: str
    query: str
    market: str
    county: str = ""  # display form ("Dallas County"); planner always populates
    cities: list[str] = Field(default_factory=list)  # county's 5-city priority list; county context in `county`
    category: PropertyCategory
    strategy: SearchStrategy
    priority: int = Field(ge=1)
    intent: str
    planner_version: str = PLANNER_VERSION
    downtown: bool = False
    depth: int = Field(default=0, ge=0)
    parent_id: str | None = None


class ResearchFeedback(BaseModel):
    """Structured feedback on one assignment.

    The optional free-text note is opaque to the planner: follow-ups are
    derived deterministically from the outcome only.
    """

    assignment_id: str = Field(min_length=1, max_length=100)
    outcome: FeedbackOutcome
    note: str | None = Field(default=None, max_length=500)


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"gsp-{digest}"


def _market_label(county: str, state: str, downtown: bool, city: str) -> str:
    """Render a market label (downtown uses the configured municipality).

    Countywide markets name the county; downtown markets name the
    explicitly configured city. Downtown refinement outside Dallas
    County is rejected outright: no caller may produce a county-based
    downtown label, and falling back silently would lie about the
    flag the assignment carries.
    """
    base = f"{county}, {state.strip()}"
    if not downtown:
        return base
    if county != PRIMARY_COUNTY_DISPLAY:
        raise ValueError(f"downtown refinement is Dallas County only, not {county!r}")
    return f"Downtown {city.strip()}, {state.strip()}"


def _render_query(strategy: SearchStrategy, category: PropertyCategory, market: str) -> str:
    return _QUERY_TEMPLATES[strategy].format(cat=_CATEGORY_PHRASES[category], market=market)


def _dedupe_key(query: str, category: PropertyCategory) -> str:
    return f"{query}\u0000{category.value}"


def plan_initial_searches(config: PlannerConfig | None = None) -> list[SearchAssignment]:
    """Generate the initial sequential assignment set (deterministic).

    Counties process in shared priority order with all three
    categories completing in one county before advancing, so the
    default plan is exactly 36 baseline assignments (3 x 12). The
    first pass uses FOR_SALE throughout -- the baseline prioritizes
    commercial properties advertised for sale -- and further passes
    advance one strategy per full county sweep when larger budgets
    request them. Downtown appears only as an explicitly configured
    refinement of Dallas County slots. Identical queries are never
    emitted twice; when the unique query space is exhausted, fewer
    assignments are returned.
    """
    cfg = config or PlannerConfig()
    city = cfg.city.strip()
    state = cfg.state.strip()
    counties = (
        [PRIMARY_COUNTY_DISPLAY]
        if not cfg.include_secondary_counties
        else list(ordered_county_names())
    )
    assignments: list[SearchAssignment] = []
    seen: set[str] = set()
    per_cat_count = {category: 0 for category in PLANNED_CATEGORIES}
    pass_index = 0
    while len(assignments) < cfg.total_max_queries:
        strategy = STRATEGIES[pass_index % len(STRATEGIES)]
        progressed = False
        for county in counties:
            downtown = bool(
                cfg.downtown_refinement and county == PRIMARY_COUNTY_DISPLAY
            )
            for category in PLANNED_CATEGORIES:
                if len(assignments) >= cfg.total_max_queries:
                    return assignments
                if per_cat_count[category] >= cfg.per_category_max:
                    continue
                market = _market_label(county, state, downtown, city)
                query = _render_query(strategy, category, market)
                key = _dedupe_key(query, category)
                if key in seen:
                    continue
                seen.add(key)
                per_cat_count[category] += 1
                progressed = True
                seq = len(assignments)
                assignments.append(
                    SearchAssignment(
                        id=_stable_id(PLANNER_VERSION, market, category.value, strategy.value, f"{seq:04d}"),
                        query=query,
                        market=market,
                        county=county,
                        cities=list(ordered_cities(county)),
                        category=category,
                        strategy=strategy,
                        priority=seq + 1,
                        intent=_STRATEGY_INTENTS[strategy],
                        downtown=downtown,
                        depth=0,
                    )
                )
        if not progressed:
            return assignments  # unique query space exhausted
        pass_index += 1
    return assignments


def _successor(strategy: SearchStrategy) -> SearchStrategy:
    return STRATEGIES[(STRATEGIES.index(strategy) + 1) % len(STRATEGIES)]


def _followup_moves(
    parent: SearchAssignment, outcome: FeedbackOutcome
) -> list[tuple[SearchStrategy, bool]]:
    """Deterministic single-step adaptation rules (no recursion)."""
    same_variant = parent.downtown
    if outcome is FeedbackOutcome.NO_RESULTS:
        return [(_successor(parent.strategy), same_variant)]
    if outcome is FeedbackOutcome.DUPLICATES_HEAVY:
        return [(parent.strategy, not same_variant)]
    if outcome is FeedbackOutcome.PROMISING:
        return [
            (parent.strategy, not same_variant),
            (_successor(parent.strategy), same_variant),
        ]
    if outcome is FeedbackOutcome.PROVIDER_FAILURE:
        return []  # never auto-retry a failed provider
    if outcome is FeedbackOutcome.MISSING_PRICING:
        next_strategy = (
            SearchStrategy.HIGH_VALUE
            if parent.strategy is not SearchStrategy.HIGH_VALUE
            else SearchStrategy.PRICE_REDUCED
        )
        return [(next_strategy, same_variant)]
    if outcome is FeedbackOutcome.MISSING_DISTRESS_EVIDENCE:
        next_strategy = (
            SearchStrategy.DISTRESSED
            if parent.strategy is not SearchStrategy.DISTRESSED
            else SearchStrategy.FORECLOSURE_REO
        )
        return [(next_strategy, same_variant)]
    return []  # pragma: no cover - exhaustive enum


def plan_followups(
    parent: SearchAssignment,
    feedback: ResearchFeedback,
    config: PlannerConfig | None = None,
    known: frozenset[str] | set[str] | tuple[str, ...] = (),
) -> list[SearchAssignment]:
    """Generate bounded follow-up assignments for one feedback report.

    Single step only: at most ``followup_max`` assignments, none beyond
    ``max_depth``, none duplicating ``known`` query keys or each other.
    Follow-ups stay in the parent assignment's county, and moves that
    would need a county-based downtown label are skipped (secondary
    counties stay countywide; only Dallas County refines downtown).
    """
    cfg = config or PlannerConfig()
    if parent.depth + 1 > cfg.max_depth:
        return []
    city = cfg.city.strip()
    state = cfg.state.strip()
    county = parent.county or PRIMARY_COUNTY_DISPLAY
    known_keys = set(known)
    followups: list[SearchAssignment] = []
    for index, (strategy, downtown) in enumerate(_followup_moves(parent, feedback.outcome)):
        if len(followups) >= cfg.followup_max:
            break
        if downtown and county != PRIMARY_COUNTY_DISPLAY:
            continue  # never a county-based downtown label
        market = _market_label(county, state, downtown, city)
        query = _render_query(strategy, parent.category, market)
        key = _dedupe_key(query, parent.category)
        if key in known_keys:
            continue
        known_keys.add(key)
        followups.append(
            SearchAssignment(
                id=_stable_id(
                    PLANNER_VERSION,
                    parent.id,
                    feedback.outcome.value,
                    strategy.value,
                    "downtown" if downtown else "citywide",
                    f"{index:02d}",
                ),
                query=query,
                market=market,
                county=county,
                cities=list(ordered_cities(county)),
                category=parent.category,
                strategy=strategy,
                priority=parent.priority + 1 + index,
                intent=_STRATEGY_INTENTS[strategy],
                downtown=downtown,
                depth=parent.depth + 1,
                parent_id=parent.id,
            )
        )
    return followups
