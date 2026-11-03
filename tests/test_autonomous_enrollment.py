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
    assert "publisher_authority_basis" in decision["missing"]


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


def test_portal_terms_data_json_license_clears():
    view = _view(license=None)

    def fetch(url, params=None):
        if "/api/views/" in url:
            return json.dumps(view).encode()
        if url.endswith("/data.json"):
            return json.dumps({"dataset": [{
                "title": "Building Energy Benchmarking Data",
                "accessURL": "https://cos-data.seattle.gov/resource/teqw-tu6e.json",
                "license": "https://creativecommons.org/publicdomain/zero/1.0/",
                "identifier": "teqw-tu6e"}]}).encode()
        return json.dumps([{"city": "Seattle"}]).encode()

    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=_FakeStore(), fetch=fetch)
    assert decision["status"] == "enrolled"
    assert decision["license_basis"] == "data_json"


def test_portal_terms_page_affirmative_grant_clears():
    view = _view(license=None)

    def fetch(url, params=None):
        if "/api/views/" in url:
            return json.dumps(view).encode()
        if url.endswith("/data.json"):
            return json.dumps({"dataset": []}).encode()
        if url.rstrip("/").endswith("seattle.gov"):
            return b'<a href="/about/terms">Terms of Use</a>'
        if "/about/terms" in url:
            return (b"<html><body>All datasets on this portal are in the "
                    b"public domain and free to use for any purpose.</body></html>")
        return json.dumps([{"city": "Seattle"}]).encode()

    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=_FakeStore(), fetch=fetch)
    assert decision["status"] == "enrolled"
    assert decision["license_basis"] == "portal_terms"


def test_portal_terms_prohibition_blocks():
    view = _view(license=None)

    def fetch(url, params=None):
        if "/api/views/" in url:
            return json.dumps(view).encode()
        if url.endswith("/data.json"):
            return json.dumps({"dataset": []}).encode()
        if url.rstrip("/").endswith("seattle.gov"):
            return b'<a href="/about/terms">Terms of Use</a>'
        if "/about/terms" in url:
            return (b"<html><body>Data is public domain but you may not "
                    b"reproduce it without written permission.</body></html>")
        return json.dumps([{"city": "Seattle"}]).encode()

    decision = run_autonomous_enrollment(
        _resource(), jurisdiction="Seattle", store=_FakeStore(), fetch=fetch)
    assert decision["status"] == "needs_more_evidence"
    assert "license_known_clear" in decision["missing"]


def test_directory_corroboration_enrolls_org_domain():
    from app.agents.gabriel.sources.autonomous_enrollment import (
        verify_directory_corroboration,
    )
    page = (b'<a href="https://www.tad.org/">Tarrant Appraisal District</a>'
            b'<a href="https://www.collincad.org/">Collin Appraisal</a>')

    def fetch(url, params=None):
        return page

    ev = verify_directory_corroboration(
        "www.tad.org", publisher_name="Tarrant Appraisal District",
        jurisdiction="Tarrant County",
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",),
        fetch=fetch)
    assert ev["matched"] is True
    assert ev["directory_host"] == "comptroller.texas.gov"


def test_directory_two_level_county_page_corroborates():
    from app.agents.gabriel.sources.autonomous_enrollment import (
        verify_directory_corroboration,
    )
    index = b'<a href="tarrant.php">220 Tarrant</a><a href="dallas.php">63 Dallas</a>'
    county = b'<p>Appraisal District Website: <a href="http://www.tad.org/">www.tad.org</a></p>'

    def fetch(url, params=None):
        return county if url.endswith("tarrant.php") else index

    ev = verify_directory_corroboration(
        "www.tad.org", publisher_name="Tarrant Appraisal District",
        jurisdiction="Tarrant County",
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",),
        fetch=fetch)
    assert ev["matched"] is True
    assert ev["directory_url"].endswith("tarrant.php")


def test_directory_must_be_government_hosted():
    from app.agents.gabriel.sources.autonomous_enrollment import (
        verify_directory_corroboration,
    )
    ev = verify_directory_corroboration(
        "www.tad.org", publisher_name="Tarrant Appraisal District",
        jurisdiction="Tarrant County",
        directory_urls=("https://example.org/directory",),
        fetch=lambda url, params=None: b'<a href="https://www.tad.org/">Tarrant</a>')
    assert ev["matched"] is False


