"""Gabriel discovery-source precedence contract tests (offline only).

The contract declares owner-directed P1 official / P2 SearXNG /
P3 Brave ordering with Qwen excluded. It authorizes no live
collection or spending; these tests validate declaration
shapes and refusal behavior against the shipped example.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.agents.gabriel.sources.precedence import (
    PrecedenceError,
    load_precedence_contract,
    parse_precedence_contract,
    precedence_report,
)

_EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "discovery_sources.example.json"
)


def _raw() -> dict[str, Any]:
    return json.loads(_EXAMPLE.read_text(encoding="utf-8"))


def test_shipped_declaration_loads_in_owner_order() -> None:
    contract = load_precedence_contract(str(_EXAMPLE))
    assert contract.tier_of("official_datasets") == 1
    assert contract.tier_of("dcad_bulk") == 1
    assert contract.tier_of("searxng") == 2
    assert contract.tier_of("brave") == 3
    assert contract.tier_of("overpass") is None  # retained index, undeclared tier


def test_brave_is_last_and_budget_gated() -> None:
    contract = load_precedence_contract(str(_EXAMPLE))
    brave = contract.source("brave")
    assert brave is not None
    assert brave.status == "existing_fallback"
    assert brave.requires_credentials is True
    assert brave.requires_budget_authorization is True
    ordered = [source.name for source in contract.ordered_tier_sources()]
    assert ordered[-1] == "brave"


def test_planned_and_catalog_states_are_honest() -> None:
    contract = load_precedence_contract(str(_EXAMPLE))
    searxng = contract.source("searxng")
    official = contract.source("official_datasets")
    assert searxng is not None and searxng.status == "planned"
    assert official is not None and official.status == "catalog_required"
    assert [s.name for s in contract.geographic_index] == ["overpass", "osm_api"]
    assert [s.name for s in contract.evidence_adapters] == [
        "census_geocoder",
        "dcad_account_page",
    ]


def test_qwen_anywhere_refused() -> None:
    raw = _raw()
    raw["tiers"][1]["sources"].append(
        {"name": "qwen3", "kind": "llm", "status": "planned"}
    )
    with pytest.raises(PrecedenceError, match="excluded from Gabriel"):
        parse_precedence_contract(raw)
    raw = _raw()
    raw["excluded_from_gabriel"] = []
    with pytest.raises(PrecedenceError, match="must keep"):
        parse_precedence_contract(raw)


def test_wrong_order_and_unknown_keys_refused() -> None:
    raw = _raw()
    raw["tiers"][0], raw["tiers"][2] = raw["tiers"][2], raw["tiers"][0]
    with pytest.raises(PrecedenceError, match="owner-directed order"):
        parse_precedence_contract(raw)
    raw = _raw()
    raw["auto_launch"] = True
    with pytest.raises(PrecedenceError, match="unknown precedence key"):
        parse_precedence_contract(raw)


def test_ungated_brave_refused() -> None:
    raw = _raw()
    raw["tiers"][2]["sources"][0]["requires_budget_authorization"] = False
    with pytest.raises(PrecedenceError, match="budget authorization"):
        parse_precedence_contract(raw)


def test_report_is_ordered_and_json_safe() -> None:
    contract = load_precedence_contract(str(_EXAMPLE))
    report = precedence_report(contract)
    assert [tier["tier"] for tier in report["tiers"]] == [1, 2, 3]
    assert report["tiers"][2]["sources"][0]["name"] == "brave"
    assert report["excluded_from_gabriel"] == ["qwen"]
    json.dumps(report)  # must serialize without custom types


def test_malformed_declarations_refused() -> None:
    with pytest.raises(PrecedenceError, match="one JSON object"):
        parse_precedence_contract(["not", "a", "dict"])
    raw = _raw()
    del raw["tiers"][1]
    with pytest.raises(PrecedenceError, match="exactly 3 tiers"):
        parse_precedence_contract(raw)
    raw = deepcopy(_raw())
    raw["tiers"][0]["sources"] = []
    with pytest.raises(PrecedenceError, match="at least one source"):
        parse_precedence_contract(raw)
