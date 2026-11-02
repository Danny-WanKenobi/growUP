"""Dynamic batch-geography tests (offline, Census 2020 dataset)."""
from __future__ import annotations

import pytest

from app.agents.gabriel.batch_geography import (
    BatchGeographyError,
    batch_cells,
    batch_plan,
    legacy_dfw_cells,
    metro_counties,
    principal_city,
)


def test_principal_city_is_computed_not_dallas():
    city = principal_city("TX")
    assert city["name"] == "Houston"
    assert city["population_2020"] == 2301572


def test_metro_counties_ranked_top_twelve():
    counties = metro_counties("TX")
    assert len(counties) == 12
    pops = [c["population_2020"] for c in counties]
    assert pops == sorted(pops, reverse=True)
    assert counties[0]["county"] == "harris county"
    names = {c["county"] for c in counties}
    # 13th/14th CSA counties are excluded by the top-12 rule.
    assert "austin county" not in names
    assert "san jacinto county" not in names


def test_batch_matrix_is_36_cells_144_target():
    plan = batch_plan("TX")
    assert plan["cell_count"] == 36
    assert plan["target_total"] == 144
    assert plan["principal_city"] == "Houston"
    categories = {cell["category"] for cell in plan["cells"]}
    assert categories == {
        "high_rise", "commercial_property", "warehouse_industrial"}
    assert all(cell["target"] == 4 for cell in plan["cells"])
    assert len({cell["county"] for cell in plan["cells"]}) == 12


def test_unknown_state_fails_closed():
    with pytest.raises(BatchGeographyError):
        batch_cells("ZZ")


def test_legacy_dfw_roster_preserved_for_dallas_work():
    cells = legacy_dfw_cells()
    assert len(cells) == 36
    assert cells[0]["county"] == "dallas county"
    assert {c["county"] for c in cells} != {
        c["county"] for c in batch_cells("TX")}
