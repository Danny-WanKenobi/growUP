"""Listing intelligence extractor (MVP-004B-3).

Transforms raw Brave results into structured, evidence-backed research
intelligence using deterministic rules only: no AI calls, no network
requests, no database writes. Every observation keeps its evidence
excerpt, source URL, timestamps, method, and rationale so Raphael (or a
human reviewer) can audit each decision.

Verification vocabulary follows the repository convention: extracted
observations are ``reported_unverified`` (or ``unknown``) -- the
extractor never verifies, never estimates, and never promotes anything
into verified operational facts. Monetary and occupancy figures stay at
the result level, unattributed to any single property, because one
result may discuss several buildings.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from app.agents.gabriel.sources.brave import BraveSearchResult
from app.properties import PropertyCategory

EXTRACTION_METHOD = "rule-based-004B-3.0"
EVIDENCE_WINDOW = 120  # characters of context kept around a matched signal
CURRENT_EVENT_DAYS = 90  # resolved dates newer than this may still be current


class ResultClassification(str, Enum):
    PROPERTY_LISTING = "property_listing"
    PROPERTY_LEAD = "property_lead"
    LISTING_DIRECTORY = "listing_directory"
    NEWS_ARTICLE = "news_article"
    LEGAL_OR_PUBLIC_RECORD = "legal_or_public_record"
    BACKGROUND_INFORMATION = "background_information"
    UNKNOWN = "unknown"


class ObservationStatus(str, Enum):
    """Extractor-side status. Only the unverified members are emitted."""

    VERIFIED = "verified"
    ESTIMATED = "estimated"
    REPORTED_UNVERIFIED = "reported_unverified"
    UNKNOWN = "unknown"


class MoneyKind(str, Enum):
    ASKING_PRICE_CURRENT = "asking_price_current"
    ASKING_PRICE_HISTORICAL = "asking_price_historical"
    SALE_PRICE_COMPLETED = "sale_price_completed"
    TRANSACTION_PROPOSED = "transaction_proposed"
    MARKET_VALUE_ESTIMATED = "market_value_estimated"
    PRICE_PER_SQFT = "price_per_sqft"
    UNCLASSIFIED = "unclassified"


class TemporalStatus(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"
    UNDETERMINED = "undetermined"


class ObservationScope(str, Enum):
    WHOLE_BUILDING = "whole_building"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class MoneyMention(BaseModel):
    amount: Decimal
    amount_high: Decimal | None = None  # second endpoint of a quoted range, in quote order
    kind: MoneyKind = MoneyKind.UNCLASSIFIED
    evidence: str
    status: ObservationStatus = ObservationStatus.REPORTED_UNVERIFIED


class OccupancyObservation(BaseModel):
    occupancy_pct: float | None = None
    vacancy_pct: float | None = None
    basis: str = ""  # reported term, e.g. "leased" (leased != physical)
    square_feet: int | None = None
    period: str | None = None
    scope: ObservationScope = ObservationScope.UNKNOWN
    evidence: str
    status: ObservationStatus = ObservationStatus.REPORTED_UNVERIFIED


class AvailabilityObservation(BaseModel):
    """Advertised available space. Availability is NOT physical vacancy."""

    square_feet: int
    evidence: str
    status: ObservationStatus = ObservationStatus.REPORTED_UNVERIFIED


class SignalObservation(BaseModel):
    signal: str
    evidence: str
    status: ObservationStatus = ObservationStatus.REPORTED_UNVERIFIED


class ExtractedProperty(BaseModel):
    """One conservatively identified property candidate (never merged)."""

    name: str | None = None
    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    category: PropertyCategory | None = None
    unit_designator: str | None = None
    evidence: str = ""


class TemporalAssessment(BaseModel):
    status: TemporalStatus = TemporalStatus.UNDETERMINED
    published_at: str | None = None
    reason: str = ""


class ResultIntelligence(BaseModel):
    assignment_id: str
    query: str
    result_url: str
    domain: str
    title: str
    retrieved_at: str = ""
    classification: ResultClassification = ResultClassification.UNKNOWN
    classification_reason: str = ""
    extraction_method: str = EXTRACTION_METHOD
    properties: list[ExtractedProperty] = []
    money: list[MoneyMention] = []
    occupancy: list[OccupancyObservation] = []
    availability: list[AvailabilityObservation] = []
    distress: list[SignalObservation] = []
    underuse: list[SignalObservation] = []
    structure: list[SignalObservation] = []
    county_mentions: list[str] = []
    mentions_downtown: bool = False
    temporal: TemporalAssessment = TemporalAssessment()
    status: ObservationStatus = ObservationStatus.REPORTED_UNVERIFIED


class ResearchIntelligence(BaseModel):
    results: list[ResultIntelligence] = []


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^<>]+>")
_WS_RE = re.compile(r"\s+")


def normalize_text(raw: str | None) -> str:
    """Strip markup, decode entities, collapse whitespace (never renders)."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _excerpt(text: str, start: int, end: int) -> str:
    low = max(0, start - EVIDENCE_WINDOW // 2)
    high = min(len(text), end + EVIDENCE_WINDOW // 2)
    return text[low:high].strip()


# ---------------------------------------------------------------------------
# Classification signals (explicit phrases only)
# ---------------------------------------------------------------------------

_SALE_OFFER_PHRASES = (
    "for sale",
    "offered for sale",
    "listed for sale",
    "now listing",
    "just listed",
    "asking price",
    "list price",
)

_DIRECTORY_PHRASES = (
    "properties for sale",
    "properties for lease",
    "listings for sale",
    "browse",
    "search results",
    "find commercial real estate",
    "commercial listings",
)
_COUNT_RE = re.compile(r"\b(\d{2,})\s+(?:industrial\s+|commercial\s+|office\s+)?(?:properties|listings|spaces|buildings)\b", re.IGNORECASE)

_LEGAL_PHRASES = (
    "case no",
    "case number",
    "docket",
    "chapter 11 filing",
    "chapter 7 filing",
    "notice of trustee",
    "notice of foreclosure",
    "court documents",
    "court filing",
    "sec filing",
    "county records",
    "deed records",
)

_NEWS_PHRASES = (
    "reported that",
    "according to",
    "said in a statement",
    "announced that",
    "sources said",
    "told reporters",
)

_COMMERCIAL_TOPICS = (
    "office",
    "warehouse",
    "industrial",
    "commercial real estate",
    "commercial property",
    "square feet",
    "sq ft",
    "listing",
    "tenant",
    "lease",
)


def _contains(text: str, phrases: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for phrase in phrases:
        if phrase in lowered:
            return phrase
    return None


def classify_result(title: str, snippet: str, url: str, has_identity: bool) -> tuple[ResultClassification, str]:
    """Classify conservatively; ambiguity falls through to unknown."""
    combined = f"{title} {snippet}"
    count_match = _COUNT_RE.search(combined)
    directory_hit = _contains(combined, _DIRECTORY_PHRASES)
    if count_match or (directory_hit and not has_identity):
        if count_match:
            return (
                ResultClassification.LISTING_DIRECTORY,
                f"aggregate count '{count_match.group(0)}' describes many listings, not one property",
            )
        return (
            ResultClassification.LISTING_DIRECTORY,
            f"directory language '{directory_hit}' without a specific property",
        )
    sale_hit = _contains(combined, _SALE_OFFER_PHRASES)
    if sale_hit and has_identity:
        return (
            ResultClassification.PROPERTY_LISTING,
            f"sale-offer phrase '{sale_hit}' tied to an identified property",
        )
    legal_hit = _contains(combined, _LEGAL_PHRASES)
    if legal_hit:
        return (
            ResultClassification.LEGAL_OR_PUBLIC_RECORD,
            f"record language '{legal_hit}'",
        )
    if has_identity:
        return (
            ResultClassification.PROPERTY_LEAD,
            "identified building without current sale-availability evidence",
        )
    news_hit = _contains(combined, _NEWS_PHRASES)
    if news_hit:
        return (
            ResultClassification.NEWS_ARTICLE,
            f"reporting language '{news_hit}' without an identified property",
        )
    topic_hit = _contains(combined, _COMMERCIAL_TOPICS)
    if topic_hit:
        return (
            ResultClassification.BACKGROUND_INFORMATION,
            f"commercial topic '{topic_hit}' without property specifics",
        )
    void = url.strip()
    return (
        ResultClassification.UNKNOWN,
        f"no classifiable signals at {void}" if void else "no classifiable signals",
    )


# ---------------------------------------------------------------------------
# Property identification (conservative patterns)
# ---------------------------------------------------------------------------

_SUFFIXES = (
    "Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|Drive|Dr|Freeway|Fwy|"
    "Highway|Hwy|Parkway|Pkwy|Plaza|Way|Court|Ct|Circle|Cir|Trail|Trl|Park"
)
_ADDRESS_RE = re.compile(
    rf"\b(\d{{1,6}}\s+[A-Z0-9][\w.'-]*(?:\s+[A-Z0-9][\w.'-]*){{0,4}}\s+(?:{_SUFFIXES})\.?)\b"
)
_CITY_STATE_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),\s*(TX|Texas)\b")
_STATE_RE = re.compile(r"\b(TX|Texas)\b")
_ZIP_RE = re.compile(r"\b(75\d{3})\b")
_NAME_RE = re.compile(
    r"\b([A-Z][\w&']*(?:\s+[A-Z][\w&']*){0,3}\s+"
    r"(?:Tower|Towers|Plaza|Center|Centre|Building|Place|Exchange|Park|Commons|Station|Hub|Square))\b"
)
_UNIT_RE = re.compile(
    r"\b(suites?|ste\.?|units?|floors?|fl\.?|buildings?|bldg\.?)\s*(#?)\s*([a-z0-9][a-z0-9-]*)",
    re.IGNORECASE,
)


def _extract_designator(text: str) -> str | None:
    """Capture an explicitly stated suite/unit/floor/building designator.

    Narrow by design: the token must contain a digit, carry a '#' prefix,
    or be a single character ending the clause. Plain-language matches
    such as "building a new lobby" are rejected. First match wins; an
    absent designator stays None and is never fabricated.
    """
    for match in _UNIT_RE.finditer(text):
        token = match.group(3)
        has_digit = any(char.isdigit() for char in token)
        hash_prefix = match.group(2) == "#"
        after = text[match.end(3) : match.end(3) + 1]
        terminal = after in ("", ",", ".", ")", ";", ":")
        if has_digit or hash_prefix or (len(token) == 1 and terminal):
            keyword = match.group(1)
            designator = f"{keyword} {match.group(2)}{token}".strip()
            return " ".join(designator.split())
    return None
_CATEGORY_KEYWORDS: tuple[tuple[PropertyCategory, tuple[str, ...]], ...] = (
    (PropertyCategory.HIGH_RISE, ("high-rise", "high rise", "skyscraper", "tower", "stories")),
    (PropertyCategory.WAREHOUSE_INDUSTRIAL, ("warehouse", "industrial", "distribution", "logistics")),
    (PropertyCategory.DOWNTOWN_COMMERCIAL, ("office", "downtown", "commercial")),
)


def _guess_category(text: str) -> PropertyCategory | None:
    lowered = text.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def extract_properties(title: str, snippet: str) -> list[ExtractedProperty]:
    """Extract identity candidates; each stays separate (never merged)."""
    combined = f"{title}. {snippet}" if snippet else title
    names = [match.group(1) for match in _NAME_RE.finditer(combined)]
    addresses = [match.group(1) for match in _ADDRESS_RE.finditer(combined)]
    city_state = _CITY_STATE_RE.search(combined)
    city = city_state.group(1) if city_state else None
    state_match = city_state.group(2) if city_state else (_STATE_RE.search(combined) or [None, None])[1]
    state = state_match if state_match in ("TX", "Texas") else None
    zip_match = _ZIP_RE.search(combined)
    zip_code = zip_match.group(1) if zip_match else None
    category = _guess_category(combined)

    unique_names = list(dict.fromkeys(names))
    unique_addresses = list(dict.fromkeys(addresses))
    # Exactly one name and at most one address describe a single building
    # and merge; anything else stays separate so attribution stays safe.
    if len(unique_names) == 1 and len(unique_addresses) <= 1:
        address = unique_addresses[0] if unique_addresses else None
        return [
            ExtractedProperty(
                name=unique_names[0], street_address=address, city=city,
                state=state, zip_code=zip_code, category=category,
                unit_designator=_extract_designator(combined),
                evidence=f"named building '{unique_names[0]}'"
                + (f" at '{address}'" if address else ""),
            )
        ]
    properties: list[ExtractedProperty] = []
    for name in unique_names:
        properties.append(
            ExtractedProperty(
                name=name, city=city, state=state, zip_code=zip_code,
                category=category, evidence=f"named building '{name}'",
            )
        )
    for address in unique_addresses:
        properties.append(
            ExtractedProperty(
                street_address=address, city=city, state=state, zip_code=zip_code,
                category=category, evidence=f"street address '{address}'",
            )
        )
    return properties


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion|thousand|mm|m|b|k)?\b", re.IGNORECASE
)
_MULTIPLIERS = {"thousand": 1_000, "k": 1_000, "million": 1_000_000, "m": 1_000_000,
                "mm": 1_000_000, "billion": 1_000_000_000, "b": 1_000_000_000}
