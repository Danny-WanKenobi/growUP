"""Contract-only checks: no sample dossiers, custody transfers or business data."""
import pytest

from app.agents.registry import AGENT_SPECS, NEW_AGENT_ROLES, describe_agents
from app.agents.stages import build_report
from app.custody import check_handoff_routing, interpret_custody_agent, CustodyError
from app.model_adapter import validate_intent
from app.observability.events import AgentId
from app.workflow_db import WorkflowError
from app.wondrue import handle_command, WondrueError


def test_roster_is_registered_without_workers_or_execution_grants():
    roster = describe_agents()
    assert len(roster["agents"]) == 10
    assert not roster["workers_started"] and not roster["external_execution_enabled"]
    assert AGENT_SPECS["uriel"].existing_runtime == "app.uriel"
    assert AGENT_SPECS["priscilla"].existing_runtime == "app.priscilla"
    for name in NEW_AGENT_ROLES:
        assert AgentId(name).value == name
        assert interpret_custody_agent("canonical-1", name)["agent"] == name
        # Pure route validation only; no receipt or synthetic dossier created.
        check_handoff_routing("review", name, "human_review")
        check_handoff_routing("return", name, "gabriel")
    assert interpret_custody_agent("mvp-1", "michael")["meaning"].startswith("Outreach")
    with pytest.raises(CustodyError):
        check_handoff_routing("forward", "manny", "gabriel")


@pytest.mark.parametrize("agent", tuple(AGENT_SPECS))
def test_agent_requires_a_real_stored_input_before_preparing(agent):
    with pytest.raises(WorkflowError, match="stored property-bound dossier"):
        build_report(agent, {})


def test_roster_is_read_only_through_wondrue():
    response = handle_command(mode="read-only", max_mode="read-only", target="real", text="",
                             intent_override={"intent":"inspect_agents", "args":{}})
    assert len(response["data"]["agents"]) == 10
    assert response["data"]["workers_started"] is False


@pytest.mark.parametrize("intent", ["preview_agent_report", "inspect_owner_queue", "inspect_lead_metrics"])
def test_data_commands_do_not_fabricate_results_without_database(intent):
    assert validate_intent({"intent":intent, "args":{}})["intent"] == intent
    with pytest.raises(WondrueError, match="database connection"):
        handle_command(mode="read-only", max_mode="read-only", target="real", text="",
                       intent_override={"intent":intent, "args":{}})


def test_manny_is_active_and_dan_remains_historical_only():
    assert AGENT_SPECS["manny"].name == "Manny"
    assert "dan" not in AGENT_SPECS and "dan" not in NEW_AGENT_ROLES
    assert AgentId("dan").value == "dan"  # Read existing audit history.
    assert "historical name" in interpret_custody_agent("canonical-1", "dan")["meaning"]
    check_handoff_routing("forward", "deborah", "manny")
    check_handoff_routing("return", "manny", "deborah")
    with pytest.raises(CustodyError, match="unknown handoff agent"):
        check_handoff_routing("forward", "deborah", "dan")