def test_org_domain_decision_with_directory_evidence():
    decision = decide_enrollment(
        _evidence(domain="www.tad.org", publisher_name="Tarrant Appraisal District",
                  attribution="Tarrant Appraisal District",
                  dataset_name="Tarrant Parcels", sample_geo_values=("Tarrant",)),
        jurisdiction="Tarrant County",
        evidence_directory={"matched": True, "matched_domain": "www.tad.org",
                            "directory_host": "comptroller.texas.gov",
                            "directory_url": "https://comptroller.texas.gov/x",
                            "matched_text": "Tarrant Appraisal District",
                            "digest": "d"})
    assert decision["status"] == "enrolled"
    assert decision["criteria"]["directory_corroborated_official_domain"] is True


def test_org_domain_decision_without_directory_stays_blocked():
    decision = decide_enrollment(
        _evidence(domain="www.tad.org", sample_geo_values=("Tarrant",)),
        jurisdiction="Tarrant County")
    assert decision["status"] == "needs_more_evidence"
    assert "publisher_authority_basis" in decision["missing"]


def _arcgis_fetch(layer=None, item=None, features=None, directory=None):
    import time as _time
    layer = layer if layer is not None else {
        "name": "TCProperty", "copyrightText": "",
        "editingInfo": {"lastEditDate": int(_time.time() * 1000) - 86400000},
        "fields": [{"name": "ACCOUNT"}, {"name": "SITUS_ADDR"},
                   {"name": "CITY"}, {"name": "LAND_USE"}],
    }
    item = item if item is not None else {
        "id": "a" * 32, "owner": "tarrantcounty", "title": "Tarrant County Parcel Property",
        "licenseInfo": "Public Domain", "access": "public",
    }
    features = features if features is not None else [
        {"properties": {"ACCOUNT": "1", "county": "Tarrant"},
         "geometry": {"type": "Point", "coordinates": [-97.3, 32.7]}}]

    def fetch(url, params=None):
        if url.endswith("tarrant.php"):
            return b'<a href="https://www.tarrantcounty.com/">Tarrant County</a>'
        if "comptroller" in url:
            return directory or b'<a href="tarrant.php">220 Tarrant</a>'
        if "/query" in url:
            return json.dumps({"type": "FeatureCollection", "features": features}).encode()
        if "sharing/rest/content/items" in url:
            return json.dumps(item).encode()
        return json.dumps(layer).encode()
    return fetch


def _arcgis_resource():
    return {
        "platform": "arcgis",
        "publisher_domain": "mapit.tarrantcounty.com",
        "publisher_name": "tarrantcounty",
        "dataset_id": "a" * 32,
        "resource_url": "https://mapit.tarrantcounty.com/arcgis/rest/services/Tax/TCProperty/MapServer/0",
    }


