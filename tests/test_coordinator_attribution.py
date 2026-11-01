"""Coordinator attribution corrections over a sanitized real-shape fixture.

Derived from the retained first-real-run snippets (same streets,
cities, and ZIPs; no network): TX/Texas spelling variants of one
target address, and one two-address/two-ZIP snippet. Covers the
three connected corrections -- shared verification annotations in
coordinator persistence, Texas state canonicalization, and
per-address location attribution -- plus the fail-closed gates
around them (no authorization from weak snippets, unambiguous or
refused association).

A passing suite here proves the corrected code paths on fixture
data only; it is not proof of real-property qualification.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from app.agents.gabriel.dedup import dedup_key, normalize_state
from app.agents.gabriel.dossier import DossierRequest, build_dossiers
from app.agents.gabriel.extractor import (
    _address_key,
    extract_intelligence,
    extract_properties,
    extract_research_intelligence,
    group_by_property,
)
from app.agents.gabriel.ranking import rank_intelligence
from app.agents.gabriel.sources.brave import BraveSearchResult
from app.workflow import KIND_DOSSIER_VERSION

# --- Sanitized fixture (streets/cities/ZIPs from the retained snippets) ---

TARGET_TX_SNIPPET = (
    "1601 Chalk Hill Road, Dallas, TX 75212 is listed as available office space."
)
TARGET_TEXAS_SNIPPET = (
    "1601 Chalk Hill Road, Dallas, Texas 75212 is listed as available office space."
)
TWO_ADDRESS_SNIPPET = (
    "Available listings include 1601 Chalk Hill Road \u2014 Dallas, TX 75212 "
    "and 8702 S Lancaster Rd \u2014 Dallas, TX 75241."
)
CHALK_HILL_KEY = "addr:1601 chalk hill road|dallas|tx|75212"
LANCASTER_KEY = "addr:8702 s lancaster rd|dallas|tx|75241"


def _result(title: str, snippet: str, url: str, rank: int = 1) -> BraveSearchResult:
    return BraveSearchResult(
        assignment_id="a1",
        query="Dallas commercial listings",
        title=title,
        url=url,
        snippet=snippet,
        domain="listings.example.com",
        retrieved_at="2026-10-30T00:00:00+00:00",
        rank=rank,
    )


def _artifact_payload(results: list[dict[str, Any]], provenance: str = "adapter_collected") -> dict[str, Any]:
    return {
        "provider": "brave",
        "assignment_id": "a1",
        "query": "Dallas commercial listings",
        "results": results,
        "retrieved_at": "2026-10-30T00:00:00+00:00",
        "collection_provenance": provenance,
        "attempt": "completed",
        "error": "",
    }


def _stored_result(title: str, snippet: str, url: str, rank: int = 1) -> dict[str, Any]:
    return {
        "url": url,
        "title": title,
        "snippet": snippet,
        "domain": "listings.example.com",
        "published_at": None,
        "retrieved_at": "2026-10-30T00:00:00+00:00",
        "rank": rank,
    }


class _Store:
    """In-memory workflow record store: (kind, record_key) -> (payload, digest)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}


class _FakeCursor:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self._rows: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        params = dict(params or {})
        if sql.startswith("SELECT * FROM workflow_records WHERE run_id"):
            stored = self._store.records.get((params["kind"], params["key"]))
            self._rows = (
                [{
                    "record_id": uuid4(),
                    "run_id": params["run"],
                    "kind": params["kind"],
                    "record_key": params["key"],
                    "version": 1,
                    "payload": stored[0],
                    "digest": stored[1],
                }]
                if stored is not None
                else []
            )
            return
        if sql.startswith("INSERT INTO workflow_records"):
            key = (params["kind"], params["key"])
            if key in self._store.records:
                self._rows = []  # ON CONFLICT DO NOTHING: caller re-reads
                return
            self._store.records[key] = (json.loads(params["payload"]), params["digest"])
            self._rows = [{
                "record_id": params["rid"],
                "run_id": params["run"],
                "kind": params["kind"],
                "record_key": params["key"],
                "version": params["version"],
                "payload": params["payload"],
                "digest": params["digest"],
            }]
            return
        if sql.startswith("UPDATE workflow_runs"):
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL in attribution fixture: {sql[:60]}")

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> Any:
        return list(self._rows)


class _FakeConn:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._store)


# --- Texas state identity -------------------------------------------------


def test_normalize_state_texas_only() -> None:
    assert normalize_state("TX") == "tx"
    assert normalize_state("Texas") == "tx"
    assert normalize_state("TEXAS") == "tx"
    assert normalize_state("Tex.") == "tex"  # narrow: only full spellings map
    assert normalize_state("California") == "california"
    assert normalize_state("CA") == "ca"
    assert normalize_state("Tennessee") == "tennessee"
    assert normalize_state(None) == ""
    assert normalize_state("") == ""