_PER_SQFT_RE = re.compile(r"(per\s+square\s+foot|/\s*sf\b|/\s*sq\.?\s*ft\.?|\bpsf\b)", re.IGNORECASE)
_PROPOSED_RE = re.compile(r"\b(proposed|pending|under contract|agreed|expected|planned)\b", re.IGNORECASE)
_COMPLETED_RE = re.compile(
    r"\b(sold\s+(?:[a-z]+\s+){0,3}for|sold\s+at|closed\s+(?:at|for)|purchased\s+for|"
    r"acquired\s+for|bought\s+for|changed\s+hands|historical\s+transaction|transaction\s+of)\b",
    re.IGNORECASE,
)
_ASKING_RE = re.compile(
    r"\b(asking(?:\s+price)?|list\s+price|listed\s+at|offered\s+at|priced\s+at)\b", re.IGNORECASE
)
_PAST_COPULA_RE = re.compile(r"\b(was|were|had been|formerly|originally)\b", re.IGNORECASE)
_PRIOR_ASKING_RE = re.compile(r"\b(reduced\s+from|cut\s+from|down\s+from)\b", re.IGNORECASE)
_VALUE_RE = re.compile(
    r"\b(valued\s+at|estimated\s+value|appraised(?:\s+at)?|market\s+value|worth)\b", re.IGNORECASE
)


