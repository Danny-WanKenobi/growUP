"""Shared seven-county operating-area definitions (MVP-004B-5).

Single source of county membership: the ranking engine (eligibility
gates) and the search planner (county coverage) both derive from these
constants so the territory can never drift between modules.

Canonical form is lowercase "<name> county" (matching extractor county
mentions); display form is "Collin County" style for markets and labels.
There is intentionally no municipality-to-county lookup.
"""

from __future__ import annotations

DALLAS_COUNTY = "dallas county"

# Secondary territory: counties directly adjacent to Dallas County.
SECONDARY_COUNTIES = frozenset(
    {
        "collin county",
        "denton county",
        "tarrant county",
        "ellis county",
        "kaufman county",
        "rockwall county",
    }
)


def display_name(canonical: str) -> str:
    """Render a canonical county name for markets and labels."""
    return " ".join(word.capitalize() for word in canonical.split())


PRIMARY_COUNTY_DISPLAY = display_name(DALLAS_COUNTY)


def secondary_county_names() -> tuple[str, ...]:
    """Alphabetical display names (deterministic rotation order)."""
    return tuple(display_name(name) for name in sorted(SECONDARY_COUNTIES))