def test_texas_spellings_share_group_identity() -> None:
    item_tx = extract_intelligence(_result("t", TARGET_TX_SNIPPET, "https://listings.example.com/1"))
    item_texas = extract_intelligence(_result("t", TARGET_TEXAS_SNIPPET, "https://listings.example.com/2"))
    assert item_tx.properties[0].state == "TX"
    assert item_texas.properties[0].state == "Texas"  # raw wording preserved
    groups = group_by_property([item_tx, item_texas])
    assert len(groups) == 1
    assert groups[0].key == CHALK_HILL_KEY
    ranked = rank_intelligence(
        extract_research_intelligence([
            _result("t", TARGET_TX_SNIPPET, "https://listings.example.com/1"),
            _result("t", TARGET_TEXAS_SNIPPET, "https://listings.example.com/2"),
        ])
    )
    assert len(ranked.candidates) == 1
    assert "state" not in ranked.candidates[0].identity_disputes


def test_state_rule_never_merges_distinct_identities() -> None:
    tx = SimpleNamespace(street_address="1601 Chalk Hill Road", city="Dallas", state="TX", zip_code="75212")
    ca = SimpleNamespace(street_address="1601 Chalk Hill Road", city="Dallas", state="CA", zip_code="75212")
    tn = SimpleNamespace(street_address="1601 Chalk Hill Road", city="Dallas", state="Tennessee", zip_code="75212")
    assert _address_key(tx) != _address_key(ca)
    assert _address_key(tx) != _address_key(tn)
    assert dedup_key({"street_address": "1601 Chalk Hill Road", "city": "Dallas", "state": "TX", "zip_code": "75212"}) != dedup_key(
        {"street_address": "1601 Chalk Hill Road", "city": "Dallas", "state": "CA", "zip_code": "75212"}
    )
    assert dedup_key({"street_address": "1601 Chalk Hill Road", "city": "Dallas", "state": "Texas", "zip_code": "75212"}) == dedup_key(
        {"street_address": "1601 Chalk Hill Road", "city": "Dallas", "state": "TX", "zip_code": "75212"}
    )


def test_distinct_addresses_and_units_stay_separate() -> None:
    item = extract_intelligence(_result("t", TWO_ADDRESS_SNIPPET, "https://listings.example.com/9"))
    groups = group_by_property([item])
    assert sorted(group.key for group in groups) == [CHALK_HILL_KEY, LANCASTER_KEY]
    suite_a = extract_intelligence(
        _result("t", "1601 Chalk Hill Road Suite 100, Dallas, TX 75212.", "https://listings.example.com/a")
    )
    suite_b = extract_intelligence(
        _result("t", "1601 Chalk Hill Road Suite 200, Dallas, TX 75212.", "https://listings.example.com/b")
    )
    assert suite_a.properties[0].unit_designator == "Suite 100"
    assert suite_b.properties[0].unit_designator == "Suite 200"
    unit_groups = group_by_property([suite_a, suite_b])
    assert len(unit_groups) == 1
    assert unit_groups[0].needs_review is True  # shared group, but flagged
    ranked_units = rank_intelligence(
        extract_research_intelligence([
            _result("t", "1601 Chalk Hill Road Suite 100, Dallas, TX 75212.", "https://listings.example.com/a"),
            _result("t", "1601 Chalk Hill Road Suite 200, Dallas, TX 75212.", "https://listings.example.com/b"),
        ])
    )
    assert len(ranked_units.candidates) == 1
    assert "unit_designator" in ranked_units.candidates[0].identity_disputes


# --- Per-address attribution -----------------------------------------------


def test_neighbor_zip_never_borrowed() -> None:
    props = extract_properties("DFW listings", TWO_ADDRESS_SNIPPET)
    by_street = {prop.street_address: prop for prop in props}
    assert set(by_street) == {"1601 Chalk Hill Road", "8702 S Lancaster Rd"}
    assert by_street["1601 Chalk Hill Road"].zip_code == "75212"
    assert by_street["8702 S Lancaster Rd"].zip_code == "75241"
    assert by_street["8702 S Lancaster Rd"].city == "Dallas"
    assert by_street["8702 S Lancaster Rd"].state == "TX"