def _parse_amount(raw: str, unit: str | None) -> Decimal:
    value = Decimal(raw.replace(",", ""))
    if unit:
        value *= _MULTIPLIERS[unit.lower()]
    return value


def _classify_money(context: str) -> MoneyKind:
    if _PER_SQFT_RE.search(context):
        return MoneyKind.PRICE_PER_SQFT
    if _PROPOSED_RE.search(context):
        return MoneyKind.TRANSACTION_PROPOSED
    if _COMPLETED_RE.search(context):
        return MoneyKind.SALE_PRICE_COMPLETED
    if _PRIOR_ASKING_RE.search(context):
        return MoneyKind.ASKING_PRICE_HISTORICAL
    if _ASKING_RE.search(context):
        before = context.rsplit("$", 1)[0][-40:] if "$" in context else context[:40]
        if _PAST_COPULA_RE.search(before):
            return MoneyKind.ASKING_PRICE_HISTORICAL
        return MoneyKind.ASKING_PRICE_CURRENT
    if _VALUE_RE.search(context):
        return MoneyKind.MARKET_VALUE_ESTIMATED
    return MoneyKind.UNCLASSIFIED


_RANGE_RE = re.compile(
    r"\$\s*(?P<first>[\d,]+(?:\.\d+)?)\s*(?:(?P<unit1>million|billion|thousand|mm|m|b|k)\b\s*)?"
    r"(?P<sep>-|\u2013|\u2014|to\b)\s*\$?\s*(?P<second>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit2>million|billion|thousand|mm|m|b|k)\b",
    re.IGNORECASE,
)
_BETWEEN_RANGE_RE = re.compile(
    r"\bbetween\s+\$\s*([\d,]+(?:\.\d+)?)\s+and\s+\$?\s*([\d,]+(?:\.\d+)?)\s*"
    r"(million|billion|thousand|mm|m|b|k)\b",
    re.IGNORECASE,
)


