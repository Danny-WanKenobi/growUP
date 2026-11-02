"""Pipeline cockpit view tests: presentation only, truthful labels."""
from __future__ import annotations

from app.model_adapter import validate_intent
from app.pipeline_view import STAGE_ORDER, build_pipeline_summary
from app.wondrue import READ_ONLY_INTENTS

FULL = {
    "gabriel": {
        "key": "addr:801 jackson st|dallas|tx|75202",
        "category_attribution": {"status": "observed"},
        "candidate_verification": {"outcome": "verified",
                                   "pathway": "authority"},
    },
    "raphael": {"outcome": "authorized", "basis": ["category_verified"]},
    "onesimus": {"complete": True, "missing": [], "not_quantified": [],
                 "outputs": {"kg_per_year": 232650.0}},
    "michael": {"assessment": {
        "complete": True, "verdict": "unfavorable_under_assumptions",
        "outputs": {"operating_net_usd_per_year": "-1504339.72"},
        "evidence_requests": ["confirm price_per_kg (assumed)"],
    }},
    "uriel": {"readiness": "outreach_ready", "score": 88},
    "priscilla": {"metrics": {"simulated": {"total": 14},
                              "actual": {"total": 0}}},
}


def test_full_pipeline_summary_is_ordered_and_labeled():
    summary = build_pipeline_summary(FULL)
    assert [stage["name"] for stage in summary["stages"]] == list(STAGE_ORDER)
    by_name = {stage["name"]: stage for stage in summary["stages"]}
    assert by_name["gabriel"]["verification_outcome"] == "verified"
    assert by_name["raphael"]["outcome"] == "authorized"
    assert "verified opportunity" in by_name["michael"]["label"]
    assert by_name["priscilla"]["simulated_events"] == 14
    assert by_name["priscilla"]["actual_events"] == 0
    assert not any("total_events" in stage for stage in summary["stages"])
    assert any("price_per_kg" in gap for gap in summary["evidence_gaps"])


def test_absent_stages_are_named_not_hidden():
    summary = build_pipeline_summary({})
    assert all(stage["status"] == "not_recorded"
               for stage in summary["stages"])
    assert len(summary["evidence_gaps"]) == len(STAGE_ORDER)


def test_unverified_category_surfaces_as_gap():
    summary = build_pipeline_summary({
        "gabriel": {"candidate_verification": {"outcome": "not_verified"}},
    })
    assert any("not_verified" in gap for gap in summary["evidence_gaps"])


def test_inspect_pipeline_is_a_validated_read_only_intent():
    assert "inspect_pipeline" in READ_ONLY_INTENTS
    cleaned = validate_intent(
        {"intent": "inspect_pipeline", "args": {"run_id": "r-1"}})
    assert cleaned["intent"] == "inspect_pipeline"