def test_shared_trailing_location_stays_unknown_for_nonadjacent_address() -> None:
    props = extract_properties(
        "Portfolio asking",
        "Asking price $50 million for the portfolio: 500 Commerce St and "
        "900 Industrial Blvd, Dallas, TX.",
    )
    by_street = {prop.street_address: prop for prop in props}
    assert set(by_street) == {"500 Commerce St", "900 Industrial Blvd"}
    assert by_street["900 Industrial Blvd"].city == "Dallas"
    assert by_street["900 Industrial Blvd"].state == "TX"
    assert by_street["500 Commerce St"].city is None
    assert by_street["500 Commerce St"].state is None
    assert by_street["500 Commerce St"].zip_code is None


def test_dossier_zip_matches_own_address() -> None:
    from app.agents.gabriel.extractor import extract_research_intelligence

    intel = extract_research_intelligence(
        [_result("DFW listings", TWO_ADDRESS_SNIPPET, "https://listings.example.com/9")]
    )
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    by_key = {dossier.key: dossier for dossier in dossier_set.dossiers}
    assert set(by_key) == {CHALK_HILL_KEY, LANCASTER_KEY}
    # ZIP lives in the identity key; each dossier keeps its own address.
    assert by_key[LANCASTER_KEY].key.endswith("|75241")
    assert by_key[CHALK_HILL_KEY].key.endswith("|75212")
    assert by_key[LANCASTER_KEY].street_address == "8702 S Lancaster Rd"
    assert by_key[LANCASTER_KEY].city == "Dallas"
    assert by_key[LANCASTER_KEY].state == "TX"
    assert by_key[CHALK_HILL_KEY].street_address == "1601 Chalk Hill Road"


# --- Coordinator verification wiring ---------------------------------------


def _gabriel_persist(artifact: dict[str, Any]) -> tuple[list[str], dict[tuple[str, str], dict[str, Any]]]:
    from app import mvp as mvp_module

    store = _Store()
    store.records[("research_artifact", "a1")] = (artifact, "seed-digest")
    conn = _FakeConn(store)
    keys, _, _ = mvp_module._gabriel_stage(conn, uuid4(), "a1", "q", True, 12.0)
    payloads = {key: payload for key, (payload, _) in store.records.items()}
    return keys, payloads


def test_coordinator_persists_explicit_verification_result() -> None:
    artifact = _artifact_payload(
        [_stored_result("DFW listings", TWO_ADDRESS_SNIPPET, "https://listings.example.com/9")]
    )
    keys, records = _gabriel_persist(artifact)
    assert sorted(keys) == [CHALK_HILL_KEY, LANCASTER_KEY]
    for key in keys:
        payload = records[(KIND_DOSSIER_VERSION, key)]
        verification = payload.get("candidate_verification")
        assert verification is not None, key
        assert verification["outcome"] == "not_verified", key
        assert verification["reasons"], key  # explicit policy reasons persisted
        assert verification["rationale"], key
    assert LANCASTER_KEY.endswith("|75241")
    assert records[(KIND_DOSSIER_VERSION, LANCASTER_KEY)]["dossier"]["street_address"] == (
        "8702 S Lancaster Rd"
    )


def test_non_adapter_artifact_skips_verification_stamp() -> None:
    artifact = _artifact_payload(
        [_stored_result("DFW listings", TWO_ADDRESS_SNIPPET, "https://listings.example.com/9")],
        provenance="human_reviewed",
    )
    keys, records = _gabriel_persist(artifact)
    assert sorted(keys) == [CHALK_HILL_KEY, LANCASTER_KEY]
    for key in keys:
        assert records[(KIND_DOSSIER_VERSION, key)].get("candidate_verification") is None


def test_weak_snippets_do_not_authorize() -> None:
    from app.agents.raphael.service import authorize_in_transaction

    artifact = _artifact_payload(
        [_stored_result("DFW listings", TWO_ADDRESS_SNIPPET, "https://listings.example.com/9")]
    )
    store = _Store()
    store.records[("research_artifact", "a1")] = (artifact, "seed-digest")
    conn = _FakeConn(store)
    from app import mvp as mvp_module

    run_id = uuid4()
    keys, _, _ = mvp_module._gabriel_stage(conn, run_id, "a1", "q", True, 12.0)
    for key in keys:
        decision = authorize_in_transaction(conn, run_id, key)
        assert decision["outcome"] in ("needs_evidence", "rejected"), key
        assert decision["outcome"] != "authorized"