def test_arcgis_enrolled_with_directory_corroboration():
    decision = run_autonomous_enrollment(
        _arcgis_resource(), jurisdiction="Tarrant County", store=_FakeStore(),
        fetch=_arcgis_fetch(),
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",))
    assert decision["status"] == "enrolled"
    assert decision["criteria"]["directory_corroborated_official_domain"] is True


def test_arcgis_unknown_license_needs_more_evidence():
    item = {"id": "a" * 32, "owner": "tarrantcounty",
            "title": "Tarrant County Parcel Property", "licenseInfo": ""}
    decision = run_autonomous_enrollment(
        _arcgis_resource(), jurisdiction="Tarrant County", store=_FakeStore(),
        fetch=_arcgis_fetch(item=item),
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",))
    assert decision["status"] == "needs_more_evidence"
    assert "license_known_clear" in decision["missing"]


def test_arcgis_stale_layer_needs_more_evidence():
    import time as _time
    layer = {"name": "TCProperty",
             "editingInfo": {"lastEditDate": int(_time.time() * 1000) - 900 * 86400000},
             "fields": [{"name": "ACCOUNT"}, {"name": "CITY"}]}
    decision = run_autonomous_enrollment(
        _arcgis_resource(), jurisdiction="Tarrant County", store=_FakeStore(),
        fetch=_arcgis_fetch(layer=layer),
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",))
    assert decision["status"] == "needs_more_evidence"
    assert "freshness_known_and_recent" in decision["missing"]


def test_verify_and_select_arcgis_enrolls_and_selects():
    from app.agents.gabriel.sources.autonomous_enrollment import verify_and_select
    entry = {
        "name": "Tarrant County Parcel Property",
        "publisher_domain": "mapit.tarrantcounty.com",
        "platform": "arcgis", "adapter": "generic_arcgis",
        "dataset_id": "a" * 32,
        "resource_url": "https://mapit.tarrantcounty.com/arcgis/rest/services/Tax/TCProperty/MapServer/0",
    }
    store = _FakeSelectionStore()
    out = verify_and_select(
        entry, jurisdiction="Tarrant County", selection_store=store,
        assignment_key="test:verified:arcgis:1", fetch=_arcgis_fetch(),
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",))
    assert out["enrollment"]["status"] == "enrolled"
    assert out["selection"]["status"] == "selected"
    assert out["selection"]["selected_adapter"] == "generic_arcgis"


def test_item_scoped_authority_enrolls_shared_host_item():
    decision = decide_enrollment(
        _evidence(domain="services2.arcgis.com", publisher_name="CCAD_Maps",
                  attribution="CCAD Parcel Feature Set",
                  dataset_name="CCAD Parcel Feature Set",
                  item_bound=True, sample_geo_values=()),
        jurisdiction="Collin County",
        evidence_directory={"identity_matched": True,
                            "identity_name": "Collin Central Appraisal District",
                            "identity_acronym": "CCAD",
                            "identity_domain": "www.collincad.org",
                            "identity_digest": "d"})
    assert decision["status"] == "enrolled"
    assert decision["authority_scope"] == "item"
    assert decision["criteria"]["item_scoped_authority"] is True


def test_item_scoped_authority_requires_identity_binding():
    decision = decide_enrollment(
        _evidence(domain="services2.arcgis.com", item_bound=True,
                  sample_geo_values=()),
        jurisdiction="Collin County")
    assert decision["status"] == "needs_more_evidence"
    assert "publisher_authority_basis" in decision["missing"]


def test_directory_identity_binds_acronym_via_district_site():
    from app.agents.gabriel.sources.autonomous_enrollment import (
        verify_directory_identity,
    )
    index = b'<a href="collin.php">043 Collin</a>'
    county = (b"Appraisal District Last Updated: 07/15/2026 Chief Appraiser: X "
              b"Website: www.collincad.org Phone: 469-742-9200")
    site = (b"<title>Collin Central Appraisal District</title>"
            b"<p>Welcome to CCAD. The Collin Central Appraisal District "
            b"appraises property in Collin County.</p>")

    def fetch(url, params=None):
        if url.endswith("collin.php"):
            return county
        if "collincad.org" in url:
            return site
        return index

    ev = verify_directory_identity(
        {"CCAD"}, jurisdiction="Collin County",
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",),
        fetch=fetch)
    assert ev["identity_matched"] is True
    assert ev["identity_name"] == "Collin Central Appraisal District"
    assert ev["identity_acronym"] == "CCAD"


def test_directory_identity_rejects_unstated_acronym():
    from app.agents.gabriel.sources.autonomous_enrollment import (
        verify_directory_identity,
    )

    def fetch(url, params=None):
        if url.endswith("collin.php"):
            return b"Appraisal District Website: www.collincad.org"
        if "collincad.org" in url:
            return b"<p>Collin Central Appraisal District home page.</p>"
        return b'<a href="collin.php">043 Collin</a>'

    ev = verify_directory_identity(
        {"CCAD"}, jurisdiction="Collin County",
        directory_urls=("https://comptroller.texas.gov/taxes/property-tax/county-directory/",),
        fetch=fetch)
    # Initials match but the acronym itself is never stated on the
    # district's own site: not bound.
    assert ev["identity_matched"] is False


def test_effective_registry_never_enrolls_shared_esri_host():
    from app.agents.gabriel.sources import autonomous_enrollment as mod
    decision = decide_enrollment(
        _evidence(domain="services2.arcgis.com", item_bound=True,
                  sample_geo_values=()),
        jurisdiction="Seattle",
        evidence_directory={"identity_matched": True})
    assert decision["status"] == "enrolled"
    mod.clear_registry_cache()
    registry = effective_registry(store=_ListStore([decision]))
    assert registry.source_class_of("https://services2.arcgis.com/x") is SourceClass.UNVETTED
    mod.clear_registry_cache()


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