def _range_spans(text: str) -> list[tuple[int, int, Decimal, Decimal]]:
    """Quoted price ranges as (start, end, first, second) in quote order.

    A range shares one trailing unit ("$5-7 million") or joins two
    unit-bearing endpoints with a dash ("$5M-$7M"). Reduction language
    ("reduced/cut/down from") is never a range: old-to-new is history,
    not a quoted span. The word "to" joins endpoints only when the
    first is unitless, so "reduced from $4 million to $3.5 million"
    keeps two historical mentions.
    """
    spans: list[tuple[int, int, Decimal, Decimal]] = []
    candidates: list[tuple[int, int, str, str | None, str, str, str]] = []
    for match in _RANGE_RE.finditer(text):
        candidates.append(
            (match.start(), match.end(), match.group("first"), match.group("unit1"),
             match.group("second"), match.group("unit2"), match.group("sep"))
        )
    for match in _BETWEEN_RANGE_RE.finditer(text):
        candidates.append(
            (match.start(), match.end(), match.group(1), None,
             match.group(2), match.group(3), "between")
        )
    for start, end, first, unit1, second, unit2, sep in sorted(candidates, key=lambda c: (c[0], c[1])):
        if any(start < kept_end and kept_start < end for kept_start, kept_end, _, _ in spans):
            continue  # overlapping range already recorded
        context = text[max(0, start - 100) : end + 100]
        if _PRIOR_ASKING_RE.search(context):
            continue  # price reduction, not a quoted range
        if unit1 is not None and sep.lower() == "to":
            # Dual-unit endpoints joined by "to" ("$4 million to $3.5
            # million") describe a sequence, not a quoted span.
            continue
        try:
            low = _parse_amount(first, unit1 or unit2)
            high = _parse_amount(second, unit2)
        except ArithmeticError:
            continue
        spans.append((start, end, low, high))
    return spans


def extract_money(text: str) -> list[MoneyMention]:
    """Find explicit dollar amounts; ambiguous ones stay unclassified.

    Quoted ranges become one ranged observation (both endpoints scaled,
    quote order preserved); overlapping point matches are skipped so
    endpoints never double-count as independent observations.
    """
    mentions: list[MoneyMention] = []
    ranges = _range_spans(text)
    for start, end, low, high in ranges:
        context = text[max(0, start - 100) : end + 100]
        mentions.append(
            MoneyMention(
                amount=low,
                amount_high=high,
                kind=_classify_money(context),
                evidence=_excerpt(text, start, end),
            )
        )
    ranged = [(start, end) for start, end, _, _ in ranges]
    for match in _AMOUNT_RE.finditer(text):
        if any(start <= match.start() < end for start, end in ranged):
            continue
        try:
            amount = _parse_amount(match.group(1), match.group(2))
        except ArithmeticError:
            continue
        start = max(0, match.start() - 100)
        context = text[start : match.end() + 100]
        mentions.append(
            MoneyMention(
                amount=amount,
                kind=_classify_money(context),
                evidence=_excerpt(text, match.start(), match.end()),
            )
        )
    return mentions


# ---------------------------------------------------------------------------
# Distress and underutilization signals (separate vocabularies)
# ---------------------------------------------------------------------------

_DISTRESS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("bankruptcy", r"\b(bankruptcy|bankrupt|chapter\s*11|chapter\s*7)\b"),
    ("foreclosure", r"\bforeclos\w*\b"),
    ("receivership", r"\b(receivership|court-appointed receiver|placed in receivership)\b"),
    ("loan_default", r"\b(loan\s+default|defaulted\s+on\s+.*loan|missed\s+loan\s+payments|delinquent\s+loan)\b"),
    ("reo_lender_owned", r"\b(reo|lender[- ]owned|bank[- ]owned)\b"),
    ("distressed_sale", r"\bdistressed\s+(sale|asset|property)\b"),
    ("auction_liquidation", r"\b(auction|liquidation)\b"),
    ("price_reduction", r"\b(price\s+(cut|reduction|reduced)|reduced\s+.*price|price\s+slashed|cut\s+.*asking\s+price)\b"),
    ("prolonged_listing", r"\b(on the market for|unsold for|lingering on the market|stale listing|listed for .* (?:months|years))\b"),
)

