"""Gabriel commercial listing search planner (MVP-004B-1).

Generates structured, deterministic research assignments for finding
commercial properties offered for sale. Listings first. Value driven.
Distress focused.

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

from app.properties import PropertyCategory

PLANNER_VERSION = "004B-1.0"

# Absolute budget caps (configurations outside these are rejected).
MAX_TOTAL_QUERIES = 54  # 3 categories x 9 strategies x 2 market variants
MAX_PER_CATEGORY_QUERIES = 18
MAX_FOLLOWUP_QUERIES = 50
MAX_DEPTH_CAP = 5

# Conservative defaults for a future metered search API.
DEFAULT_TOTAL_QUERIES = 12
DEFAULT_PER_CATEGORY_QUERIES = 4
DEFAULT_FOLLOWUP_QUERIES = 6
DEFAULT_MAX_DEPTH = 2

PLANNED_CATEGORIES = (
    PropertyCategory.HIGH_RISE,
    PropertyCategory.DOWNTOWN_COMMERCIAL,
    PropertyCategory.WAREHOUSE_INDUSTRIAL,
)

_CATEGORY_PHRASES = {
    PropertyCategory.HIGH_RISE: "high-rise office building",
    PropertyCategory.DOWNTOWN_COMMERCIAL: "commercial office building",
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
    downtown_refinement: bool = True
    city: str = Field(default="Dallas", min_length=1, max_length=200)
    state: str = Field(default="TX", min_length=1, max_length=100)


class SearchAssignment(BaseModel):
    """One deterministic research assignment (no execution)."""

    id: str
    query: str
    market: str
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


def _market_label(city: str, state: str, downtown: bool) -> str:
    base = f"{city.strip()}, {state.strip()}"
    return f"Downtown {base}" if downtown else base


def _render_query(strategy: SearchStrategy, category: PropertyCategory, market: str) -> str:
    return _QUERY_TEMPLATES[strategy].format(cat=_CATEGORY_PHRASES[category], market=market)


def _dedupe_key(query: str, category: PropertyCategory) -> str:
    return f"{query}\u0000{category.value}"


def plan_initial_searches(config: PlannerConfig | None = None) -> list[SearchAssignment]:
    """Generate the initial balanced assignment set (deterministic).

    Round-robin across the three primary categories keeps allocation
    balanced; strategies cycle deterministically per category. Downtown
    appears only as a bounded secondary refinement (every fourth slot).
    Identical queries are never emitted twice; when the unique query
    space is exhausted, fewer assignments are returned.
    """
    cfg = config or PlannerConfig()
    city = cfg.city.strip()
    state = cfg.state.strip()
    assignments: list[SearchAssignment] = []
    seen: set[str] = set()
    per_cat = cfg.per_category_max
    for round_index in range(per_cat):
        for cat_index, category in enumerate(PLANNED_CATEGORIES):
            if len(assignments) >= cfg.total_max_queries:
                return assignments
            slot = round_index * len(PLANNED_CATEGORIES) + cat_index
            downtown = bool(cfg.downtown_refinement and slot % 4 == 3)
            base = (round_index + cat_index * per_cat) % len(STRATEGIES)
            chosen: tuple[SearchStrategy, bool] | None = None
            # Prefer the planned market variant, then the flipped one, so
            # high budgets backfill instead of colliding; at default budgets
            # the first candidate always wins and output is unchanged.
            for flip in (False, True):
                if not cfg.downtown_refinement and flip:
                    break
                variant = downtown != flip
                for offset in range(len(STRATEGIES)):
                    candidate = STRATEGIES[(base + offset) % len(STRATEGIES)]
                    market = _market_label(city, state, variant)
                    query = _render_query(candidate, category, market)
                    if _dedupe_key(query, category) not in seen:
                        chosen = (candidate, variant)
                        break
                if chosen is not None:
                    break
            if chosen is None:
                continue  # unique query space exhausted for this slot
            strategy, downtown = chosen
            market = _market_label(city, state, downtown)
            query = _render_query(strategy, category, market)
            seen.add(_dedupe_key(query, category))
            seq = len(assignments)
            assignments.append(
                SearchAssignment(
                    id=_stable_id(PLANNER_VERSION, market, category.value, strategy.value, f"{seq:04d}"),
                    query=query,
                    market=market,
                    category=category,
                    strategy=strategy,
                    priority=seq + 1,
                    intent=_STRATEGY_INTENTS[strategy],
                    downtown=downtown,
                    depth=0,
                )
            )
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
    """
    cfg = config or PlannerConfig()
    if parent.depth + 1 > cfg.max_depth:
        return []
    city = cfg.city.strip()
    state = cfg.state.strip()
    known_keys = set(known)
    followups: list[SearchAssignment] = []
    for index, (strategy, downtown) in enumerate(_followup_moves(parent, feedback.outcome)):
        if len(followups) >= cfg.followup_max:
            break
        market = _market_label(city, state, downtown)
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
