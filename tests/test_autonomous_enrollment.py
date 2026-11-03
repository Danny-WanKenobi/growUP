"""Autonomous publisher enrollment: deterministic, evidence-only, fail-closed."""
from __future__ import annotations

import json
import time

import pytest

from app.agents.gabriel.sources.autonomous_enrollment import (
    EnrollmentError,
    PublisherEvidence,
    decide_enrollment,
    effective_registry,
    run_autonomous_enrollment,
    verify_socrata_resource,
)
from app.agents.gabriel.source_registry import PRODUCTION_REGISTRY, SourceClass


def _resource(**over):
    base = {
        "platform": "socrata",
        "publisher_domain": "cos-data.seattle.gov",
        "publisher_name": "City of Seattle",
        "dataset_id": "teqw-tu6e",
        "resource_url": "https://cos-data.seattle.gov/resource/teqw-tu6e.json",
    }
    base.update(over)
    return base


def _view(**over):
    base = {
        "id": "teqw-tu6e",
        "name": "Building Energy Benchmarking Data",
        "attribution": "City of Seattle",
        "license": {"name": "Public Domain"},
        "rowsUpdatedAt": int(time.time()) - 86400,
        "columns": [
            {"fieldName": "taxparcelidentificationnumber"},
            {"fieldName": "address"},
            {"fieldName": "latitude"},
            {"fieldName": "longitude"},
            {"fieldName": "largestpropertyusetype"},
        ],
    }
    base.update(over)
    return base


def _fetch(view=None, rows=None, calls=None):
    def fetch(url, params=None):
        if calls is not None:
            calls.append(url)
        if "/api/views/" in url:
            return json.dumps(view if view is not None else _view()).encode()
        return json.dumps(rows if rows is not None else [{"city": "Seattle"}]).encode()
    return fetch


class _FakeStore:
    def __init__(self):
        self.state = "winner"
        self.completed_with = None

    def claim(self, domain, dataset_id, fingerprint):
        if self.state == "completed":
            return {"status": "completed", "run_id": "r-1",
                    "decision": {"status": "enrolled", "domain": domain}}
        return {"status": self.state, "run_id": "r-1"}

    def complete(self, domain, dataset_id, *, decision, evidence_payload):
        self.completed_with = (decision, evidence_payload)


def test_enrolled_when_all_criteria_observed():
    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=_FakeStore(), fetch=_fetch())
    assert decision["status"] == "enrolled"
    assert decision["missing"] == []
    assert decision["review_due"] > decision["decided_at"][:10]


def test_unknown_license_needs_more_evidence():
    view = _view(license={"name": "Some Custom Terms"})
    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=_FakeStore(), fetch=_fetch(view=view))
    assert decision["status"] == "needs_more_evidence"
    assert "license_known_clear" in decision["missing"]


def test_non_government_domain_needs_more_evidence():
    decision = run_autonomous_enrollment(
        _resource(publisher_domain="www.tad.org",
                  resource_url="https://www.tad.org/resource/teqw-tu6e.json"),
        jurisdiction="Tarrant County", store=_FakeStore(), fetch=_fetch())
    assert decision["status"] == "needs_more_evidence"
    assert "government_domain" in decision["missing"]


def test_stale_freshness_needs_more_evidence():
    view = _view(rowsUpdatedAt=int(time.time()) - 900 * 86400)
    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=_FakeStore(), fetch=_fetch(view=view))
    assert decision["status"] == "needs_more_evidence"
    assert "freshness_known_and_recent" in decision["missing"]


def test_geography_mismatch_needs_more_evidence():
    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=_FakeStore(),
        fetch=_fetch(rows=[{"city": "Chicago"}]))
    assert decision["status"] == "needs_more_evidence"
    assert "sample_geography_consistent" in decision["missing"]


def test_host_mismatch_refused_before_network():
    calls: list = []
    with pytest.raises(EnrollmentError):
        verify_socrata_resource(
            _resource(resource_url="https://evil.example.org/resource/teqw-tu6e.json"),
            jurisdiction="Seattle", fetch=_fetch(calls=calls))
    assert calls == []


def test_replay_returns_retained_decision_without_network():
    store = _FakeStore()
    store.state = "completed"
    calls: list = []
    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=store, fetch=_fetch(calls=calls))
    assert decision["status"] == "enrolled"
    assert calls == []


def test_uncertain_claim_refuses_retransmission():
    store = _FakeStore()
    store.state = "uncertain"
    with pytest.raises(EnrollmentError):
        run_autonomous_enrollment(
            _resource(), jurisdiction="Seattle", store=store, fetch=_fetch())


