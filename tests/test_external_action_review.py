"""Human review presentation tests (Phase 3C-3B-3B-3B-2).

Pure-function tests over synthetic dossiers matching the real
get_reconciliation_case() contract (native UUIDs and datetimes,
same keys). No database, no network, no filesystem: structural
tests pin the module's import and call surface to prove it.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.external_action_review import (
    ReviewInputError,
    build_reconciliation_review,
)

_T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def _action(state="UNKNOWN", **overrides):
    base = {
        "action_id": uuid4(),
        "idempotency_key": f"key-{uuid4().hex[:12]}",
        "action_type": "outreach_email",
        "agent": "michael",
        "property_id": None,
        "request_fingerprint": "sha256:test-fixture",
        "authorized_by": "operator-alice",
        "authorization_reference": "ticket-1",
        "authorized_at": _T0 - timedelta(hours=4),
        "state": state,
        "attempt_count": 1,
        "created_at": _T0 - timedelta(hours=4),
        "updated_at": _T0 - timedelta(hours=1),
    }
    base.update(overrides)
    return base


def _attempt(number=1, outcome="UNKNOWN", **overrides):
    base = {
        "attempt_id": uuid4(),
        "action_id": uuid4(),
        "attempt_number": number,
        "executor_id": "w-1",
        "claimed_at": _T0 - timedelta(hours=2),
        "outcome": outcome,
        "outcome_recorded_at": (
            _T0 - timedelta(hours=1) if outcome is not None else None
        ),
        "created_at": _T0 - timedelta(hours=2),
        "updated_at": _T0 - timedelta(hours=1),
    }
    base.update(overrides)
    return base


def _audit(event_id, occurred, event_type):
    return {
        "event_id": event_id,
        "occurred_at": occurred,
        "event_type": event_type,
        "severity": "INFO",
        "summary": f"{event_type} recorded",
    }


def _decision(tid, outcome="SUCCEEDED", tier="human_reviewed", **overrides):
    base = {
        "decision_id": uuid4(),
        "attempt_id": tid,
        "outcome": outcome,
        "prior_outcome": "UNKNOWN",
        "reconciled_by": "operator-alice",
        "reconciliation_reference": "recon-1",
        "approval_signature": "ab" * 64,
        "signing_public_key": "cd" * 32,
        "signing_key_fingerprint": "sha256:" + "ef" * 32,
        "approval_issued_at": _T0 - timedelta(hours=1),
        "approval_expires_at": _T0 + timedelta(hours=1),
        "evidence_kind": "provider_receipt",
        "evidence_source": "provider-support-desk",
        "evidence_observed_at": _T0 - timedelta(hours=2),
        "evidence_digest": "sha256:" + "12" * 32,
        "evidence_summary": "Provider confirmed completion.",
        "artifact_ref": "receipt-8842",
        "provider_reference": "op-71c0",
        "storage_ref": None,
        "verification_tier": tier,
        "decided_at": _T0 - timedelta(minutes=30),
    }
    base.update(overrides)
    return base


def _assemble(action, attempts, decision, audits, status,
              attempts_truncated=False, audits_truncated=False):
    return {
        "action": action,
        "attempts": attempts,
        "attempts_truncated": attempts_truncated,
        "reconciliation": decision,
        "audits": audits,
        "audits_truncated": audits_truncated,
        "interpretation": {"status": status, "detail": "dossier detail"},
        "generated_at": _T0,
    }


def _unknown_dossier():
    aid, tid = uuid4(), uuid4()
    action = _action("UNKNOWN", action_id=aid)
    attempt = _attempt(1, "UNKNOWN", attempt_id=tid, action_id=aid)
    audits = [
        _audit("evt-1", _T0 - timedelta(hours=3),
               "external_action.authorized"),
        _audit("evt-2", _T0 - timedelta(hours=1),
               "external_action.transitioned"),
    ]
    return _assemble(action, [attempt], None, audits, "unresolved_unknown")


def _reconciled_dossier():
    aid, tid = uuid4(), uuid4()
    action = _action("SUCCEEDED", action_id=aid)
    attempt = _attempt(1, "UNKNOWN", attempt_id=tid, action_id=aid)
    decision = _decision(tid)
    audits = [
        _audit("evt-1", _T0 - timedelta(hours=3),
               "external_action.authorized"),
        _audit("evt-9", _T0 - timedelta(minutes=30),
               "external_action.reconciled"),
    ]
    return _assemble(action, [attempt], decision, audits, "reconciled")


# --- Readiness statuses ---


def test_ready_for_unresolved_unknown() -> None:
    report = build_reconciliation_review(_unknown_dossier())
    assert report["readiness"]["status"] == "ready_for_human_review"
    assert "does not authorize reconciliation" in report["readiness"]["reason"]
    summary = report["case_summary"]
    assert summary["state"] == "UNKNOWN"
    assert summary["interpretation_status"] == "unresolved_unknown"
    assert "awaits human review" in summary["review_reason"]
    assert isinstance(summary["action_id"], str)  # UUID coerced, JSON-safe
    guidance = report["review_guidance"]
    assert any("UNKNOWN outcome" in m for m in guidance["missing_information"])
    assert any("provider operation behind attempt" in s
               for s in guidance["permissible_steps"])
    assert any("Do not retry" in p for p in guidance["prohibitions"])


def test_needs_evidence_while_attempting() -> None:
    aid = uuid4()
    action = _action("ATTEMPTING", action_id=aid)
    attempt = _attempt(1, None, action_id=aid)
    for status in ("attempting", "stale_attempting"):
        report = build_reconciliation_review(
            _assemble(action, [attempt], None, [], status)
        )
        assert report["readiness"]["status"] == "needs_evidence"
        assert "Recorded execution outcome." in (
            report["review_guidance"]["missing_information"]
        )


def test_needs_evidence_when_truncated() -> None:
    dossier = _unknown_dossier()
    dossier["audits_truncated"] = True
    report = build_reconciliation_review(dossier)
    assert report["readiness"]["status"] == "needs_evidence"
    assert "incomplete" in report["readiness"]["reason"]
    assert "Complete audit history (display truncated)." in (
        report["review_guidance"]["missing_information"]
    )


def test_review_completed_for_reconciled() -> None:
    report = build_reconciliation_review(_reconciled_dossier())
    assert report["readiness"]["status"] == "review_completed"
    evidence = report["evidence_assessment"]
    assert evidence["classification"] == "human_reviewed"
    assert evidence["verification_tier"] == "human_reviewed"
    assert any("not independent provider verification" in lim
               for lim in evidence["limitations"])


@pytest.mark.parametrize("state,status", [
    ("SUCCEEDED", "terminal"),
    ("FAILED", "terminal"),
    ("AUTHORIZED", "authorized"),
    ("PENDING", "pending"),
])
def test_not_applicable_states(state, status) -> None:
    aid = uuid4()
    action = _action(state, action_id=aid, attempt_count=0)
    dossier = _assemble(action, [], None, [], status)
    report = build_reconciliation_review(dossier)
    assert report["readiness"]["status"] == "not_applicable"
    assert report["evidence_assessment"]["classification"] == "missing"
    assert report["execution_history"]["note"] == "No execution attempts recorded."
    assert report["audit_timeline"]["note"] == (
        "No permanent lifecycle audits recorded for this action."
    )


# --- Evidence assessment ---


def test_evidence_missing_without_decision() -> None:
    report = build_reconciliation_review(_unknown_dossier())
    evidence = report["evidence_assessment"]
    assert evidence["classification"] == "missing"
    assert evidence["completeness"] == "missing"
    assert evidence["verification_tier"] is None
    assert all(v is None for v in evidence["references"].values())
    assert evidence["limitations"] == [
        "No reconciliation evidence is recorded for this action."
    ]


def test_evidence_complete_unverified_is_reported() -> None:
    # Regression: populated reference fields alone must never
    # confer credibility; without a review tier the claim is a
    # report no matter how complete its references.
    dossier = _reconciled_dossier()
    dossier["reconciliation"]["verification_tier"] = None
    report = build_reconciliation_review(dossier)
    evidence = report["evidence_assessment"]
    assert evidence["classification"] == "reported"
    assert evidence["classification"] != "documented"
    assert evidence["completeness"] == "complete"
    assert evidence["verification_tier"] is None
    assert evidence["references"]["artifact_ref"] == "receipt-8842"
    assert any("not independently inspected" in lim
               for lim in evidence["limitations"])


def test_evidence_reported_when_partial() -> None:
    dossier = _reconciled_dossier()
    decision = dossier["reconciliation"]
    decision["verification_tier"] = None
    decision["artifact_ref"] = None
    decision["evidence_digest"] = None
    report = build_reconciliation_review(dossier)
    evidence = report["evidence_assessment"]
    assert evidence["classification"] == "reported"
    assert evidence["completeness"] == "partial"
    assert any("artifact_ref" in lim and "digest" in lim
               for lim in evidence["limitations"])


def test_evidence_unrecognized_tier_flagged() -> None:
    dossier = _reconciled_dossier()
    dossier["reconciliation"]["verification_tier"] = "system_verified"
    report = build_reconciliation_review(dossier)
    evidence = report["evidence_assessment"]
    assert evidence["classification"] == "reported"  # unreviewed, however complete
    assert evidence["verification_tier"] == "system_verified"  # as recorded
    assert any("Unrecognized verification tier" in lim
               for lim in evidence["limitations"])


# --- Execution history and audit timeline ---


def test_attempt_categories_and_honest_language() -> None:
    aid = uuid4()
    action = _action("UNKNOWN", action_id=aid, attempt_count=3)
    attempts = [
        _attempt(1, "FAILED", action_id=aid),
        _attempt(2, None, action_id=aid),
        _attempt(3, "UNKNOWN", action_id=aid),
    ]
    dossier = _assemble(action, attempts, None, [], "unresolved_unknown")
    report = build_reconciliation_review(dossier)
    categories = [a["category"] for a in report["execution_history"]["attempts"]]
    assert categories == ["failed", "open", "unknown"]
    blob = json.dumps(report)
    assert "definitely executed" not in blob
    assert "Claimed is not confirmed executed" in blob
    assert "never rewritten by reconciliation" in blob


def test_attempting_never_described_as_executed() -> None:
    aid = uuid4()
    action = _action("ATTEMPTING", action_id=aid)
    attempt = _attempt(1, None, action_id=aid)
    dossier = _assemble(action, [attempt], None, [], "attempting")
    report = build_reconciliation_review(dossier)
    attempt = report["execution_history"]["attempts"][0]
    assert attempt["category"] == "open"
    assert attempt["outcome"] is None
    assert "not confirmed executed" in attempt["description"]
    assert "Await execution resolution" in (
        report["review_guidance"]["permissible_steps"][0]
    )


def test_reconciled_keeps_unknown_history() -> None:
    report = build_reconciliation_review(_reconciled_dossier())
    assert report["case_summary"]["state"] == "SUCCEEDED"
    assert report["execution_history"]["attempts"][0]["outcome"] == "UNKNOWN"
    assert report["evidence_assessment"]["outcome"] == "SUCCEEDED"
    assert "preserved history" in (
        report["execution_history"]["attempts"][0]["description"]
    )


def test_audit_timeline_sorted_and_truncation_flagged() -> None:
    dossier = _unknown_dossier()
    dossier["audits"] = list(reversed(dossier["audits"]))  # out of order in
    dossier["audits"].append(_audit("evt-?", None, "external_action.claimed"))
    dossier["audits_truncated"] = True
    report = build_reconciliation_review(dossier)
    timeline = report["audit_timeline"]
    assert [e["event_id"] for e in timeline["events"]] == [
        "evt-1", "evt-2", "evt-?",  # chronological, unknown time last
    ]
    assert timeline["truncated"] is True
    assert timeline["note"] == (
        "Audit history is truncated; the displayed timeline is incomplete."
    )


def test_truncated_attempts_flagged() -> None:
    dossier = _unknown_dossier()
    dossier["attempts_truncated"] = True
    report = build_reconciliation_review(dossier)
    history = report["execution_history"]
    assert history["attempts_truncated"] is True
    assert "truncated to the first 1" in history["note"]
    assert "Complete attempt history (display truncated)." in (
        report["review_guidance"]["missing_information"]
    )


# --- Malformed and contradictory inputs ---


@pytest.mark.parametrize("bad", [
    None,
    "not-a-dossier",
    42,
    [],
])
def test_rejects_non_mapping_dossier(bad) -> None:
    with pytest.raises(ReviewInputError, match="must be a mapping"):
        build_reconciliation_review(bad)


def test_rejects_missing_action_and_state() -> None:
    base = _unknown_dossier()
    broken = copy.deepcopy(base)
    del broken["action"]
    with pytest.raises(ReviewInputError, match="action.*object"):
        build_reconciliation_review(broken)
    broken = copy.deepcopy(base)
    broken["action"] = "not-an-object"
    with pytest.raises(ReviewInputError, match="action.*object"):
        build_reconciliation_review(broken)
    broken = copy.deepcopy(base)
    del broken["action"]["state"]
    with pytest.raises(ReviewInputError, match="action.state is required"):
        build_reconciliation_review(broken)


def test_rejects_unknown_status() -> None:
    dossier = _unknown_dossier()
    dossier["interpretation"]["status"] = "super_ready"
    with pytest.raises(ReviewInputError, match="unknown interpretation status"):
        build_reconciliation_review(dossier)
    dossier["interpretation"] = None
    with pytest.raises(ReviewInputError, match="interpretation.*object"):
        build_reconciliation_review(dossier)


@pytest.mark.parametrize("state,status", [
    ("UNKNOWN", "attempting"),
    ("ATTEMPTING", "unresolved_unknown"),
    ("SUCCEEDED", "pending"),
    ("PENDING", "terminal"),
    ("AUTHORIZED", "reconciled"),
])
def test_rejects_state_status_mismatch(state, status) -> None:
    dossier = _unknown_dossier()
    dossier["action"]["state"] = state
    dossier["interpretation"]["status"] = status
    with pytest.raises(ReviewInputError, match="contradicts"):
        build_reconciliation_review(dossier)


def test_rejects_decision_status_mismatch() -> None:
    reconciled = _reconciled_dossier()
    reconciled["interpretation"]["status"] = "terminal"
    reconciled["action"]["state"] = "SUCCEEDED"
    with pytest.raises(ReviewInputError, match="contradicts"):
        build_reconciliation_review(reconciled)
    unresolved = _unknown_dossier()
    unresolved["interpretation"]["status"] = "reconciled"
    unresolved["action"]["state"] = "SUCCEEDED"
    with pytest.raises(ReviewInputError, match="contradicts"):
        build_reconciliation_review(unresolved)
    broken = _unknown_dossier()
    broken["reconciliation"] = "not-an-object"
    with pytest.raises(ReviewInputError, match="object or null"):
        build_reconciliation_review(broken)


def test_rejects_bad_collections_and_flags() -> None:
    base = _unknown_dossier()
    for field in ("attempts", "audits"):
        broken = copy.deepcopy(base)
        broken[field] = {"not": "a-list"}
        with pytest.raises(ReviewInputError, match="must be a list"):
            build_reconciliation_review(broken)
    for field in ("attempts_truncated", "audits_truncated"):
        broken = copy.deepcopy(base)
        broken[field] = "yes"
        with pytest.raises(ReviewInputError, match="must be a boolean"):
            build_reconciliation_review(broken)
    broken = copy.deepcopy(base)
    broken["attempts"] = ["not-an-object"]
    with pytest.raises(ReviewInputError, match="must be an object"):
        build_reconciliation_review(broken)
    broken = copy.deepcopy(base)
    broken["audits"] = [None]
    with pytest.raises(ReviewInputError, match="must be an object"):
        build_reconciliation_review(broken)


def test_rejects_bad_scalar_types() -> None:
    base = _unknown_dossier()
    broken = copy.deepcopy(base)
    broken["attempts"][0]["outcome"] = "MAYBE"
    with pytest.raises(ReviewInputError, match="vocabulary"):
        build_reconciliation_review(broken)
    broken = copy.deepcopy(base)
    broken["attempts"][0]["attempt_number"] = True
    with pytest.raises(ReviewInputError, match="integer or null"):
        build_reconciliation_review(broken)
    broken = copy.deepcopy(base)
    broken["attempts"][0]["claimed_at"] = 12345
    with pytest.raises(ReviewInputError, match="timestamp or null"):
        build_reconciliation_review(broken)
    broken = copy.deepcopy(base)
    broken["audits"][0]["severity"] = ["INFO"]
    with pytest.raises(ReviewInputError, match="text or null"):
        build_reconciliation_review(broken)


# --- Dossier identity consistency ---


def test_rejects_attempt_action_mismatch() -> None:
    dossier = _unknown_dossier()
    dossier["attempts"][0]["action_id"] = uuid4()  # belongs elsewhere
    with pytest.raises(ReviewInputError, match="does not match the parent"):
        build_reconciliation_review(dossier)
    dossier = _unknown_dossier()
    dossier["attempts"][0]["action_id"] = None  # linkage unverifiable
    with pytest.raises(ReviewInputError, match="attempt.action_id is required"):
        build_reconciliation_review(dossier)
    dossier = _unknown_dossier()
    dossier["action"]["action_id"] = None  # nothing to match against
    with pytest.raises(ReviewInputError, match="action.action_id is required"):
        build_reconciliation_review(dossier)


def test_rejects_duplicate_attempt_ids() -> None:
    dossier = _unknown_dossier()
    twin = copy.deepcopy(dossier["attempts"][0])
    twin["attempt_number"] = 2
    dossier["attempts"].append(twin)
    dossier["action"]["attempt_count"] = 2
    with pytest.raises(ReviewInputError, match="duplicate attempt_id"):
        build_reconciliation_review(dossier)


@pytest.mark.parametrize("number", [0, -1])
def test_rejects_non_positive_attempt_numbers(number) -> None:
    dossier = _unknown_dossier()
    dossier["attempts"][0]["attempt_number"] = number
    with pytest.raises(ReviewInputError, match="must be positive"):
        build_reconciliation_review(dossier)


def test_rejects_missing_attempt_number() -> None:
    dossier = _unknown_dossier()
    dossier["attempts"][0]["attempt_number"] = None
    with pytest.raises(ReviewInputError, match="attempt_number is required"):
        build_reconciliation_review(dossier)


def test_rejects_unordered_attempt_numbers() -> None:
    aid = uuid4()
    action = _action("UNKNOWN", action_id=aid, attempt_count=2)
    attempts = [
        _attempt(2, "UNKNOWN", attempt_id=uuid4(), action_id=aid),
        _attempt(1, "FAILED", attempt_id=uuid4(), action_id=aid),
    ]
    dossier = _assemble(action, attempts, None, [], "unresolved_unknown")
    with pytest.raises(ReviewInputError, match="not ordered by attempt number"):
        build_reconciliation_review(dossier)
    attempts[0]["attempt_number"] = 1  # duplicate numbers, distinct ids
    with pytest.raises(ReviewInputError, match="not ordered by attempt number"):
        build_reconciliation_review(dossier)


def test_rejects_numbers_exceeding_attempt_count() -> None:
    dossier = _unknown_dossier()
    dossier["attempts"][0]["attempt_number"] = 2  # count is 1
    with pytest.raises(ReviewInputError, match="exceeds action.attempt_count"):
        build_reconciliation_review(dossier)
    dossier = _unknown_dossier()
    dossier["action"]["attempt_count"] = None
    with pytest.raises(ReviewInputError, match="non-negative integer"):
        build_reconciliation_review(dossier)
    dossier = _unknown_dossier()
    dossier["action"]["attempt_count"] = -1
    with pytest.raises(ReviewInputError, match="non-negative integer"):
        build_reconciliation_review(dossier)


def test_rejects_dangling_decision_reference() -> None:
    dossier = _reconciled_dossier()
    dossier["reconciliation"]["attempt_id"] = uuid4()  # resolves nowhere
    with pytest.raises(ReviewInputError, match="matches no historical attempt"):
        build_reconciliation_review(dossier)
    dossier = _reconciled_dossier()
    dossier["reconciliation"]["attempt_id"] = None
    with pytest.raises(ReviewInputError, match="decision.attempt_id is required"):
        build_reconciliation_review(dossier)


def test_rejects_decision_outcome_mismatch() -> None:
    dossier = _reconciled_dossier()
    dossier["reconciliation"]["outcome"] = "FAILED"  # action says SUCCEEDED
    with pytest.raises(ReviewInputError, match="does not match the terminal"):
        build_reconciliation_review(dossier)
    dossier = _reconciled_dossier()
    dossier["reconciliation"]["outcome"] = None
    with pytest.raises(ReviewInputError, match="decision.outcome is required"):
        build_reconciliation_review(dossier)


@pytest.mark.parametrize("rewritten", ["SUCCEEDED", "FAILED", None])
def test_rejects_non_unknown_reconciled_attempt(rewritten) -> None:
    dossier = _reconciled_dossier()
    dossier["attempts"][0]["outcome"] = rewritten
    with pytest.raises(ReviewInputError, match="preserve its UNKNOWN history"):
        build_reconciliation_review(dossier)


def test_truncated_page_skips_unverifiable_linkage() -> None:
    # The resolved (latest) attempt legitimately sits beyond a
    # truncated page: unverifiable linkage must not read as
    # contradictory, and the case still reports its truncation.
    dossier = _reconciled_dossier()
    other = uuid4()
    dossier["attempts"][0]["attempt_id"] = other
    dossier["attempts"][0]["attempt_number"] = 1
    dossier["action"]["attempt_count"] = 51
    dossier["attempts_truncated"] = True
    report = build_reconciliation_review(dossier)
    assert report["readiness"]["status"] == "review_completed"
    assert report["execution_history"]["attempts_truncated"] is True
    assert "Complete attempt history (display truncated)." in (
        report["review_guidance"]["missing_information"]
    )


def test_truncated_linkage_disclosed_not_invalidated() -> None:
    dossier = _reconciled_dossier()
    dossier["attempts"][0]["attempt_id"] = uuid4()  # resolved attempt off-page
    dossier["action"]["attempt_count"] = 51
    dossier["attempts_truncated"] = True
    report = build_reconciliation_review(dossier)
    assert report["readiness"]["status"] == "review_completed"
    assert report["evidence_assessment"]["outcome"] == "SUCCEEDED"
    assert (
        "The reconciliation decision is recorded, but its relationship"
        " to the resolved attempt cannot be confirmed from the truncated"
        " history displayed in this report."
    ) in report["evidence_assessment"]["limitations"]
    blob = json.dumps(report).lower()
    assert "never existed" not in blob
    assert "invalid" not in blob
    # The same dangling reference on a COMPLETE page still fails closed.
    dossier["attempts_truncated"] = False
    with pytest.raises(ReviewInputError, match="matches no historical attempt"):
        build_reconciliation_review(dossier)


# --- Sensitive-field exclusion, immutability, determinism ---


def test_sensitive_and_unexpected_fields_excluded() -> None:
    dossier = _reconciled_dossier()
    dossier["action"]["executor_proof"] = "supersecret-proof"
    dossier["action"]["password"] = "hunter2"
    dossier["attempts"][0]["auth_token"] = "token-abc"
    dossier["reconciliation"]["totally_bogus_secret"] = "shh"
    dossier["audits"][0]["details"] = {"conn": "postgres://user:pass@host/db"}
    dossier["top_level_secret"] = "smuggled"
    report = build_reconciliation_review(dossier)
    blob = json.dumps(report)
    for leaked in ("supersecret-proof", "hunter2", "token-abc", "shh",
                   "postgres://", "smuggled", "ab" * 64, "cd" * 32,
                   "approval_signature", "signing_public_key",
                   "signing_key_fingerprint"):
        assert leaked not in blob
    assert report["evidence_assessment"]["references"]["artifact_ref"] == (
        "receipt-8842"
    )


def test_input_immutable_and_output_fresh() -> None:
    dossier = _reconciled_dossier()
    before = copy.deepcopy(dossier)
    report = build_reconciliation_review(dossier)
    assert dossier == before  # build changed nothing
    report["case_summary"]["state"] = "MUTATED"
    report["execution_history"]["attempts"][0]["outcome"] = "MUTATED"
    report["evidence_assessment"]["references"]["digest"] = "MUTATED"
    report["audit_timeline"]["events"].clear()
    assert dossier == before  # report shares no mutable state with input


def test_deterministic_output_and_json_compatible() -> None:
    first = build_reconciliation_review(_unknown_dossier())
    second = build_reconciliation_review(_unknown_dossier())
    assert first["readiness"] == second["readiness"]
    assert first["evidence_assessment"] == second["evidence_assessment"]
    blob = json.dumps(first)  # must not raise: UUIDs/datetimes coerced
    assert json.loads(blob)["case_summary"]["state"] == "UNKNOWN"


# --- Structural purity guards ---


def test_review_module_is_pure() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_action_review.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module in {
                "__future__", "collections.abc", "datetime", "typing", "uuid",
            }, node.module
            assert not (node.module or "").startswith("app"), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app"), alias.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                "execute", "commit", "rollback", "connect", "open",
                "read", "write", "urlopen", "request", "send",
            }, node.func.attr
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            first = node.value.lstrip().upper().split(None, 1)
            assert not first or first[0].rstrip(";") not in {
                "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
                "DROP", "TRUNCATE", "GRANT", "REVOKE",
            }, first