_UNDERUSE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("low_occupancy", r"\b(low\s+occupancy|occupancy\s+(?:fell|dropped|declined|plunged)|under-?occupied)\b"),
    ("high_vacancy", r"\b(high\s+vacancy|vacancy\s+(?:rose|climbed|soared)|rising\s+vacancy)\b"),
    ("unleased_space", r"\b(unleased|square\s+feet\s+of\s+empty|empty\s+square\s+feet)\b"),
    ("tenant_departure", r"\b(tenant\w*\s+(?:left|departed|vacated|moved\s+out)|lost\s+(?:its|a|the)\s+tenant|anchor\s+tenant\s+(?:left|departed|closed))\b"),
    ("unused_space", r"\b(unused\s+(?:commercial\s+)?space|sitting\s+(?:empty|vacant)|half\s+empty)\b"),
    ("partial_abandonment", r"\b(partially\s+abandoned|abandoned\s+floors?)\b"),
)


def _extract_signals(text: str, patterns: tuple[tuple[str, str], ...]) -> list[SignalObservation]:
    seen: set[str] = set()
    observations: list[SignalObservation] = []
    for signal, pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and signal not in seen:
            seen.add(signal)
            observations.append(
                SignalObservation(signal=signal, evidence=_excerpt(text, match.start(), match.end()))
            )
    return observations


def extract_distress(text: str) -> list[SignalObservation]:
    return _extract_signals(text, _DISTRESS_PATTERNS)


def extract_underuse(text: str) -> list[SignalObservation]:
    return _extract_signals(text, _UNDERUSE_PATTERNS)


_STRUCTURE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("demolished", r"\b(demolished|torn down|razed|has been demolished)\b"),
    ("proposed", r"\b(proposed\s+(?:tower|building|development|project|office)|"
                 r"planned\s+(?:tower|building)|yet to be built|to be constructed|never built)\b"),
    ("land_vacant", r"\b(vacant land|empty lot|vacant lot|land for sale)\b"),
    ("land_parcel", r"(?<!and )\b(parcel|acreage|pad site)\b"),
    ("incompatible_use", r"\b(single-family\s+(?:home|house|residence)|single family home)\b"),
)

_COUNTY_RE = re.compile(r"\b(?:[Tt]he\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+County\b")
_DOWNTOWN_RE = re.compile(r"\bdowntown\b", re.IGNORECASE)


def extract_structure(text: str) -> list[SignalObservation]:
    """Detect explicit physical-status signals (demolished/proposed/land/use)."""
    return _extract_signals(text, _STRUCTURE_PATTERNS)


def extract_county_mentions(text: str) -> list[str]:
    """Collect '<Name> County' mentions (evidence, not a lookup)."""
    return [f"{match.group(1)} County" for match in _COUNTY_RE.finditer(text)]


def mentions_downtown(text: str) -> bool:
    """Whether the text explicitly references a downtown area."""
    return bool(_DOWNTOWN_RE.search(text))


# ---------------------------------------------------------------------------
# Occupancy, vacancy, availability
# ---------------------------------------------------------------------------

_PCT_BEFORE_RE = re.compile(
    r"\b(\d{1,3}(?:\.\d+)?)\s*%\s*(occupied|occupancy|leased|vacant|vacancy)\b", re.IGNORECASE
)
_PCT_AFTER_RE = re.compile(
    r"\b(occupancy|vacancy)(?:\s+rate)?\s+(?:of|at|fell to|rose to|dropped to|is|was|stands at)?\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_WHOLE_SCOPE_RE = re.compile(
    r"\b(entire building|whole building|building-wide|the tower|the building|the property)\b", re.IGNORECASE
)
_PARTIAL_SCOPE_RE = re.compile(r"\b(floors?|suite|wing|portion|section|part of|block of space)\b", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"\b(Q[1-4]\s+\d{4}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}|\bin\s+\d{4}\b|\d{4})\b"
)
_SQFT_NEAR_RE = re.compile(r"\b([\d,]{4,})\s*(?:square feet|sq\.?\s*ft\.?)\b", re.IGNORECASE)
_AVAILABLE_RE = re.compile(
    r"\b([\d,]+)\s*(?:square feet|sq\.?\s*ft\.?)\s*(?:of\s+)?(available|for lease|up for lease)\b",
    re.IGNORECASE,
)


def _scope_of(context: str) -> ObservationScope:
    whole = bool(_WHOLE_SCOPE_RE.search(context))
    partial = bool(_PARTIAL_SCOPE_RE.search(context))
    if whole and not partial:
        return ObservationScope.WHOLE_BUILDING
    if partial and not whole:
        return ObservationScope.PARTIAL
    return ObservationScope.UNKNOWN


def _period_near(text: str, start: int, end: int) -> str | None:
    window = text[max(0, start - 80) : end + 80]
    match = _PERIOD_RE.search(window)
    return match.group(0) if match else None


