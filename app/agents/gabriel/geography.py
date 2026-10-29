"""Shared twelve-county operating-area definitions (B5-1 expansion).

Single source of county membership AND priority order: the ranking
engine (eligibility gates) and the search planner (county coverage)
both derive from these constants so the territory can never drift
between modules.

Canonical form is lowercase "<name> county" (matching extractor county
mentions); display form is "Collin County" style for markets and labels.
Municipality priority lists are keyed by canonical county, so cities
spanning county lines (Grand Prairie, Frisco, Royse City) keep the
county context of the list they were drawn from. There is intentionally
no reverse municipality-to-county lookup: a bare city name never
assigns county membership.
"""

from __future__ import annotations

DALLAS_COUNTY = "dallas county"

# Operating territory in exact priority order: Dallas County holds
# primary priority; the other eleven are eligible secondary territory.
COUNTY_PRIORITY_ORDER = (
    "dallas county",
    "tarrant county",
    "collin county",
    "denton county",
    "ellis county",
    "johnson county",
    "kaufman county",
    "parker county",
    "rockwall county",
    "hunt county",
    "wise county",
    "navarro county",
)

# Secondary territory: every operating county except Dallas.
SECONDARY_COUNTIES = frozenset(COUNTY_PRIORITY_ORDER[1:])

# Approved municipality search-priority order per county (B5-1): exactly
# five cities each, display form, most populous first. Keyed by canonical
# county so multi-county municipalities keep their county context.
CITY_PRIORITY_ORDER: dict[str, tuple[str, ...]] = {
    "dallas county": ("Dallas", "Irving", "Garland", "Mesquite", "Grand Prairie"),
    "tarrant county": ("Fort Worth", "Arlington", "Mansfield", "Grand Prairie", "North Richland Hills"),
    "collin county": ("Plano", "McKinney", "Frisco", "Allen", "Wylie"),
    "denton county": ("Denton", "Lewisville", "Frisco", "Carrollton", "Flower Mound"),
    "ellis county": ("Waxahachie", "Midlothian", "Ennis", "Red Oak", "Glenn Heights"),
    "johnson county": ("Burleson", "Cleburne", "Joshua", "Venus", "Alvarado"),
    "kaufman county": ("Forney", "Terrell", "Kaufman", "Crandall", "Mabank"),
    "parker county": ("Weatherford", "Willow Park", "Aledo", "Springtown", "Reno"),
    "rockwall county": ("Rockwall", "Fate", "Royse City", "Heath", "Rowlett"),
    "hunt county": ("Greenville", "Commerce", "Caddo Mills", "Royse City", "West Tawakoni"),
    "wise county": ("Decatur", "Bridgeport", "Boyd", "Runaway Bay", "Rhome"),
    "navarro county": ("Corsicana", "Kerens", "Rice", "Blooming Grove", "Dawson"),
}


def display_name(canonical: str) -> str:
    """Render a canonical county name for markets and labels."""
    return " ".join(word.capitalize() for word in canonical.split())


PRIMARY_COUNTY_DISPLAY = display_name(DALLAS_COUNTY)


def ordered_county_names() -> tuple[str, ...]:
    """Display names in priority order (deterministic county sequencing)."""
    return tuple(display_name(name) for name in COUNTY_PRIORITY_ORDER)


def ordered_cities(county: str) -> tuple[str, ...]:
    """Prioritized municipalities for one operating county (display form).

    Accepts the canonical ("dallas county") or display ("Dallas County")
    county name; anything else raises KeyError. The returned order is
    the approved search-priority order for that county.
    """
    return CITY_PRIORITY_ORDER[county.strip().lower()]