def test_shared_policy_success_authorizes_through_persistence() -> None:
    """A genuine policy success passes Raphael via the canonical contract.

    The success verdict is produced by the shared policy itself from
    isolated test evidence and a test-only registry -- the serialized
    success spelling is never handwritten. Feeding it through dossier
    persistence and Raphael proves the consumer accepts the producer's
    canonical contract.
    """
    from app.agents.gabriel.source_registry import (
        RegistryEntry,
        SourceClass,
        SourceRegistry,
    )
    from app.agents.gabriel.verification import (
        IdentityAttestation,
        VerificationOutcome,
        VerificationPathway,
        VerificationRequest,
        VoiceInput,
        verify_category,
    )
    from app.agents.raphael.service import authorize_in_transaction
    from app.workflow import digest_payload
    from app.workflow_db import put_record_in_transaction

    registry = SourceRegistry(
        entries={
            "records.example.invalid": RegistryEntry(
                hostname="records.example.invalid",
                source_class=SourceClass.AUTHORITATIVE,
                outlet_name="Test Records Office",
                publisher="Test Records Office",
                added_by="contract-test",
            )
        }
    )
    verdict = verify_category(
        VerificationRequest(
            claim="commercial_property",
            voices=[
                VoiceInput(
                    url="https://records.example.invalid/parcel/1",
                    provenance="adapter_collected",
                    defining_facts={"use": "office", "use_status": "current"},
                )
            ],
            identity=IdentityAttestation(group_key="contract-test", aligned=True),
            registry=registry,
        )
    )
    assert verdict.outcome is VerificationOutcome.VERIFIED
    assert verdict.pathway is VerificationPathway.AUTHORITY
    serialized = verdict.model_dump(mode="json")
    assert serialized["outcome"] == VerificationOutcome.VERIFIED.value
    assert serialized["pathway"] == VerificationPathway.AUTHORITY.value

    store = _Store()
    conn = _FakeConn(store)
    run_id = uuid4()
    payload = {
        "version": 1,
        "builder_version": "contract-test",
        "artifact_refs": ["a1"],
        "property_id": "",
        "dossier": {
            "identity_disputes": {},
            "conflicts": [],
            "category_attribution": {"status": "observed"},
            "sources": ["https://records.example.invalid/parcel/1"],
        },
        "candidate_verification": serialized,
    }
    put_record_in_transaction(
        conn, run_id, kind=KIND_DOSSIER_VERSION, record_key="main",
        version=1, payload=payload, digest=digest_payload(payload),
    )
    decision = authorize_in_transaction(conn, run_id, "main")
    assert decision["outcome"] == "authorized"


# --- Association ------------------------------------------------------------


def test_association_texas_spellings_match_unambiguous(monkeypatch) -> None:
    from app import mvp as mvp_module

    target_id = str(uuid4())
    dossier = SimpleNamespace(
        key=CHALK_HILL_KEY,
        street_address="1601 Chalk Hill Road",
        city="Dallas",
        state="Texas",  # snippet spelling vs stored "TX"
    )
    monkeypatch.setattr(
        "app.properties_db.get_property",
        lambda pid, config=None: {
            "street_address": "1601 Chalk Hill Road",
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75212",
        },
    )
    seen: dict[str, Any] = {}

    def _unique(*, street: str, city: str, state: str, zip_code: str, config: Any = None) -> Any:
        seen.update({"street": street, "city": city, "state": state, "zip": zip_code})
        return [{"id": target_id}]

    monkeypatch.setattr("app.properties_db.find_by_address", _unique)
    association, review = mvp_module.associate_dossiers_with_property([dossier], target_id)
    assert association == {CHALK_HILL_KEY: target_id}
    assert review is None
    assert seen["state"] == "tx"  # canonical spelling sent to storage lookup


def test_association_shared_address_fails_closed(monkeypatch) -> None:
    from app import mvp as mvp_module

    target_id = str(uuid4())
    dossier = SimpleNamespace(
        key=CHALK_HILL_KEY,
        street_address="1601 Chalk Hill Road",
        city="Dallas",
        state="Texas",
    )
    monkeypatch.setattr(
        "app.properties_db.get_property",
        lambda pid, config=None: {
            "street_address": "1601 Chalk Hill Road",
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75212",
        },
    )
    monkeypatch.setattr(
        "app.properties_db.find_by_address",
        lambda **kwargs: [{"id": target_id}, {"id": str(uuid4())}],
    )
    association, review = mvp_module.associate_dossiers_with_property([dossier], target_id)
    assert association == {}
    assert review is not None
    assert review["reason"] == "target_address_shared_by_multiple_properties"


def test_find_by_address_texas_sql_mapping(monkeypatch) -> None:
    from app import properties_db as properties_module

    captured: dict[str, Any] = {}

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            captured["sql"] = sql
            captured["params"] = dict(params or {})

        def fetchall(self) -> list[Any]:
            return []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(
        properties_module, "get_connection", lambda config=None: _Conn()
    )
    assert properties_module.find_by_address(
        street="1601 chalk hill road", city="dallas", state="tx", zip_code="75212"
    ) == []
    assert "WHEN 'texas' THEN 'tx'" in captured["sql"]
    assert captured["params"]["state"] == "tx"