def _sqft_near(text: str, start: int, end: int) -> int | None:
    window = text[max(0, start - 80) : end + 80]
    match = _SQFT_NEAR_RE.search(window)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _valid_pct(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 100 else None


def extract_occupancy(text: str) -> list[OccupancyObservation]:
    """Extract explicitly reported occupancy/vacancy figures (never infer)."""
    observations: list[OccupancyObservation] = []
    for match in _PCT_BEFORE_RE.finditer(text):
        pct = _valid_pct(match.group(1))
        if pct is None:
            continue
        term = match.group(2).lower()
        is_vacancy = term in ("vacant", "vacancy")
        observations.append(
            OccupancyObservation(
                occupancy_pct=None if is_vacancy else pct,
                vacancy_pct=pct if is_vacancy else None,
                basis=term,
                square_feet=_sqft_near(text, match.start(), match.end()),
                period=_period_near(text, match.start(), match.end()),
                scope=_scope_of(text[max(0, match.start() - 80) : match.end() + 80]),
                evidence=_excerpt(text, match.start(), match.end()),
            )
        )
    for match in _PCT_AFTER_RE.finditer(text):
        pct = _valid_pct(match.group(2))
        if pct is None:
            continue
        is_vacancy = match.group(1).lower() == "vacancy"
        observations.append(
            OccupancyObservation(
                occupancy_pct=None if is_vacancy else pct,
                vacancy_pct=pct if is_vacancy else None,
                basis=match.group(1).lower(),
                square_feet=_sqft_near(text, match.start(), match.end()),
                period=_period_near(text, match.start(), match.end()),
                scope=_scope_of(text[max(0, match.start() - 80) : match.end() + 80]),
                evidence=_excerpt(text, match.start(), match.end()),
            )
        )
    return observations


def extract_availability(text: str) -> list[AvailabilityObservation]:
    """Preserve advertised availability; never call it vacancy."""
    observations: list[AvailabilityObservation] = []
    for match in _AVAILABLE_RE.finditer(text):
        try:
            sqft = int(match.group(1).replace(",", ""))
        except ValueError:
            continue
        observations.append(
            AvailabilityObservation(
                square_feet=sqft, evidence=_excerpt(text, match.start(), match.end())
            )
        )
    return observations


# ---------------------------------------------------------------------------
# Temporal awareness
# ---------------------------------------------------------------------------

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})\b"
)
_MONTH_YEAR_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b")
_YEAR_ONLY_RE = re.compile(r"\b(19|20)\d{2}\b")
_RELATIVE_RE = re.compile(r"\b(\d+)\s+(day|week|month|year)s?\s+ago\b", re.IGNORECASE)