class _FakeSelectionStore:
    def __init__(self):
        self.completed = None

    def claim(self, assignment_key, fingerprint, mission_data):
        return {"status": "winner", "run_id": "run-verified-1"}

    def complete(self, assignment_key, *, decision, evidence):
        self.completed = (decision, evidence)


def test_verify_and_select_enrolls_and_selects_collin_like():
    from app.agents.gabriel.sources.autonomous_enrollment import verify_and_select
    view = _view(id="abcd-1234", name="Collin CAD Appraisal Data - 2025",
                 attribution="Collin County Appraisal District")
    entry = {
        "name": "Collin CAD Appraisal Data - 2025",
        "publisher_domain": "data.texas.gov",
        "adapter": "generic_socrata",
        "resource_url": "https://data.texas.gov/resource/abcd-1234.json",
    }

    def fetch(url, params=None):
        if "/api/views/" in url:
            return json.dumps(view).encode()
        return json.dumps([{"county": "Collin"}]).encode()

    store = _FakeSelectionStore()
    out = verify_and_select(entry, jurisdiction="Collin County",
                            selection_store=store,
                            assignment_key="test:verified:1", fetch=fetch)
    assert out["enrollment"]["status"] == "enrolled"
    assert out["selection"] is not None
    assert out["selection"]["status"] == "selected"
    assert out["selection"]["selected_adapter"] == "generic_socrata"
    assert store.completed is not None


def test_verify_and_select_needs_evidence_selects_nothing():
    from app.agents.gabriel.sources.autonomous_enrollment import verify_and_select
    view = _view(id="abcd-1234", name="Collin CAD Appraisal Data - 2025",
                 attribution="Collin County Appraisal District",
                 license={"name": "Custom Terms"})
    entry = {
        "name": "Collin CAD Appraisal Data - 2025",
        "publisher_domain": "data.texas.gov",
        "adapter": "generic_socrata",
        "resource_url": "https://data.texas.gov/resource/abcd-1234.json",
    }

    def fetch(url, params=None):
        if "/api/views/" in url:
            return json.dumps(view).encode()
        return json.dumps([{"county": "Collin"}]).encode()

    out = verify_and_select(entry, jurisdiction="Collin County",
                            selection_store=_FakeSelectionStore(),
                            assignment_key="test:verified:2", fetch=fetch)
    assert out["enrollment"]["status"] == "needs_more_evidence"
    assert out["selection"] is None


def test_non_socrata_platform_no_network():
    decision = run_autonomous_enrollment(
        _resource(platform="ckan"), jurisdiction="Seattle", store=_FakeStore(),
        fetch=_fetch(calls=[]))
    assert decision["status"] == "needs_more_evidence"


def _evidence(**over) -> PublisherEvidence:
    base = dict(
        domain="cos-data.seattle.gov", dataset_id="teqw-tu6e",
        resource_url="https://cos-data.seattle.gov/resource/teqw-tu6e.json",
        publisher_name="City of Seattle", attribution="City of Seattle",
        dataset_name="Buildings", license_id="Public Domain", license_clear=True,
        freshness_days=3, fields=("address", "latitude"),
        sample_rows=2, sample_geo_values=("Seattle",),
        evidence_digest="d", retrieved_at="2026-11-03T00:00:00+00:00")
    base.update(over)
    return PublisherEvidence(**base)


def test_decision_digest_verifies():
    from app.agents.gabriel.sources.autonomous_enrollment import _digest
    decision = decide_enrollment(_evidence(), jurisdiction="Seattle")
    assert decision["digest"] == _digest(decision)


class _ListStore:
    def __init__(self, decisions):
        self._decisions = decisions

    def load_enrollments(self):
        return self._decisions


def _enrolled_decision(domain="cos-data.seattle.gov", review_due="2099-01-01"):
    decision = decide_enrollment(_evidence(domain=domain), jurisdiction="Seattle")
    decision["review_due"] = review_due
    from app.agents.gabriel.sources.autonomous_enrollment import _digest
    decision["digest"] = _digest(decision)
    return decision


def test_effective_registry_merges_only_live_enrollments():
    from app.agents.gabriel.sources import autonomous_enrollment as mod
    mod.clear_registry_cache()
    registry = effective_registry(store=_ListStore([_enrolled_decision()]))
    assert registry.source_class_of("https://cos-data.seattle.gov/x") is SourceClass.AUTHORITATIVE
    # Curated registry itself is never mutated.
    assert set(PRODUCTION_REGISTRY.entries) == {"www.dallascad.org"}
    mod.clear_registry_cache()
    expired = effective_registry(store=_ListStore([_enrolled_decision(review_due="2000-01-01")]))
    assert expired.source_class_of("https://cos-data.seattle.gov/x") is SourceClass.UNVETTED
    mod.clear_registry_cache()
