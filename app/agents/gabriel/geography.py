"""Shared ten-county operating-area definitions (ten-county expansion).

Single source of county membership AND priority order: the ranking
engine (eligibility gates) and the search planner (county coverage)
both derive from these constants so the territory can never drift
between modules.

Canonical form is lowercase "<name> county" (matching extractor county
mentions); display form is "Collin County" style for markets and labels.
There is intentionally no municipality-to-county lookup.
"""

from __future__ import annotations

DALLAS_COUNTY = "dallas county"

# Operating territory in exact priority order: Dallas County holds
# primary priority; the other nine are eligible secondary territory.
# Johnson, Parker, and Wise extend beyond the original adjacent ring.
COUNTY_PRIORITY_ORDER = (
    "dallas county",
    "tarrant county",
    "collin county",
    "denton county",
    "ellis county",
    "rockwall county",
    "kaufman county",
    "johnson county",
    "parker county",
    "wise county",
)

# Secondary territory: every operating county except Dallas.
SECONDARY_COUNTIES = frozenset(COUNTY_PRIORITY_ORDER[1:])


def display_name(canonical: str) -> str:
    """Render a canonical county name for markets and labels."""
    return " ".join(word.capitalize() for word in canonical.split())


PRIMARY_COUNTY_DISPLAY = display_name(DALLAS_COUNTY)


def ordered_county_names() -> tuple[str, ...]:
    """Display names in priority order (deterministic county sequencing)."""
    return tuple(display_name(name) for name in COUNTY_PRIORITY_ORDER)
