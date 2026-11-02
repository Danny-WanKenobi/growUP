"""Uriel opportunity-ranking tests: deterministic, preparation-only."""
from __future__ import annotations

import pytest

from app.uriel import UrielError, draft_outreach, rank_opportunities

READY = {
    "property_id": "p-ready",
    "account": "00000100324000000",
    "address": "801 JACKSON ST",
    "category_verified": True,
    "raphael_outcome": "authorized",
    "engineering_complete": True,
    "finance_complete": True,
    "finance_verdict": "unfavorable_under_assumptions",
    "evidence_gaps": ["zoning permission unresolved"],
    "owner_contact": "owner@example.com",
}
PENDING = {
    "property_id": "p-pending",
    "account": "00000700090000000",
    "address": "2323 CHALK HILL RD",
    "category_verified": False,
    "raphael_outcome": "needs_evidence",
    "engineering_complete": False,
    "finance_complete": False,
    "finance_verdict": None,
    "evidence_gaps": ["mixed recorded use", "county admission"],
    "owner_contact": None,
}


def test_ranking_orders_ready_first_with_breakdown():
    result = rank_opportunities([PENDING, READY])
    ranked = result["ranked"]
    assert [entry["property_id"] for entry in ranked] == ["p-ready", "p-pending"]
    ready = ranked[0]
    assert ready["readiness"] == "outreach_ready"
    assert ready["score"] == sum(ready["score_breakdown"].values())
    assert ready["score_breakdown"]["category_verified"] == 30
    assert ranked[1]["readiness"] == "not_ready"
    assert "category not verified" in ranked[1]["missing_data"]


def test_ranking_is_deterministic_and_dedupes():
    first = rank_opportunities([READY, PENDING, dict(READY)])
    second = rank_opportunities([PENDING, dict(READY), READY])
    assert [e["key"] for e in first["ranked"]] == \
        [e["key"] for e in second["ranked"]]
    assert len(first["duplicates"]) == 1
    assert first["duplicates"][0]["key"] == "account:00000100324000000"


def test_verified_facts_outrank_favorable_projection():
    projected = dict(PENDING, property_id="p-proj", account="2",
                     finance_complete=True,
                     finance_verdict="favorable_under_assumptions")
    verified = dict(PENDING, property_id="p-ver", account="3",
                    category_verified=True, raphael_outcome="authorized")
    ranked = rank_opportunities([projected, verified])["ranked"]
    assert ranked[0]["property_id"] == "p-ver"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_draft_outreach_claims_only_verified_facts():
    entry = rank_opportunities([READY])["ranked"][0]
    draft = draft_outreach(entry)
    assert draft["status"] == "draft_not_sent"
    body = draft["body"].lower()
    assert "801 jackson st" in body
    assert "vacant" not in body
    assert "available for lease" not in body
    assert "suitable" not in body


def test_draft_outreach_refuses_unready_entries():
    entry = rank_opportunities([PENDING])["ranked"][0]
    with pytest.raises(UrielError):
        draft_outreach(entry)