def _resolve_published(published_at: str | None, retrieved_at: str) -> datetime | None:
    """Best-effort date resolution; None when it cannot be established."""
    if not published_at:
        return None
    text = published_at.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    relative = _RELATIVE_RE.search(text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * amount
        from datetime import timedelta

        return retrieved - timedelta(days=days)
    month_day = _MONTH_DAY_YEAR_RE.search(text)
    if month_day:
        month = _MONTHS.index(month_day.group(1)) + 1
        return datetime(int(month_day.group(3)), month, int(month_day.group(2)), tzinfo=timezone.utc)
    month_year = _MONTH_YEAR_RE.search(text)
    if month_year:
        month = _MONTHS.index(month_year.group(1)) + 1
        return datetime(int(month_year.group(2)), month, 1, tzinfo=timezone.utc)
    return None


def assess_temporal(
    published_at: str | None, retrieved_at: str, offers_sale_now: bool
) -> TemporalAssessment:
    """Current only with a resolved recent date plus present-tense offer."""
    resolved = _resolve_published(published_at, retrieved_at)
    if resolved is None:
        year_match = _YEAR_ONLY_RE.search(published_at or "")
        if year_match:
            try:
                retrieved_year = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00")).year
            except ValueError:
                retrieved_year = None
            if retrieved_year is not None and int(year_match.group(0)) < retrieved_year:
                return TemporalAssessment(
                    status=TemporalStatus.HISTORICAL,
                    published_at=published_at,
                    reason=f"year {year_match.group(0)} precedes retrieval year; historical only",
                )
        return TemporalAssessment(
            status=TemporalStatus.UNDETERMINED,
            published_at=published_at,
            reason="publication date cannot be established; status unknown",
        )
    try:
        retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
    except ValueError:
        return TemporalAssessment(
            status=TemporalStatus.UNDETERMINED,
            published_at=published_at,
            reason="retrieval timestamp unreadable; status unknown",
        )
    age_days = (retrieved - resolved).days
    if age_days > CURRENT_EVENT_DAYS:
        return TemporalAssessment(
            status=TemporalStatus.HISTORICAL,
            published_at=published_at,
            reason=f"published {age_days} days before retrieval; does not establish current status",
        )
    if offers_sale_now:
        return TemporalAssessment(
            status=TemporalStatus.CURRENT,
            published_at=published_at,
            reason="recent publication with present-tense sale language (still unverified)",
        )
    return TemporalAssessment(
        status=TemporalStatus.UNDETERMINED,
        published_at=published_at,
        reason="recent but without present-tense availability language",
    )


# ---------------------------------------------------------------------------
# Assembly and grouping
# ---------------------------------------------------------------------------

_PRESENT_SALE_RE = re.compile(
    r"\b(is\s+(?:now\s+)?(?:for sale|listed|offered)|now\s+(?:for sale|listed|offered)|"
    r"currently\s+(?:for sale|listed|available)|for sale)\b",
    re.IGNORECASE,
)


def extract_intelligence(result: BraveSearchResult) -> ResultIntelligence:
    """Extract structured intelligence from one retrieved result (pure)."""
    title = normalize_text(result.title)
    snippet = normalize_text(result.snippet)
    combined = f"{title}. {snippet}" if snippet else title
    properties = extract_properties(title, snippet)
    has_identity = bool(properties)
    classification, reason = classify_result(title, snippet, result.url, has_identity)
    offers_now = bool(_PRESENT_SALE_RE.search(combined))
    temporal = assess_temporal(result.published_at, result.retrieved_at, offers_now)
    underuse = extract_underuse(combined)
    occupancy = extract_occupancy(combined)
    for observation in occupancy:
        if (
            observation.occupancy_pct is not None
            and observation.occupancy_pct < 50
            and not any(signal.signal == "low_occupancy" for signal in underuse)
        ):
            underuse.append(
                SignalObservation(
                    signal="low_occupancy",
                    evidence=f"reported occupancy {observation.occupancy_pct}%: {observation.evidence}",
                )
            )
    money = extract_money(combined)
    availability = extract_availability(combined)
    distress = extract_distress(combined)
    structure = extract_structure(combined)
    observed = bool(properties or money or occupancy or availability or distress or underuse)
    status = (
        ObservationStatus.REPORTED_UNVERIFIED
        if observed or classification is not ResultClassification.UNKNOWN
        else ObservationStatus.UNKNOWN
    )
    return ResultIntelligence(
        assignment_id=result.assignment_id,
        query=result.query,
        result_url=result.url,
        domain=result.domain or _domain_of_url(result.url),
        title=title,
        retrieved_at=result.retrieved_at,
        classification=classification,
        classification_reason=reason,
        properties=properties,
        money=money,
        occupancy=occupancy,
        availability=availability,
        distress=distress,
        underuse=underuse,
        structure=structure,
        county_mentions=extract_county_mentions(combined),
        mentions_downtown=mentions_downtown(combined),
        temporal=temporal,
        status=status,
    )


def _domain_of_url(url: str) -> str:
    from app.agents.gabriel.source_identity import canonical_hostname

    return canonical_hostname(url)


class PropertyGroup(BaseModel):
    """Provisionally grouped observations; attributes never merged.

    Grouping organizes evidence for review. It never asserts that grouped
    observations describe the same physical building.
    """

    key: str
    basis: str  # "exact_address", "same_document", or "no_identity"
    needs_review: bool = False
    review_reasons: list[str] = []
    members: list[int] = []  # indices into the intelligence list


def _normalize_identity(value: str | None) -> str:
    from app.agents.gabriel.dedup import normalize_text as _norm

    return _norm(value)


def _address_key(prop: ExtractedProperty) -> str | None:
    street = _normalize_identity(prop.street_address)
    if not street:
        return None
    city = _normalize_identity(prop.city)
    state = _normalize_identity(prop.state)
    zip_code = _normalize_identity(prop.zip_code)
    return f"addr:{street}|{city}|{state}|{zip_code}"


def _identity_signature(item: ResultIntelligence) -> tuple[tuple[str, str, str, str, str], ...] | None:
    """Complete normalized identity signature for addressless grouping."""
    parts = []
    for prop in item.properties:
        part = (
            _normalize_identity(prop.name),
            _normalize_identity(prop.city),
            _normalize_identity(prop.state),
            _normalize_identity(prop.zip_code),
            _normalize_identity(prop.unit_designator),
        )
        if any(part):
            parts.append(part)
    if not parts:
        return None
    return tuple(sorted(parts))


_DISPUTE_FIELDS = ("name", "street_address", "city", "state", "zip_code", "unit_designator")
_MAX_DISPUTE_VARIANTS = 10
_MAX_DISPUTE_VALUE_CHARS = 200


def eligible_identity_props(item: ResultIntelligence, group_key: str) -> list[ExtractedProperty]:
    """Props that may inform one group's identity (never another address).

    Only props at this group's address, or with no address at all, are
    eligible; props explicitly at another address belong to that group.
    Mirrors the eligibility rule in _refresh_address_group.
    """
    eligible = []
    for prop in item.properties:
        key = _address_key(prop)
        if key is not None and key != group_key:
            continue
        eligible.append(prop)
    return eligible


def identity_disputes(
    items: list[ResultIntelligence], members: list[int], group_key: str
) -> dict[str, list[str]]:
    """Field-level identity disputes among a group's eligible observations.

    Compares normalized values across eligible props; absent values are
    ignored (missing data is not a conflict). Returns only disputed
    fields (two or more distinct normalized values), each mapped to its
    observed raw variants ordered by normalized value (deterministic).
    Comparison runs on full values; displayed variants are
    length-bounded without merging disputes.
    """
    by_field: dict[str, dict[str, str]] = {field: {} for field in _DISPUTE_FIELDS}
    for index in members:
        for prop in eligible_identity_props(items[index], group_key):
            for field in _DISPUTE_FIELDS:
                raw = getattr(prop, field)
                if not raw:
                    continue
                norm = _normalize_identity(raw)
                if not norm:
                    continue
                by_field[field].setdefault(norm, raw)
    disputes: dict[str, list[str]] = {}
    for field in _DISPUTE_FIELDS:
        variants = by_field[field]
        if len(variants) > 1:
            ordered = [variants[norm] for norm in sorted(variants)]
            disputes[field] = [
                value[:_MAX_DISPUTE_VALUE_CHARS] for value in ordered[:_MAX_DISPUTE_VARIANTS]
            ]
    return disputes


def assembled_identity(
    items: list[ResultIntelligence], members: list[int], group_key: str
) -> dict[str, str | None]:
    """First observed identity values among a group's eligible props.

    Same eligibility as identity_disputes: observations explicitly at
    another address can never supply this group's identity. Later,
    conflicting, or absent values never override the first observed;
    use identity_disputes to detect conflicts.
    """
    found: dict[str, str | None] = {field: None for field in _DISPUTE_FIELDS}
    for index in members:
        for prop in eligible_identity_props(items[index], group_key):
            for field in _DISPUTE_FIELDS:
                if found[field] is None:
                    raw = getattr(prop, field)
                    if raw:
                        found[field] = raw
        if all(found[field] is not None for field in _DISPUTE_FIELDS):
            break
    return found


def _refresh_address_group(group: PropertyGroup, items: list[ResultIntelligence]) -> None:
    """Recompute identity conflicts for one address group (deterministic).

    Only props at this group's address, or with no address at all, inform
    the verdict; props explicitly at another address belong to that group.
    """
    names: set[str] = set()
    designators: set[str] = set()
    designator_absent = False
    portfolio = False
    for index in group.members:
        item = items[index]
        keys = {key for key in (_address_key(prop) for prop in item.properties) if key}
        if len(keys) > 1:
            portfolio = True
        for prop in item.properties:
            key = _address_key(prop)
            if key is not None and key != group.key:
                continue
            name = _normalize_identity(prop.name)
            if name:
                names.add(name)
            designator = _normalize_identity(prop.unit_designator)
            if designator:
                designators.add(designator)
            else:
                designator_absent = True
    reasons = []
    if len(names) > 1:
        quoted = ", ".join(f"'{name}'" for name in sorted(names))
        reasons.append(f"conflicting observed building names: {quoted}")
    if len(designators) > 1:
        quoted = ", ".join(f"'{designator}'" for designator in sorted(designators))
        reasons.append(f"conflicting observed unit/building designators: {quoted}")
    elif designators and designator_absent:
        reasons.append("unit/building designator present in some observations but absent in others")
    if portfolio:
        reasons.append("multi-building portfolio association spanning multiple address groups")
    group.review_reasons = reasons
    group.needs_review = bool(reasons)


def group_by_property(items: list[ResultIntelligence]) -> list[PropertyGroup]:
    """Group results by exact address or repeated identical observation.

    Addressed observations group by normalized address only; names and
    URLs never join them. Addressless observations group only when the
    exact source URL and the complete normalized identity signature both
    match. Anything else stays separate. Grouping is provisional evidence
    organization, never proof of physical-building identity.
    """
    groups: dict[str, PropertyGroup] = {}
    order: list[str] = []
    for index, item in enumerate(items):
        addr_keys: list[str] = []
        for prop in item.properties:
            key = _address_key(prop)
            if key is not None and key not in addr_keys:
                addr_keys.append(key)
        if addr_keys:
            touched: list[str] = []
            for key in addr_keys:
                if key in groups:
                    if index not in groups[key].members:
                        groups[key].members.append(index)
                else:
                    groups[key] = PropertyGroup(key=key, basis="exact_address", members=[index])
                    order.append(key)
                touched.append(key)
            for key in touched:
                _refresh_address_group(groups[key], items)
            continue
        signature = _identity_signature(item)
        url = (item.result_url or "").strip()
        if signature is not None and url:
            # Full repr keeps equality collision-safe; the key is opaque
            # downstream (only the "ungrouped:" prefix is interpreted).
            key = f"doc:{url}|{signature!r}"
            if key in groups:
                if index not in groups[key].members:
                    groups[key].members.append(index)
            else:
                groups[key] = PropertyGroup(key=key, basis="same_document", members=[index])
                order.append(key)
            continue
        key = f"ungrouped:{index}"
        groups[key] = PropertyGroup(
            key=key,
            basis="no_identity",
            needs_review=True,
            review_reasons=["insufficient identity evidence to group this observation"],
            members=[index],
        )
        order.append(key)
    return [groups[key] for key in order]


def extract_research_intelligence(results: list[BraveSearchResult]) -> ResearchIntelligence:
    """Extract intelligence for a full result set (pure, order-preserving)."""
    return ResearchIntelligence(results=[extract_intelligence(item) for item in results])
