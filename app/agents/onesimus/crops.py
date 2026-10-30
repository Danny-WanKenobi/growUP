"""Approved MVP crop profile: butterhead lettuce in passive Kratky culture.

Engineering inputs only; prices and other financial figures belong to
Michael. Every figure carries provenance and classification. Built-in
defaults are planning assumptions, not measured results: they are
labeled `assumed` (chosen defaults) or `estimated` (ranges chosen from
experience, applied without site measurement or cited reference).
Nothing here is `verified` against a real deployment; `unknown` marks
quantities no default may invent.

Classifications: verified | estimated | experimental | assumed | unknown.
"""

from __future__ import annotations

from app.agents.onesimus.service import ClassifiedInput

CROP_ID = "butterhead_lettuce_kratky_mvp1"


def crop_profile() -> dict[str, ClassifiedInput]:
    """Butterhead lettuce defaults with units, provenance, classification."""
    C = ClassifiedInput
    return {
        "spacing_m": C(
            name="spacing_m", value=0.2, unit="m",
            provenance="mvp-defaults: 20cm grid for butterhead heads",
            classification="assumed",
        ),
        "cycles_per_year": C(
            name="cycles_per_year", value=10.0, unit="cycles/year",
            provenance="mvp-defaults: ~35-day cycle with turnaround",
            classification="assumed",
        ),
        "saleable_kg_per_head": C(
            name="saleable_kg_per_head", value=0.15, unit="kg/head",
            provenance="mvp-defaults: assumed butterhead head weight; no cited source",
            classification="assumed",
        ),
        "reservoir_l_per_site": C(
            name="reservoir_l_per_site", value=4.0, unit="L/site",
            provenance="mvp-defaults: individual Kratky container sizing",
            classification="assumed",
        ),
        "root_zone_air_fraction": C(
            name="root_zone_air_fraction", value=1.0 / 3.0, unit="fraction",
            provenance="mvp-defaults: assumed one-third air gap at fill; no cited source",
            classification="estimated",
        ),
        "topup_l_per_site_cycle": C(
            name="topup_l_per_site_cycle", value=0.5, unit="L/site/cycle",
            provenance="mvp-defaults: evaporation/transpiration allowance",
            classification="assumed",
        ),
        "light_w_per_m2": C(
            name="light_w_per_m2", value=30.0, unit="W/m2",
            provenance="mvp-defaults: assumed LED allowance; no cited source",
            classification="estimated",
        ),
        "light_hours_per_day": C(
            name="light_hours_per_day", value=14.0, unit="h/day",
            provenance="mvp-defaults: assumed leafy-green photoperiod; no cited source",
            classification="estimated",
        ),
    }
